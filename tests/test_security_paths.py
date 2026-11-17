"""Exercise ownership checks through alternate helper and cleanup paths."""

from __future__ import annotations

import contextlib
import json
import unittest
from unittest import mock

from intermux import engine, state
from intermux.errors import PermissionDeniedError
from intermux.helper import server
from intermux.helper.protocol import PROTOCOL_VERSION
from intermux.system import netns
from intermux.system.session import UserInfo

USER = UserInfo(uid=1000, gid=1000, username="u", home="/home/u")


def _request(action, params):
    return json.dumps({"v": PROTOCOL_VERSION, "action": action, "params": params})


def _binding(uid):
    return state.Binding(iface="wlan0", app="/usr/bin/foreign", app_name="foreign",
                         namespace=netns.namespace_name("wlan0"), subnet="10.200.7.0/24",
                         table=1007, priority=1007, uid=uid)


class HelperOwnershipPathsTests(unittest.TestCase):
    SETTINGS = {"set_dns": {"iface": "wlan0", "dns": ["9.9.9.9"]},
                "set_limit": {"iface": "wlan0", "down": 5.0}}

    def test_settings_resolve_the_requesting_uid_from_pkexec_and_sudo(self):
        for action, params in self.SETTINGS.items():
            for env_key in ("PKEXEC_UID", "SUDO_UID"):
                with self.subTest(action=action, env=env_key), \
                        mock.patch.object(server.session, "user_for_uid", return_value=USER) as resolve, \
                        mock.patch.object(server.state, "state_lock", return_value=mock.MagicMock()), \
                        mock.patch.object(server.engine, action, return_value={}) as fn:
                    response = server.handle(_request(action, params),
                                             euid=0, environ={env_key: str(USER.uid)})
                self.assertTrue(response["ok"], response)
                resolve.assert_called_once_with(USER.uid)
                self.assertEqual(fn.call_args.kwargs["uid"], USER.uid)

    def test_settings_allow_root_without_a_requesting_user(self):
        for action, params in self.SETTINGS.items():
            with self.subTest(action=action), \
                    mock.patch.object(server.state, "state_lock", return_value=mock.MagicMock()), \
                    mock.patch.object(server.engine, action, return_value={}) as fn:
                response = server.handle(_request(action, params), euid=0, environ={})
            self.assertTrue(response["ok"], response)
            self.assertIsNone(fn.call_args.kwargs["uid"])

    def test_invalid_requesting_user_cannot_bypass_ownership_checks(self):
        actions = dict(self.SETTINGS, reset={}, release_adoption={"id": "0123456789abcdef"})
        for action, params in actions.items():
            for raw_uid in ("garbage", ""):
                with self.subTest(action=action, uid=raw_uid), \
                        mock.patch.object(server, "_dispatch") as dispatch:
                    response = server.handle(_request(action, params),
                                             euid=0, environ={"PKEXEC_UID": raw_uid})
                self.assertFalse(response["ok"])
                self.assertEqual(response["error"]["code"], "permission_denied")
                dispatch.assert_not_called()

    def test_zero_requesting_uid_has_root_reset_and_settings_permissions(self):
        actions = dict(self.SETTINGS, reset={"kill_apps": True}, release_adoption={"id": "0123456789abcdef"})
        for action, params in actions.items():
            for env_key in ("PKEXEC_UID", "SUDO_UID"):
                with self.subTest(action=action, env=env_key), \
                        mock.patch.object(server.session, "user_for_uid") as resolve, \
                        mock.patch.object(server.state, "state_lock", return_value=mock.MagicMock()), \
                        mock.patch.object(server.engine, action, return_value={}) as fn:
                    response = server.handle(_request(action, params), euid=0, environ={env_key: "0"})
                    self.assertTrue(response["ok"], response)
                    if action == "reset":
                        fn.assert_called_once_with(kill_apps=False, uid=None)
                    elif action == "release_adoption":
                        fn.assert_called_once_with(params["id"], user=None)
                    else:
                        self.assertIsNone(fn.call_args.kwargs["uid"])
                    resolve.assert_not_called()

    def test_batch_limits_pass_the_requesting_uid(self):
        with mock.patch.object(server.engine, "set_limit", return_value={}) as limit:
            response = server._run_batch([{"op": "set_limit", "iface": "wlan0", "down": 5.0,
                                           "up": None}], USER)
        self.assertTrue(response[0]["ok"])
        limit.assert_called_once_with("wlan0", 5.0, None, uid=USER.uid)


class InterfaceOwnershipPathsTests(unittest.TestCase):
    def test_legacy_binding_owner_requires_the_recorded_namespace(self):
        b = _binding(None)
        b.pid = 123
        for inode, expected in ((77, USER.uid), (88, None), (None, None)):
            with self.subTest(inode=inode), \
                    mock.patch.object(engine.procs, "_status_ids", return_value=(1, USER.uid)), \
                    mock.patch.object(engine.procs, "_netns", return_value=inode), \
                    mock.patch.object(engine.procs, "netns_inode", return_value=77):
                self.assertEqual(engine.binding_owner(b), expected)

    def test_legacy_binding_with_a_missing_namespace_has_no_owner(self):
        b = _binding(None)
        b.pid = 123
        with mock.patch.object(engine.procs, "_status_ids", return_value=(1, USER.uid)), \
                mock.patch.object(engine.procs, "_netns", return_value=None), \
                mock.patch.object(engine.procs, "netns_inode", return_value=None):
            self.assertIsNone(engine.binding_owner(b))

    def test_move_refused_at_destination_leaves_the_source_app_running(self):
        source = _binding(USER.uid)
        destination = _binding(2000)
        destination.iface = "eth0"
        destination.namespace = netns.namespace_name("eth0")
        st = state.State(bindings=[source, destination])
        with mock.patch.object(engine.state, "load_state", return_value=st), \
                mock.patch.object(engine, "_find_interface", return_value=mock.Mock(bindable=True)), \
                mock.patch.object(engine, "stop_app", return_value={"stopped": [], "still_running": []}) as stop, \
                mock.patch.object(engine, "_bind_again", side_effect=PermissionDeniedError("another user")) as bind, \
                mock.patch.object(engine.state, "save_state") as save:
            with self.assertRaises(PermissionDeniedError):
                engine.move_app(source.iface, source.app, destination.iface, user=USER, env={})
        stop.assert_not_called()
        bind.assert_not_called()
        save.assert_not_called()
        self.assertEqual(st.bindings, [source, destination])

    def test_bind_cannot_change_shared_settings_through_a_different_app(self):
        for settings in ({"dns": ["9.9.9.9"]}, {"dns": "auto"}, {"limit": {"down": 5.0, "up": None}}):
            st = state.State(bindings=[_binding(2000)])
            with self.subTest(settings=settings), \
                    mock.patch.object(engine.os.path, "isdir", return_value=True), \
                    mock.patch.object(engine, "_find_interface", return_value=mock.Mock(bindable=True)), \
                    mock.patch.object(engine.state, "load_state", return_value=st), \
                    mock.patch.object(engine.state, "save_state") as save, \
                    mock.patch.object(engine, "_bind_plumbed") as plumbed:
                with self.assertRaises(PermissionDeniedError):
                    engine.bind("/usr/bin/mine", "wlan0", user=USER, env={}, **settings)
            save.assert_not_called()
            plumbed.assert_not_called()
            self.assertEqual((st.dns, st.limits), ({}, {}))

    def test_limit_cannot_change_another_users_adopted_interface(self):
        a = state.Adoption(id="0123456789abcdef", iface="wlan0", app="/usr/bin/x", app_name="x",
                           slot=2, group="intermux/adopt-0123456789abcdef", mark=258, table=2002,
                           priority=2002, uid=2000)
        with mock.patch.object(engine.state, "load_state", return_value=state.State(adoptions=[a])), \
                mock.patch.object(engine.shaping, "set_limit") as limit:
            with self.assertRaises(PermissionDeniedError):
                engine.set_limit("wlan0", 5.0, None, uid=USER.uid)
        limit.assert_not_called()

    def test_reset_preserves_foreign_rules_and_settings_even_when_no_process_is_alive(self):
        b = _binding(2000)
        a = state.Adoption(id="0123456789abcdef", iface="wlan0", app="/usr/bin/x", app_name="x",
                           slot=2, group="intermux/adopt-0123456789abcdef", mark=258, table=2002,
                           priority=2002, uid=2000)
        st = state.State(bindings=[b], adoptions=[a], subnets={b.iface: b.subnet},
                         dns={b.iface: ["9.9.9.9"]}, limits={b.iface: {"down": 5.0, "up": None}})
        with contextlib.ExitStack() as stack:
            targets = {
                "load_state": (engine.state, "load_state", {"return_value": st}),
                "save_state": (engine.state, "save_state", {}),
                "namespaces": (netns, "list_namespaces_matching", {"return_value": [b.namespace]}),
                "veths": (netns, "list_veths_matching", {"return_value": [netns.veth_names(b.iface)[0]]}),
                "pids": (engine.procs, "pids_in_netns", {"return_value": []}),
                "nft_tables": (engine.adopt_sys, "list_nft_tables", {"return_value": ["intermux_adopt2"]}),
                "delete_nft": (engine.adopt_sys, "delete_nft", {}),
                "sweep": (engine.adopt_sys, "sweep_leftovers", {}),
                "rules": (engine.routing, "intermux_rules", {"return_value": []}),
                "nat": (engine.firewall, "list_intermux_rules", {"return_value": []}),
                "release": (engine, "_release_plumbing", {}),
                "teardown": (engine, "_teardown_binding_plumbing", {}),
                "delete_ns": (netns, "delete_namespace", {}),
                "delete_veth": (netns, "delete_veth", {}),
                "stale_etc": (engine, "_remove_stale_etc_netns", {}),
            }
            mocks = {name: stack.enter_context(mock.patch.object(obj, attr, **kw))
                     for name, (obj, attr, kw) in targets.items()}
            response = engine.reset(uid=USER.uid)
        for name in ("delete_nft", "sweep", "release", "teardown", "delete_ns", "delete_veth"):
            mocks[name].assert_not_called()
        kept = mocks["save_state"].call_args.args[0]
        self.assertEqual((kept.bindings, kept.adoptions, kept.subnets, kept.dns, kept.limits),
                         (st.bindings, st.adoptions, st.subnets, st.dns, st.limits))
        self.assertEqual(response["kept_for_other_users"], 1)


if __name__ == "__main__":
    unittest.main()
