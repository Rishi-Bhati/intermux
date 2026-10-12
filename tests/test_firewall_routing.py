"""Pure-function tests for routing and firewall helpers (no system commands are run)."""
from __future__ import annotations

import unittest
from ipaddress import IPv4Network
from unittest import mock

from intermux.system import firewall, routing


def _rule(src=None, priority=None, table=None, srclen=None, **extra):
    rule = {}
    if src is not None:
        rule["src"] = src
    if srclen is not None:
        rule["srclen"] = srclen
    if priority is not None:
        rule["priority"] = priority
    if table is not None:
        rule["table"] = table
    rule.update(extra)
    return rule


class RoutingRuleParsingTests(unittest.TestCase):
    def test_src_with_prefix_string(self):
        self.assertEqual(routing.rule_src_network(_rule(src="10.200.7.0/24")),
                         IPv4Network("10.200.7.0/24"))

    def test_src_with_separate_srclen(self):
        self.assertEqual(routing.rule_src_network(_rule(src="10.200.7.0", srclen=24)),
                         IPv4Network("10.200.7.0/24"))

    def test_src_all_or_missing_is_none(self):
        self.assertIsNone(routing.rule_src_network(_rule(src="all")))
        self.assertIsNone(routing.rule_src_network({"priority": 5}))

    def test_src_ipv6_is_none(self):
        self.assertIsNone(routing.rule_src_network(_rule(src="fd00::/64")))

    def test_priority_and_table_accept_strings_and_ints(self):
        rule = _rule(priority="1007", table="1007")
        self.assertEqual(routing.rule_priority(rule), 1007)
        self.assertEqual(routing.rule_table(rule), 1007)
        self.assertEqual(routing.rule_table(_rule(table="main")), None)

    def test_rule_matches_all_three_fields(self):
        rule = _rule(src="10.200.3.0/24", priority=1003, table=1003)
        self.assertTrue(routing.rule_matches(rule, IPv4Network("10.200.3.0/24"), 1003, 1003))
        self.assertTrue(routing.rule_matches(rule, "10.200.3.0/24", 1003, 1003))
        self.assertFalse(routing.rule_matches(rule, IPv4Network("10.200.4.0/24"), 1003, 1003))
        self.assertFalse(routing.rule_matches(rule, IPv4Network("10.200.3.0/24"), 1004, 1003))
        self.assertFalse(routing.rule_matches(rule, IPv4Network("10.200.3.0/24"), 1003, 1004))


class IntermuxRuleFilterTests(unittest.TestCase):
    def test_band_edges(self):
        # PRIORITY_BASE is 1000; intermux owns 1001..1254
        self.assertFalse(routing.is_intermux_rule(_rule(src="10.200.1.0/24", priority=1000, table=1001)))
        self.assertTrue(routing.is_intermux_rule(_rule(src="10.200.1.0/24", priority=1001, table=1001)))
        self.assertTrue(routing.is_intermux_rule(_rule(src="10.200.254.0/24", priority=1254, table=1254)))
        self.assertFalse(routing.is_intermux_rule(_rule(src="10.200.254.0/24", priority=1255, table=1255)))

    def test_pool_membership(self):
        self.assertTrue(routing.is_intermux_rule(_rule(src="10.200.9.0/24", priority=1009, table=1009)))
        self.assertFalse(routing.is_intermux_rule(_rule(src="10.201.9.0/24", priority=1009)))
        self.assertFalse(routing.is_intermux_rule(_rule(src="192.168.1.0/24", priority=1009)))
        self.assertFalse(routing.is_intermux_rule(_rule(src="all", priority=1009)))

    def test_foreign_rule_in_band_is_not_selected(self):
        self.assertFalse(routing.is_intermux_rule(_rule(src="10.200.5.0/24", priority=1005, table=3000)))
        self.assertFalse(routing.is_intermux_rule(_rule(src="10.200.5.0/24", priority=1006, table=1005)))
        self.assertFalse(routing.is_intermux_rule(_rule(src="10.200.5.0/24", priority=1005, table="main")))
        self.assertFalse(routing.is_intermux_rule(_rule(src="10.200.5.7/32", priority=1005, table=1005)))
        self.assertFalse(routing.is_intermux_rule(_rule(src="10.200.5.0/25", priority=1005, table=1005)))
        self.assertEqual(routing.select_intermux_rules([_rule(src="10.200.5.0/24", priority=1005, table=3000)]), [])

    def test_select_skips_non_dicts_and_foreign_rules(self):
        rules = [
            {"priority": 0, "src": "all", "table": "local"},
            {"priority": 32766, "src": "all", "table": "main"},
            {"priority": 1005, "src": "10.200.5.0", "srclen": 24, "table": "1005"},
            {"priority": 1050, "src": "172.16.0.0/16", "table": "1050"},
            "not a rule",
        ]
        selected = routing.select_intermux_rules(rules)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["table"], "1005")


class FirewallRuleTests(unittest.TestCase):
    subnet = IPv4Network("10.200.7.0/24")

    def test_nat_rules_shape_and_order(self):
        rules = firewall.nat_rules(self.subnet, "wlan0")
        self.assertEqual(len(rules), 3)
        self.assertEqual(rules[0].table, "nat")
        self.assertEqual(rules[0].chain, "POSTROUTING")
        self.assertEqual(rules[0].spec, ("-s", "10.200.7.0/24", "-o", "wlan0", "-j", "MASQUERADE"))
        self.assertFalse(rules[0].insert)
        self.assertEqual(rules[1].spec, ("-s", "10.200.7.0/24", "-j", "ACCEPT"))
        self.assertTrue(rules[1].insert)
        self.assertEqual(rules[2].spec, ("-d", "10.200.7.0/24", "-m", "conntrack",
                                         "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"))
        self.assertTrue(rules[2].insert)

    def test_rule_args_for_check_and_insert(self):
        nat, fwd, _ = firewall.nat_rules(self.subnet, "eth0")
        self.assertEqual(firewall.rule_args(nat, "-C"),
                         ["-t", "nat", "-C", "POSTROUTING", "-s", "10.200.7.0/24",
                          "-o", "eth0", "-j", "MASQUERADE"])
        self.assertEqual(firewall.rule_args(fwd, "-I"),
                         ["-I", "FORWARD", "-s", "10.200.7.0/24", "-j", "ACCEPT"])

    def test_delete_args_from_nat_line(self):
        self.assertEqual(
            firewall.delete_rule_args("-A POSTROUTING -s 10.200.1.0/24 -o wlan0 -j MASQUERADE"),
            ["-t", "nat", "-D", "POSTROUTING", "-s", "10.200.1.0/24", "-o", "wlan0", "-j", "MASQUERADE"],
        )

    def test_delete_args_from_filter_line(self):
        self.assertEqual(
            firewall.delete_rule_args("-A FORWARD -d 10.200.1.0/24 -m conntrack "
                                      "--ctstate RELATED,ESTABLISHED -j ACCEPT"),
            ["-D", "FORWARD", "-d", "10.200.1.0/24", "-m", "conntrack",
             "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
        )

    def test_delete_args_reject_non_append_lines(self):
        self.assertIsNone(firewall.delete_rule_args("-P FORWARD DROP"))
        self.assertIsNone(firewall.delete_rule_args("-N INTERMUX"))
        self.assertIsNone(firewall.delete_rule_args(""))
        self.assertIsNone(firewall.delete_rule_args('-A FORWARD -s "unterminated'))

    def test_filter_intermux_lines_keeps_only_pool_rules(self):
        text = (
            "-P POSTROUTING ACCEPT\n"
            "-A POSTROUTING -s 10.200.1.0/24 -o wlan0 -j MASQUERADE\n"
            "-A POSTROUTING -s 192.168.0.0/24 -o wlan0 -j MASQUERADE\n"
            "\n"
            "  -A POSTROUTING -s 10.200.2.0/24 -o eth0 -j MASQUERADE  \n"
        )
        self.assertEqual(
            firewall.filter_intermux_lines(text),
            ["-A POSTROUTING -s 10.200.1.0/24 -o wlan0 -j MASQUERADE",
             "-A POSTROUTING -s 10.200.2.0/24 -o eth0 -j MASQUERADE"],
        )

    def test_foreign_network_containing_pool_text_is_not_selected(self):
        self.assertEqual(
            firewall.filter_intermux_lines("-A POSTROUTING -s 110.200.1.0/24 -o wlan0 -j MASQUERADE"), [])

    def test_user_network_inside_pool_is_selected(self):
        self.assertEqual(
            firewall.filter_intermux_lines("-A FORWARD -s 10.200.3.0/24 -j ACCEPT"),
            ["-A FORWARD -s 10.200.3.0/24 -j ACCEPT"])

    def test_host_address_in_pool_is_not_selected(self):
        self.assertEqual(firewall.filter_intermux_lines("-A FORWARD -s 10.200.3.7/32 -j ACCEPT"), [])
        self.assertEqual(firewall.filter_intermux_lines("-A FORWARD -d 10.200.3.7/24 -j ACCEPT"), [])

    def test_invalid_octet_is_not_selected(self):
        self.assertEqual(
            firewall.filter_intermux_lines("-A FORWARD -s 10.200.300.0/24 -j ACCEPT"), [])

    def test_destination_selector_and_negation(self):
        self.assertEqual(
            firewall.filter_intermux_lines("-A FORWARD -d 10.200.9.0/24 -j ACCEPT"),
            ["-A FORWARD -d 10.200.9.0/24 -j ACCEPT"])
        self.assertEqual(
            firewall.filter_intermux_lines("-A FORWARD ! -s 10.200.9.0/24 -j DROP"), [])

    def test_pool_edges(self):
        self.assertTrue(firewall.is_pool_subnet("10.200.1.0/24"))
        self.assertTrue(firewall.is_pool_subnet("10.200.254.0/24"))
        self.assertFalse(firewall.is_pool_subnet("10.200.0.0/24"))
        self.assertFalse(firewall.is_pool_subnet("10.200.255.0/24"))
        self.assertFalse(firewall.is_pool_subnet("10.200.0.0/16"))
        self.assertFalse(firewall.is_pool_subnet("10.200.3.0/25"))
        self.assertFalse(firewall.is_pool_subnet("not-an-address"))


class EnsurePolicyTests(unittest.TestCase):
    """ensure_policy builds the command list; the system commands are mocked out."""

    subnet = IPv4Network("10.200.7.0/24")

    def test_interface_route_and_unreachable_fallback(self):
        with mock.patch.object(routing, "run_checked") as run_checked, \
                mock.patch.object(routing, "json_cmd", return_value=[]), \
                mock.patch.object(routing, "list_rules", return_value=[]):
            routing.ensure_policy(self.subnet, "wlan0", 1007, 1007, "192.168.1.1", "veth0_0a0b0c0d")
        self.assertEqual([c.args[0] for c in run_checked.call_args_list], [
            [routing.IP, "route", "replace", "10.200.7.0/24", "dev", "veth0_0a0b0c0d", "scope", "link",
             "src", "10.200.7.1", "table", "1007"],
            [routing.IP, "route", "replace", "default", "via", "192.168.1.1",
             "dev", "wlan0", "table", "1007", "metric", "100"],
            [routing.IP, "route", "replace", "unreachable", "default",
             "metric", "1000", "table", "1007"],
            [routing.IP, "rule", "add", "from", "10.200.7.0/24",
             "lookup", "1007", "priority", "1007"],
        ])

    def test_existing_rule_is_not_added_again(self):
        existing = [{"src": "10.200.7.0/24", "priority": 1007, "table": 1007}]
        with mock.patch.object(routing, "run_checked") as run_checked, \
                mock.patch.object(routing, "json_cmd", return_value=[]), \
                mock.patch.object(routing, "list_rules", return_value=existing):
            routing.ensure_policy(self.subnet, "wlan0", 1007, 1007, None)
        commands = [c.args[0] for c in run_checked.call_args_list]
        self.assertEqual(len(commands), 2)
        self.assertNotIn("rule", commands[0])
        self.assertEqual(commands[1][:3], [routing.IP, "route", "replace"])


if __name__ == "__main__":
    unittest.main()


class PolicyTableTests(unittest.TestCase):
    """policy_table_ok / stray_routes read `ip -j route show table T` output."""

    subnet = IPv4Network("10.200.1.0/24")
    good = [{"dst": "default", "gateway": "10.64.128.159", "dev": "wlan0", "metric": 100, "flags": []},
            {"type": "unreachable", "dst": "default", "metric": 1000, "flags": []},
            {"dst": "10.200.1.0/24", "dev": "veth0_0a0b0c0d", "scope": "link", "prefsrc": "10.200.1.1", "flags": []}]

    def ok(self, routes, gw="10.64.128.159", iface="wlan0"):
        return routing.policy_table_ok(routes, self.subnet, iface, gw, "veth0_0a0b0c0d")

    def test_complete_table(self):
        self.assertTrue(self.ok(self.good))
        self.assertEqual(routing.stray_routes(self.good, self.subnet, "wlan0", "10.64.128.159", "veth0_0a0b0c0d"), [])

    def test_only_the_unreachable_fallback_left_after_a_flap(self):
        self.assertFalse(self.ok(self.good[1:]))

    def test_subnet_route_missing(self):
        self.assertFalse(self.ok(self.good[:2]))

    def test_gateway_changed_or_other_device(self):
        self.assertFalse(self.ok(self.good, gw="10.64.128.1"))
        self.assertFalse(self.ok(self.good, iface="usb0"))

    def test_stale_lower_metric_default_is_stray(self):
        stale = {"dst": "default", "gateway": "10.231.212.182", "dev": "enp7s0f4u1", "flags": []}
        routes = [stale] + self.good
        self.assertFalse(self.ok(routes))
        strays = routing.stray_routes(routes, self.subnet, "wlan0", "10.64.128.159", "veth0_0a0b0c0d")
        self.assertEqual(strays, [stale])
        self.assertEqual(routing.route_del_args(stale, 1001),
                         [routing.IP, "route", "del", "default", "via", "10.231.212.182", "dev", "enp7s0f4u1",
                          "table", "1001"])
        self.assertEqual(routing.route_del_args({"type": "unreachable", "dst": "default", "metric": 5}, 1001),
                         [routing.IP, "route", "del", "unreachable", "default", "metric", "5", "table", "1001"])

    def test_device_route_without_gateway(self):
        routes = [{"dst": "default", "dev": "ppp0", "metric": 100}, self.good[1], self.good[2]]
        self.assertTrue(self.ok(routes, gw=None, iface="ppp0"))

    def test_ensure_policy_deletes_strays_after_adding(self):
        stale = {"dst": "default", "gateway": "10.231.212.182", "dev": "enp7s0f4u1", "flags": []}
        order = []
        with mock.patch.object(routing, "run_checked", side_effect=lambda a, **k: order.append(("add", a))), \
                mock.patch.object(routing, "run", side_effect=lambda a, **k: order.append(("del", a))), \
                mock.patch.object(routing, "json_cmd", return_value=[stale] + self.good), \
                mock.patch.object(routing, "list_rules", return_value=[]):
            routing.ensure_policy(self.subnet, "wlan0", 1001, 1001, "10.64.128.159", "veth0_0a0b0c0d")
        kinds = [k for k, _ in order]
        self.assertEqual(kinds, ["add", "add", "add", "del", "add"])
        self.assertEqual(order[3][1][:4], [routing.IP, "route", "del", "default"])
