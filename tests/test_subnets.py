from __future__ import annotations

import ipaddress
import unittest

from intermux.errors import InterMuxError
from intermux.subnets import (
    host_ip,
    index_of,
    ns_ip,
    pick_subnet,
    subnet_for_index,
    used_networks,
)

Net = ipaddress.IPv4Network


class SubnetForIndexTests(unittest.TestCase):
    def test_known_values(self):
        self.assertEqual(subnet_for_index(1), Net("10.200.1.0/24"))
        self.assertEqual(subnet_for_index(254), Net("10.200.254.0/24"))

    def test_out_of_range_rejected(self):
        for bad in (0, -1, 255, 1000):
            with self.subTest(n=bad), self.assertRaises(ValueError):
                subnet_for_index(bad)

    def test_bool_and_non_int_rejected(self):
        with self.assertRaises(ValueError):
            subnet_for_index(True)
        with self.assertRaises(ValueError):
            subnet_for_index("3")  # type: ignore[arg-type]


class IndexOfTests(unittest.TestCase):
    def test_round_trip(self):
        for n in range(1, 255):
            with self.subTest(n=n):
                self.assertEqual(index_of(subnet_for_index(n)), n)

    def test_outside_pool_rejected(self):
        for cidr in ("10.0.1.0/24", "192.168.1.0/24", "10.201.1.0/24"):
            with self.subTest(cidr=cidr), self.assertRaises(ValueError):
                index_of(Net(cidr))

    def test_wrong_prefix_rejected(self):
        with self.assertRaises(ValueError):
            index_of(Net("10.200.0.0/16"))
        with self.assertRaises(ValueError):
            index_of(Net("10.200.5.0/25"))

    def test_ipv6_rejected(self):
        with self.assertRaises(ValueError):
            index_of(ipaddress.IPv6Network("fd00::/64"))  # type: ignore[arg-type]

    def test_index_zero_rejected(self):
        with self.assertRaises(ValueError):
            index_of(Net("10.200.0.0/24"))


class HostNsIpTests(unittest.TestCase):
    def test_host_and_ns_addresses(self):
        net = Net("10.200.7.0/24")
        self.assertEqual(host_ip(net), ipaddress.IPv4Address("10.200.7.1"))
        self.assertEqual(ns_ip(net), ipaddress.IPv4Address("10.200.7.2"))


class UsedNetworksTests(unittest.TestCase):
    def test_parses_valid_and_skips_invalid(self):
        result = used_networks([
            "10.0.0.0/8",
            "192.168.1.5/24",
            "172.16.0.9",
            "fe80::/64",
            "not an address",
            "10.1.2.3/33",
            "",
            "  10.200.3.0/24  ",
        ])
        self.assertEqual(result, [
            Net("10.0.0.0/8"),
            Net("192.168.1.0/24"),
            Net("172.16.0.9/32"),
            Net("10.200.3.0/24"),
        ])

    def test_empty_input(self):
        self.assertEqual(used_networks([]), [])


class PickSubnetTests(unittest.TestCase):
    def test_empty_used_gives_first(self):
        self.assertEqual(pick_subnet([]), Net("10.200.1.0/24"))

    def test_skips_taken(self):
        self.assertEqual(pick_subnet([Net("10.200.1.0/24")]), Net("10.200.2.0/24"))

    def test_overlap_counts_as_taken(self):
        self.assertEqual(pick_subnet([Net("10.200.1.0/25")]), Net("10.200.2.0/24"))
        self.assertEqual(pick_subnet([Net("10.200.5.0/24"), Net("10.201.0.0/16")]), Net("10.200.1.0/24"))

    def test_preferred_used_when_free(self):
        self.assertEqual(pick_subnet([Net("10.200.1.0/24")], preferred=Net("10.200.9.0/24")),
                         Net("10.200.9.0/24"))

    def test_preferred_ignored_when_taken(self):
        self.assertEqual(pick_subnet([Net("10.200.9.0/24")], preferred=Net("10.200.9.0/24")),
                         Net("10.200.1.0/24"))

    def test_preferred_ignored_when_outside_pool(self):
        self.assertEqual(pick_subnet([], preferred=Net("192.168.5.0/24")), Net("10.200.1.0/24"))

    def test_all_taken_raises_no_subnet(self):
        used = [subnet_for_index(n) for n in range(1, 255)]
        with self.assertRaises(InterMuxError) as ctx:
            pick_subnet(used)
        self.assertEqual(ctx.exception.code, "no_subnet")

    def test_supernet_covering_pool_exhausts_it(self):
        with self.assertRaises(InterMuxError) as ctx:
            pick_subnet([Net("10.200.0.0/16")])
        self.assertEqual(ctx.exception.code, "no_subnet")

    def test_accepts_generator(self):
        used = (n for n in [Net("10.200.1.0/24")])
        self.assertEqual(pick_subnet(used), Net("10.200.2.0/24"))


if __name__ == "__main__":
    unittest.main()
