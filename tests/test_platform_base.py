"""Cross-platform unit tests for intermux.platform abstraction and M1 contracts."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

from intermux import bugreport, platform, state
from intermux.errors import ValidationError
from intermux.helper import protocol
from intermux.platform import base, paths


class PlatformPathsTests(unittest.TestCase):
    def test_linux_paths_preserve_legacy_constants(self) -> None:
        resolved = paths.resolve_paths(platform="linux", home="/home/alice")
        self.assertEqual(str(resolved.state_dir), "/var/lib/intermux")
        self.assertEqual(str(resolved.state_file), "/var/lib/intermux/state.json")
        self.assertEqual(str(resolved.run_dir), "/run/intermux")
        self.assertEqual(str(resolved.helper_lock_path), "/run/intermux/helper.lock")
        self.assertEqual(str(resolved.app_log_dir), "/home/alice/.local/state/intermux/logs")
        self.assertEqual(str(resolved.history_path), "/home/alice/.local/state/intermux/history.json")
        self.assertEqual(str(resolved.report_dir), "/home/alice/intermux-reports")

    def test_windows_paths_use_program_files_program_data_and_local_appdata(self) -> None:
        env = {
            "USERPROFILE": r"C:\Users\alice",
            "LOCALAPPDATA": r"C:\Users\alice\AppData\Local",
            "ProgramData": r"C:\ProgramData",
            "ProgramFiles": r"C:\Program Files",
        }
        resolved = paths.resolve_paths(platform="windows", environ=env)
        self.assertEqual(str(resolved.state_dir), r"C:\ProgramData\InterMux")
        self.assertEqual(str(resolved.state_file), r"C:\ProgramData\InterMux\state.json")
        self.assertEqual(str(resolved.app_log_dir), r"C:\Users\alice\AppData\Local\InterMux\logs")
        self.assertEqual(str(resolved.history_path), r"C:\Users\alice\AppData\Local\InterMux\history.json")
        self.assertEqual(str(resolved.report_dir), r"C:\Users\alice\intermux-reports")
        self.assertEqual(str(resolved.program_files_dir), r"C:\Program Files\InterMux")
        self.assertEqual(str(resolved.ipc_token_path), r"C:\Users\alice\AppData\Local\InterMux\ipc.token")


class ValidatorTests(unittest.TestCase):
    def test_linux_iface_validation_strict(self) -> None:
        self.assertEqual(base.validate_iface_name("wlan0", platform="linux"), "wlan0")
        for bad in ("", "Wi Fi", "Ethernet 2", "..", "waytoolonginterfacename"):
            with self.assertRaises(ValidationError):
                base.validate_iface_name(bad, platform="linux")

    def test_windows_iface_validation_allows_spaces_and_parens(self) -> None:
        for ok in ("Wi-Fi", "Ethernet 2", "Local Area Connection* 10", "WireGuard Tunnel"):
            self.assertEqual(base.validate_iface_name(ok, platform="windows"), ok)
        for bad in ("", "   ", "..", "a..b", "foo/bar", r"foo\bar", "bad\x00name", "x" * 257):
            with self.assertRaises(ValidationError):
                base.validate_iface_name(bad, platform="windows")

    def test_windows_app_path_validation(self) -> None:
        self.assertEqual(
            base.validate_app_path(r"C:\Program Files\Mozilla Firefox\firefox.exe", platform="windows"),
            r"C:\Program Files\Mozilla Firefox\firefox.exe",
        )
        self.assertEqual(
            base.validate_app_path(r"\\server\share\app.exe", platform="windows"),
            r"\\server\share\app.exe",
        )
        for bad in ("/usr/bin/firefox", "firefox.exe", r"C:\dir\\", r"C:\a=b\app.exe", "C:\\bad\napp.exe"):
            with self.assertRaises(ValidationError):
                base.validate_app_path(bad, platform="windows")

    def test_protocol_validate_request_windows_mode(self) -> None:
        action, params = protocol.validate_request(
            {
                "v": 1,
                "action": "bind",
                "params": {
                    "iface": "Ethernet 2",
                    "app": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    "dns": ["1.1.1.1"],
                },
            },
            platform="windows",
        )
        self.assertEqual(action, "bind")
        self.assertEqual(params["iface"], "Ethernet 2")


class StateAndScrubTests(unittest.TestCase):
    def test_state_from_dict_windows_accepts_windows_iface_and_namespace(self) -> None:
        raw = {
            "version": 1,
            "bindings": [
                {
                    "iface": "Wi-Fi",
                    "app": r"C:\Program Files\Firefox\firefox.exe",
                    "app_name": "firefox.exe",
                    "namespace": "win_1234abcd",
                    "subnet": "198.19.1.0/24",
                    "table": 1001,
                    "priority": 1001,
                }
            ],
        }
        loaded = state.state_from_dict(raw, platform="windows")
        self.assertEqual(len(loaded.bindings), 1)
        self.assertEqual(loaded.bindings[0].iface, "Wi-Fi")

    def test_bugreport_scrubs_windows_user_paths(self) -> None:
        text = r"Failed opening C:\Users\Rishi\AppData\Local\InterMux\logs\app.log from 192.168.1.50"
        scrubbed = bugreport.scrub_text(text, r"C:\Users\Rishi", "Rishi")
        self.assertNotIn("Rishi", scrubbed)
        self.assertNotIn("192.168.1.50", scrubbed)

    @unittest.skipUnless(sys.platform == "linux", "Linux backend test")
    def test_linux_platform_backend_smoke(self) -> None:
        platform.reset_cached_platform()
        plat = platform.get_platform()
        self.assertEqual(plat.name, "linux")
        self.assertEqual(str(plat.paths.state_dir), "/var/lib/intermux")
