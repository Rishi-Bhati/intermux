"""Tests for the window's "No password prompts" switch: the pkexec toggle path and its guards."""

from __future__ import annotations

import io
import os
import subprocess
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import intermux.__main__ as main_mod
from intermux import cli
from intermux.errors import MissingToolError, PermissionDeniedError, ValidationError
from intermux.system import passwordless

REPO = Path(__file__).resolve().parents[1]
TOGGLE_SCRIPT = REPO / "packaging" / "linux" / "intermux-passwordless"
POLICY = REPO / "packaging" / "linux" / "org.intermux.policy"

TOGGLE_ENV = {"PKEXEC_UID": "1000", passwordless.POLKIT_ENV: "1"}


class ToggleGuardTests(unittest.TestCase):

    def test_via_toggle_action_needs_pkexec_and_marker(self):
        with mock.patch.dict(os.environ, TOGGLE_ENV, clear=True):
            self.assertTrue(passwordless.via_toggle_action())
        with mock.patch.dict(os.environ, {"PKEXEC_UID": "1000"}, clear=True):
            self.assertFalse(passwordless.via_toggle_action())
        # The marker alone (no pkexec) means sudo or a plain shell, not the toggle.
        with mock.patch.dict(os.environ, {passwordless.POLKIT_ENV: "1"}, clear=True):
            self.assertFalse(passwordless.via_toggle_action())

    def test_require_sudo_root_accepts_toggle_action(self):
        with mock.patch("os.geteuid", return_value=0), mock.patch.dict(os.environ, TOGGLE_ENV, clear=True):
            passwordless.require_sudo_root()

    def test_require_sudo_root_still_refuses_plain_pkexec(self):
        with mock.patch("os.geteuid", return_value=0), \
             mock.patch.dict(os.environ, {"PKEXEC_UID": "1000", passwordless.POLKIT_ENV: "0"}, clear=True):
            with self.assertRaises(PermissionDeniedError):
                passwordless.require_sudo_root()

    def test_toggle_targets_the_pkexec_user(self):
        pw = SimpleNamespace(pw_name="rishi", pw_uid=1000)
        with mock.patch.dict(os.environ, dict(TOGGLE_ENV, SUDO_USER="mallory"), clear=True), \
             mock.patch("pwd.getpwuid", return_value=pw) as by_uid, \
             mock.patch("pwd.getpwnam", return_value=pw):
            self.assertEqual(passwordless.resolve_target_user(None), "rishi")
            by_uid.assert_called_with(1000)

    def test_toggle_refuses_another_user(self):
        pw = SimpleNamespace(pw_name="rishi", pw_uid=1000)
        with mock.patch.dict(os.environ, TOGGLE_ENV, clear=True), mock.patch("pwd.getpwuid", return_value=pw):
            with self.assertRaises(ValidationError):
                passwordless.resolve_target_user("alice")

    def test_toggle_refuses_root_caller(self):
        pw = SimpleNamespace(pw_name="root", pw_uid=0)
        with mock.patch.dict(os.environ, dict(TOGGLE_ENV, PKEXEC_UID="0"), clear=True), \
             mock.patch("pwd.getpwuid", return_value=pw):
            with self.assertRaises(ValidationError):
                passwordless.resolve_target_user(None)

    def test_main_lets_toggle_action_through(self):
        with mock.patch("os.geteuid", return_value=0), mock.patch.dict(os.environ, TOGGLE_ENV, clear=True), \
             mock.patch("intermux.cli.main", return_value=0) as cli_mock:
            self.assertEqual(main_mod.main(["passwordless", "on"]), 0)
            cli_mock.assert_called_once_with(["passwordless", "on"])

    def test_main_still_blocks_plain_pkexec(self):
        err = io.StringIO()
        with mock.patch("os.geteuid", return_value=0), \
             mock.patch.dict(os.environ, {"PKEXEC_UID": "1000"}, clear=True), mock.patch("sys.stderr", err):
            self.assertEqual(main_mod.main(["passwordless", "on"]), 1)

    def test_cli_off_all_refused_through_toggle(self):
        err = io.StringIO()
        with mock.patch("os.geteuid", return_value=0), mock.patch.dict(os.environ, TOGGLE_ENV, clear=True), \
             mock.patch("intermux.system.passwordless.disable") as dis, mock.patch("sys.stderr", err):
            self.assertEqual(cli.main(["passwordless", "off", "--all"]), 1)
            dis.assert_not_called()
            self.assertIn("sudo intermux passwordless off --all", err.getvalue())


class SetFromGuiTests(unittest.TestCase):

    def setUp(self):
        self.which = mock.patch("shutil.which", return_value="/usr/bin/pkexec")
        self.which.start()
        self.addCleanup(self.which.stop)
        self.exists = mock.patch.object(Path, "is_file", return_value=True)
        self.exists.start()
        self.addCleanup(self.exists.stop)

    def _run(self, returncode, stdout="", stderr=""):
        proc = subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)
        return mock.patch("subprocess.run", return_value=proc)

    def test_command_uses_the_toggle_script(self):
        self.assertEqual(passwordless.toggle_command(True),
                         ["/usr/bin/pkexec", str(passwordless.TOGGLE_PATH), "on"])
        self.assertEqual(passwordless.toggle_command(False)[-1], "off")

    def test_success_returns_lines(self):
        with self._run(0, stdout="Added rishi to the intermux group.\n\nInstalled polkit rule: x\n") as run:
            lines = passwordless.set_from_gui(True)
        self.assertEqual(lines, ["Added rishi to the intermux group.", "Installed polkit rule: x"])
        self.assertEqual(run.call_args[0][0][-1], "on")

    def test_cancelled_prompt(self):
        for code in (126, 127):
            with self._run(code), self.assertRaises(PermissionDeniedError):
                passwordless.set_from_gui(False)

    def test_failure_reports_last_error_line(self):
        with self._run(1, stderr="[X] neither gpasswd nor usermod was found\n"):
            with self.assertRaises(ValidationError) as ctx:
                passwordless.set_from_gui(True)
        self.assertEqual(str(ctx.exception), "neither gpasswd nor usermod was found")

    def test_missing_script(self):
        self.exists.stop()
        with mock.patch.object(Path, "is_file", return_value=False):
            with self.assertRaises(MissingToolError) as ctx:
                passwordless.toggle_command(True)
        self.exists.start()
        self.assertIn("sudo intermux passwordless on", str(ctx.exception))


class PackagingTests(unittest.TestCase):

    def test_policy_has_password_checked_toggle_action(self):
        actions = {a.get("id"): a for a in ET.parse(POLICY).getroot().iter("action")}
        toggle = actions[passwordless.POLKIT_ACTION]
        for key in ("allow_any", "allow_inactive", "allow_active"):
            # auth_admin, never auth_admin_keep or yes: every change asks again.
            self.assertEqual(toggle.find(f"defaults/{key}").text, "auth_admin")
        paths = {a.get("id"): a.find("annotate[@key='org.freedesktop.policykit.exec.path']").text
                 for a in actions.values()}
        self.assertEqual(paths[passwordless.POLKIT_ACTION], str(passwordless.TOGGLE_PATH))
        self.assertEqual(paths["org.intermux.run"], "/usr/lib/intermux/intermux")

    def test_group_rule_never_covers_the_toggle(self):
        for text in (passwordless.RULES_TEXT, passwordless.PKLA_TEXT):
            self.assertNotIn(passwordless.POLKIT_ACTION, text)
            self.assertIn("org.intermux.run", text)

    def test_script_sets_marker_and_only_passes_on_or_off(self):
        text = TOGGLE_SCRIPT.read_text()
        self.assertTrue(os.access(TOGGLE_SCRIPT, os.X_OK))
        self.assertIn(f"{passwordless.POLKIT_ENV}=1", text)
        self.assertIn('exec /usr/lib/intermux/intermux passwordless "$1"', text)
        for bad in ([], ["--all"], ["status"], ["on; id"], ["ON"]):
            proc = subprocess.run(["sh", str(TOGGLE_SCRIPT), *bad], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 2, bad)

    def test_every_installer_ships_the_script(self):
        for rel in ("install.sh", "packaging/arch/PKGBUILD", "packaging/debian/rules",
                    "packaging/fedora/intermux.spec"):
            text = (REPO / rel).read_text()
            self.assertIn("packaging/linux/intermux-passwordless", text, rel)
            self.assertIn("/usr/lib/intermux/intermux-passwordless", text.replace("$INSTALL_LIB", "/usr/lib/intermux"),
                          rel)


class QueueHintTests(unittest.TestCase):

    def test_hint_follows_the_setting(self):
        try:
            from intermux.ui import app as app_mod
        except ImportError:
            self.skipTest("tkinter not available")
        self.assertIn("without a password", app_mod.queue_password_hint(True))
        self.assertIn("asks for your password", app_mod.queue_password_hint(False))


class HeaderSwitchTests(unittest.TestCase):
    """Drives the real window: ticking the switch confirms, runs pkexec in the worker and re-reads the status."""

    def setUp(self):
        try:
            import tkinter as tk
            from intermux.ui import app as app_mod
        except ImportError:
            self.skipTest("tkinter not available")
        try:
            self.root = tk.Tk()
        except tk.TclError:
            self.skipTest("Tk cannot open display")
        self.root.withdraw()
        self.app_mod = app_mod
        self.enabled = False
        status = mock.patch("intermux.system.passwordless.status", side_effect=lambda: {"enabled": self.enabled})
        status.start()
        self.addCleanup(status.stop)
        no_refresh = mock.patch.object(app_mod.InterMuxApp, "refresh_snapshot", lambda self: None)
        no_refresh.start()
        self.addCleanup(no_refresh.stop)
        self.window = app_mod.InterMuxApp(self.root)
        self.addCleanup(self.window._on_close)

    def _settle(self):
        import time
        deadline = time.time() + 5
        while self.window._busy and time.time() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.root.update()

    def test_turn_on_after_confirming(self):
        def fake_set(on):
            self.enabled = on
            return ["ok"]
        with mock.patch.object(self.app_mod, "_ask_choice", return_value=("on", False)) as ask, \
             mock.patch("intermux.system.passwordless.set_from_gui", side_effect=fake_set) as setter:
            self.window.passwordless_check.invoke()
            self._settle()
        ask.assert_called_once()
        setter.assert_called_once_with(True)
        self.assertTrue(self.window.passwordless_on.get())
        self.assertIn("without a password", self.window.queue_hint_var.get())

    def test_cancelled_confirmation_changes_nothing(self):
        with mock.patch.object(self.app_mod, "_ask_choice", return_value=(None, False)), \
             mock.patch("intermux.system.passwordless.set_from_gui") as setter:
            self.window.passwordless_check.invoke()
            self._settle()
        setter.assert_not_called()
        self.assertFalse(self.window.passwordless_on.get())

    def test_turn_off_without_confirmation_and_cancelled_prompt_keeps_state(self):
        self.enabled = True
        self.window._update_passwordless()
        with mock.patch.object(self.app_mod, "_ask_choice") as ask, \
             mock.patch("intermux.system.passwordless.set_from_gui",
                        side_effect=PermissionDeniedError("authorization was cancelled or denied")):
            self.window.passwordless_check.invoke()
            self._settle()
        ask.assert_not_called()
        self.assertTrue(self.window.passwordless_on.get())
        self.assertIn("unchanged", self.window.status_var.get())


if __name__ == "__main__":
    unittest.main()
