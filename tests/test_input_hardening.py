"""Edge cases in user input: request validation, DNS and limit parsing, app resolution and CLI prompts."""

from __future__ import annotations

import io
import os
import stat
import tempfile
import unittest
from unittest import mock

from intermux import cli, dnsconf
from intermux.apps import desktop_entries, resolve_app_path
from intermux.errors import NotFoundError
from intermux.helper import protocol
from intermux.system import shaping


def _request(action, **params):
    return {"v": 1, "action": action, "params": params}


class LimitOverflowTests(unittest.TestCase):
    def test_huge_integers_are_out_of_range_not_a_crash(self):
        for value in (10 ** 400, -(10 ** 400), 2 ** 1024):
            with self.assertRaises(ValueError):
                shaping.check_mbit(value)
            with self.assertRaises(protocol.ValidationError):
                protocol.validate_request(_request("set_limit", iface="eth0", down=value))

    def test_huge_integer_inside_a_batch(self):
        with self.assertRaises(protocol.ValidationError):
            protocol.validate_request(_request("batch", ops=[{"op": "set_limit", "iface": "eth0",
                                                              "down": 10 ** 400}]))


class AppPathValidationTests(unittest.TestCase):
    def test_equals_sign_is_refused(self):
        # env(1) would read "/opt/a=b" as a variable assignment and never start the program.
        with self.assertRaises(protocol.ValidationError) as ctx:
            protocol.validate_request(_request("bind", iface="eth0", app="/opt/a=b"))
        self.assertIn("=", str(ctx.exception))

    def test_lone_surrogates_are_refused(self):
        with self.assertRaises(protocol.ValidationError):
            protocol.validate_request(_request("kill_app", app="/tmp/\ud800"))
        with self.assertRaises(protocol.ValidationError):
            protocol.validate_request(_request("bind", iface="eth0", app="/usr/bin/x", env={"LANG": "\ud800"}))

    def test_length_is_counted_in_bytes(self):
        with self.assertRaises(protocol.ValidationError):
            protocol.validate_request(_request("kill_app", app="/" + "é" * 4095))
        protocol.validate_request(_request("kill_app", app="/" + "a" * 4095))

    def test_messages_state_the_rule(self):
        with self.assertRaises(protocol.ValidationError) as ctx:
            protocol.validate_request(_request("stop_app", iface="bad name", app="/usr/bin/x"))
        self.assertIn("1 to 15 characters", str(ctx.exception))
        with self.assertRaises(protocol.ValidationError) as ctx:
            protocol.validate_request(_request("release_adoption", id="XYZ"))
        self.assertIn("hexadecimal", str(ctx.exception))


class DnsParsingTests(unittest.TestCase):
    def test_preset_name_with_addresses_uses_the_addresses(self):
        self.assertEqual(dnsconf.parse_servers("Cloudflare:9.9.9.9"), ["9.9.9.9"])
        self.assertEqual(dnsconf.parse_servers("Cloudflare: 1.1.1.1, 1.0.0.1"), ["1.1.1.1", "1.0.0.1"])
        self.assertEqual(dnsconf.parse_servers("cloudflare:"), ["1.1.1.1", "1.0.0.1"])
        self.assertEqual(dnsconf.parse_servers("quad9"), ["9.9.9.9", "149.112.112.112"])
        with self.assertRaises(ValueError):
            dnsconf.parse_servers("Google: 127.0.0.1")


class LaunchedProgramTests(unittest.TestCase):
    def test_env_wrappers_resolve_to_the_program(self):
        lp = desktop_entries.launched_program
        self.assertEqual(lp(["env", "GDK_BACKEND=x11", "audacity"]), "audacity")
        self.assertEqual(lp(["/usr/bin/env", "-u", "QT_QPA_PLATFORM", "A=1", "--", "app", "--flag"]), "app")
        self.assertEqual(lp(["firefox", "--new-window"]), "firefox")
        self.assertIsNone(lp(["env", "A=1"]))
        self.assertIsNone(lp([]))


class ResolveAppPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _file(self, name, executable):
        path = os.path.join(self.dir, name)
        with open(path, "w") as handle:
            handle.write("#!/bin/sh\n")
        mode = stat.S_IRUSR | stat.S_IWUSR | (stat.S_IXUSR if executable else 0)
        os.chmod(path, mode)
        return path

    def test_directories_and_plain_files_are_refused(self):
        with self.assertRaises(NotFoundError):
            resolve_app_path(self.dir)
        with self.assertRaises(NotFoundError):
            resolve_app_path(self._file("notes.txt", executable=False))
        self.assertEqual(resolve_app_path(self._file("tool", executable=True)), os.path.join(self.dir, "tool"))

    def test_menu_name_resolves_to_its_binary(self):
        binary = self._file("realapp", executable=True)
        entry = desktop_entries.AppEntry(name="Real App", comment="", icon="", exec_argv=[binary], binary=binary,
                                         bindable=True, reason="", desktop_file="x.desktop")
        with mock.patch("shutil.which", return_value=None), \
                mock.patch.object(desktop_entries, "scan_desktop_entries", return_value=[entry]):
            self.assertEqual(resolve_app_path("real app"), binary)
            with self.assertRaises(NotFoundError):
                resolve_app_path("Other App")


class CliInputTests(unittest.TestCase):
    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_bad_interface_is_refused_before_any_progress_line(self):
        with mock.patch("intermux.helper.client.call") as call:
            code, out, err = self._run(["stop", "--iface", "bad name", "--app", "/bin/sh"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("1 to 15 characters", err)
        call.assert_not_called()

    def test_empty_dns_is_refused(self):
        with mock.patch("intermux.helper.client.call") as call:
            code, _out, err = self._run(["bind", "--app", "/bin/sh", "--iface", "eth0", "--dns", ""])
        self.assertEqual(code, 1)
        self.assertIn("no DNS servers given", err)
        call.assert_not_called()

    def test_blank_search_is_refused(self):
        code, _out, err = self._run(["apps", "--search", "  "])
        self.assertEqual(code, 2)
        self.assertIn("--search", err)

    def test_confirm_accepts_y_and_needs_the_word_when_asked(self):
        with mock.patch("builtins.input", return_value="Y"):
            self.assertTrue(cli._confirm("? "))
        with mock.patch("builtins.input", return_value="YES"):
            self.assertTrue(cli._confirm("? ", word="yes"))
        with mock.patch("builtins.input", return_value="y"):
            self.assertFalse(cli._confirm("? ", word="yes"))
        with mock.patch("builtins.input", side_effect=EOFError), mock.patch("sys.stdout", io.StringIO()):
            self.assertFalse(cli._confirm("? "))

    def test_report_out_pointing_at_a_file(self):
        with tempfile.NamedTemporaryFile() as handle:
            code, _out, err = self._run(["report", "--out", handle.name])
        self.assertEqual(code, 2)
        self.assertIn("must be a folder", err)


if __name__ == "__main__":
    unittest.main()
