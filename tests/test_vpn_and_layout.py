"""Tests for NetworkManager VPN helpers, tunnel detection, and the pure helpers of the new layout."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from intermux.system import netinfo, nm


class NmParsingTests(unittest.TestCase):
    OUTPUT = (
        "Wired connection 1:aaaaaaaa-1111-2222-3333-444444444444:802-3-ethernet:enp3s0:activated\n"
        "Mullvad\\: Sweden:bbbbbbbb-1111-2222-3333-444444444444:wireguard:mullvad-se:activated\n"
        "Work:cccccccc-1111-2222-3333-444444444444:vpn::\n"
        "tailscale0:dddddddd-1111-2222-3333-444444444444:tun:tailscale0:activated\n"
        "broken line\n"
    )

    def test_parse_keeps_only_vpns_active_first(self):
        conns = nm.parse_connections(self.OUTPUT)
        self.assertEqual([c.name for c in conns], ["Mullvad: Sweden", "Work"])
        self.assertTrue(conns[0].active)
        self.assertEqual(conns[0].device, "mullvad-se")
        self.assertEqual(conns[1].device, "")
        self.assertEqual(conns[0].kind_label, "WireGuard")

    def test_split_terse_unescapes(self):
        self.assertEqual(nm.split_terse(r"a\:b:c\\d:"), ["a:b", "c\\d", ""])

    def test_private_args(self):
        self.assertEqual(nm.private_args("vpn"), ["ipv4.never-default", "yes", "ipv6.never-default", "yes"])
        self.assertIn("wireguard.ip4-auto-default-route", nm.private_args("wireguard"))

    def test_import_kind(self):
        self.assertEqual(nm.import_kind("/x/Mullvad.CONF"), "wireguard")
        self.assertEqual(nm.import_kind("a.ovpn"), "openvpn")
        self.assertIsNone(nm.import_kind("a.txt"))

    def test_connect_makes_it_apps_only_first(self):
        conn = nm.VpnConnection("W", "u-1", "wireguard", "", False)
        ok = mock.Mock(ok=True, stdout="", stderr="")
        with mock.patch.object(nm.commands, "which", return_value="/usr/bin/nmcli"), \
                mock.patch.object(nm.commands, "run", return_value=ok) as run:
            nm.connect(conn)
        first, second = (call[0][0] for call in run.call_args_list)
        self.assertEqual(first[:4], ["/usr/bin/nmcli", "connection", "modify", "u-1"])
        self.assertIn("ipv4.never-default", first)
        self.assertEqual(second[-3:], ["connection", "up", "u-1"])

    def test_connect_without_apps_only_does_not_modify(self):
        conn = nm.VpnConnection("W", "u-1", "vpn", "", False)
        ok = mock.Mock(ok=True, stdout="", stderr="")
        with mock.patch.object(nm.commands, "which", return_value="/usr/bin/nmcli"), \
                mock.patch.object(nm.commands, "run", return_value=ok) as run:
            nm.connect(conn, apps_only=False)
        self.assertEqual(len(run.call_args_list), 1)

    def test_import_returns_uuid_and_errors_are_readable(self):
        out = mock.Mock(ok=True, stdout="Connection 'm' (0f7b0c2e-1a2b-4c3d-9e8f-001122334455) successfully added.\n",
                        stderr="")
        with mock.patch.object(nm.commands, "which", return_value="/usr/bin/nmcli"), \
                mock.patch.object(nm.commands, "run", return_value=out):
            self.assertEqual(nm.import_file("/tmp/m.conf"), "0f7b0c2e-1a2b-4c3d-9e8f-001122334455")
        bad = mock.Mock(ok=False, stdout="", stderr="Error: plugin missing\n")
        with mock.patch.object(nm.commands, "which", return_value="/usr/bin/nmcli"), \
                mock.patch.object(nm.commands, "run", return_value=bad):
            with self.assertRaises(nm.InterMuxError) as ctx:
                nm.import_file("/tmp/m.ovpn")
        self.assertIn("plugin missing", ctx.exception.message)

    def test_missing_nmcli(self):
        with mock.patch.object(nm.commands, "which", return_value=None):
            self.assertFalse(nm.available())
            with self.assertRaises(nm.MissingToolError):
                nm.list_vpns()


class TunnelDetectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _dev(self, name, **files):
        d = self.base / name
        d.mkdir()
        for fname, text in files.items():
            (d / fname).write_text(text)
        return name

    def test_any_named_tunnel_is_vpn(self):
        self.assertEqual(netinfo.classify_kind(self._dev("mullvad-se", uevent="DEVTYPE=wireguard\nINTERFACE=x\n"),
                                               self.base), "vpn")
        self.assertEqual(netinfo.classify_kind(self._dev("proton0", tun_flags="0x1001\n"), self.base), "vpn")
        self.assertEqual(netinfo.classify_kind(self._dev("isp", type="512\n"), self.base), "vpn")

    def test_tailscale_and_plain_devices_are_not(self):
        self.assertNotEqual(netinfo.classify_kind(self._dev("tailscale0", tun_flags="0x1001\n"), self.base), "vpn")
        self.assertEqual(netinfo.classify_kind(self._dev("enp3s0", type="1\n"), self.base), "ethernet")


class LayoutHelperTests(unittest.TestCase):
    def test_limit_ops_one_per_interface(self):
        from intermux.ui.app import limit_ops_for
        items = [{"iface": "wlan0", "limit": {"down": 5.0, "up": None}}, {"iface": "eth0", "limit": None},
                 {"iface": "wlan0", "limit": {"down": 10.0, "up": 2.0}}]
        self.assertEqual(limit_ops_for(items), [{"op": "set_limit", "iface": "wlan0", "down": 10.0, "up": 2.0}])

    def test_pending_detail_mentions_limit(self):
        from intermux.ui.app import pending_detail
        text = pending_detail({"iface": "wlan0", "dns": "keep", "limit": {"down": 20.0, "up": None}}, [])
        self.assertTrue(text.endswith("limit ↓ 20 Mbit/s"))

    def test_limit_text_and_meta(self):
        from intermux.ui import connections
        self.assertEqual(connections.limit_text(None), "none")
        self.assertEqual(connections.limit_text({"down": 20.0, "up": 5.0}), "↓ 20 ↑ 5 Mbit/s")
        self.assertEqual(connections.limit_text({"down": 20.0, "up": 0.5}), "↓ 20 Mbit/s  ↑ 500 kbit/s")
        meta = connections.app_meta({"running": True, "pids": [1, 2]}, (2048.0, 0.0), "1.2.3.4", False)
        self.assertEqual(meta, "2 processes  ·  ↓ 2.0 KB/s  ↑ 0 B/s  ·  IP 1.2.3.4")
        self.assertEqual(connections.app_meta({"running": False, "pids": []}, (2048.0, 0.0), "", False),
                         "not running")
        self.assertEqual(connections.app_meta({"alive": True}, None, "", True), "moved while running")

    def test_ellipsize(self):
        from intermux.ui.tooltip import ellipsize_to
        measure = len
        self.assertEqual(ellipsize_to("hello", 10, measure), "hello")
        self.assertEqual(ellipsize_to("hello world", 6, measure), "hello…")
        self.assertEqual(ellipsize_to("hello", 1, measure), "…")

    def test_sash_colour(self):
        from intermux.ui import theme
        from intermux.ui.app import sash_colour
        self.assertTrue(sash_colour(theme.PALETTE).startswith("#"))


class BatchLimitTests(unittest.TestCase):
    def test_batch_accepts_set_limit_and_runs_it(self):
        from intermux.helper import protocol, server
        _, params = protocol.validate_request({"v": 1, "action": "batch", "params": {"ops": [
            {"op": "set_limit", "iface": "wlan0", "down": 5}]}})
        user = mock.Mock(uid=1000)
        with mock.patch.object(server.engine, "set_limit", return_value={"down": 5.0}) as limit:
            results = server._run_batch(params["ops"], user)
        limit.assert_called_once_with("wlan0", 5.0, None)
        self.assertTrue(results[0]["ok"])
        with self.assertRaises(protocol.ValidationError):
            protocol.validate_request({"v": 1, "action": "batch", "params": {"ops": [
                {"op": "set_limit", "iface": "wlan0", "down": -1}]}})
        with self.assertRaises(protocol.ValidationError):
            protocol.validate_request({"v": 1, "action": "batch", "params": {"ops": [
                {"op": "reset"}]}})


if __name__ == "__main__":
    unittest.main()
