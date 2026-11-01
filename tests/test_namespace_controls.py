"""Tests for per-namespace DNS, bandwidth limits, restart and removal. Every system call is mocked."""

from __future__ import annotations

import unittest
from unittest import mock

from intermux import dnsconf, engine, state
from intermux.errors import ConflictError, MissingToolError, NotFoundError, ValidationError
from intermux.system import shaping
from intermux.system.session import UserInfo

USER = UserInfo(uid=1000, gid=1000, username="u", home="/home/u")


def _binding(iface="wlan0", app="/usr/bin/x", profile_mode="default"):
    return state.Binding(iface=iface, app=app, app_name="x", namespace=engine.netns.namespace_name(iface),
                         subnet="10.200.7.0/24", table=1007, priority=1007, profile_mode=profile_mode)


class DnsConfTests(unittest.TestCase):
    def test_parse_servers(self):
        self.assertEqual(dnsconf.parse_servers("1.1.1.1, 9.9.9.9"), ["1.1.1.1", "9.9.9.9"])
        self.assertEqual(dnsconf.parse_servers("1.1.1.1 1.1.1.1;8.8.8.8"), ["1.1.1.1", "8.8.8.8"])
        self.assertEqual(dnsconf.parse_servers("quad9"), list(dnsconf.PRESETS["Quad9"]))
        self.assertEqual(dnsconf.parse_servers("Google: 8.8.8.8, 8.8.4.4"), ["8.8.8.8", "8.8.4.4"])
        for bad in ("", "127.0.0.53", "169.254.1.1", "224.0.0.1", "0.0.0.0", "dns.google", "1.1.1.1 1.0.0.1 8.8.8.8 8.8.4.4 9.9.9.9"):
            with self.subTest(text=bad), self.assertRaises(ValueError):
                dnsconf.parse_servers(bad)

    def test_describe(self):
        self.assertEqual(dnsconf.describe([]), "Automatic")
        self.assertEqual(dnsconf.describe(["1.1.1.1", "1.0.0.1"]), "Cloudflare")
        self.assertEqual(dnsconf.describe(["10.0.0.1"]), "10.0.0.1")


class ShapingTests(unittest.TestCase):
    def test_check_mbit(self):
        self.assertIsNone(shaping.check_mbit(None))
        self.assertEqual(shaping.check_mbit(20), 20.0)
        for bad in (0, -5, 10 ** 7, "5", True, float("nan")):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                shaping.check_mbit(bad)

    def test_tbf_args(self):
        self.assertEqual(shaping.tbf_args(20), ["tbf", "rate", "20000kbit", "burst", "25000", "latency", "100ms"])
        self.assertEqual(shaping.tbf_args(1)[4], str(16 * 1024))   # never below 16 KiB

    def test_set_limit_commands(self):
        with mock.patch.object(shaping.commands, "which", return_value="/usr/sbin/tc"), \
                mock.patch.object(shaping.commands, "ip_bin", return_value="/usr/sbin/ip"), \
                mock.patch.object(shaping.commands, "run_checked") as checked, \
                mock.patch.object(shaping.commands, "run") as run:
            shaping.set_limit("veth0_a", 10)
            shaping.set_limit("veth1_a", None, namespace="ns_a")
        self.assertEqual(checked.call_args[0][0][:7],
                         ["/usr/sbin/tc", "qdisc", "replace", "dev", "veth0_a", "root", "tbf"])
        self.assertEqual(run.call_args[0][0],
                         ["/usr/sbin/ip", "netns", "exec", "ns_a", "/usr/sbin/tc", "qdisc", "del", "dev",
                          "veth1_a", "root"])

    def test_missing_tc(self):
        with mock.patch.object(shaping.commands, "which", return_value=None):
            with self.assertRaises(MissingToolError):
                shaping.set_limit("veth0_a", 10)

    def test_format(self):
        self.assertEqual(shaping.format_mbit(None), "no limit")
        self.assertEqual(shaping.format_mbit(0.5), "500 kbit/s")
        self.assertEqual(shaping.format_mbit(20), "20 Mbit/s")


class StateFieldTests(unittest.TestCase):
    def test_round_trip_and_forget(self):
        st = state.State(subnets={"wlan0": "10.200.7.0/24"}, dns={"wlan0": ["1.1.1.1"]},
                         limits={"wlan0": {"down": 20.0, "up": None}})
        again = state.state_from_dict(state.state_to_dict(st))
        self.assertEqual(again.dns, st.dns)
        self.assertEqual(again.limits, st.limits)
        again.forget_iface("wlan0")
        self.assertEqual((again.subnets, again.dns, again.limits), ({}, {}, {}))

    def test_bad_entries_dropped(self):
        st = state.state_from_dict({"dns": {"a": ["127.0.0.1"], "b": "x"}, "limits": {"a": {"down": -1}, "b": 3}})
        self.assertEqual((st.dns, st.limits), ({}, {}))


class _EngineCase(unittest.TestCase):
    def setUp(self):
        self.st = state.State(subnets={"wlan0": "10.200.7.0/24"}, bindings=[_binding()])
        self.saved = []
        patches = [
            mock.patch.object(engine.state, "load_state", side_effect=lambda *_: self.st),
            mock.patch.object(engine.state, "save_state", side_effect=lambda s, p: self.saved.append(s)),
            mock.patch.object(engine.netns, "namespace_exists", return_value=True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class SetLimitTests(_EngineCase):
    def test_limits_both_ends_and_records(self):
        with mock.patch.object(engine.shaping, "set_limit") as limit:
            out = engine.set_limit("wlan0", 20.0, None)
        host_v, ns_v = engine.netns.veth_names("wlan0")
        limit.assert_any_call(host_v, 20.0)
        limit.assert_any_call(ns_v, None, namespace=engine.netns.namespace_name("wlan0"))
        self.assertEqual(self.st.limits, {"wlan0": {"down": 20.0, "up": None}})
        self.assertEqual(out["down"], 20.0)

    def test_clearing_forgets(self):
        self.st.limits["wlan0"] = {"down": 5.0, "up": 5.0}
        with mock.patch.object(engine.shaping, "set_limit"):
            engine.set_limit("wlan0", None, None)
        self.assertEqual(self.st.limits, {})

    def test_unknown_namespace(self):
        with self.assertRaises(NotFoundError):
            engine.set_limit("eth9", 1.0, None)

    def test_bind_warning_instead_of_failure(self):
        with mock.patch.object(engine.shaping, "set_limit", side_effect=MissingToolError("tc is not installed")):
            warnings = engine._apply_limits("wlan0", {"down": 1.0, "up": None})
        self.assertEqual(len(warnings), 1)
        self.assertIn("tc is not installed", warnings[0])
        self.assertEqual(engine._apply_limits("wlan0", None), [])


class SetDnsTests(_EngineCase):
    def test_custom_servers(self):
        with mock.patch.object(engine, "configure_namespace_dns", return_value=[]) as conf:
            out = engine.set_dns("wlan0", ["9.9.9.9"])
        self.assertEqual(conf.call_args[0][3], ["9.9.9.9"])
        self.assertEqual(self.st.dns, {"wlan0": ["9.9.9.9"]})
        self.assertFalse(out["automatic"])

    def test_auto_uses_interface_servers(self):
        self.st.dns["wlan0"] = ["9.9.9.9"]
        iface = mock.Mock(gateways=["192.168.1.1"])
        with mock.patch.object(engine, "_find_interface", return_value=iface), \
                mock.patch.object(engine, "configure_namespace_dns", return_value=[]) as conf:
            out = engine.set_dns("wlan0", dnsconf.AUTO)
        self.assertEqual(conf.call_args[0][3], ["192.168.1.1"] + list(engine.PUBLIC_DNS))
        self.assertEqual(self.st.dns, {})
        self.assertTrue(out["automatic"])

    def test_bad_servers(self):
        with self.assertRaises(ValidationError):
            engine.set_dns("wlan0", ["127.0.0.53"])


class RemoveNamespaceTests(_EngineCase):
    def test_removes_when_empty(self):
        procs_table = [mock.Mock(pid=50, netns=77, uid=1000), mock.Mock(pid=60, netns=77, uid=0)]
        with mock.patch.object(engine.procs, "list_procs", return_value=procs_table), \
                mock.patch.object(engine.procs, "netns_inode", return_value=77), \
                mock.patch.object(engine.procs, "descendants", side_effect=lambda pids, uid: pids) as desc, \
                mock.patch.object(engine.procs, "terminate", return_value=[]) as term, \
                mock.patch.object(engine.procs, "pids_in_netns", return_value=[]), \
                mock.patch.object(engine, "_teardown_iface") as td:
            out = engine.remove_namespace("wlan0", uid=1000)
        desc.assert_called_once_with([50], uid=1000)        # only the requesting user's processes
        term.assert_called_once()
        td.assert_called_once_with("wlan0", "10.200.7.0/24")
        self.assertTrue(out["removed"])
        self.assertEqual((self.st.bindings, self.st.subnets), ([], {}))

    def test_kept_while_other_processes_remain(self):
        with mock.patch.object(engine.procs, "list_procs", return_value=[]), \
                mock.patch.object(engine.procs, "netns_inode", return_value=77), \
                mock.patch.object(engine.procs, "pids_in_netns", return_value=[60]), \
                mock.patch.object(engine, "_teardown_iface") as td:
            out = engine.remove_namespace("wlan0", uid=1000)
        td.assert_not_called()
        self.assertFalse(out["removed"])
        self.assertEqual(out["still_running"], [60])
        self.assertEqual(len(self.st.bindings), 1)

    def test_unknown(self):
        with self.assertRaises(NotFoundError):
            engine.remove_namespace("eth9", uid=1000)


class RestartTests(_EngineCase):
    def test_restart_stops_then_binds_with_same_profile(self):
        self.st.bindings = [_binding(profile_mode="separate")]
        with mock.patch.object(engine, "stop_app", return_value={"stopped": [5], "still_running": []}), \
                mock.patch.object(engine, "bind", return_value={"pid": 9}) as bind:
            out = engine.restart_app("wlan0", "/usr/bin/x", user=USER, env={"DISPLAY": ":0"})
        self.assertEqual(out, {"pid": 9})
        kwargs = bind.call_args[1]
        self.assertEqual((kwargs["running_policy"], kwargs["keep_profile"]), ("separate", "separate"))

    def test_restart_default_profile_closes(self):
        with mock.patch.object(engine, "stop_app", return_value={"stopped": [], "still_running": []}), \
                mock.patch.object(engine, "bind", return_value={}) as bind:
            engine.restart_app("wlan0", "/usr/bin/x", user=USER, env={})
        self.assertEqual((bind.call_args[1]["running_policy"], bind.call_args[1]["keep_profile"]), ("close", None))

    def test_restart_refuses_when_app_survives(self):
        with mock.patch.object(engine, "stop_app", return_value={"stopped": [], "still_running": [5]}), \
                mock.patch.object(engine, "bind") as bind:
            with self.assertRaises(ConflictError):
                engine.restart_app("wlan0", "/usr/bin/x", user=USER, env={})
        bind.assert_not_called()

    def test_restart_unknown_binding(self):
        with self.assertRaises(NotFoundError):
            engine.restart_app("wlan0", "/usr/bin/other", user=USER, env={})


class ServerDispatchTests(unittest.TestCase):
    def test_new_actions_reach_the_engine(self):
        from intermux.helper import server
        user = UserInfo(uid=1000, gid=1000, username="u", home="/home/u")
        calls = {
            "set_dns": ({"iface": "wlan0", "dns": ["1.1.1.1"]}, "set_dns"),
            "set_limit": ({"iface": "wlan0", "down": 5.0, "up": None}, "set_limit"),
            "remove_namespace": ({"iface": "wlan0"}, "remove_namespace"),
            "reconnect": ({}, "refresh_routes"),
            "restart_app": ({"iface": "wlan0", "app": "/usr/bin/x", "env": {}}, "restart_app"),
        }
        for action, (params, target) in calls.items():
            with self.subTest(action=action), \
                    mock.patch.object(server.state, "state_lock", return_value=mock.MagicMock()), \
                    mock.patch.object(server.engine, target, return_value={"ok": 1}) as fn:
                self.assertEqual(server._dispatch(action, params, user), {"ok": 1})
                fn.assert_called_once()


if __name__ == "__main__":
    unittest.main()
