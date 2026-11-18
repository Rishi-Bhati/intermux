"""Tests for passwordless helper execution (intermux.system.passwordless and CLI wiring)."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

import intermux.__main__ as main_mod
from intermux import cli
from intermux.cli import build_parser
from intermux.errors import MissingToolError, PermissionDeniedError, ValidationError
from intermux.system import commands, passwordless
from intermux.system.commands import CmdResult


def _parse(argv):
    """Parse argv with the real parser, silencing argparse's stderr on errors."""
    parser = build_parser()
    with contextlib.redirect_stderr(io.StringIO()):
        return parser.parse_args(argv)


class ParsePkactionVersionTests(unittest.TestCase):

    def test_version_0_105(self):
        self.assertEqual(passwordless.parse_pkaction_version("pkaction version 0.105"), (0, 105))

    def test_version_124(self):
        self.assertEqual(passwordless.parse_pkaction_version("pkaction version 124"), (124,))

    def test_version_0_106(self):
        self.assertEqual(passwordless.parse_pkaction_version("pkaction version 0.106"), (0, 106))

    def test_version_with_trailing_newline_or_whitespace(self):
        self.assertEqual(passwordless.parse_pkaction_version("  pkaction version 124\n"), (124,))

    def test_version_three_components(self):
        self.assertEqual(passwordless.parse_pkaction_version("pkaction version 0.105.1"), (0, 105, 1))

    def test_raw_numbers(self):
        self.assertEqual(passwordless.parse_pkaction_version("124"), (124,))
        self.assertEqual(passwordless.parse_pkaction_version("0.105"), (0, 105))

    def test_garbage_returns_none(self):
        for bad in ("", "garbage", "pkaction version", "pkaction version abc", "command not found"):
            with self.subTest(bad=bad):
                self.assertIsNone(passwordless.parse_pkaction_version(bad))


class DetectBackendTests(unittest.TestCase):

    def test_detect_backend_0_105_returns_pkla(self):
        res = CmdResult(["pkaction", "--version"], 0, "pkaction version 0.105\n", "")
        with mock.patch("intermux.system.commands.run", return_value=res):
            self.assertEqual(passwordless.detect_backend(), "pkla")

    def test_detect_backend_124_returns_rules(self):
        res = CmdResult(["pkaction", "--version"], 0, "pkaction version 124\n", "")
        with mock.patch("intermux.system.commands.run", return_value=res):
            self.assertEqual(passwordless.detect_backend(), "rules")

    def test_detect_backend_0_106_returns_rules(self):
        res = CmdResult(["pkaction", "--version"], 0, "pkaction version 0.106\n", "")
        with mock.patch("intermux.system.commands.run", return_value=res):
            self.assertEqual(passwordless.detect_backend(), "rules")

    def test_detect_backend_missing_pkaction_localauthority_only_returns_pkla(self):
        res = CmdResult(["pkaction", "--version"], 127, "", "command not found")
        layout = {
            Path("/etc/polkit-1/localauthority"): True,
            Path("/etc/polkit-1/rules.d"): False,
            Path("/usr/share/polkit-1/rules.d"): False,
        }
        with mock.patch("intermux.system.commands.run", return_value=res), \
             mock.patch.object(Path, "exists", autospec=True,
                               side_effect=lambda p: layout.get(p, False)):
            self.assertEqual(passwordless.detect_backend(), "pkla")

    def test_detect_backend_missing_pkaction_with_rules_dir_returns_rules(self):
        res = CmdResult(["pkaction", "--version"], 127, "", "command not found")
        layout = {
            Path("/etc/polkit-1/localauthority"): True,
            Path("/etc/polkit-1/rules.d"): True,
            Path("/usr/share/polkit-1/rules.d"): False,
        }
        with mock.patch("intermux.system.commands.run", return_value=res), \
             mock.patch.object(Path, "exists", autospec=True,
                               side_effect=lambda p: layout.get(p, False)):
            self.assertEqual(passwordless.detect_backend(), "rules")

    def test_detect_backend_missing_pkaction_with_usr_rules_dir_returns_rules(self):
        res = CmdResult(["pkaction", "--version"], 127, "", "command not found")
        layout = {
            Path("/etc/polkit-1/localauthority"): True,
            Path("/etc/polkit-1/rules.d"): False,
            Path("/usr/share/polkit-1/rules.d"): True,
        }
        with mock.patch("intermux.system.commands.run", return_value=res), \
             mock.patch.object(Path, "exists", autospec=True,
                               side_effect=lambda p: layout.get(p, False)):
            self.assertEqual(passwordless.detect_backend(), "rules")

    def test_detect_backend_missing_pkaction_and_no_dirs_returns_rules(self):
        res = CmdResult(["pkaction", "--version"], 127, "", "command not found")
        with mock.patch("intermux.system.commands.run", return_value=res), \
             mock.patch.object(Path, "exists", autospec=True, return_value=False):
            self.assertEqual(passwordless.detect_backend(), "rules")


class RequireSudoRootTests(unittest.TestCase):

    def test_refuses_non_root(self):
        with mock.patch("os.geteuid", return_value=1000), \
             mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(PermissionDeniedError) as ctx:
                passwordless.require_sudo_root()
            self.assertIn("run: sudo intermux passwordless on", str(ctx.exception))

    def test_refuses_pkexec_uid(self):
        with mock.patch("os.geteuid", return_value=0), \
             mock.patch.dict(os.environ, {"PKEXEC_UID": "1000"}):
            with self.assertRaises(PermissionDeniedError) as ctx:
                passwordless.require_sudo_root()
            self.assertIn("passwordless can only be changed with sudo, not through pkexec", str(ctx.exception))

    def test_passes_when_sudo_root(self):
        with mock.patch("os.geteuid", return_value=0), \
             mock.patch.dict(os.environ, {"SUDO_USER": "rishi"}, clear=True):
            # Does not raise
            passwordless.require_sudo_root()


class ResolveTargetUserTests(unittest.TestCase):

    def test_explicit_valid_user(self):
        pw = SimpleNamespace(pw_name="alice", pw_uid=1001)
        with mock.patch("pwd.getpwnam", return_value=pw):
            self.assertEqual(passwordless.resolve_target_user("alice"), "alice")

    def test_explicit_root_refused(self):
        with self.assertRaises(ValidationError) as ctx:
            passwordless.resolve_target_user("root")
        self.assertIn("root", str(ctx.exception))

    def test_explicit_uid_0_refused(self):
        pw = SimpleNamespace(pw_name="toor", pw_uid=0)
        with mock.patch("pwd.getpwnam", return_value=pw):
            with self.assertRaises(ValidationError) as ctx:
                passwordless.resolve_target_user("toor")
            self.assertIn("uid 0", str(ctx.exception))

    def test_unknown_user_refused(self):
        with mock.patch("pwd.getpwnam", side_effect=KeyError("no such user")):
            with self.assertRaises(ValidationError) as ctx:
                passwordless.resolve_target_user("nobody_here")
            self.assertIn("nobody_here", str(ctx.exception))

    def test_sudo_user_valid(self):
        pw = SimpleNamespace(pw_name="rishi", pw_uid=1000)
        with mock.patch.dict(os.environ, {"SUDO_USER": "rishi"}), \
             mock.patch("pwd.getpwnam", return_value=pw):
            self.assertEqual(passwordless.resolve_target_user(None), "rishi")

    def test_sudo_user_root_refused(self):
        with mock.patch.dict(os.environ, {"SUDO_USER": "root"}):
            with self.assertRaises(ValidationError) as ctx:
                passwordless.resolve_target_user(None)
            self.assertIn("--user", str(ctx.exception))

    def test_no_sudo_user_and_no_explicit_refused(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValidationError) as ctx:
                passwordless.resolve_target_user(None)
            self.assertIn("--user", str(ctx.exception))


class MarkerTests(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.TemporaryDirectory()
        self.marker_path = Path(self.tmpdir.name) / "passwordless.json"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_read_marker_missing(self):
        with mock.patch.object(passwordless, "MARKER_PATH", self.marker_path):
            self.assertIsNone(passwordless.read_marker())

    def test_read_marker_corrupt_json(self):
        self.marker_path.write_text("{bad json")
        with mock.patch.object(passwordless, "MARKER_PATH", self.marker_path):
            self.assertIsNone(passwordless.read_marker())

    def test_read_marker_wrong_shape(self):
        for bad_data in (
            [],
            {"v": "1", "backend": "rules", "rule_path": "/x", "group_created": True},
            {"v": 1, "backend": "other", "rule_path": "/x", "group_created": True},
            {"v": 1, "backend": "rules", "rule_path": 123, "group_created": True},
            {"v": 1, "backend": "rules", "rule_path": "/x", "group_created": "yes"},
        ):
            with self.subTest(bad_data=bad_data):
                self.marker_path.write_text(json.dumps(bad_data))
                with mock.patch.object(passwordless, "MARKER_PATH", self.marker_path):
                    self.assertIsNone(passwordless.read_marker())

    def test_read_marker_valid(self):
        valid = {"v": 1, "backend": "rules", "rule_path": "/etc/polkit-1/rules.d/49-intermux-passwordless.rules",
                 "group_created": True}
        self.marker_path.write_text(json.dumps(valid))
        with mock.patch.object(passwordless, "MARKER_PATH", self.marker_path):
            self.assertEqual(passwordless.read_marker(), valid)


class StatusTests(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.TemporaryDirectory()
        self.marker_path = Path(self.tmpdir.name) / "passwordless.json"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_status_member_enabled(self):
        valid_marker = {"v": 1, "backend": "rules", "rule_path": "/path/rules", "group_created": True}
        self.marker_path.write_text(json.dumps(valid_marker))
        gr = SimpleNamespace(gr_name="intermux", gr_gid=2000, gr_mem=["alice", "rishi"])

        with mock.patch.object(passwordless, "MARKER_PATH", self.marker_path), \
             mock.patch("grp.getgrnam", return_value=gr), \
             mock.patch("os.getuid", return_value=1000), \
             mock.patch("pwd.getpwuid", return_value=SimpleNamespace(pw_name="rishi", pw_gid=1000)):
            st = passwordless.status()
            self.assertTrue(st["enabled"])
            self.assertTrue(st["rule_installed"])
            self.assertEqual(st["backend"], "rules")
            self.assertEqual(st["rule_path"], "/path/rules")
            self.assertTrue(st["group_exists"])
            self.assertEqual(st["members"], ["alice", "rishi"])
            self.assertEqual(st["user"], "rishi")
            self.assertTrue(st["user_in_group"])

    def test_status_non_member_disabled(self):
        valid_marker = {"v": 1, "backend": "rules", "rule_path": "/path/rules", "group_created": False}
        self.marker_path.write_text(json.dumps(valid_marker))
        gr = SimpleNamespace(gr_name="intermux", gr_gid=2000, gr_mem=["alice"])

        with mock.patch.object(passwordless, "MARKER_PATH", self.marker_path), \
             mock.patch("grp.getgrnam", return_value=gr), \
             mock.patch("pwd.getpwnam", return_value=SimpleNamespace(pw_name="bob", pw_gid=1002)), \
             mock.patch("os.getgrouplist", return_value=[1002]):
            st = passwordless.status(user="bob")
            self.assertFalse(st["enabled"])
            self.assertTrue(st["rule_installed"])
            self.assertFalse(st["user_in_group"])
            self.assertEqual(st["user"], "bob")

    def test_status_missing_marker(self):
        gr = SimpleNamespace(gr_name="intermux", gr_gid=2000, gr_mem=["rishi"])
        with mock.patch.object(passwordless, "MARKER_PATH", self.marker_path), \
             mock.patch("grp.getgrnam", return_value=gr):
            st = passwordless.status(user="rishi")
            self.assertFalse(st["enabled"])
            self.assertFalse(st["rule_installed"])
            self.assertIsNone(st["backend"])
            self.assertIsNone(st["rule_path"])
            self.assertTrue(st["user_in_group"])

    def test_status_corrupt_marker(self):
        self.marker_path.write_text("{bad")
        with mock.patch.object(passwordless, "MARKER_PATH", self.marker_path):
            st = passwordless.status(user="rishi")
            self.assertFalse(st["enabled"])
            self.assertFalse(st["rule_installed"])

    def test_status_sudo_user_when_root(self):
        with mock.patch.object(passwordless, "MARKER_PATH", self.marker_path), \
             mock.patch("os.getuid", return_value=0), \
             mock.patch.dict(os.environ, {"SUDO_USER": "rishi"}), \
             mock.patch("grp.getgrnam", side_effect=KeyError("no group")):
            st = passwordless.status()
            self.assertEqual(st["user"], "rishi")

    def test_status_never_raises(self):
        with mock.patch.object(passwordless, "read_marker", side_effect=RuntimeError("unexpected")):
            st = passwordless.status()
            self.assertFalse(st["enabled"])
            self.assertFalse(st["rule_installed"])


class EnableTests(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tmpdir.name)
        self.rules_path = self.base / "polkit-1" / "rules.d" / "49-intermux-passwordless.rules"
        self.pkla_path = self.base / "polkit-1" / "localauthority" / "50-local.d" / "49-intermux-passwordless.pkla"
        self.marker_path = self.base / "var" / "lib" / "intermux" / "passwordless.json"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_enable_rules_backend_group_missing(self):
        calls = []

        def fake_run_checked(argv, **kwargs):
            calls.append(list(argv))
            return CmdResult(list(argv), 0, "", "")

        with mock.patch.object(passwordless, "RULES_PATH", self.rules_path), \
             mock.patch.object(passwordless, "PKLA_PATH", self.pkla_path), \
             mock.patch.object(passwordless, "MARKER_PATH", self.marker_path), \
             mock.patch("grp.getgrnam", side_effect=KeyError("intermux")), \
             mock.patch("shutil.which", return_value="/usr/bin/gpasswd"), \
             mock.patch("intermux.system.commands.run_checked", side_effect=fake_run_checked), \
             mock.patch.object(passwordless, "detect_backend", return_value="rules"):

            lines = passwordless.enable("alice")
            self.assertTrue(any("alice" in line for line in lines))
            self.assertTrue(any("Takes effect now" in line for line in lines))

            # groupadd and gpasswd called
            self.assertIn(["groupadd", "--system", "intermux"], calls)
            self.assertIn(["gpasswd", "-a", "alice", "intermux"], calls)

            # rules file exists with exact content
            self.assertTrue(self.rules_path.is_file())
            self.assertEqual(self.rules_path.read_text(encoding="utf-8"), passwordless.RULES_TEXT)
            self.assertFalse(self.pkla_path.exists())

            # marker file exists with group_created: True
            marker = json.loads(self.marker_path.read_text(encoding="utf-8"))
            self.assertEqual(marker["v"], 1)
            self.assertEqual(marker["backend"], "rules")
            self.assertEqual(marker["rule_path"], str(self.rules_path))
            self.assertTrue(marker["group_created"])

    def test_enable_pkla_backend_and_removes_other_rule_file(self):
        calls = []

        def fake_run_checked(argv, **kwargs):
            calls.append(list(argv))
            return CmdResult(list(argv), 0, "", "")

        # Write existing rules file that should be removed
        self.rules_path.parent.mkdir(parents=True, exist_ok=True)
        self.rules_path.write_text("old rules")

        gr = SimpleNamespace(gr_name="intermux", gr_gid=2000, gr_mem=["bob"])

        with mock.patch.object(passwordless, "RULES_PATH", self.rules_path), \
             mock.patch.object(passwordless, "PKLA_PATH", self.pkla_path), \
             mock.patch.object(passwordless, "MARKER_PATH", self.marker_path), \
             mock.patch("grp.getgrnam", return_value=gr), \
             mock.patch("shutil.which", return_value="/usr/bin/gpasswd"), \
             mock.patch("intermux.system.commands.run_checked", side_effect=fake_run_checked), \
             mock.patch.object(passwordless, "detect_backend", return_value="pkla"):

            passwordless.enable("alice")

            # groupadd was NOT called because group already existed
            self.assertNotIn(["groupadd", "--system", "intermux"], calls)
            self.assertIn(["gpasswd", "-a", "alice", "intermux"], calls)

            # pkla file exists with exact content
            self.assertTrue(self.pkla_path.is_file())
            self.assertEqual(self.pkla_path.read_text(encoding="utf-8"), passwordless.PKLA_TEXT)
            # Old rules file was removed
            self.assertFalse(self.rules_path.exists())

            # marker file records group_created: False
            marker = json.loads(self.marker_path.read_text(encoding="utf-8"))
            self.assertEqual(marker["backend"], "pkla")
            self.assertFalse(marker["group_created"])

    def test_enable_keeps_group_created_true_from_earlier_marker(self):
        self.marker_path.parent.mkdir(parents=True, exist_ok=True)
        self.marker_path.write_text(json.dumps({
            "v": 1, "backend": "rules", "rule_path": str(self.rules_path), "group_created": True
        }))

        gr = SimpleNamespace(gr_name="intermux", gr_gid=2000, gr_mem=["alice"])

        with mock.patch.object(passwordless, "RULES_PATH", self.rules_path), \
             mock.patch.object(passwordless, "PKLA_PATH", self.pkla_path), \
             mock.patch.object(passwordless, "MARKER_PATH", self.marker_path), \
             mock.patch("grp.getgrnam", return_value=gr), \
             mock.patch("shutil.which", return_value="/usr/bin/gpasswd"), \
             mock.patch("intermux.system.commands.run_checked", return_value=CmdResult([], 0, "", "")), \
             mock.patch.object(passwordless, "detect_backend", return_value="rules"):

            passwordless.enable("bob")
            marker = json.loads(self.marker_path.read_text(encoding="utf-8"))
            self.assertTrue(marker["group_created"])

    def test_enable_usermod_fallback_when_gpasswd_missing(self):
        calls = []

        def fake_which(cmd):
            return "/usr/sbin/usermod" if cmd == "usermod" else None

        gr = SimpleNamespace(gr_name="intermux", gr_gid=2000, gr_mem=[])

        with mock.patch.object(passwordless, "RULES_PATH", self.rules_path), \
             mock.patch.object(passwordless, "PKLA_PATH", self.pkla_path), \
             mock.patch.object(passwordless, "MARKER_PATH", self.marker_path), \
             mock.patch("grp.getgrnam", return_value=gr), \
             mock.patch("shutil.which", side_effect=fake_which), \
             mock.patch("intermux.system.commands.run_checked",
                        side_effect=lambda argv, **kw: calls.append(list(argv)) or CmdResult(list(argv), 0, "", "")), \
             mock.patch.object(passwordless, "detect_backend", return_value="rules"):

            passwordless.enable("alice")
            self.assertIn(["usermod", "-aG", "intermux", "alice"], calls)

    def test_enable_raises_when_no_tool_available(self):
        gr = SimpleNamespace(gr_name="intermux", gr_gid=2000, gr_mem=[])
        with mock.patch.object(passwordless, "RULES_PATH", self.rules_path), \
             mock.patch.object(passwordless, "PKLA_PATH", self.pkla_path), \
             mock.patch.object(passwordless, "MARKER_PATH", self.marker_path), \
             mock.patch("grp.getgrnam", return_value=gr), \
             mock.patch("shutil.which", return_value=None):
            with self.assertRaises(MissingToolError):
                passwordless.enable("alice")

    def test_enable_is_idempotent(self):
        gr = SimpleNamespace(gr_name="intermux", gr_gid=2000, gr_mem=["alice"])
        with mock.patch.object(passwordless, "RULES_PATH", self.rules_path), \
             mock.patch.object(passwordless, "PKLA_PATH", self.pkla_path), \
             mock.patch.object(passwordless, "MARKER_PATH", self.marker_path), \
             mock.patch("grp.getgrnam", return_value=gr), \
             mock.patch("shutil.which", return_value="/usr/bin/gpasswd"), \
             mock.patch("intermux.system.commands.run_checked", return_value=CmdResult([], 0, "", "")), \
             mock.patch.object(passwordless, "detect_backend", return_value="rules"):

            passwordless.enable("alice")
            first_marker = self.marker_path.read_text(encoding="utf-8")
            passwordless.enable("alice")
            second_marker = self.marker_path.read_text(encoding="utf-8")
            self.assertEqual(first_marker, second_marker)


class DisableTests(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tmpdir.name)
        self.rules_path = self.base / "polkit-1" / "rules.d" / "49-intermux-passwordless.rules"
        self.pkla_path = self.base / "polkit-1" / "localauthority" / "50-local.d" / "49-intermux-passwordless.pkla"
        self.marker_path = self.base / "var" / "lib" / "intermux" / "passwordless.json"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_disable_all_when_nothing_installed(self):
        calls = []
        with mock.patch.object(passwordless, "RULES_PATH", self.rules_path), \
             mock.patch.object(passwordless, "PKLA_PATH", self.pkla_path), \
             mock.patch.object(passwordless, "MARKER_PATH", self.marker_path), \
             mock.patch("intermux.system.commands.run_checked",
                        side_effect=lambda argv, **kw: calls.append(list(argv)) or CmdResult(list(argv), 0, "", "")):

            lines = passwordless.disable(all_users=True)
            self.assertEqual(lines, ["Passwordless was not on"])
            self.assertEqual(calls, [])

    def test_disable_all_deletes_files_and_calls_groupdel_when_group_created(self):
        calls = []
        self.marker_path.parent.mkdir(parents=True, exist_ok=True)
        self.marker_path.write_text(json.dumps({
            "v": 1, "backend": "rules", "rule_path": str(self.rules_path), "group_created": True
        }))
        self.rules_path.parent.mkdir(parents=True, exist_ok=True)
        self.rules_path.write_text("some rule")

        gr = SimpleNamespace(gr_name="intermux", gr_gid=2000, gr_mem=[])

        with mock.patch.object(passwordless, "RULES_PATH", self.rules_path), \
             mock.patch.object(passwordless, "PKLA_PATH", self.pkla_path), \
             mock.patch.object(passwordless, "MARKER_PATH", self.marker_path), \
             mock.patch("grp.getgrnam", return_value=gr), \
             mock.patch("intermux.system.commands.run_checked",
                        side_effect=lambda argv, **kw: calls.append(list(argv)) or CmdResult(list(argv), 0, "", "")):

            lines = passwordless.disable(all_users=True)
            self.assertFalse(self.rules_path.exists())
            self.assertFalse(self.marker_path.exists())
            self.assertIn(["groupdel", "intermux"], calls)
            self.assertTrue(any("Removed polkit rule" in l for l in lines))

    def test_disable_all_deletes_files_but_no_groupdel_when_group_created_false(self):
        calls = []
        self.marker_path.parent.mkdir(parents=True, exist_ok=True)
        self.marker_path.write_text(json.dumps({
            "v": 1, "backend": "rules", "rule_path": str(self.rules_path), "group_created": False
        }))
        self.rules_path.parent.mkdir(parents=True, exist_ok=True)
        self.rules_path.write_text("some rule")

        with mock.patch.object(passwordless, "RULES_PATH", self.rules_path), \
             mock.patch.object(passwordless, "PKLA_PATH", self.pkla_path), \
             mock.patch.object(passwordless, "MARKER_PATH", self.marker_path), \
             mock.patch("intermux.system.commands.run_checked",
                        side_effect=lambda argv, **kw: calls.append(list(argv)) or CmdResult(list(argv), 0, "", "")):

            lines = passwordless.disable(all_users=True)
            self.assertFalse(self.rules_path.exists())
            self.assertFalse(self.marker_path.exists())
            self.assertNotIn(["groupdel", "intermux"], calls)

    def test_disable_user_group_still_has_members(self):
        calls = []
        gr_after = SimpleNamespace(gr_name="intermux", gr_gid=2000, gr_mem=["bob"])

        with mock.patch.object(passwordless, "RULES_PATH", self.rules_path), \
             mock.patch.object(passwordless, "PKLA_PATH", self.pkla_path), \
             mock.patch.object(passwordless, "MARKER_PATH", self.marker_path), \
             mock.patch("grp.getgrnam", return_value=gr_after), \
             mock.patch("shutil.which", return_value="/usr/bin/gpasswd"), \
             mock.patch("intermux.system.commands.run_checked",
                        side_effect=lambda argv, **kw: calls.append(list(argv)) or CmdResult(list(argv), 0, "", "")):

            lines = passwordless.disable(user="alice")
            self.assertIn(["gpasswd", "-d", "alice", "intermux"], calls)
            self.assertNotIn(["groupdel", "intermux"], calls)
            self.assertEqual(lines, ["Removed alice from the intermux group."])

    def test_disable_user_group_empty_falls_through_to_all(self):
        calls = []
        self.marker_path.parent.mkdir(parents=True, exist_ok=True)
        self.marker_path.write_text(json.dumps({
            "v": 1, "backend": "rules", "rule_path": str(self.rules_path), "group_created": True
        }))
        self.rules_path.parent.mkdir(parents=True, exist_ok=True)
        self.rules_path.write_text("some rule")

        call_count = [0]

        def fake_getgrnam(name):
            if name != "intermux":
                raise KeyError(name)
            call_count[0] += 1
            if call_count[0] == 1:
                return SimpleNamespace(gr_name="intermux", gr_gid=2000, gr_mem=["alice"])
            return SimpleNamespace(gr_name="intermux", gr_gid=2000, gr_mem=[])

        with mock.patch.object(passwordless, "RULES_PATH", self.rules_path), \
             mock.patch.object(passwordless, "PKLA_PATH", self.pkla_path), \
             mock.patch.object(passwordless, "MARKER_PATH", self.marker_path), \
             mock.patch("grp.getgrnam", side_effect=fake_getgrnam), \
             mock.patch("shutil.which", return_value="/usr/bin/gpasswd"), \
             mock.patch("intermux.system.commands.run_checked",
                        side_effect=lambda argv, **kw: calls.append(list(argv)) or CmdResult(list(argv), 0, "", "")):

            lines = passwordless.disable(user="alice")
            self.assertIn(["gpasswd", "-d", "alice", "intermux"], calls)
            self.assertIn(["groupdel", "intermux"], calls)
            self.assertFalse(self.rules_path.exists())
            self.assertFalse(self.marker_path.exists())
            self.assertTrue(any("Removed alice" in l for l in lines))
            self.assertTrue(any("Removed polkit rule" in l for l in lines))


class MainDispatchTests(unittest.TestCase):

    def test_passwordless_as_fake_root_with_pkexec_uid_returns_1(self):
        err = io.StringIO()
        with mock.patch("os.geteuid", return_value=0), \
             mock.patch.dict(os.environ, {"PKEXEC_UID": "1000"}), \
             mock.patch("sys.stderr", err):
            code = main_mod.main(["passwordless", "on"])
            self.assertEqual(code, 1)
            self.assertIn("passwordless can only be changed with sudo, not through pkexec", err.getvalue())

    def test_passwordless_as_fake_root_with_sudo_user_passes_guard(self):
        with mock.patch("os.geteuid", return_value=0), \
             mock.patch.dict(os.environ, {"SUDO_USER": "rishi"}, clear=True), \
             mock.patch("intermux.cli.main", return_value=0) as cli_mock:
            code = main_mod.main(["passwordless", "on"])
            self.assertEqual(code, 0)
            cli_mock.assert_called_once_with(["passwordless", "on"])

    def test_interfaces_as_root_refused(self):
        err = io.StringIO()
        with mock.patch("os.geteuid", return_value=0), \
             mock.patch("sys.stderr", err):
            code = main_mod.main(["interfaces"])
            self.assertEqual(code, 1)
            self.assertIn("run InterMux commands as your own user", err.getvalue())


class CliParserAndCommandTests(unittest.TestCase):

    def test_passwordless_off_user_and_all_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            _parse(["passwordless", "off", "--user", "alice", "--all"])

    def test_passwordless_status_json(self):
        out = io.StringIO()
        st_data = {
            "enabled": False,
            "rule_installed": False,
            "backend": None,
            "rule_path": None,
            "group_exists": False,
            "members": [],
            "user": "rishi",
            "user_in_group": False,
        }
        with mock.patch("intermux.system.passwordless.status", return_value=st_data), \
             mock.patch("sys.stdout", out):
            code = cli.main(["passwordless", "status", "--json"])
            self.assertEqual(code, 0)
            data = json.loads(out.getvalue())
            self.assertEqual(data["user"], "rishi")
            self.assertFalse(data["enabled"])

    def test_passwordless_status_human_off(self):
        out = io.StringIO()
        st_data = {
            "enabled": False,
            "rule_installed": False,
            "backend": None,
            "rule_path": None,
            "group_exists": False,
            "members": [],
            "user": "rishi",
            "user_in_group": False,
        }
        with mock.patch("intermux.system.passwordless.status", return_value=st_data), \
             mock.patch("sys.stdout", out):
            code = cli.main(["passwordless", "status"])
            self.assertEqual(code, 0)
            self.assertIn("Passwordless: off. Turn on: sudo intermux passwordless on", out.getvalue())

    def test_passwordless_status_human_on(self):
        out = io.StringIO()
        st_data = {
            "enabled": True,
            "rule_installed": True,
            "backend": "rules",
            "rule_path": "/etc/polkit-1/rules.d/49-intermux-passwordless.rules",
            "group_exists": True,
            "members": ["rishi"],
            "user": "rishi",
            "user_in_group": True,
        }
        with mock.patch("intermux.system.passwordless.status", return_value=st_data), \
             mock.patch("sys.stdout", out):
            code = cli.main(["passwordless", "status"])
            self.assertEqual(code, 0)
            self.assertIn("Passwordless: on for rishi", out.getvalue())
            self.assertIn("(JS rules)", out.getvalue())

    def test_passwordless_on_cli_flow(self):
        out = io.StringIO()
        with mock.patch("intermux.system.passwordless.require_sudo_root") as req_mock, \
             mock.patch("intermux.system.passwordless.resolve_target_user", return_value="alice") as res_mock, \
             mock.patch("intermux.system.passwordless.enable", return_value=["line1", "line2"]) as en_mock, \
             mock.patch("sys.stdout", out):
            code = cli.main(["passwordless", "on", "--user", "alice"])
            self.assertEqual(code, 0)
            req_mock.assert_called_once()
            res_mock.assert_called_once_with("alice")
            en_mock.assert_called_once_with("alice")
            self.assertIn("line1\nline2\n", out.getvalue())

    def test_passwordless_off_all_cli_flow(self):
        out = io.StringIO()
        with mock.patch("intermux.system.passwordless.require_sudo_root") as req_mock, \
             mock.patch("intermux.system.passwordless.disable", return_value=["disabled all"]) as dis_mock, \
             mock.patch("sys.stdout", out):
            code = cli.main(["passwordless", "off", "--all"])
            self.assertEqual(code, 0)
            req_mock.assert_called_once()
            dis_mock.assert_called_once_with(all_users=True)
            self.assertIn("disabled all\n", out.getvalue())

    def test_passwordless_permission_denied_reports_error(self):
        err = io.StringIO()
        with mock.patch("intermux.system.passwordless.require_sudo_root",
                        side_effect=PermissionDeniedError("run: sudo intermux passwordless on")), \
             mock.patch("sys.stderr", err):
            code = cli.main(["passwordless", "on"])
            self.assertEqual(code, 1)
            self.assertIn("[X] run: sudo intermux passwordless on", err.getvalue())


class GuiIndicatorTests(unittest.TestCase):

    def test_gui_indicator_shows_when_passwordless_enabled(self):
        try:
            import tkinter as tk
        except ImportError:
            self.skipTest("tkinter not available")

        from intermux.ui import app as app_mod
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("Tk cannot open display")
        root.withdraw()

        with mock.patch("intermux.system.passwordless.status", return_value={"enabled": True}), \
             mock.patch.object(app_mod.InterMuxApp, "refresh_all", lambda self: None):
            window = app_mod.InterMuxApp(root)
            self.assertTrue(window.passwordless_on.get())
            self.assertIn("without a password", window.queue_hint_var.get())
            window._on_close()

    def test_gui_indicator_empty_when_passwordless_disabled(self):
        try:
            import tkinter as tk
        except ImportError:
            self.skipTest("tkinter not available")

        from intermux.ui import app as app_mod
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("Tk cannot open display")
        root.withdraw()

        with mock.patch("intermux.system.passwordless.status", return_value={"enabled": False}), \
             mock.patch.object(app_mod.InterMuxApp, "refresh_all", lambda self: None):
            window = app_mod.InterMuxApp(root)
            self.assertFalse(window.passwordless_on.get())
            self.assertIn("asks for your password", window.queue_hint_var.get())
            window._on_close()


if __name__ == "__main__":
    unittest.main()
