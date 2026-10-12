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


if __name__ == "__main__":
    unittest.main()
