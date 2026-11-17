"""Static checks on the shell scripts: requests they send to the helper must pass protocol validation."""

from __future__ import annotations

import json
import os
import re
import unittest
from pathlib import Path

from intermux.helper.protocol import validate_request

ROOT = Path(__file__).resolve().parents[1]


class UninstallScriptTests(unittest.TestCase):
    def test_reset_request_is_a_valid_helper_request(self) -> None:
        text = (ROOT / "uninstall.sh").read_text()
        match = re.search(r"printf '%s\\n' '(\{.*?\})'", text)
        self.assertIsNotNone(match, "uninstall.sh must send its reset request to the helper")
        action, params = validate_request(json.loads(match.group(1)))
        self.assertEqual(action, "reset")
        self.assertTrue(params["kill_apps"])

    def test_uninstall_does_not_run_cli_commands_as_root(self) -> None:
        # intermux/__main__.py refuses CLI commands as root, so `intermux reset` would always fail here.
        text = (ROOT / "uninstall.sh").read_text()
        self.assertNotRegex(text, r'"\$INSTALL_BIN" reset')

    def test_uninstall_calls_passwordless_off_before_removing_bundle(self) -> None:
        text = (ROOT / "uninstall.sh").read_text()
        self.assertIn("passwordless off --all", text)
        self.assertIn('rm -rf -- "$INSTALL_LIB"', text)
        idx_pw = text.index("passwordless off --all")
        idx_rm = text.index('rm -rf -- "$INSTALL_LIB"')
        self.assertLess(idx_pw, idx_rm, "uninstall.sh must call passwordless off --all before removing $INSTALL_LIB")

    def test_uninstall_removes_both_rule_paths_and_marker(self) -> None:
        text = (ROOT / "uninstall.sh").read_text()
        js_rule = "/etc/polkit-1/rules.d/49-intermux-passwordless.rules"
        pkla = "/etc/polkit-1/localauthority/50-local.d/49-intermux-passwordless.pkla"
        marker = "/var/lib/intermux/passwordless.json"
        self.assertIn(js_rule, text)
        self.assertIn(pkla, text)
        self.assertIn(marker, text)
        self.assertIn('rm -f -- "$POLKIT_RULE" "$POLKIT_PKLA" "$PASSWORDLESS_MARKER"', text)


class InstallScriptTests(unittest.TestCase):
    def test_install_script_flags_and_passwordless_command(self) -> None:
        text = (ROOT / "install.sh").read_text()
        self.assertIn("--passwordless", text)
        self.assertIn("--no-passwordless", text)
        self.assertIn('passwordless on --user "$SUDO_USER"', text)

    def test_install_script_question_reads_from_tty(self) -> None:
        text = (ROOT / "install.sh").read_text()
        self.assertIn("read -r answer </dev/tty", text)
        self.assertIn("Don't ask for your password for InterMux? [y/N]", text)
        self.assertIn("/dev/tty", text)


class GetInstallScriptTests(unittest.TestCase):
    def test_get_install_accepts_and_forwards_both_flags(self) -> None:
        text = (ROOT / "get/install.sh").read_text()
        self.assertIn("--passwordless", text)
        self.assertIn("--no-passwordless", text)
        self.assertIn('as_root bash "$_dir/install.sh" "$_pw_flag"', text)

    def test_get_install_has_no_bashisms(self) -> None:
        text = (ROOT / "get/install.sh").read_text()
        self.assertNotIn("[[", text)
        self.assertNotIn("function ", text)
        self.assertNotIn("local ", text)


class PackagingTests(unittest.TestCase):
    def test_every_package_ships_the_watcher_unit(self) -> None:
        for rel in ("packaging/debian/rules", "packaging/fedora/intermux.spec", "packaging/arch/PKGBUILD",
                    "install.sh"):
            self.assertIn("intermux-watch.service", (ROOT / rel).read_text(), rel)

    def test_debian_native_version_has_no_revision(self) -> None:
        self.assertIn("native", (ROOT / "packaging/debian/source/format").read_text())
        first = (ROOT / "packaging/debian/changelog").read_text().splitlines()[0]
        self.assertRegex(first, r"^intermux \(\d+(\.\d+)*\) ")

    def test_pyinstaller_pins_match(self) -> None:
        install = re.search(r'PYINSTALLER_VERSION="([\d.]+)"', (ROOT / "install.sh").read_text())
        self.assertIsNotNone(install)
        workflow = (ROOT / ".github/workflows/build_linux.yml").read_text()
        self.assertIn("pyinstaller==" + install.group(1), workflow)

    def test_builds_do_not_need_home(self) -> None:
        for rel in ("packaging/debian/rules", "packaging/fedora/intermux.spec", "packaging/arch/PKGBUILD",
                    "install.sh"):
            self.assertIn("PYINSTALLER_CONFIG_DIR", (ROOT / rel).read_text(), rel)

    def test_watcher_is_enabled_by_arch_and_fedora(self) -> None:
        self.assertIn("systemctl enable --now intermux-watch", (ROOT / "packaging/arch/intermux.install").read_text())
        self.assertIn("enable intermux-watch.service", (ROOT / "packaging/linux/intermux-watch.preset").read_text())
        self.assertIn("%systemd_post intermux-watch.service", (ROOT / "packaging/fedora/intermux.spec").read_text())

    def test_no_bashisms_in_rpm_scriptlets(self) -> None:
        self.assertNotIn("&>", (ROOT / "packaging/fedora/intermux.spec").read_text())

    def test_install_script_trust_and_preflight(self) -> None:
        text = (ROOT / "install.sh").read_text()
        self.assertIn("--allow-local-bundle", text)
        self.assertIn("import tkinter", text)
        self.assertIn("/run/systemd/system", text)
        self.assertNotIn("command -v systemctl", text)

    def test_release_tarball_ships_install_files(self) -> None:
        text = (ROOT / ".github/workflows/build_linux.yml").read_text()
        for needle in ("install.sh", "uninstall.sh", "README.md", "packaging/linux", "README-install.txt"):
            self.assertIn(needle, text)

    def test_debian_prerm_exists_executable_and_calls_passwordless_off(self) -> None:
        prerm = ROOT / "packaging/debian/prerm"
        self.assertTrue(prerm.is_file(), "packaging/debian/prerm must exist")
        self.assertTrue(os.access(prerm, os.X_OK), "packaging/debian/prerm must be executable")
        text = prerm.read_text()
        self.assertIn("passwordless off --all", text)
        self.assertIn("/usr/lib/intermux/intermux", text)

    def test_arch_and_fedora_hooks_call_passwordless_off(self) -> None:
        arch = (ROOT / "packaging/arch/intermux.install").read_text()
        self.assertIn("passwordless off --all", arch)
        self.assertIn("/usr/lib/intermux/intermux", arch)

        fedora = (ROOT / "packaging/fedora/intermux.spec").read_text()
        self.assertIn("passwordless off --all", fedora)
        self.assertIn("if [ $1 -eq 0 ]", fedora)
        self.assertIn("/usr/lib/intermux/intermux", fedora)


if __name__ == "__main__":
    unittest.main()
