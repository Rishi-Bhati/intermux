"""Pure tests for engine.build_launch_argv. No system calls."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from unittest import mock

from intermux import config
from intermux import engine
from intermux.engine import build_launch_argv
from intermux.errors import ConflictError, NotFoundError, PermissionDeniedError
from intermux.system.session import UserInfo


class BuildLaunchArgvTests(unittest.TestCase):
    def setUp(self) -> None:
        self.user = UserInfo(uid=1000, gid=1000, username="alice", home="/home/alice")
        self.ns = "ns_1a2b3c4d"

    def test_exact_argv_order(self) -> None:
        argv = build_launch_argv(self.ns, self.user, {"DISPLAY": ":0"}, "/usr/bin/firefox",
                                 ["--no-remote"], ip_bin="ip", runuser_bin="runuser")
        self.assertEqual(argv, [
            "ip", "netns", "exec", "ns_1a2b3c4d", "runuser", "-u", "alice", "--",
            "env", "-i", "DISPLAY=:0",
            "HOME=/home/alice", "USER=alice", "LOGNAME=alice",
            f"PATH={config.APP_PATH_ENV}", "TMPDIR=/tmp/intermux-ns_1a2b3c4d-1000",
            "/usr/bin/firefox", "--no-remote",
        ])

    def test_env_pairs_keep_insertion_order_and_precede_fixed_vars(self) -> None:
        env = {"WAYLAND_DISPLAY": "/run/user/1000/wayland-0", "LANG": "en_US.UTF-8"}
        argv = build_launch_argv(self.ns, self.user, env, "/usr/bin/app", [])
        start = argv.index("env")
        self.assertEqual(argv[start:start + 2], ["env", "-i"])
        self.assertEqual(argv[start + 2:start + 4],
                         ["WAYLAND_DISPLAY=/run/user/1000/wayland-0", "LANG=en_US.UTF-8"])
        self.assertEqual(argv[start + 4], "HOME=/home/alice")

    def test_no_env_and_no_extra_args(self) -> None:
        argv = build_launch_argv(self.ns, self.user, {}, "/usr/bin/app", [])
        self.assertEqual(argv[-1], "/usr/bin/app")
        self.assertEqual(argv[argv.index("/usr/bin/app") - 1], f"TMPDIR=/tmp/intermux-{self.ns}-1000")
        self.assertFalse(any(a.startswith("DISPLAY=") for a in argv))

    def test_extra_args_follow_app_path_in_order(self) -> None:
        argv = build_launch_argv(self.ns, self.user, {}, "/opt/app", ["--profile", "/x", "--flag"])
        self.assertEqual(argv[-4:], ["/opt/app", "--profile", "/x", "--flag"])

    def test_default_binaries_are_ip_and_runuser(self) -> None:
        argv = build_launch_argv(self.ns, self.user, {}, "/usr/bin/app", [])
        self.assertEqual(argv[0], "ip")
        self.assertEqual(argv[4], "runuser")

    def test_custom_binaries_are_used(self) -> None:
        argv = build_launch_argv(self.ns, self.user, {}, "/usr/bin/app", [],
                                 ip_bin="/usr/sbin/ip", runuser_bin="/usr/sbin/runuser")
        self.assertEqual(argv[0], "/usr/sbin/ip")
        self.assertEqual(argv[4], "/usr/sbin/runuser")

    def test_all_elements_are_strings(self) -> None:
        argv = build_launch_argv(self.ns, self.user, {"LANG": "C"}, "/usr/bin/app", ["-x"])
        self.assertTrue(all(isinstance(a, str) for a in argv))


class AdoptEngineTests(unittest.TestCase):
    USER = UserInfo(uid=1000, gid=1000, username="u", home="/home/u")

    def _adoption(self, uid=1000):
        from intermux import state
        return state.Adoption(id="0123456789abcdef", iface="wlan0", app="/usr/bin/x", app_name="x", slot=1,
                              group="intermux/adopt-0123456789abcdef", mark=257, table=2001,
                              priority=2001, uid=uid, pids=[5])

    def _run_adopt(self, move_result, real_uid=1000):
        from intermux import state
        iface = mock.Mock(bindable=True, gateways=["10.0.0.1"])
        iface.name = "wlan0"
        proc = mock.Mock(pid=5)
        patches = [
            mock.patch.object(engine, "_find_interface", return_value=iface),
            mock.patch.object(engine.os.path, "exists", return_value=True),
            mock.patch.object(engine.apps, "get_handler", return_value=mock.Mock(comm_names=lambda a: [])),
            mock.patch.object(engine.procs, "find_app_procs", return_value=[proc]),
            mock.patch.object(engine.procs, "list_procs", return_value=[]),
            mock.patch.object(engine.procs, "descendants", return_value=[5, 6]),
            mock.patch.object(engine, "filter_host_netns", return_value=[5, 6]),
            mock.patch.object(engine, "_real_uid", return_value=real_uid),
            mock.patch.object(engine.state, "load_state", return_value=state.State()),
            mock.patch.object(engine.state, "save_state"),
            mock.patch.object(engine.adopt_sys, "ensure_group"),
            mock.patch.object(engine.adopt_sys, "move_pids", side_effect=move_result),
            mock.patch.object(engine.adopt_sys, "apply_nft"),
            mock.patch.object(engine.adopt_sys, "apply_policy"),
            mock.patch.object(engine.adopt_sys, "release_group"),
            mock.patch.object(engine.adopt_sys, "delete_nft"),
            mock.patch.object(engine.adopt_sys, "remove_policy"),
        ]
        mocks = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        return mocks

    def test_empty_move_raises_and_rolls_back(self):
        mocks = self._run_adopt(lambda g, p: [])
        with self.assertRaises(NotFoundError):
            engine.adopt("wlan0", "/usr/bin/x", user=self.USER)
        mocks[14].assert_called_once_with(mock.ANY, [])
        mocks[15].assert_called_once()
        mocks[16].assert_called_once()
        mocks[9].assert_not_called()

    def test_failure_rollback_passes_empty_list(self):
        mocks = self._run_adopt(lambda g, p: list(p))
        mocks[12].side_effect = ConflictError("boom")
        with self.assertRaises(ConflictError):
            engine.adopt("wlan0", "/usr/bin/x", user=self.USER)
        mocks[14].assert_called_once_with(mock.ANY, [])

    def test_other_users_pid_skipped(self):
        self._run_adopt(lambda g, p: list(p), real_uid=2000)
        with self.assertRaises(NotFoundError):
            engine.adopt("wlan0", "/usr/bin/x", user=self.USER)

    def test_success_records_uid(self):
        self._run_adopt(lambda g, p: list(p))
        out = engine.adopt("wlan0", "/usr/bin/x", user=self.USER)
        self.assertEqual(out["adoption"]["uid"], 1000)
        self.assertEqual(out["moved_pids"], [5, 6])

    def test_release_refuses_other_uid(self):
        from intermux import state
        st = state.State(adoptions=[self._adoption(uid=1000)])
        other = UserInfo(uid=2000, gid=2000, username="o", home="/home/o")
        with mock.patch.object(engine.state, "load_state", return_value=st), \
                mock.patch.object(engine, "_release_plumbing") as rel, \
                mock.patch.object(engine.state, "save_state") as save:
            with self.assertRaises(PermissionDeniedError):
                engine.release_adoption("0123456789abcdef", user=other)
        rel.assert_not_called()
        save.assert_not_called()
        self.assertEqual(len(st.adoptions), 1)

    def test_release_keeps_record_when_pids_remain(self):
        from intermux import state
        st = state.State(adoptions=[self._adoption()])
        with mock.patch.object(engine.state, "load_state", return_value=st), \
                mock.patch.object(engine.state, "save_state"), \
                mock.patch.object(engine.procs, "list_procs", return_value=[]), \
                mock.patch.object(engine, "_release_plumbing", return_value=[]), \
                mock.patch.object(engine.adopt_sys, "read_group_pids", return_value=[9]):
            out = engine.release_adoption("0123456789abcdef", user=self.USER)
        self.assertEqual(out["still_running"], [9])
        self.assertEqual(len(st.adoptions), 1)


class OpenLogFileTests(unittest.TestCase):
    def test_fifo_is_rejected_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            os.mkfifo(os.path.join(d, "x.log"))
            dirfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.assertRaises(ConflictError) as ctx:
                    engine._open_log_file(dirfd, "x.log")
            finally:
                os.close(dirfd)
            self.assertEqual(ctx.exception.code, "unsafe_path")

    def test_hardlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "a"), "w").close()
            os.link(os.path.join(d, "a"), os.path.join(d, "x.log"))
            dirfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.assertRaises(ConflictError):
                    engine._open_log_file(dirfd, "x.log")
            finally:
                os.close(dirfd)

    def test_regular_file_created_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            dirfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
            try:
                fd, created = engine._open_log_file(dirfd, "x.log")
            finally:
                os.close(dirfd)
            try:
                self.assertTrue(created)
                self.assertTrue(stat.S_ISREG(os.fstat(fd).st_mode))
                import fcntl
                self.assertFalse(fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_NONBLOCK)
            finally:
                os.close(fd)


class ReapTests(unittest.TestCase):
    def test_finished_processes_are_dropped(self) -> None:
        done, running = mock.Mock(), mock.Mock()
        done.poll.return_value = 0
        running.poll.return_value = None
        with mock.patch.dict(engine._launched, {1: done, 2: running}, clear=True):
            engine._reap_launched()
            self.assertEqual(list(engine._launched), [2])


class FilterHostNetnsTests(unittest.TestCase):
    def _proc(self, pid, netns):
        from intermux.system.procs import Proc

        return Proc(pid=pid, ppid=1, uid=1000, comm="x", exe="/x", netns=netns)

    def test_filters_foreign_namespaces(self):
        table = [self._proc(1, 100), self._proc(2, 200), self._proc(3, None)]
        self.assertEqual(engine.filter_host_netns(table, [1, 2, 3, 4], 100), [1, 3])

    def test_unknown_host_netns_keeps_known_pids(self):
        table = [self._proc(1, 100), self._proc(2, 200)]
        self.assertEqual(engine.filter_host_netns(table, [1, 2], None), [1, 2])



class NamespaceDnsTests(unittest.TestCase):
    def test_gateway_first_then_public_resolvers(self) -> None:
        from intermux.system.netinfo import Interface

        iface = Interface(name="wlan0", kind="wifi", up=True, operstate="UP", flags=["UP"], mac=None, mtu=None,
                          addresses=[], default_route=True, gateways=["10.64.128.159"])
        self.assertEqual(engine.namespace_dns_servers(iface), ["10.64.128.159", "1.1.1.1", "9.9.9.9"])

    def test_host_only_resolvers_are_not_used_and_ipv6_gateway_is_skipped(self) -> None:
        from intermux.system.netinfo import Interface

        iface = Interface(name="usb0", kind="usb", up=True, operstate="UP", flags=["UP"], mac=None, mtu=None,
                          addresses=[], default_route=True, gateways=["fe80::1", "10.231.212.182"])
        servers = engine.namespace_dns_servers(iface)
        self.assertEqual(servers[0], "10.231.212.182")
        self.assertNotIn("100.100.100.100", servers)
        self.assertNotIn("fe80::1", servers)


class DnsRedirectTests(unittest.TestCase):
    """The namespace nat table that keeps DNS working after the host replaces /etc/resolv.conf."""

    def test_restore_body_exempts_own_servers_and_redirects_the_rest(self) -> None:
        from intermux.system import netns

        body = netns.dns_redirect_restore(["10.64.128.159", "1.1.1.1", "fe80::1"], "veth1_0a0b0c0d", "10.200.1.2")
        lines = body.splitlines()
        self.assertEqual(lines[0], "*nat")
        self.assertEqual(lines[-1], "COMMIT")
        for proto in ("udp", "tcp"):
            for addr in ("10.64.128.159", "1.1.1.1"):
                rule = f"-A OUTPUT -d {addr}/32 -p {proto} -m {proto} --dport 53 -j RETURN"
                self.assertIn(rule, lines)
            dnat = f"-A OUTPUT -p {proto} -m {proto} --dport 53 -j DNAT --to-destination 10.64.128.159"
            self.assertIn(dnat, lines)
            # exemptions come before the catch-all DNAT
            self.assertLess(lines.index(f"-A OUTPUT -d 1.1.1.1/32 -p {proto} -m {proto} --dport 53 -j RETURN"),
                            lines.index(dnat))
        self.assertIn("-A POSTROUTING -s 127.0.0.0/8 -o veth1_0a0b0c0d -j SNAT --to-source 10.200.1.2", lines)
        self.assertNotIn("fe80::1", body)

    def test_restore_body_rejects_bad_input(self) -> None:
        from intermux.errors import ValidationError
        from intermux.system import netns

        with self.assertRaises(ValidationError):
            netns.dns_redirect_restore(["fe80::1"], "veth1_0a0b0c0d", "10.200.1.2")
        with self.assertRaises(ValidationError):
            netns.dns_redirect_restore(["1.1.1.1"], "eth0; rm -rf /", "10.200.1.2")
        with self.assertRaises(ValidationError):
            netns.dns_redirect_restore(["1.1.1.1 -j ACCEPT"], "veth1_0a0b0c0d", "10.200.1.2")

    def test_resolv_conf_has_short_timeouts(self) -> None:
        import tempfile
        from pathlib import Path

        from intermux.system import netns

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(netns.config, "ETC_NETNS_DIR", Path(tmp)):
            netns.write_resolv_conf("ns_0a0b0c0d", ["10.64.128.159", "1.1.1.1"])
            body = (Path(tmp) / "ns_0a0b0c0d" / "resolv.conf").read_text()
        self.assertEqual(body.splitlines()[1:], ["nameserver 10.64.128.159", "nameserver 1.1.1.1",
                                                 "options timeout:1 attempts:2"])


class DnsProbeTests(unittest.TestCase):
    def test_probe_reply_parsing(self) -> None:
        import struct

        from intermux.system import netns

        query = netns.dns_probe_query()
        self.assertEqual(len(query), 17)
        qid = struct.unpack("!H", query[:2])[0]
        ok = struct.pack("!HHHHHH", qid, 0x8180, 1, 1, 0, 0)
        nxdomain = struct.pack("!HHHHHH", qid, 0x8183, 1, 0, 0, 0)
        refused = struct.pack("!HHHHHH", qid, 0x8185, 1, 0, 0, 0)
        other_id = struct.pack("!HHHHHH", qid ^ 1, 0x8180, 1, 1, 0, 0)
        self.assertTrue(netns.dns_probe_ok(ok))
        self.assertTrue(netns.dns_probe_ok(nxdomain))
        self.assertFalse(netns.dns_probe_ok(refused))
        self.assertFalse(netns.dns_probe_ok(other_id))
        self.assertFalse(netns.dns_probe_ok(query))       # a query, not a reply
        self.assertFalse(netns.dns_probe_ok(b"\x00"))

    def test_answering_servers_move_first(self) -> None:
        servers = ["172.17.0.1", "1.1.1.1", "9.9.9.9"]
        self.assertEqual(engine.order_dns_servers(servers, ["9.9.9.9", "1.1.1.1"]),
                         ["1.1.1.1", "9.9.9.9", "172.17.0.1"])
        self.assertEqual(engine.order_dns_servers(servers, []), servers)


if __name__ == "__main__":
    unittest.main()
