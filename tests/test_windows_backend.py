"""Unit tests for the M3 Windows platform backend (cross-platform + Win32 security checks)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from intermux.errors import PermissionDeniedError
from intermux.platform import win_apps, win_netinfo, win_pipe, windows


class WinNetinfoTests(unittest.TestCase):
    def test_classify_kind_and_vpn_bindable(self) -> None:
        self.assertEqual(win_netinfo.classify_win_kind(71, "Wi-Fi", "Intel Wi-Fi 6"), "wifi")
        self.assertEqual(win_netinfo.classify_win_kind(6, "Ethernet", "Realtek PCIe GbE"), "ethernet")
        self.assertEqual(win_netinfo.classify_win_kind(53, "WireGuard Tunnel", "WireGuard"), "vpn")
        self.assertEqual(win_netinfo.classify_win_kind(6, "vEthernet (WSL)", "Hyper-V Virtual"), "virtual")

        parsed = win_netinfo.parse_engine_interfaces([
            {
                "name": "Mullvad",
                "kind": "vpn",
                "up": True,
                "bindable": True,
                "ipv4_cidrs": ["10.64.0.2/32"],
                "gateways": [],
                "index": 18,
                "link_speed_bps": 125_000_000,
            }
        ])
        self.assertEqual(len(parsed), 1)
        self.assertTrue(parsed[0].bindable)
        self.assertEqual(parsed[0].kind, "vpn")


class WinPipeSecurityTests(unittest.TestCase):
    def test_pipe_name_is_user_scoped_and_deterministic(self) -> None:
        p1 = win_pipe.pipe_name_for_sid("S-1-5-21-100-200-1001")
        p2 = win_pipe.pipe_name_for_sid("S-1-5-21-100-200-1002")
        self.assertTrue(p1.startswith(r"\\.\pipe\intermux-net-"))
        self.assertNotEqual(p1, p2)

    def test_server_identity_verification_rejects_fake_pipe_servers(self) -> None:
        expected = r"C:\Program Files\InterMux\intermux-net.exe"
        # Valid elevated Program Files image passes
        win_pipe.verify_pipe_server_identity(1234, expected, True, expected)
        # Case-insensitive path match passes
        win_pipe.verify_pipe_server_identity(1234, r"c:\program files\intermux\intermux-net.exe", True, expected)

        # Non-elevated squatter must be rejected
        with self.assertRaises(PermissionDeniedError):
            win_pipe.verify_pipe_server_identity(1234, expected, False, expected)

        # User-writable spoofed executable path must be rejected even if elevated
        with self.assertRaises(PermissionDeniedError):
            win_pipe.verify_pipe_server_identity(
                1234, r"C:\Users\bob\AppData\Local\InterMux\intermux-net.exe", True, expected
            )

        # Invalid PID must be rejected
        with self.assertRaises(PermissionDeniedError):
            win_pipe.verify_pipe_server_identity(0, expected, True, expected)


class WinAppsTests(unittest.TestCase):
    def test_chromium_and_firefox_handlers_separate_profiles(self) -> None:
        edge = win_apps.get_handler(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
        self.assertEqual(edge.name, "chromium")
        args = edge.extra_args("Wi-Fi", "separate")
        self.assertTrue(any(a.startswith("--user-data-dir=") and "msedge-Wi-Fi" in a for a in args))

        ff = win_apps.get_handler(r"C:\Program Files\Mozilla Firefox\firefox.exe")
        self.assertEqual(ff.name, "firefox")
        ff_args = ff.extra_args("Ethernet 2", "separate")
        self.assertIn("--no-remote", ff_args)
        self.assertIn("--profile", ff_args)

    def test_windows_platform_helper_dispatch_mocked(self) -> None:
        plat = windows.WindowsPlatform()
        with mock.patch("intermux.platform.win_pipe.call_engine", return_value={"iface": "Wi-Fi", "down": 10.0, "up": None}) as m:
            res = plat.elevation.helper_call("set_limit", {"iface": "Wi-Fi", "down": 10.0, "up": None})
            self.assertEqual(res["down"], 10.0)
            m.assert_called_once_with("set_limit", {"iface": "Wi-Fi", "down": 10.0, "up": None})
