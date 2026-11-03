"""Networking fixes found in pre-release testing: DNS probe order, nsswitch, teardown and reset robustness."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from intermux import config, engine
from intermux.errors import SystemCommandError
from intermux.system import adopt, netns, procs


class NsswitchTests(unittest.TestCase):
    def test_systemd_resolved_is_taken_out_of_hosts(self):
        host = ("passwd: files systemd\n"
                "hosts:      files myhostname resolve [!UNAVAIL=return] dns # comment\n"
                "networks: files\n")
        out = netns.namespace_nsswitch(host)
        self.assertIn("\nhosts: files myhostname dns\n", out)
        self.assertIn("passwd: files systemd", out)       # the rest of the file is kept
        self.assertIn("networks: files", out)

    def test_dns_is_added_when_resolve_was_the_only_resolver(self):
        out = netns.namespace_nsswitch("hosts: files mymachines resolve [!UNAVAIL=return] myhostname\n")
        self.assertIn("hosts: files mymachines myhostname dns", out)

    def test_hosts_without_resolve_needs_no_copy(self):
        self.assertIsNone(netns.namespace_nsswitch("hosts: files mdns4_minimal [NOTFOUND=return] dns\n"))

    def test_file_written_and_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = Path(tmp) / "nsswitch.conf"
            etc = Path(tmp) / "netns"
            with mock.patch.object(netns, "HOST_NSSWITCH", host), \
                    mock.patch.object(config, "ETC_NETNS_DIR", etc):
                host.write_text("hosts: files resolve dns\n")
                netns.write_nsswitch_conf("ns_0123abcd")
                target = etc / "ns_0123abcd" / "nsswitch.conf"
                self.assertIn("hosts: files dns", target.read_text())
                host.write_text("hosts: files dns\n")
                netns.write_nsswitch_conf("ns_0123abcd")
                self.assertFalse(target.exists())


class DnsProbeOrderTests(unittest.TestCase):
    def test_redirect_allows_the_new_servers_before_the_probe(self):
        order = []
        with mock.patch.object(engine.firewall, "iptables_cmd", return_value="iptables"), \
                mock.patch.object(engine.netns, "ensure_dns_redirect",
                                  side_effect=lambda ns, servers, *a: order.append(("redirect", list(servers)))), \
                mock.patch.object(engine.netns, "probe_dns",
                                  side_effect=lambda ns, servers: order.append(("probe", list(servers))) or ["9.9.9.9"]), \
                mock.patch.object(engine.netns, "write_resolv_conf"), \
                mock.patch.object(engine.netns, "write_nsswitch_conf"):
            warnings = engine.configure_namespace_dns("ns_0123abcd", "veth1_0123abcd", "10.200.1.2",
                                                      ["203.0.113.99", "9.9.9.9"])
        self.assertEqual(order[0], ("redirect", ["203.0.113.99", "9.9.9.9"]))
        self.assertEqual(order[1][0], "probe")
        self.assertEqual(order[2], ("redirect", ["9.9.9.9", "203.0.113.99"]))   # the answering one first
        self.assertEqual(warnings, [])


class TeardownTests(unittest.TestCase):
    def test_every_step_runs_and_the_veth_leaves_firewalld_first(self):
        calls = []

        def record(name, fail=False):
            def step(*args):
                calls.append(name)
                if fail:
                    raise SystemCommandError(f"{name} failed")
            return step

        with mock.patch.object(engine.firewall, "remove_nat", record("nat", fail=True)), \
                mock.patch.object(engine.routing, "remove_policy", record("policy")), \
                mock.patch.object(engine.firewall, "untrust_veth", record("untrust")), \
                mock.patch.object(engine.netns, "delete_veth", record("veth")), \
                mock.patch.object(engine.netns, "delete_namespace", record("namespace")), \
                mock.patch.object(engine, "_remove_tmpdirs", record("tmpdirs")):
            with self.assertRaises(SystemCommandError):
                engine._teardown_iface("wlan0", "10.200.1.0/24")
        self.assertEqual(calls, ["nat", "policy", "untrust", "veth", "namespace", "tmpdirs"])

    def test_tmpdirs_of_the_namespace_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            uid = os.getuid()
            mine = Path(tmp) / f"intermux-ns_0123abcd-{uid}"
            other = Path(tmp) / f"intermux-ns_ffffffff-{uid}"
            wrong_owner = Path(tmp) / "intermux-ns_0123abcd-4242"
            for d in (mine, other, wrong_owner):
                d.mkdir()
            real_listdir, real_join = os.listdir, os.path.join
            with mock.patch.object(engine.os, "listdir", lambda p: real_listdir(tmp) if p == "/tmp" else real_listdir(p)), \
                    mock.patch.object(engine.os.path, "join",
                                      lambda a, *b: real_join(tmp, *b) if a == "/tmp" else real_join(a, *b)):
                engine._remove_tmpdirs("ns_0123abcd")
            self.assertFalse(mine.exists())
            self.assertTrue(other.exists())
            self.assertTrue(wrong_owner.exists())   # its owner is not the uid in its name


class ProcessIdentityTests(unittest.TestCase):
    def test_start_time_identifies_a_process(self):
        self.assertIsInstance(procs.start_time(os.getpid()), int)
        self.assertIsNone(procs.start_time(2 ** 30))

    def test_pid_group_only_reports_intermux_groups(self):
        with mock.patch("builtins.open", mock.mock_open(read_data="0::/intermux/adopt-0123456789abcdef\n")):
            self.assertEqual(adopt.pid_group(123), "intermux/adopt-0123456789abcdef")
        with mock.patch("builtins.open", mock.mock_open(read_data="0::/user.slice/app.slice\n")):
            self.assertIsNone(adopt.pid_group(123))

    def test_unreadable_group_is_not_empty(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(adopt, "CGROUP_ROOT", Path(tmp)):
            self.assertEqual(adopt.read_group_pids_or_none("intermux/adopt-0123456789abcdef"), [])
            group = Path(tmp) / "intermux" / "adopt-0123456789abcdef"
            group.mkdir(parents=True)
            (group / "cgroup.procs").write_text("10\n11\n")
            self.assertEqual(adopt.read_group_pids_or_none("intermux/adopt-0123456789abcdef"), [10, 11])
            with mock.patch.object(Path, "read_text", side_effect=PermissionError("denied")):
                self.assertIsNone(adopt.read_group_pids_or_none("intermux/adopt-0123456789abcdef"))


class AdoptionRoutingTests(unittest.TestCase):
    def test_a_fallback_with_a_lower_metric_is_not_healthy(self):
        good = [{"dst": "default", "dev": "wlan0", "gateway": "10.0.0.1", "metric": 100},
                {"dst": "default", "type": "unreachable", "metric": 1000}]
        self.assertTrue(adopt.table_ok(good, "wlan0", "10.0.0.1"))
        stray = [good[0], {"dst": "default", "type": "unreachable", "metric": 50}]
        self.assertFalse(adopt.table_ok(stray, "wlan0", "10.0.0.1"))
        self.assertFalse(adopt.table_ok([dict(good[0], metric=500), good[1]], "wlan0", "10.0.0.1"))

    def test_slots_loaded_in_the_kernel_are_not_reused(self):
        with mock.patch.object(adopt, "list_nft_tables", return_value=["intermux_adopt1", "intermux_adopt3"]):
            self.assertEqual(adopt.kernel_slots(), {1, 3})
            self.assertEqual(adopt.pick_slot({2} | adopt.kernel_slots()), 4)
        with mock.patch.object(adopt, "list_nft_tables", side_effect=OSError("no nft")):
            self.assertEqual(adopt.kernel_slots(), set())


if __name__ == "__main__":
    unittest.main()
