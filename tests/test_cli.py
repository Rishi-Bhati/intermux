"""Parser tests for the InterMux CLI. These never call main() and never touch the system."""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from intermux import cli
from intermux.cli import build_parser


def _parse(argv):
    """Parse argv with the real parser, silencing argparse's stderr on errors."""
    parser = build_parser()
    with contextlib.redirect_stderr(io.StringIO()):
        return parser.parse_args(argv)


class ParserTests(unittest.TestCase):

    def test_bind_parses(self):
        args = _parse(["bind", "--app", "firefox", "--iface", "wlan0"])
        self.assertEqual(args.command, "bind")
        self.assertEqual(args.app, "firefox")
        self.assertEqual(args.iface, "wlan0")
        self.assertFalse(args.separate_profile)
        self.assertFalse(args.close_existing)

    def test_bind_flags(self):
        args = _parse(["bind", "--app", "/usr/bin/firefox", "--iface", "eth0",
                       "--separate-profile"])
        self.assertTrue(args.separate_profile)
        self.assertFalse(args.close_existing)

    def test_bind_separate_and_close_are_exclusive(self):
        with self.assertRaises(SystemExit):
            _parse(["bind", "--app", "firefox", "--iface", "wlan0",
                    "--separate-profile", "--close-existing"])

    def test_bind_requires_app_and_iface(self):
        with self.assertRaises(SystemExit):
            _parse(["bind", "--iface", "wlan0"])
        with self.assertRaises(SystemExit):
            _parse(["bind", "--app", "firefox"])

    def test_reset_without_yes_parses(self):
        args = _parse(["reset"])
        self.assertEqual(args.command, "reset")
        self.assertFalse(args.yes)
        self.assertFalse(args.kill_apps)

    def test_reset_flags(self):
        args = _parse(["reset", "--kill-apps", "--yes"])
        self.assertTrue(args.kill_apps)
        self.assertTrue(args.yes)

    def test_interfaces_all_flag(self):
        self.assertFalse(_parse(["interfaces"]).all)
        self.assertTrue(_parse(["interfaces", "--all"]).all)

    def test_apps_search(self):
        args = _parse(["apps", "--search", "fire"])
        self.assertEqual(args.command, "apps")
        self.assertEqual(args.search, "fire")
        self.assertIsNone(_parse(["apps"]).search)

    def test_stop_parses(self):
        args = _parse(["stop", "--app", "firefox", "--iface", "wlan0"])
        self.assertEqual((args.command, args.app, args.iface), ("stop", "firefox", "wlan0"))

    def test_kill_force(self):
        self.assertFalse(_parse(["kill", "--app", "firefox"]).force)
        self.assertTrue(_parse(["kill", "--app", "firefox", "--force"]).force)

    def test_verify_requires_iface(self):
        self.assertEqual(_parse(["verify", "--iface", "wlan0"]).iface, "wlan0")
        with self.assertRaises(SystemExit):
            _parse(["verify"])

    def test_simple_commands(self):
        for command in ("status", "teardown-idle", "check"):
            with self.subTest(command=command):
                self.assertEqual(_parse([command]).command, command)

    def test_no_command_gives_none(self):
        self.assertIsNone(_parse([]).command)

    def test_unknown_command_exits(self):
        with self.assertRaises(SystemExit):
            _parse(["bogus"])

    def test_helper_is_not_a_subcommand(self):
        with self.assertRaises(SystemExit):
            _parse(["helper"])

    def test_hyphenated_command_names(self):
        self.assertEqual(_parse(["teardown-idle"]).command, "teardown-idle")
        with self.assertRaises(SystemExit):
            _parse(["teardown_idle"])


class AdoptParserTests(unittest.TestCase):
    def test_adopt_parses(self):
        from intermux import cli

        args = cli.build_parser().parse_args(["adopt", "--app", "firefox", "--iface", "wlan0"])
        self.assertEqual((args.command, args.app, args.iface), ("adopt", "firefox", "wlan0"))

    def test_release_parses(self):
        from intermux import cli

        args = cli.build_parser().parse_args(["release", "0123456789abcdef"])
        self.assertEqual((args.command, args.id), ("release", "0123456789abcdef"))


if __name__ == "__main__":
    unittest.main()


class StatusTests(unittest.TestCase):
    def test_status_shows_namespace_and_nat(self) -> None:
        from types import SimpleNamespace
        from unittest import mock

        from intermux import cli

        b = SimpleNamespace(app_name="app", iface="wlan0", subnet="10.200.1.0/30", profile_mode="default",
                            namespace="ns1", log_path=None)
        st = SimpleNamespace(bindings=[b])
        buf = io.StringIO()
        with mock.patch("intermux.state.load_state", return_value=st), \
             mock.patch("intermux.system.procs.pids_in_netns", return_value=[]), \
             mock.patch("intermux.system.netns.namespace_exists", side_effect=RuntimeError("x")), \
             mock.patch("intermux.system.firewall.list_intermux_rules",
                        return_value=["-A POSTROUTING -s 10.200.1.0/30 -j MASQUERADE"]), \
             contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["status"]), 0)
        out = buf.getvalue()
        self.assertIn("NAMESPACE", out)
        self.assertIn("unknown", out)
        self.assertIn("yes", out)


class RunningChoiceTests(unittest.TestCase):
    def test_choice_mapping(self):
        from intermux.cli import running_choice_action
        self.assertEqual(running_choice_action("1"), ("policy", "close"))
        self.assertEqual(running_choice_action("2"), ("policy", "separate"))
        self.assertEqual(running_choice_action("3"), ("policy", None))
        self.assertEqual(running_choice_action("4"), ("adopt", None))

    def test_unlisted_input_rejected(self):
        from intermux.cli import running_choice_action
        for bad in ("", "0", "5", "x", "11"):
            self.assertIsNone(running_choice_action(bad))


class ResetReportTests(unittest.TestCase):
    def _run(self, data):
        import io

        out = io.StringIO()
        with mock.patch("intermux.helper.client.call", return_value=data), mock.patch("sys.stdout", out):
            rc = cli.main(["reset", "--yes"])
        return rc, out.getvalue()

    def test_clean_reset_succeeds(self):
        rc, out = self._run({"namespaces": ["ns_a"], "veths": [], "rules": 1, "nat_rules": 3,
                             "still_running": [], "kept_apps": []})
        self.assertEqual(rc, 0)
        self.assertNotIn("kept", out)

    def test_kept_apps_are_reported(self):
        rc, out = self._run({"namespaces": [], "veths": [], "rules": 0, "nat_rules": 0,
                             "still_running": [], "kept_apps": ["firefox"], "kept_namespaces": ["ns_a"]})
        self.assertEqual(rc, 1)
        self.assertIn("firefox", out)



class StillRunningReportTests(unittest.TestCase):
    def _run(self, argv, data):
        import io

        out = io.StringIO()
        with mock.patch("intermux.helper.client.call", return_value=data), \
             mock.patch("intermux.cli._resolve_app", return_value="/usr/bin/x"), mock.patch("sys.stdout", out):
            rc = cli.main(argv)
        return rc, out.getvalue()

    def test_release_with_survivors_warns(self):
        rc, out = self._run(["release", "abc"], {"released": [1], "still_running": [7, 8]})
        self.assertEqual(rc, 1)
        self.assertIn("7, 8", out)

    def test_release_clean(self):
        rc, _ = self._run(["release", "abc"], {"released": [1], "still_running": []})
        self.assertEqual(rc, 0)

    def test_stop_and_kill_with_survivors(self):
        for argv in (["stop", "--app", "x", "--iface", "wlan0"], ["kill", "--app", "x"]):
            rc, out = self._run(argv, {"stopped": [], "still_running": [9]})
            self.assertEqual(rc, 1, argv)
            self.assertIn("9", out)
