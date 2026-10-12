"""Tests for intermux.system.netinfo: pure ip-JSON parser, sysfs classification, serialization."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

from intermux.system import netinfo
from intermux.system.netinfo import Address, Interface, classify_kind, is_virtual_name, parse_ip_json

# `ip -j link show`: lo (skipped), wlan0 (up), enp3s0 (up, no default route),
# enp0s20u1 (USB, down), tun0 (point-to-point, virtual), docker0 (UP flag but DOWN operstate)
LINK_JSON = """
[
  {"ifindex": 1, "ifname": "lo", "flags": ["LOOPBACK", "UP", "LOWER_UP"], "mtu": 65536,
   "operstate": "UNKNOWN", "address": "00:00:00:00:00:00"},
  {"ifindex": 2, "ifname": "wlan0", "flags": ["BROADCAST", "MULTICAST", "UP", "LOWER_UP"], "mtu": 1500,
   "operstate": "UP", "address": "a4:c3:f0:11:22:33"},
  {"ifindex": 3, "ifname": "enp3s0", "flags": ["BROADCAST", "MULTICAST", "UP", "LOWER_UP"], "mtu": 1500,
   "operstate": "UP", "address": "52:54:00:12:34:56"},
  {"ifindex": 4, "ifname": "enp0s20u1", "flags": ["BROADCAST", "MULTICAST"], "mtu": 1500,
   "operstate": "DOWN", "address": "00:e0:4c:68:01:02"},
  {"ifindex": 5, "ifname": "tun0", "flags": ["POINTOPOINT", "MULTICAST", "NOARP", "UP", "LOWER_UP"],
   "mtu": 1400, "operstate": "UNKNOWN", "link_type": "none"},
  {"ifindex": 6, "ifname": "docker0", "flags": ["NO-CARRIER", "BROADCAST", "MULTICAST", "UP"], "mtu": 1500,
   "operstate": "DOWN", "address": "02:42:ac:11:00:01"}
]
"""

# `ip -j addr show`
ADDR_JSON = """
[
  {"ifindex": 1, "ifname": "lo", "addr_info": [
    {"family": "inet", "local": "127.0.0.1", "prefixlen": 8, "scope": "host", "label": "lo"}
  ]},
  {"ifindex": 2, "ifname": "wlan0", "addr_info": [
    {"family": "inet", "local": "192.168.1.23", "prefixlen": 24, "broadcast": "192.168.1.255",
     "scope": "global", "dynamic": true, "label": "wlan0"},
    {"family": "inet6", "local": "fe80::1234", "prefixlen": 64, "scope": "link"}
  ]},
  {"ifindex": 3, "ifname": "enp3s0", "addr_info": [
    {"family": "inet", "local": "10.0.0.5", "prefixlen": 24, "scope": "global"}
  ]},
  {"ifindex": 5, "ifname": "tun0", "addr_info": [
    {"family": "inet", "local": "10.42.0.15", "prefixlen": 32, "address": "10.42.0.1", "scope": "global"}
  ]},
  {"ifindex": 6, "ifname": "docker0", "addr_info": [
    {"family": "inet", "local": "172.17.0.1", "prefixlen": 16, "scope": "global"}
  ]}
]
"""

# `ip -j route show table main`. The point-to-point default has no "gateway" key.
ROUTE_JSON = """
[
  {"dst": "default", "gateway": "192.168.1.1", "dev": "wlan0", "protocol": "dhcp", "metric": 600, "flags": []},
  {"dst": "192.168.1.0/24", "dev": "wlan0", "protocol": "kernel", "scope": "link",
   "prefsrc": "192.168.1.23", "metric": 600, "flags": []},
  {"dst": "default", "dev": "tun0", "scope": "link"},
  {"dst": "10.0.0.0/24", "dev": "enp3s0", "protocol": "kernel", "scope": "link", "prefsrc": "10.0.0.5"},
  {"dst": "172.17.0.0/16", "dev": "docker0", "protocol": "kernel", "scope": "link", "prefsrc": "172.17.0.1"}
]
"""


def _by_name(interfaces: List[Interface]) -> Dict[str, Interface]:
    return {i.name: i for i in interfaces}


class _SysfsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.sys_net = self.root / "sys" / "class" / "net"
        self.sys_net.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _iface_dir(self, name: str) -> Path:
        path = self.sys_net / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _device_under(self, name: str, target_rel: str) -> None:
        target = self.root / "devices" / target_rel
        target.mkdir(parents=True, exist_ok=True)
        os.symlink(str(target), str(self._iface_dir(name) / "device"))


class ParseIpJsonTests(_SysfsTestCase):
    def setUp(self) -> None:
        super().setUp()
        # sysfs layout: wlan0 is wireless, enp3s0 is PCI, enp0s20u1 hangs off a USB bus
        self._iface_dir("wlan0").joinpath("wireless").mkdir()
        self._device_under("enp3s0", "pci0000:00/0000:00:1f.6")
        self._device_under("enp0s20u1", "pci0000:00/0000:00:14.0/usb1/1-1")
        self.ifaces = parse_ip_json(json.loads(LINK_JSON), json.loads(ADDR_JSON), json.loads(ROUTE_JSON),
                                    sys_class_net=self.sys_net)
        self.by_name = _by_name(self.ifaces)

    def test_lo_skipped_and_link_order_kept(self) -> None:
        self.assertEqual([i.name for i in self.ifaces],
                         ["wlan0", "enp3s0", "enp0s20u1", "tun0", "docker0"])

    def test_kinds(self) -> None:
        kinds = {name: i.kind for name, i in self.by_name.items()}
        self.assertEqual(kinds, {
            "wlan0": "wifi",
            "enp3s0": "ethernet",
            "enp0s20u1": "usb",
            "tun0": "virtual",
            "docker0": "virtual",
        })

    def test_up_state(self) -> None:
        self.assertTrue(self.by_name["wlan0"].up)
        self.assertTrue(self.by_name["enp3s0"].up)
        self.assertFalse(self.by_name["enp0s20u1"].up)
        self.assertTrue(self.by_name["tun0"].up)
        # UP flag present but operstate DOWN means not up
        self.assertFalse(self.by_name["docker0"].up)
        self.assertEqual(self.by_name["docker0"].operstate, "DOWN")

    def test_flags_mac_mtu(self) -> None:
        wlan = self.by_name["wlan0"]
        self.assertIn("UP", wlan.flags)
        self.assertEqual(wlan.mac, "a4:c3:f0:11:22:33")
        self.assertEqual(wlan.mtu, 1500)
        self.assertIsNone(self.by_name["tun0"].mac)
        self.assertEqual(self.by_name["tun0"].mtu, 1400)

    def test_addresses_include_inet6_but_ipv4_cidrs_do_not(self) -> None:
        wlan = self.by_name["wlan0"]
        self.assertEqual(wlan.addresses, [
            Address(family="inet", local="192.168.1.23", prefixlen=24, peer=None, scope="global"),
            Address(family="inet6", local="fe80::1234", prefixlen=64, peer=None, scope="link"),
        ])
        self.assertEqual(wlan.ipv4_cidrs, ["192.168.1.23/24"])

    def test_peer_address_from_address_key(self) -> None:
        self.assertEqual(self.by_name["tun0"].addresses, [
            Address(family="inet", local="10.42.0.15", prefixlen=32, peer="10.42.0.1", scope="global"),
        ])
        self.assertEqual(self.by_name["tun0"].ipv4_cidrs, ["10.42.0.15/32"])

    def test_p2p_default_route_without_gateway(self) -> None:
        tun = self.by_name["tun0"]
        self.assertTrue(tun.default_route)
        self.assertEqual(tun.gateways, [])
        self.assertFalse(tun.bindable)

    def test_default_route_and_gateways(self) -> None:
        wlan = self.by_name["wlan0"]
        self.assertTrue(wlan.default_route)
        self.assertEqual(wlan.gateways, ["192.168.1.1"])
        enp = self.by_name["enp3s0"]
        self.assertFalse(enp.default_route)
        self.assertEqual(enp.gateways, [])

    def test_bindable(self) -> None:
        self.assertTrue(self.by_name["wlan0"].bindable)
        # up with an address but no default route
        self.assertFalse(self.by_name["enp3s0"].bindable)
        # down, no address
        self.assertFalse(self.by_name["enp0s20u1"].bindable)
        # virtual names and kinds are never bindable
        self.assertFalse(self.by_name["tun0"].bindable)
        self.assertFalse(self.by_name["docker0"].bindable)

    def test_to_dict_is_json_safe(self) -> None:
        data = self.by_name["wlan0"].to_dict()
        encoded = json.loads(json.dumps(data))
        self.assertEqual(encoded["name"], "wlan0")
        self.assertEqual(encoded["kind"], "wifi")
        self.assertEqual(encoded["addresses"][0], {
            "family": "inet", "local": "192.168.1.23", "prefixlen": 24, "peer": None, "scope": "global",
        })
        self.assertEqual(encoded["gateways"], ["192.168.1.1"])


class ParseIpJsonEdgeCaseTests(_SysfsTestCase):
    def test_missing_keys_are_tolerated(self) -> None:
        links = [{"ifname": "eth9"}, {"flags": ["UP"]}, "junk", 42]
        addrs = [{"ifname": "eth9"}]
        routes = [{"dev": "eth9"}]
        ifaces = parse_ip_json(links, addrs, routes, sys_class_net=self.sys_net)
        self.assertEqual(len(ifaces), 1)
        eth = ifaces[0]
        self.assertEqual(eth.name, "eth9")
        self.assertEqual(eth.kind, "ethernet")
        self.assertFalse(eth.up)
        self.assertEqual(eth.operstate, "UNKNOWN")
        self.assertEqual(eth.flags, [])
        self.assertIsNone(eth.mac)
        self.assertIsNone(eth.mtu)
        self.assertEqual(eth.addresses, [])
        self.assertFalse(eth.default_route)
        self.assertEqual(eth.gateways, [])

    def test_lowerlayerdown_is_not_up(self) -> None:
        links = [{"ifname": "wlan0", "flags": ["UP", "LOWER_UP"], "operstate": "LOWERLAYERDOWN"}]
        self.assertFalse(parse_ip_json(links, [], [], sys_class_net=self.sys_net)[0].up)

    def test_gateways_deduplicated_and_ipv4_only(self) -> None:
        links = [{"ifname": "wlan0", "flags": ["UP"], "operstate": "UP"}]
        routes = [
            {"dst": "default", "gateway": "192.168.1.1", "dev": "wlan0", "metric": 600},
            {"dst": "default", "gateway": "192.168.1.1", "dev": "wlan0", "metric": 700},
            {"dst": "default", "gateway": "fe80::1", "dev": "wlan0", "metric": 650},
            {"dst": "default", "gateway": "192.168.1.254", "dev": "wlan0", "metric": 800},
            {"dst": "default", "dev": "wlan0", "metric": 900},
        ]
        wlan = parse_ip_json(links, [], routes, sys_class_net=self.sys_net)[0]
        self.assertTrue(wlan.default_route)
        self.assertEqual(wlan.gateways, ["192.168.1.1", "192.168.1.254"])

    def test_zero_slash_zero_counts_as_default(self) -> None:
        links = [{"ifname": "wlan0", "flags": ["UP"], "operstate": "UP"}]
        routes = [{"dst": "0.0.0.0/0", "gateway": "10.1.1.1", "dev": "wlan0"}]
        wlan = parse_ip_json(links, [], routes, sys_class_net=self.sys_net)[0]
        self.assertTrue(wlan.default_route)
        self.assertEqual(wlan.gateways, ["10.1.1.1"])

    def test_addr_info_filtering_and_defaults(self) -> None:
        links = [{"ifname": "eth0", "flags": ["UP"], "operstate": "UP"}]
        addrs = [{"ifname": "eth0", "addr_info": [
            {"family": "link", "local": "00:11:22:33:44:55"},
            {"family": "inet", "prefixlen": 24},
            {"family": "inet", "local": "10.1.1.1", "prefixlen": "8"},
        ]}]
        eth = parse_ip_json(links, addrs, [], sys_class_net=self.sys_net)[0]
        self.assertEqual(eth.addresses, [Address(family="inet", local="10.1.1.1", prefixlen=8, peer=None, scope="")])

    def test_is_virtual_property_uses_kind_too(self) -> None:
        iface = Interface(name="wlan9", kind="virtual", up=True, operstate="UP", flags=[], mac=None,
                          mtu=None, addresses=[], default_route=True, gateways=[])
        self.assertTrue(iface.is_virtual)
        self.assertFalse(iface.bindable)


class IsVirtualNameTests(unittest.TestCase):
    def test_virtual_prefixes(self) -> None:
        for name in ("lo", "veth0_1a2b3c4d", "br-1f2e3d4c", "docker0", "virbr0", "vmnet8", "vboxnet0", "tun0", "tap0"):
            with self.subTest(name=name):
                self.assertTrue(is_virtual_name(name))

    def test_physical_names(self) -> None:
        for name in ("eth0", "wlan0", "enp3s0", "wlp2s0", "wwan0", "enx00e04c680102", "bnep0"):
            with self.subTest(name=name):
                self.assertFalse(is_virtual_name(name))


class ClassifyKindTests(_SysfsTestCase):
    def test_wifi_by_wireless_dir(self) -> None:
        (self._iface_dir("wlp2s0") / "wireless").mkdir()
        self.assertEqual(classify_kind("wlp2s0", self.sys_net), "wifi")

    def test_wifi_by_phy80211_link(self) -> None:
        phy = self.root / "phy0"
        phy.mkdir()
        os.symlink(str(phy), str(self._iface_dir("wlan1") / "phy80211"))
        self.assertEqual(classify_kind("wlan1", self.sys_net), "wifi")

    def test_wifi_wins_over_usb(self) -> None:
        self._device_under("wlx001122334455", "pci0000:00/0000:00:14.0/usb1/1-2")
        (self._iface_dir("wlx001122334455") / "wireless").mkdir()
        self.assertEqual(classify_kind("wlx001122334455", self.sys_net), "wifi")

    def test_mobile_by_name(self) -> None:
        self._iface_dir("wwan0")
        self.assertEqual(classify_kind("wwan0", self.sys_net), "mobile")

    def test_bluetooth_by_name(self) -> None:
        self._iface_dir("bnep0")
        self.assertEqual(classify_kind("bnep0", self.sys_net), "bluetooth")

    def test_usb_by_device_realpath(self) -> None:
        self._device_under("enx00112233aabb", "pci0000:00/0000:00:14.0/usb1/1-1")
        self.assertEqual(classify_kind("enx00112233aabb", self.sys_net), "usb")

    def test_ethernet_by_prefix(self) -> None:
        self._device_under("enp3s0", "pci0000:00/0000:00:1f.6")
        self._iface_dir("eth0")
        self.assertEqual(classify_kind("enp3s0", self.sys_net), "ethernet")
        self.assertEqual(classify_kind("eth0", self.sys_net), "ethernet")

    def test_ethernet_without_sysfs_entry(self) -> None:
        self.assertEqual(classify_kind("eno1", self.sys_net), "ethernet")

    def test_virtual_wins_over_wifi_and_usb(self) -> None:
        (self._iface_dir("docker0") / "wireless").mkdir()
        self._device_under("tun0", "pci0000:00/0000:00:14.0/usb1/1-3")
        self.assertEqual(classify_kind("docker0", self.sys_net), "virtual")
        self.assertEqual(classify_kind("tun0", self.sys_net), "virtual")
        self.assertEqual(classify_kind("veth1a2b", self.sys_net), "virtual")

    def test_other(self) -> None:
        self._iface_dir("ib0")
        self.assertEqual(classify_kind("ib0", self.sys_net), "other")
        self.assertEqual(classify_kind("xyz0", self.sys_net), "other")


class ListInterfacesTests(unittest.TestCase):
    def _fake_json_cmd(self, calls: List[List[str]]):
        def fake(argv: List[str], **kwargs: Any) -> Any:
            calls.append(list(argv))
            if "link" in argv:
                return [{"ifname": "eth9", "flags": ["UP"], "operstate": "UP"}]
            if "addr" in argv:
                return []
            return []
        return fake

    def test_returns_empty_when_ip_missing(self) -> None:
        with mock.patch.object(netinfo.commands, "which", return_value=None), \
                mock.patch.object(netinfo.commands, "json_cmd") as json_cmd:
            self.assertEqual(netinfo.list_interfaces(), [])
            json_cmd.assert_not_called()

    def test_runs_three_ip_json_commands(self) -> None:
        calls: List[List[str]] = []
        with mock.patch.object(netinfo.commands, "which", return_value="/usr/sbin/ip"), \
                mock.patch.object(netinfo.commands, "json_cmd", side_effect=self._fake_json_cmd(calls)):
            ifaces = netinfo.list_interfaces()
        self.assertEqual([i.name for i in ifaces], ["eth9"])
        self.assertEqual([argv[1:] for argv in calls], [
            ["-j", "link", "show"],
            ["-j", "addr", "show"],
            ["-j", "route", "show", "table", "main"],
        ])
        self.assertEqual(calls[0][0], "/usr/sbin/ip")


if __name__ == "__main__":
    unittest.main()
