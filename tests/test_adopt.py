from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from intermux.errors import InterMuxError, ValidationError
from intermux.system import adopt

ID = "0123456789abcdef"


class SlotTests(unittest.TestCase):
    def test_group_rel(self):
        self.assertEqual(adopt.group_rel(ID), f"intermux/adopt-{ID}")

    def test_group_rel_invalid(self):
        for bad in ("", "short", "ABCDEF0123", "../../etc/pass", "g" * 10, "a" * 33):
            with self.assertRaises(ValidationError):
                adopt.group_rel(bad)

    def test_arithmetic(self):
        self.assertEqual(adopt.slot_mark(3), 0x103)
        self.assertEqual(adopt.slot_table(3), 2003)
        self.assertEqual(adopt.slot_priority(3), 2003)
        self.assertEqual(adopt.nft_table_name(3), "intermux_adopt3")

    def test_pick_slot(self):
        self.assertEqual(adopt.pick_slot([]), 1)
        self.assertEqual(adopt.pick_slot([1, 2, 4]), 3)

    def test_pick_slot_full(self):
        with self.assertRaises(InterMuxError) as ctx:
            adopt.pick_slot(range(1, 201))
        self.assertEqual(ctx.exception.code, "no_slot")


class NftScriptTests(unittest.TestCase):
    def test_contents(self):
        script = adopt.nft_script(5, adopt.group_rel(ID), "wlan0")
        self.assertIn("table inet intermux_adopt5", script)
        self.assertIn("0x105", script)
        self.assertIn(f'socket cgroupv2 level 2 "intermux/adopt-{ID}"', script)
        self.assertIn('oifname "wlan0" masquerade', script)

    def test_unsafe_iface(self):
        for bad in ('eth0"; flush ruleset', "a b", "", "x" * 16):
            with self.assertRaises(ValidationError):
                adopt.nft_script(1, adopt.group_rel(ID), bad)

    def _output_rules(self, script):
        chain = script.split("chain output {", 1)[1].split("\n  }", 1)[0]
        return [line.strip() for line in chain.splitlines() if "socket cgroupv2" in line]

    def test_dns_leaves_by_its_normal_route(self):
        # Marking DNS sends queries for 100.100.100.100 (Tailscale), a LAN router or an IPv6 resolver out of
        # the adopted interface, where they are unreachable: every lookup of the app then fails.
        rules = self._output_rules(adopt.nft_script(5, adopt.group_rel(ID), "wlan0"))
        self.assertTrue(rules[0].endswith("meta l4proto { tcp, udp } th dport 53 return"), rules)
        self.assertTrue(rules[-1].endswith("meta mark set 0x105 ct mark set 0x105"), rules)

    def test_ipv6_is_rejected_before_marking(self):
        # A silent IPv6 blackhole makes connect() hang for minutes; a reject makes apps fall back to IPv4 at once.
        # The rejects sit in their own chain behind an explicit IPv6 match: `nft list` prints a
        # `meta nfproto ipv6 meta l4proto tcp reject` rule without the IPv6 part, and loading that listing
        # again would reset every TCP connection of the app, IPv4 included.
        script = adopt.nft_script(5, adopt.group_rel(ID), "wlan0")
        rules = self._output_rules(script)
        self.assertTrue(rules[1].endswith("ip6 daddr ::1 return"), rules)   # local IPv6 keeps working
        self.assertTrue(rules[2].endswith("meta nfproto ipv6 jump reject_ipv6"), rules)
        self.assertEqual(len(rules), 4)
        chain = script.split("chain reject_ipv6 {", 1)[1].split("\n  }", 1)[0]
        self.assertEqual([line.strip() for line in chain.strip().splitlines()],
                         ["meta l4proto tcp reject with tcp reset", "reject with icmpx type admin-prohibited"])

    def test_script_is_accepted_by_nft(self):
        """Check the syntax with the real nft (`nft -c` loads nothing). The cgroup match needs the cgroup to
        exist, so it is swapped for a plain match; everything else is checked as generated."""
        import re
        import shutil
        import subprocess
        import tempfile

        nft, unshare = shutil.which("nft"), shutil.which("unshare")
        if not nft or not unshare:
            self.skipTest("nft or unshare is not installed")
        script = adopt.nft_script(5, adopt.group_rel(ID), "wlan0")
        script = re.sub(r'socket cgroupv2 level \d+ "[^"]+"', "meta skuid 0", script)
        with tempfile.NamedTemporaryFile("w", suffix=".nft") as handle:
            handle.write(script)
            handle.flush()
            result = subprocess.run([unshare, "-rn", nft, "-c", "-f", handle.name], capture_output=True, text=True)
        if "not permitted" in result.stderr or "Permission denied" in result.stderr or "unshare" in result.stderr:
            self.skipTest("cannot create a user and network namespace here")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_only_the_original_direction_is_marked(self):
        # Replies to a connection someone else opened must leave the way the request came in.
        rules = self._output_rules(adopt.nft_script(5, adopt.group_rel(ID), "wlan0"))
        self.assertIn("ct direction original meta mark set 0x105", rules[-1])

    def test_loading_twice_gives_the_same_ruleset(self):
        script = adopt.nft_script(5, adopt.group_rel(ID), "wlan0")
        self.assertTrue(script.startswith("add table inet intermux_adopt5\ndelete table inet intermux_adopt5\n"))

    def test_only_intermux_groups_are_accepted(self):
        for bad in ("intermux/../../tmp", "user.slice", "intermux/adopt-XYZ", ""):
            with self.assertRaises(ValidationError):
                adopt.nft_script(5, bad, "wlan0")
            with self.assertRaises(ValidationError):
                adopt.read_group_pids(bad)

    def test_reply_mark_is_restored_for_rp_filter(self):
        script = adopt.nft_script(5, adopt.group_rel(ID), "wlan0")
        pre = script.split("chain prerouting {", 1)[1].split("\n  }", 1)[0]
        self.assertIn("type filter hook prerouting priority mangle;", pre)
        self.assertIn("ct mark 0x105 meta mark set 0x105", pre)


class CgroupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "cgroup.controllers").write_text("cpu memory\n")
        patcher = mock.patch.object(adopt, "CGROUP_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.rel = adopt.group_rel(ID)

    def test_ensure_requires_cgroup_v2(self):
        (self.root / "cgroup.controllers").unlink()
        with self.assertRaises(InterMuxError) as ctx:
            adopt.ensure_group(self.rel)
        self.assertEqual(ctx.exception.code, "cgroup_v1")
        self.assertFalse((self.root / "intermux").exists())

    def test_read_missing(self):
        self.assertEqual(adopt.read_group_pids(self.rel), [])

    def test_ensure_idempotent(self):
        adopt.ensure_group(self.rel)
        adopt.ensure_group(self.rel)
        self.assertTrue((self.root / self.rel).is_dir())

    def test_move_and_read(self):
        adopt.ensure_group(self.rel)
        moved = adopt.move_pids(self.rel, [10, 11])
        self.assertEqual(moved, [10, 11])
        self.assertEqual(adopt.read_group_pids(self.rel), [11])
        self.assertTrue((self.root / self.rel / "cgroup.procs").is_file())

    def test_move_skips_vanished(self):
        # group directory missing: every write fails with OSError
        self.assertEqual(adopt.move_pids(self.rel, [1, 2]), [])

    def test_release(self):
        adopt.ensure_group(self.rel)
        procs = self.root / self.rel / "cgroup.procs"
        procs.write_text("20\n21\n")
        moved = adopt.release_group(self.rel, [22])
        self.assertEqual(sorted(moved), [20, 21, 22])
        self.assertTrue((self.root / "cgroup.procs").is_file())

    def test_release_missing_group(self):
        self.assertEqual(adopt.release_group(self.rel), [])


class LeftoverRulesTests(unittest.TestCase):
    def test_filter(self):
        rules = [
            {"priority": 2001, "fwmark": "0x101", "table": "2001"},
            {"priority": 2200, "fwmark": "0x1c8/0xffffffff", "table": "2200"},
            {"priority": 2000, "fwmark": "0x101", "table": "2001"},
            {"priority": 2201, "fwmark": "0x101", "table": "2001"},
            {"priority": 2001, "fwmark": "0x100", "table": "2001"},
            {"priority": 2001, "fwmark": "0x1c9", "table": "2001"},
            {"priority": 2001, "table": "main"},
            {"priority": "bad", "fwmark": "0x101"},
            "junk",
        ]
        got = adopt.leftover_rules(rules)
        self.assertEqual([r["priority"] for r in got], [2001, 2200])
        self.assertEqual(adopt.leftover_rules({}), [])


class PolicyTests(unittest.TestCase):
    def test_rule_args_v4_and_v6(self):
        self.assertEqual(adopt.rule_args(1), ["rule", "add", "fwmark", "0x101", "lookup", "2001",
                                              "priority", "2001"])
        self.assertEqual(adopt.rule_args(1, 6, "del")[:3], ["-6", "rule", "del"])

    def test_route_commands_kill_switch_and_v6_blackhole(self):
        cmds = adopt.route_commands(1, "wlan0", "10.0.0.1")
        self.assertEqual(cmds[0], ["route", "replace", "default", "via", "10.0.0.1", "dev", "wlan0",
                                   "table", "2001", "metric", "100"])
        self.assertEqual(cmds[1], ["route", "replace", "unreachable", "default", "metric", "1000",
                                   "table", "2001"])
        self.assertEqual(cmds[2], ["-6", "route", "replace", "unreachable", "default", "table", "2001"])
        self.assertNotIn("via", adopt.route_commands(1, "wlan0", None)[0])

    def test_table_ok(self):
        good = [{"dst": "default", "gateway": "10.0.0.1", "dev": "wlan0"},
                {"dst": "default", "type": "unreachable"}]
        self.assertTrue(adopt.table_ok(good, "wlan0", "10.0.0.1"))
        self.assertFalse(adopt.table_ok(good[1:], "wlan0", "10.0.0.1"))
        self.assertFalse(adopt.table_ok(good[:1], "wlan0", "10.0.0.1"))
        self.assertFalse(adopt.table_ok(good, "wlan0", "10.0.0.9"))
        self.assertFalse(adopt.table_ok(None, "wlan0", None))

    def _run(self, existing_rules):
        calls = []
        ok = mock.Mock()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conf = Path(tmp.name)
        (self.conf / "wlan0").mkdir()
        with mock.patch.object(adopt, "SYSCTL_CONF_DIR", self.conf), \
             mock.patch.object(adopt, "_ip_bin", return_value="ip"), \
             mock.patch.object(adopt, "_rule_exists", side_effect=lambda s, f=4: f in existing_rules), \
             mock.patch.object(adopt.commands, "run_checked",
                               side_effect=lambda argv, **k: calls.append(list(argv)) or ok):
            adopt.apply_policy(1, "wlan0", "10.0.0.1")
        return calls

    def test_apply_installs_both_rules_and_routes(self):
        calls = self._run(set())
        self.assertIn(["ip", "rule", "add", "fwmark", "0x101", "lookup", "2001", "priority", "2001"], calls)
        self.assertIn(["ip", "-6", "rule", "add", "fwmark", "0x101", "lookup", "2001", "priority", "2001"],
                      calls)
        self.assertIn(["ip", "-6", "route", "replace", "unreachable", "default", "table", "2001"], calls)

    def test_apply_enables_src_valid_mark_on_the_interface(self):
        self._run(set())
        self.assertEqual((self.conf / "wlan0" / "src_valid_mark").read_text(), "1")

    def test_src_valid_mark_missing_interface_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(adopt, "SYSCTL_CONF_DIR", Path(tmp)):
            adopt.enable_src_valid_mark("gone0")
            self.assertFalse((Path(tmp) / "gone0").exists())

    def test_src_valid_mark_rejects_traversal(self):
        for bad in ("..", ".", "a/b", ""):
            with self.assertRaises(ValidationError):
                adopt.enable_src_valid_mark(bad)

    def test_apply_is_idempotent_when_rules_exist(self):
        calls = self._run({4, 6})
        self.assertFalse([c for c in calls if "rule" in c])

    def test_apply_rejects_unsafe_iface(self):
        with self.assertRaises(ValidationError):
            adopt.apply_policy(1, "wl;an", None)

    def test_remove_policy_removes_v6_too(self):
        calls = []
        with mock.patch.object(adopt, "_ip_bin", return_value="ip"), \
             mock.patch.object(adopt.commands, "run_checked", side_effect=lambda a, **k: calls.append(list(a))):
            adopt.remove_policy(1)
        self.assertIn(["ip", "-6", "rule", "del", "fwmark", "0x101", "lookup", "2001", "priority", "2001"],
                      calls)
        self.assertIn(["ip", "-6", "route", "flush", "table", "2001"], calls)

    def test_sweep_removes_v6_rule_without_v4(self):
        calls = []
        rule = {"priority": 2001, "fwmark": "0x101", "table": "2001"}

        def fake_json(argv, **k):
            return [rule] if "-6" in argv else []

        with mock.patch.object(adopt, "_ip_bin", return_value="ip"), \
             mock.patch.object(adopt.commands, "json_cmd", side_effect=fake_json), \
             mock.patch.object(adopt.commands, "run_checked", side_effect=lambda a, **k: calls.append(list(a))), \
             mock.patch.object(adopt, "CGROUP_ROOT", tempfile.mkdtemp()):
            adopt.sweep_leftovers()
        self.assertIn(["ip", "-6", "rule", "del", "fwmark", "0x101", "lookup", "2001", "priority", "2001"],
                      calls)
        self.assertIn(["ip", "-6", "route", "flush", "table", "2001"], calls)


class RefreshTests(unittest.TestCase):
    def _iface(self, bindable=True, gws=("10.0.0.1",)):
        return mock.Mock(bindable=bindable, gateways=list(gws))

    def test_refresh_policy_noop_when_healthy(self):
        good = [{"dst": "default", "gateway": "10.0.0.1", "dev": "wlan0"},
                {"dst": "default", "type": "unreachable"}]
        with mock.patch.object(adopt, "_ip_bin", return_value="ip"), \
             mock.patch.object(adopt.commands, "json_cmd", return_value=good), \
             mock.patch.object(adopt, "_rule_exists", return_value=True), \
             mock.patch.object(adopt, "apply_policy") as apply:
            self.assertFalse(adopt.refresh_policy(1, "wlan0", "10.0.0.1"))
        apply.assert_not_called()

    def test_refresh_policy_reapplies_when_default_dropped(self):
        only_fallback = [{"dst": "default", "type": "unreachable"}]
        with mock.patch.object(adopt, "_ip_bin", return_value="ip"), \
             mock.patch.object(adopt.commands, "json_cmd", return_value=only_fallback), \
             mock.patch.object(adopt, "_rule_exists", return_value=True), \
             mock.patch.object(adopt, "apply_policy") as apply:
            self.assertTrue(adopt.refresh_policy(1, "wlan0", "10.0.0.1"))
        apply.assert_called_once_with(1, "wlan0", "10.0.0.1")

    def test_refresh_adoptions_skips_down_and_missing(self):
        a1 = mock.Mock(iface="up0", slot=1)
        a2 = mock.Mock(iface="down0", slot=2)
        a3 = mock.Mock(iface="gone0", slot=3)
        ifaces = {"up0": self._iface(), "down0": self._iface(bindable=False)}
        with mock.patch.object(adopt, "refresh_policy", return_value=True) as rp:
            self.assertEqual(adopt.refresh_adoptions([a1, a2, a3], ifaces), [1])
        rp.assert_called_once_with(1, "up0", "10.0.0.1")


if __name__ == "__main__":
    unittest.main()
