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

    def test_uninstall_disables_socket_and_removes_units(self) -> None:
        text = (ROOT / "uninstall.sh").read_text()
        self.assertIn("systemctl --global disable intermuxd.socket", text)
        self.assertIn("intermuxd.socket", text)
        self.assertIn("intermuxd.service", text)
        self.assertIn("intermux-tray.desktop", text)
        self.assertIn("systemctl daemon-reload", text)


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

    def test_install_script_daemon_units_and_socket(self) -> None:
        text = (ROOT / "install.sh").read_text()
        self.assertIn('if [ -f "$INSTALL_LIB/intermuxd" ]; then', text)
        idx_guard = text.index('if [ -f "$INSTALL_LIB/intermuxd" ]; then')
        end_guard = text.index("fi", idx_guard)
        block = text[idx_guard:end_guard]
        self.assertIn("intermuxd.socket", block)
        self.assertIn("intermuxd.service", block)
        self.assertIn("intermux-tray.desktop", block)
        self.assertIn("systemctl --global enable intermuxd.socket", block)
        # Ensure systemctl --global enable is only within that conditional block
        self.assertEqual(text.count("systemctl --global enable intermuxd.socket"), 1)


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
        # A native package has no "-revision"; a pre-release uses Debian's "~" so it sorts before the final.
        self.assertRegex(first, r"^intermux \(\d+(\.\d+)*(~[a-z]+\d+)?\) ")

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

    def test_new_unit_files_and_desktop_file_exist_with_exact_lines(self) -> None:
        socket_file = ROOT / "packaging/linux/intermuxd.socket"
        self.assertTrue(socket_file.is_file(), "packaging/linux/intermuxd.socket must exist")
        socket_text = socket_file.read_text()
        self.assertIn("[Unit]", socket_text)
        self.assertIn("Description=InterMux daemon socket", socket_text)
        self.assertIn("Documentation=https://github.com/Rishi-Bhati/intermux", socket_text)
        self.assertIn("[Socket]", socket_text)
        self.assertIn("ListenStream=%t/intermux/daemon.sock", socket_text)
        self.assertIn("SocketMode=0600", socket_text)
        self.assertIn("DirectoryMode=0700", socket_text)
        self.assertIn("[Install]", socket_text)
        self.assertIn("WantedBy=sockets.target", socket_text)

        service_file = ROOT / "packaging/linux/intermuxd.service"
        self.assertTrue(service_file.is_file(), "packaging/linux/intermuxd.service must exist")
        service_text = service_file.read_text()
        self.assertIn("[Unit]", service_text)
        self.assertIn("Description=InterMux daemon (watches interfaces and routed apps for the tray and window)", service_text)
        self.assertIn("Requires=intermuxd.socket", service_text)
        self.assertIn("After=intermuxd.socket", service_text)
        self.assertIn("[Service]", service_text)
        self.assertIn("ExecStart=/usr/lib/intermux/intermuxd", service_text)
        self.assertIn("Restart=on-failure", service_text)
        self.assertIn("RestartSec=2", service_text)
        self.assertIn("[Install]", service_text)
        self.assertIn("Also=intermuxd.socket", service_text)

        desktop_file = ROOT / "packaging/linux/intermux-tray.desktop"
        self.assertTrue(desktop_file.is_file(), "packaging/linux/intermux-tray.desktop must exist")
        desktop_text = desktop_file.read_text()
        self.assertIn("Type=Application", desktop_text)
        self.assertIn("Name=InterMux tray", desktop_text)
        self.assertIn("Comment=Status and quick controls for InterMux", desktop_text)
        self.assertIn("Exec=/usr/lib/intermux/intermux-tray", desktop_text)
        self.assertIn("Icon=intermux", desktop_text)
        self.assertIn("Categories=Network;", desktop_text)
        self.assertIn("Terminal=false", desktop_text)

    def test_build_linux_copies_both_binaries_before_checksum_step(self) -> None:
        text = (ROOT / ".github/workflows/build_linux.yml").read_text()
        self.assertIn("intermuxd", text)
        self.assertIn("intermux-tray", text)
        idx_daemon = text.index("intermuxd dist/intermux/")
        idx_tray = text.index("intermux-tray dist/intermux/")
        idx_checksum = text.index("- name: Package and checksum")
        self.assertLess(idx_daemon, idx_checksum, "intermuxd copy step must appear before checksum step")
        self.assertLess(idx_tray, idx_checksum, "intermux-tray copy step must appear before checksum step")

    def test_pkgbuild_has_cargo_in_makedepends(self) -> None:
        pkgbuild = (ROOT / "packaging/arch/PKGBUILD").read_text()
        match = re.search(r"makedepends=\(([^)]+)\)", pkgbuild)
        self.assertIsNotNone(match, "PKGBUILD must define makedepends")
        self.assertIn("'cargo'", match.group(1))


if __name__ == "__main__":
    unittest.main()
