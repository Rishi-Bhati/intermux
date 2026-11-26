"""Tier 1 & 2 opaque-box contract tests for CLI, IPC v1, validation, shaping, DNS, and paths."""

from __future__ import annotations

import unittest

from intermux import bugreport, cli, config, dnsconf, state
from intermux.errors import PermissionDeniedError, ValidationError
from intermux.helper import protocol
from intermux.platform import base, paths, win_netinfo, win_pipe
from intermux.system import shaping, traffic
from tests.e2e_win import harness


class ContractTests(unittest.TestCase):
    def test_T1_F01_interface_classification_and_vpn_bindable(self) -> None:
        """T1-F01: Windows interface classification includes VPNs as bindable and excludes virtual switches."""
        ifaces = win_netinfo.parse_engine_interfaces([
            {"name": "Wi-Fi", "kind": "wifi", "up": True, "ipv4_cidrs": ["192.168.1.10/24"], "gateways": ["192.168.1.1"]},
            {"name": "WireGuard", "kind": "vpn", "up": True, "ipv4_cidrs": ["10.9.0.2/32"], "gateways": []},
            {"name": "vEthernet (WSL)", "kind": "virtual", "up": True, "ipv4_cidrs": ["172.22.0.1/20"], "gateways": []},
        ])
        self.assertTrue(ifaces[0].bindable)
        self.assertTrue(ifaces[1].bindable)
        self.assertFalse(ifaces[2].bindable)

    def test_T1_F07_shaping_bounds_and_formatting(self) -> None:
        """T1-F07: Shaping bounds 0.064..100000 Mbit/s match v2.2.1."""
        self.assertIsNone(shaping.check_mbit(None))
        self.assertEqual(shaping.check_mbit(0.064), 0.064)
        self.assertEqual(shaping.check_mbit(100_000.0), 100_000.0)
        for bad in (0.063, 100_000.1, -1, float("nan"), True):
            with self.assertRaises(ValueError):
                shaping.check_mbit(bad)

    def test_T1_F08_dns_presets_and_validation(self) -> None:
        """T1-F08: DNS presets and invalid server rejections."""
        for preset in ("cloudflare", "google", "quad9"):
            self.assertTrue(len(dnsconf.parse_servers(preset)) >= 2)
        for bad in ("127.0.0.1", "169.254.1.1", "224.0.0.1", "0.0.0.0"):
            with self.assertRaises(ValueError):
                dnsconf.check_servers([bad])

    def test_T1_F10_cli_parser_exposes_all_v221_commands(self) -> None:
        """T1-F10: Unified CLI parser exposes all v2.2.1 commands."""
        parser = cli.build_parser()
        sub = next(a for a in parser._actions if hasattr(a, "choices") and a.choices)
        expected = {
            "interfaces", "apps", "bind", "status", "dns", "limit", "restart",
            "remove", "move", "reconnect", "stop", "kill", "verify", "adopt",
            "release", "teardown-idle", "reset", "check", "report", "passwordless",
        }
        self.assertTrue(expected.issubset(set(sub.choices.keys())))

    def test_T2_F13_windows_iface_and_path_boundaries(self) -> None:
        """T2-F13: Windows validation rejects traversal/control chars and accepts spaces/UNC."""
        self.assertEqual(base.validate_iface_name("Local Area Connection* 10", platform="windows"), "Local Area Connection* 10")
        for bad in ("", "..", "a..b", "a/b", r"a\b", "bad\x00"):
            with self.assertRaises(ValidationError):
                base.validate_iface_name(bad, platform="windows")

    def test_T2_F13_fake_pipe_server_identity_rejected(self) -> None:
        """T2-F13: Client rejects fake or unprivileged Named Pipe servers."""
        expected = r"C:\Program Files\InterMux\intermux-net.exe"
        with self.assertRaises(PermissionDeniedError):
            win_pipe.verify_pipe_server_identity(555, expected, False, expected)
        with self.assertRaises(PermissionDeniedError):
            win_pipe.verify_pipe_server_identity(555, r"C:\Users\bob\AppData\Local\intermux-net.exe", True, expected)

    def test_T2_F14_bugreport_scrubs_windows_secrets(self) -> None:
        """T2-F14: Bug report scrubber redacts C:\\Users\\<name>, IPs, MACs, URL queries, and tokens."""
        raw = r"User C:\Users\Alice\AppData\Local\InterMux\logs\app.log ip=10.20.30.40 mac=AA:BB:CC:DD:EE:FF https://x.test/cb?token=secret rsk_1234abcd"
        out = bugreport.scrub_text(raw, r"C:\Users\Alice", "Alice")
        for sensitive in ("Alice", "10.20.30.40", "AA:BB:CC:DD:EE:FF", "secret", "rsk_1234abcd"):
            self.assertNotIn(sensitive, out)

    def test_T1_F15_paths_contract(self) -> None:
        """T1-F15: Linux and Windows path contracts stay cleanly separated."""
        harness.make_isolated_env(self)
        lin = paths.resolve_paths(platform="linux", home="/home/u")
        win = paths.resolve_paths(platform="windows")
        self.assertEqual(lin.state_dir.as_posix(), config.STATE_DIR.as_posix())
        self.assertIn("InterMux", str(win.program_files_dir))
