"""Static checks on the shell scripts: requests they send to the helper must pass protocol validation."""

from __future__ import annotations

import json
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


if __name__ == "__main__":
    unittest.main()
