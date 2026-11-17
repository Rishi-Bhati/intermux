"""Regression tests for the InterMux 2.2 helper hardening (spec: security-fixes.md, items F1 F2 F3 F6 F7 F8 F11).

Written before the fixes. Every system touch is replaced: /proc and the cgroup tree are temp directories,
commands and the engine's system helpers are mocks, as in the existing tests. Nothing here needs root.
"""

from __future__ import annotations

import contextlib
import os
import pathlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from intermux import engine, state, watch
from intermux.apps import firefox
from intermux.errors import InterMuxError, PermissionDeniedError, ValidationError
from intermux.helper import server
from intermux.state import state_lock
from intermux.system import adopt, netns, safefs
from intermux.system.session import UserInfo

USER = UserInfo(uid=1000, gid=1000, username="u", home="/home/u")
OTHER_UID = 2000
ID = "0123456789abcdef"
ID2 = "fedcba9876543210"
ORIGIN_A = "/user.slice/user-1000.slice/app.slice/a.scope"
ORIGIN_B = "/user.slice/user-1000.slice/app.slice/b.scope"
ORIGIN_OTHER_USER = "/user.slice/user-2000.slice/app.slice/a.scope"
V2_LINE = "0::/user.slice/user-1000.slice/app.slice/x.scope\n"
MIXED_CGROUP = ("12:cpu,cpuacct:/user.slice\n"
                "11:memory:/user.slice/user-1000.slice\n"
                "0::/user.slice/user-1000.slice/session-2.scope\n")
V1_ONLY_CGROUP = "12:cpu,cpuacct:/user.slice\n1:name=systemd:/user.slice/user-1000.slice/session-2.scope\n"


def _dead_pid() -> int:
    """The pid of a process that has already exited."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _binding(iface="wlan0", app="/usr/bin/x", uid=None, pid=None):
    b = state.Binding(iface=iface, app=app, app_name=os.path.basename(app), namespace=netns.namespace_name(iface),
                      subnet="10.200.7.0/24", table=1007, priority=1007, pid=pid)
    # Set as an attribute: the field belongs to the fix. Before the fix the assertions fail, not the constructor.
    b.uid = uid
    return b


def _adoption(adopt_id=ID, uid=1000, iface="wlan0", origins=None):
    a = state.Adoption(id=adopt_id, iface=iface, app="/usr/bin/x", app_name="x", slot=1,
                       group=adopt.group_rel(adopt_id), mark=257, table=2001, priority=2001, uid=uid, pids=[5])
    a.origins = dict(origins or {})
    return a


def _patch(tc, target, attr, **kwargs):
    p = mock.patch.object(target, attr, **kwargs)
    started = p.start()
    tc.addCleanup(p.stop)
    return started


def _call_values(call):
    return list(call.args) + list(call.kwargs.values())


def _adopt_patches(tc, st, move=None):
    """Every system call adopt() makes, mocked. Returns the mocks by short name."""
    iface = mock.Mock(bindable=True, gateways=["10.0.0.1"])
    iface.name = "wlan0"
    targets = {
        "find_interface": (engine, "_find_interface", dict(return_value=iface)),
        "get_handler": (engine.apps, "get_handler", dict(return_value=mock.Mock(comm_names=lambda a: []))),
        "find_app_procs": (engine.procs, "find_app_procs", dict(return_value=[mock.Mock(pid=5)])),
        "list_procs": (engine.procs, "list_procs", dict(return_value=[])),
        "descendants": (engine.procs, "descendants", dict(return_value=[5])),
        "start_time": (engine.procs, "start_time", dict(return_value=1)),
        "filter_host_netns": (engine, "filter_host_netns", dict(return_value=[5])),
        "real_uid": (engine, "_real_uid", dict(return_value=1000)),
        "load_state": (engine.state, "load_state", dict(return_value=st)),
        "save_state": (engine.state, "save_state", {}),
        "kernel_slots": (adopt, "kernel_slots", dict(return_value=set())),
        "pid_group": (adopt, "pid_group", dict(return_value=None)),
        "ensure_group": (adopt, "ensure_group", {}),
        "move_pids": (adopt, "move_pids", dict(side_effect=move or (lambda g, p: list(p)))),
        "apply_policy": (adopt, "apply_policy", dict(return_value=[])),
        "apply_nft": (adopt, "apply_nft", {}),
        "release_group": (adopt, "release_group", dict(return_value=[])),
        "delete_nft": (adopt, "delete_nft", {}),
        "remove_policy": (adopt, "remove_policy", {}),
        # create=True: the function is new in this fix, and the patch must not be what fails.
        "read_cgroup_path": (adopt, "read_cgroup_path", dict(return_value=None, create=True)),
    }
    return {name: _patch(tc, target, attr, **kw) for name, (target, attr, kw) in targets.items()}


# ---------------------------------------------------------------------------
# F1: releasing an adoption never moves processes to the root cgroup
# ---------------------------------------------------------------------------

class F1CgroupReleaseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "cgroup.controllers").write_text("cpu memory\n")
        # The ROOT cgroup.procs: any write to it is the escape this item fixes.
        (self.root / "cgroup.procs").write_text("SENTINEL\n")
        _patch(self, adopt, "CGROUP_ROOT", new=self.root)
        self.rel = adopt.group_rel(ID)
        adopt.ensure_group(self.rel)
        (self.root / self.rel / "cgroup.procs").write_text("10\n11\n")
        self.origin_a = self._scope(ORIGIN_A)
        self.origin_b = self._scope(ORIGIN_B)

    def _scope(self, cgroup_path):
        d = self.root / cgroup_path.lstrip("/")
        d.mkdir(parents=True)
        (d / "cgroup.procs").write_text("")
        return d

    def _root_procs(self):
        return (self.root / "cgroup.procs").read_text()

    # parse_cgroup_v2_path

    def test_parse_returns_the_v2_path(self):
        self.assertEqual(adopt.parse_cgroup_v2_path(V2_LINE), "/user.slice/user-1000.slice/app.slice/x.scope")

    def test_parse_takes_the_v2_line_from_a_mixed_file(self):
        self.assertEqual(adopt.parse_cgroup_v2_path(MIXED_CGROUP),
                         "/user.slice/user-1000.slice/session-2.scope")

    def test_parse_without_a_v2_line_is_none(self):
        self.assertIsNone(adopt.parse_cgroup_v2_path(V1_ONLY_CGROUP))
        self.assertIsNone(adopt.parse_cgroup_v2_path(""))

    def test_read_cgroup_path_reads_proc_pid_cgroup(self):
        with tempfile.TemporaryDirectory() as proc:
            (Path(proc) / "4242").mkdir()
            (Path(proc) / "4242" / "cgroup").write_text(V2_LINE)
            self.assertEqual(adopt.read_cgroup_path(4242, proc_dir=proc),
                             "/user.slice/user-1000.slice/app.slice/x.scope")

    def test_read_cgroup_path_is_none_when_unreadable(self):
        with tempfile.TemporaryDirectory() as proc:
            self.assertIsNone(adopt.read_cgroup_path(9999, proc_dir=proc))

    # safe_release_target

    def test_safe_target_accepts_the_callers_own_slice(self):
        path = "/user.slice/user-1000.slice/app.slice/a.scope"
        self.assertEqual(adopt.safe_release_target(path, 1000), path)

    def test_safe_target_rejects_another_users_slice(self):
        for bad in ("/user.slice/user-2000.slice/app.slice/a.scope",
                    "/user.slice/user-10000.slice/app.slice/a.scope"):
            with self.subTest(path=bad):
                self.assertIsNone(adopt.safe_release_target(bad, 1000))

    def test_safe_target_rejects_paths_outside_the_users_slice(self):
        for bad in ("/", "/system.slice/x", "/user.slice", "app.slice/a.scope", ""):
            with self.subTest(path=bad):
                self.assertIsNone(adopt.safe_release_target(bad, 1000))

    def test_safe_target_rejects_traversal_and_unnormalized_paths(self):
        for bad in ("/user.slice/user-1000.slice/../user-2000.slice/app.slice/a.scope",
                    "/user.slice/user-1000.slice//app.slice/a.scope",
                    "/user.slice/user-1000.slice/./app.slice/a.scope",
                    "/user.slice/user-1000.slice/app.slice/a.scope/"):
            with self.subTest(path=bad):
                self.assertIsNone(adopt.safe_release_target(bad, 1000))

    def test_safe_target_rejects_inside_intermux_group_parent(self):
        for bad in ("/user.slice/user-1000.slice/intermux/adopt-0123456789abcdef",
                    "/intermux/adopt-0123456789abcdef"):
            with self.subTest(path=bad):
                self.assertIsNone(adopt.safe_release_target(bad, 1000))

    def test_safe_target_rejects_none(self):
        self.assertIsNone(adopt.safe_release_target(None, 1000))

    # release_group

    def test_each_pid_goes_to_its_own_origin_cgroup(self):
        moved = adopt.release_group(self.rel, pids=[10, 11], origins={"10": ORIGIN_A, "11": ORIGIN_B}, uid=1000)
        self.assertEqual(sorted(moved), [10, 11])
        self.assertEqual((self.origin_a / "cgroup.procs").read_text().split(), ["10"])
        self.assertEqual((self.origin_b / "cgroup.procs").read_text().split(), ["11"])
        self.assertEqual(self._root_procs(), "SENTINEL\n")

    def test_pid_without_origin_goes_to_another_origin_of_the_adoption(self):
        moved = adopt.release_group(self.rel, pids=[10, 11], origins={"10": ORIGIN_A}, uid=1000)
        self.assertEqual(sorted(moved), [10, 11])
        self.assertEqual((self.origin_a / "cgroup.procs").read_text().split(), ["11"])
        self.assertEqual(self._root_procs(), "SENTINEL\n")

    def test_pid_without_any_usable_origin_is_not_written_anywhere(self):
        moved = adopt.release_group(self.rel, pids=[10, 11], origins={"10": "/system.slice/x.scope"}, uid=1000)
        self.assertEqual(moved, [])
        self.assertEqual(self._root_procs(), "SENTINEL\n")
        self.assertEqual((self.origin_a / "cgroup.procs").read_text(), "")
        self.assertFalse((self.root / "system.slice").exists())

    def test_origin_of_another_user_is_never_a_target(self):
        other = self._scope(ORIGIN_OTHER_USER)
        moved = adopt.release_group(self.rel, pids=[10], origins={"10": ORIGIN_OTHER_USER}, uid=1000)
        self.assertEqual(moved, [])
        self.assertEqual((other / "cgroup.procs").read_text(), "")
        self.assertEqual(self._root_procs(), "SENTINEL\n")

    def test_no_origins_means_nothing_is_moved_and_root_is_untouched(self):
        self.assertEqual(adopt.release_group(self.rel, pids=[10, 11]), [])
        self.assertEqual(self._root_procs(), "SENTINEL\n")
        self.assertEqual((self.root / self.rel / "cgroup.procs").read_text(), "10\n11\n")

    # engine callers pass the adoption's origins and uid

    def test_engine_adopt_records_origins_before_the_pids_move(self):
        events = []
        mocks = _adopt_patches(self, state.State(), move=lambda g, p: (events.append("move"), list(p))[1])
        mocks["read_cgroup_path"].side_effect = lambda pid, *a, **k: (events.append("read"), ORIGIN_A)[1]
        out = engine.adopt("wlan0", "/usr/bin/x", user=USER)
        self.assertEqual(events[:2], ["read", "move"])
        self.assertEqual(out["adoption"]["origins"], {"5": ORIGIN_A})

    def test_release_adoption_passes_origins_and_uid_to_release_group(self):
        a = _adoption(origins={"5": ORIGIN_A})
        with mock.patch.object(engine.state, "load_state", return_value=state.State(adoptions=[a])), \
                mock.patch.object(engine.state, "save_state"), \
                mock.patch.object(engine.procs, "list_procs", return_value=[]), \
                mock.patch.object(engine.adopt_sys, "read_group_pids", return_value=[]), \
                mock.patch.object(engine.adopt_sys, "release_group", return_value=[]) as rel, \
                mock.patch.object(engine.adopt_sys, "remove_policy"), \
                mock.patch.object(engine.adopt_sys, "delete_nft"), \
                mock.patch.object(engine.adopt_sys, "forget_marked_connections"):
            engine.release_adoption(ID, user=USER)
        rel.assert_called_once()
        values = _call_values(rel.call_args)
        self.assertIn(a.origins, values)
        self.assertIn(1000, values)

    def test_teardown_idle_passes_origins_and_uid_to_release_group(self):
        a = _adoption(origins={"5": ORIGIN_A})
        with mock.patch.object(engine.state, "load_state", return_value=state.State(adoptions=[a])), \
                mock.patch.object(engine.state, "save_state"), \
                mock.patch.object(engine.adopt_sys, "read_group_pids_or_none", return_value=[]), \
                mock.patch.object(engine.adopt_sys, "release_group", return_value=[]) as rel, \
                mock.patch.object(engine.adopt_sys, "remove_policy"), \
                mock.patch.object(engine.adopt_sys, "delete_nft"), \
                mock.patch.object(engine.adopt_sys, "forget_marked_connections"):
            engine.teardown_idle()
        rel.assert_called_once()
        values = _call_values(rel.call_args)
        self.assertIn(a.origins, values)
        self.assertIn(1000, values)

    # state

    def test_origins_survive_a_state_round_trip(self):
        a = _adoption(origins={"10": ORIGIN_A})
        again = state.state_from_dict(state.state_to_dict(state.State(adoptions=[a])))
        self.assertEqual(again.adoptions[0].origins, {"10": ORIGIN_A})

    def test_state_without_origins_loads_with_an_empty_dict(self):
        raw = state.state_to_dict(state.State(adoptions=[_adoption()]))
        raw["adoptions"][0].pop("origins", None)
        self.assertEqual(state.state_from_dict(raw).adoptions[0].origins, {})

    def test_bad_origin_entries_are_dropped_on_load(self):
        raw = state.state_to_dict(state.State(adoptions=[_adoption()]))
        raw["adoptions"][0]["origins"] = {"12": ORIGIN_A, "abc": ORIGIN_B, "13": 5}
        self.assertEqual(state.state_from_dict(raw).adoptions[0].origins, {"12": ORIGIN_A})


# ---------------------------------------------------------------------------
# F2: a user may only change their own bindings and adoptions
# ---------------------------------------------------------------------------

class F2OwnershipTests(unittest.TestCase):

    # binding uid in state

    def test_binding_uid_round_trips(self):
        st = state.State(bindings=[_binding(uid=1000)])
        self.assertEqual(state.state_from_dict(state.state_to_dict(st)).bindings[0].uid, 1000)

    def test_old_binding_without_uid_loads_as_none(self):
        raw = state.state_to_dict(state.State(bindings=[_binding()]))
        raw["bindings"][0].pop("uid", None)
        self.assertIsNone(state.state_from_dict(raw).bindings[0].uid)

    def test_invalid_binding_uid_loads_as_none(self):
        for bad in (-1, "abc", [1000]):
            with self.subTest(uid=bad):
                raw = state.state_to_dict(state.State(bindings=[_binding()]))
                raw["bindings"][0]["uid"] = bad
                self.assertIsNone(state.state_from_dict(raw).bindings[0].uid)

    # binding_owner

    def test_recorded_uid_is_the_owner(self):
        self.assertEqual(engine.binding_owner(_binding(uid=OTHER_UID)), OTHER_UID)

    def test_recorded_uid_wins_over_a_dead_pid(self):
        self.assertEqual(engine.binding_owner(_binding(uid=OTHER_UID, pid=_dead_pid())), OTHER_UID)

    def test_without_uid_the_live_pid_gives_the_owner(self):
        inode = os.stat("/proc/self/ns/net").st_ino
        with mock.patch.object(engine.procs, "netns_inode", return_value=inode):
            self.assertEqual(engine.binding_owner(_binding(pid=os.getpid())), os.getuid())

    def test_without_uid_and_a_dead_pid_nobody_owns_it(self):
        self.assertIsNone(engine.binding_owner(_binding(pid=_dead_pid())))

    def test_without_uid_or_pid_nobody_owns_it(self):
        self.assertIsNone(engine.binding_owner(_binding()))

    # stop_app / restart_app / move_app

    def _stop_patches(self, st):
        _patch(self, engine.state, "load_state", return_value=st)
        _patch(self, engine.procs, "netns_inode", return_value=None)
        return _patch(self, engine.procs, "terminate", return_value=[])

    def test_stop_app_refuses_a_binding_owned_by_another_user(self):
        st = state.State(bindings=[_binding(uid=OTHER_UID)])
        terminate = self._stop_patches(st)
        with self.assertRaises(PermissionDeniedError) as ctx:
            engine.stop_app("wlan0", "/usr/bin/x", uid=USER.uid)
        self.assertIn("another user", ctx.exception.message)
        terminate.assert_not_called()
        self.assertEqual([(b.uid, b.app) for b in st.bindings], [(OTHER_UID, "/usr/bin/x")])

    def test_stop_app_refuses_an_owner_found_from_a_live_pid(self):
        st = state.State(bindings=[_binding(pid=os.getpid())])
        terminate = self._stop_patches(st)
        _patch(self, engine.procs, "netns_inode", return_value=os.stat("/proc/self/ns/net").st_ino)
        with self.assertRaises(PermissionDeniedError):
            engine.stop_app("wlan0", "/usr/bin/x", uid=os.getuid() + 1)
        terminate.assert_not_called()

    def test_restart_app_refuses_a_binding_owned_by_another_user(self):
        st = state.State(bindings=[_binding(uid=OTHER_UID)])
        terminate = self._stop_patches(st)
        bind = _patch(self, engine, "bind")
        with self.assertRaises(PermissionDeniedError):
            engine.restart_app("wlan0", "/usr/bin/x", user=USER, env={})
        bind.assert_not_called()
        terminate.assert_not_called()

    def test_move_app_refuses_a_binding_owned_by_another_user(self):
        st = state.State(bindings=[_binding(uid=OTHER_UID)])
        self._stop_patches(st)
        _patch(self, engine, "_find_interface", return_value=mock.Mock(bindable=True))
        bind = _patch(self, engine, "bind")
        save = _patch(self, engine.state, "save_state")
        teardown = _patch(self, engine, "_teardown_iface")
        with self.assertRaises(PermissionDeniedError):
            engine.move_app("wlan0", "/usr/bin/x", "eth0", user=USER, env={})
        bind.assert_not_called()
        save.assert_not_called()
        teardown.assert_not_called()

    # bind

    def _bind_patches(self, st):
        iface = mock.Mock(bindable=True, gateways=["192.168.1.1"], ipv4_cidrs=[])
        iface.name = "wlan0"
        _patch(self, engine.os.path, "isdir", return_value=True)
        _patch(self, engine, "_find_interface", return_value=iface)
        _patch(self, engine.apps, "get_handler", return_value=mock.Mock(comm_names=lambda a: []))
        _patch(self, engine.procs, "find_app_procs", return_value=[])
        _patch(self, engine.procs, "netns_inode", return_value=None)
        _patch(self, engine.state, "load_state", return_value=st)
        save = _patch(self, engine.state, "save_state")
        plumbed = _patch(self, engine, "_bind_plumbed", return_value={"binding": {}})
        return save, plumbed

    def test_bind_refuses_when_another_user_runs_the_app_on_the_interface(self):
        st = state.State(subnets={"wlan0": "10.200.7.0/24"}, bindings=[_binding(uid=OTHER_UID)])
        save, plumbed = self._bind_patches(st)
        with self.assertRaises(PermissionDeniedError) as ctx:
            engine.bind("/usr/bin/x", "wlan0", user=USER, env={})
        self.assertIn("another user", ctx.exception.message)
        plumbed.assert_not_called()
        save.assert_not_called()
        self.assertEqual(st.bindings[0].uid, OTHER_UID)

    def test_bind_by_the_same_user_is_allowed(self):
        st = state.State(subnets={"wlan0": "10.200.7.0/24"}, bindings=[_binding(uid=USER.uid)])
        _save, plumbed = self._bind_patches(st)
        engine.bind("/usr/bin/x", "wlan0", user=USER, env={})
        plumbed.assert_called_once()

    # set_dns / set_limit

    def _ns_patches(self, st):
        _patch(self, engine.state, "load_state", return_value=st)
        save = _patch(self, engine.state, "save_state")
        _patch(self, engine.netns, "namespace_exists", return_value=True)
        return save

    def test_set_dns_refused_when_another_user_has_a_binding_on_the_iface(self):
        st = state.State(subnets={"wlan0": "10.200.7.0/24"}, bindings=[_binding(uid=OTHER_UID)])
        save = self._ns_patches(st)
        configure = _patch(self, engine, "configure_namespace_dns", return_value=[])
        with self.assertRaises(PermissionDeniedError) as ctx:
            engine.set_dns("wlan0", ["9.9.9.9"], uid=USER.uid)
        self.assertIn("other users", ctx.exception.message)
        configure.assert_not_called()
        save.assert_not_called()
        self.assertEqual(st.dns, {})

    def test_set_dns_refused_for_another_users_adoption_on_the_iface(self):
        st = state.State(subnets={"wlan0": "10.200.7.0/24"}, adoptions=[_adoption(uid=OTHER_UID)])
        self._ns_patches(st)
        configure = _patch(self, engine, "configure_namespace_dns", return_value=[])
        with self.assertRaises(PermissionDeniedError):
            engine.set_dns("wlan0", ["9.9.9.9"], uid=USER.uid)
        configure.assert_not_called()

    def test_set_dns_allowed_when_everything_on_the_iface_is_the_callers(self):
        st = state.State(subnets={"wlan0": "10.200.7.0/24"}, bindings=[_binding(uid=USER.uid)],
                         adoptions=[_adoption(uid=USER.uid)])
        self._ns_patches(st)
        _patch(self, engine, "configure_namespace_dns", return_value=[])
        out = engine.set_dns("wlan0", ["9.9.9.9"], uid=USER.uid)
        self.assertEqual(out["servers"], ["9.9.9.9"])

    def test_set_dns_without_a_requesting_user_is_not_restricted(self):
        st = state.State(subnets={"wlan0": "10.200.7.0/24"}, bindings=[_binding(uid=OTHER_UID)])
        self._ns_patches(st)
        _patch(self, engine, "configure_namespace_dns", return_value=[])
        self.assertEqual(engine.set_dns("wlan0", ["9.9.9.9"])["servers"], ["9.9.9.9"])

    def test_set_limit_refused_when_another_user_has_a_binding_on_the_iface(self):
        st = state.State(subnets={"wlan0": "10.200.7.0/24"}, bindings=[_binding(uid=OTHER_UID)])
        save = self._ns_patches(st)
        limit = _patch(self, engine.shaping, "set_limit")
        with self.assertRaises(PermissionDeniedError):
            engine.set_limit("wlan0", 5.0, None, uid=USER.uid)
        limit.assert_not_called()
        save.assert_not_called()
        self.assertEqual(st.limits, {})

    def test_set_limit_allowed_for_the_callers_own_binding(self):
        st = state.State(subnets={"wlan0": "10.200.7.0/24"}, bindings=[_binding(uid=USER.uid)])
        self._ns_patches(st)
        _patch(self, engine.shaping, "set_limit")
        engine.set_limit("wlan0", 5.0, None, uid=USER.uid)
        self.assertEqual(st.limits, {"wlan0": {"down": 5.0, "up": None}})

    def test_set_limit_without_a_requesting_user_is_not_restricted(self):
        st = state.State(subnets={"wlan0": "10.200.7.0/24"}, bindings=[_binding(uid=OTHER_UID)])
        self._ns_patches(st)
        _patch(self, engine.shaping, "set_limit")
        self.assertEqual(engine.set_limit("wlan0", 5.0, None)["down"], 5.0)

    def test_helper_passes_the_requesting_uid_to_set_dns_and_set_limit(self):
        requests = {
            "set_dns": {"iface": "wlan0", "dns": ["9.9.9.9"]},
            "set_limit": {"iface": "wlan0", "down": 5.0, "up": None},
        }
        for action, params in requests.items():
            with self.subTest(action=action), \
                    mock.patch.object(server.state, "state_lock", return_value=mock.MagicMock()), \
                    mock.patch.object(server.engine, action, return_value={}) as fn:
                server._dispatch(action, params, USER)
            self.assertIn(USER.uid, _call_values(fn.call_args))

    # reset

    def _reset(self, st, uid):
        saved = []
        mocks = {
            "release": _patch(self, engine, "_release_plumbing"),
            "teardown_binding": _patch(self, engine, "_teardown_binding_plumbing"),
            "delete_namespace": _patch(self, engine.netns, "delete_namespace"),
            "remove_tmpdirs": _patch(self, engine, "_remove_tmpdirs"),
            "remove_stale_etc": _patch(self, engine, "_remove_stale_etc_netns"),
            "list_namespaces": _patch(self, engine.netns, "list_namespaces_matching",
                                      return_value=[b.namespace for b in st.bindings]),
            "list_veths": _patch(self, engine.netns, "list_veths_matching", return_value=[]),
            "delete_veth": _patch(self, engine.netns, "delete_veth"),
            "untrust_veth": _patch(self, engine.firewall, "untrust_veth"),
            "pids_in_netns": _patch(self, engine.procs, "pids_in_netns", return_value=[]),
            "list_nft": _patch(self, engine.adopt_sys, "list_nft_tables", return_value=[]),
            "sweep": _patch(self, engine.adopt_sys, "sweep_leftovers"),
            "rules": _patch(self, engine.routing, "intermux_rules", return_value=[]),
            "nat": _patch(self, engine.firewall, "list_intermux_rules", return_value=[]),
            "load_state": _patch(self, engine.state, "load_state", return_value=st),
            "save_state": _patch(self, engine.state, "save_state", side_effect=lambda s, p: saved.append(s)),
        }
        out = engine.reset(uid=uid)
        return out, mocks, saved

    def test_reset_by_a_user_releases_only_their_adoptions(self):
        st = state.State(adoptions=[_adoption(ID, uid=USER.uid, iface="wlan0"),
                                    _adoption(ID2, uid=OTHER_UID, iface="eth0")])
        _out, mocks, saved = self._reset(st, USER.uid)
        self.assertEqual([c.args[0].id for c in mocks["release"].call_args_list], [ID])
        self.assertEqual([a.id for a in saved[-1].adoptions], [ID2])

    def test_reset_by_a_user_keeps_an_interface_another_user_uses(self):
        st = state.State(subnets={"wlan0": "10.200.7.0/24", "eth0": "10.200.8.0/24"},
                         bindings=[_binding("wlan0", "/usr/bin/a", uid=USER.uid),
                                   _binding("eth0", "/usr/bin/b", uid=OTHER_UID)])
        out, mocks, saved = self._reset(st, USER.uid)
        self.assertEqual([c.args[0].iface for c in mocks["teardown_binding"].call_args_list], ["wlan0"])
        self.assertEqual([c.args[0] for c in mocks["delete_namespace"].call_args_list],
                         [netns.namespace_name("wlan0")])
        self.assertEqual(out["kept_for_other_users"], 1)
        self.assertEqual([b.iface for b in saved[-1].bindings], ["eth0"])

    def test_reset_by_a_user_keeps_an_interface_with_another_users_adoption(self):
        st = state.State(subnets={"wlan0": "10.200.7.0/24"}, bindings=[_binding("wlan0", uid=USER.uid)],
                         adoptions=[_adoption(ID2, uid=OTHER_UID, iface="wlan0")])
        out, mocks, _saved = self._reset(st, USER.uid)
        mocks["teardown_binding"].assert_not_called()
        mocks["delete_namespace"].assert_not_called()
        mocks["release"].assert_not_called()
        self.assertEqual(out["kept_for_other_users"], 1)

    def test_reset_by_root_without_a_user_still_releases_everything(self):
        st = state.State(subnets={"wlan0": "10.200.7.0/24", "eth0": "10.200.8.0/24"},
                         bindings=[_binding("wlan0", uid=USER.uid), _binding("eth0", uid=OTHER_UID)],
                         adoptions=[_adoption(ID, uid=USER.uid), _adoption(ID2, uid=OTHER_UID, iface="eth0")])
        _out, mocks, _saved = self._reset(st, None)
        self.assertEqual(mocks["release"].call_count, 2)
        self.assertEqual(mocks["teardown_binding"].call_count, 2)


# ---------------------------------------------------------------------------
# F3: helper and watcher set their own umask; root-written files get explicit modes
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _recording_os_open():
    """Yield the (path, flags, mode) of every os.open call while still performing it."""
    real_open = os.open
    calls = []

    def spy(path, flags, mode=0o777, *args, **kwargs):
        calls.append((os.fspath(path), flags, mode))
        return real_open(path, flags, mode, *args, **kwargs)

    with mock.patch.object(os, "open", new=spy):
        yield calls


class F3UmaskAndModeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def _umask_is_first(self, module, main):
        events = []

        class Stop(Exception):
            pass

        def record_umask(mask):
            events.append(("umask", mask))
            return 0o022

        def stop_at_logging(*_args, **_kwargs):
            events.append("logging")
            raise Stop()

        with mock.patch.object(module.os, "umask", side_effect=record_umask), \
                mock.patch.object(module.logging, "basicConfig", side_effect=stop_at_logging):
            with self.assertRaises(Stop):
                main()
        self.assertEqual(events[0], ("umask", 0o022), events)

    def test_helper_main_sets_umask_0022_before_anything_else(self):
        self._umask_is_first(server, server.main)

    def test_watch_main_sets_umask_0022_before_anything_else(self):
        self._umask_is_first(watch, watch.main)

    def _assert_created_with_mode(self, calls, name, mode):
        hits = [c for c in calls if c[0].endswith(name) and c[1] & os.O_CREAT]
        self.assertTrue(any(c[1] & os.O_NOFOLLOW and c[2] == mode for c in hits),
                        f"{name} must be created by os.open(..., O_NOFOLLOW, {oct(mode)}); calls: {calls}")

    def test_resolv_conf_is_created_with_mode_0644_and_no_follow(self):
        etc = self.dir / "netns"
        with mock.patch.object(netns.config, "ETC_NETNS_DIR", etc), _recording_os_open() as calls:
            netns.write_resolv_conf("ns_0a0b0c0d", ["1.1.1.1"])
        self._assert_created_with_mode(calls, "resolv.conf", 0o644)

    def test_nsswitch_conf_is_created_with_mode_0644_and_no_follow(self):
        host = self.dir / "nsswitch.conf"
        host.write_text("hosts: files resolve dns\n")
        etc = self.dir / "netns"
        with mock.patch.object(netns.config, "ETC_NETNS_DIR", etc), \
                mock.patch.object(netns, "HOST_NSSWITCH", host), _recording_os_open() as calls:
            netns.write_nsswitch_conf("ns_0a0b0c0d")
        self._assert_created_with_mode(calls, "nsswitch.conf", 0o644)

    def test_resolv_conf_does_not_follow_a_symlink(self):
        etc = self.dir / "netns"
        (etc / "ns_0a0b0c0d").mkdir(parents=True)
        victim = self.dir / "victim"
        victim.write_text("keep\n")
        os.symlink(victim, etc / "ns_0a0b0c0d" / "resolv.conf")
        with mock.patch.object(netns.config, "ETC_NETNS_DIR", etc):
            try:
                netns.write_resolv_conf("ns_0a0b0c0d", ["1.1.1.1"])
            except (OSError, InterMuxError):
                pass
        self.assertEqual(victim.read_text(), "keep\n")


# ---------------------------------------------------------------------------
# F6: the helper lock cannot be held by other users
# ---------------------------------------------------------------------------

class F6LockModeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        # Pinned so a stricter umask in the runner cannot hide a 0644 file.
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)

    def test_new_lock_file_is_created_0600(self):
        lock = self.dir / "helper.lock"
        with state_lock(lock):
            pass
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)

    def test_existing_0644_lock_file_becomes_0600(self):
        lock = self.dir / "helper.lock"
        lock.write_text("")
        os.chmod(lock, 0o644)
        with state_lock(lock):
            pass
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)

    def test_new_lock_directory_is_created_0700(self):
        lock = self.dir / "run" / "helper.lock"
        with state_lock(lock):
            pass
        self.assertEqual(lock.parent.stat().st_mode & 0o777, 0o700)

    def test_existing_lock_directory_is_made_0700(self):
        run = self.dir / "run"
        run.mkdir()
        os.chmod(run, 0o755)
        with state_lock(run / "helper.lock"):
            pass
        self.assertEqual(run.stat().st_mode & 0o777, 0o700)


# ---------------------------------------------------------------------------
# F7: never take over a directory the user does not own
# ---------------------------------------------------------------------------

class F7UserDirTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name) / "home"
        self.base.mkdir()
        self.uid = os.getuid()
        self.gid = os.getgid()
        # Run the root code path. fchown and fchmod are mocked so nothing is really changed.
        _patch(self, safefs, "_is_root", return_value=True)
        self.fchown = _patch(self, safefs.os, "fchown")
        self.fchmod = _patch(self, safefs.os, "fchmod")

    def test_existing_directory_of_another_user_is_refused_untouched(self):
        prof = self.base / "prof"
        prof.mkdir(mode=0o755)           # owned by this process, i.e. not by uid + 1
        with self.assertRaises(ValidationError) as ctx:
            safefs.ensure_user_dir(prof, self.base, self.uid + 1, self.gid)
        self.assertIn("refusing to take over a directory owned by another user", str(ctx.exception))
        self.fchown.assert_not_called()
        self.fchmod.assert_not_called()

    def test_directory_created_by_the_call_is_chowned_as_before(self):
        safefs.ensure_user_dir(self.base / "new" / "prof", self.base, self.uid, self.gid)
        self.assertIn(mock.call(mock.ANY, self.uid, self.gid), self.fchown.call_args_list)
        self.fchmod.assert_called_with(mock.ANY, 0o700)

    def test_existing_directory_already_owned_by_the_user_is_chowned_as_before(self):
        prof = self.base / "prof"
        prof.mkdir(mode=0o755)
        safefs.ensure_user_dir(prof, self.base, self.uid, self.gid)
        self.assertIn(mock.call(mock.ANY, self.uid, self.gid), self.fchown.call_args_list)


# ---------------------------------------------------------------------------
# F8: a Firefox profile outside the home is not even probed
# ---------------------------------------------------------------------------

class F8FirefoxProfileTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.ff = self.home / ".mozilla" / "firefox"
        self.ff.mkdir(parents=True)
        self.outside = self.root / "outside"
        self.outside.mkdir()

    def _lookup(self, ini_text):
        """Run default_profile_dir and record every Path.is_dir call it makes."""
        (self.ff / "profiles.ini").write_text(ini_text)
        real_is_dir = pathlib.Path.is_dir
        probed = []

        def spy(path_self, *args, **kwargs):
            probed.append(path_self)
            return real_is_dir(path_self, *args, **kwargs)

        with mock.patch.object(pathlib.Path, "is_dir", spy):
            found = firefox.default_profile_dir(str(self.home))
        return found, probed

    @staticmethod
    def _same_dir(candidate, target):
        return os.path.realpath(str(candidate)) == os.path.realpath(str(target))

    def test_absolute_profile_path_outside_home_is_not_found_and_not_probed(self):
        ini = f"[Profile0]\nName=x\nIsRelative=0\nPath={self.outside}\nDefault=1\n"
        found, probed = self._lookup(ini)
        self.assertIsNone(found)
        self.assertFalse(any(self._same_dir(p, self.outside) for p in probed), probed)

    def test_relative_traversal_outside_home_is_not_found_and_not_probed(self):
        rel = os.path.relpath(self.outside, self.ff)
        ini = f"[Profile0]\nName=x\nIsRelative=1\nPath={rel}\nDefault=1\n"
        found, probed = self._lookup(ini)
        self.assertIsNone(found)
        self.assertFalse(any(self._same_dir(p, self.outside) for p in probed), probed)

    def test_profile_inside_home_is_still_found(self):
        (self.ff / "abc.default").mkdir()
        found, _probed = self._lookup("[Profile0]\nPath=abc.default\nDefault=1\n")
        self.assertEqual(found, self.ff / "abc.default")


# ---------------------------------------------------------------------------
# F11: at most MAX_ADOPTIONS_PER_USER adoptions per user
# ---------------------------------------------------------------------------

class F11AdoptionCapTests(unittest.TestCase):
    def setUp(self):
        self.st = state.State()
        self.mocks = _adopt_patches(self, self.st)

    def _fill(self, count, uid):
        self.st.adoptions = [_adoption(f"{i:016x}", uid=uid) for i in range(1, count + 1)]

    def test_the_cap_is_twenty(self):
        self.assertEqual(engine.MAX_ADOPTIONS_PER_USER, 20)

    def test_adopt_refused_when_the_caller_already_has_twenty(self):
        self._fill(20, USER.uid)
        with self.assertRaises(ValidationError) as ctx:
            engine.adopt("wlan0", "/usr/bin/x", user=USER)
        self.assertIn("too many adopted apps", ctx.exception.message)
        self.mocks["ensure_group"].assert_not_called()
        self.mocks["move_pids"].assert_not_called()

    def test_adopt_allowed_with_nineteen(self):
        self._fill(19, USER.uid)
        out = engine.adopt("wlan0", "/usr/bin/x", user=USER)
        self.assertEqual(out["adoption"]["uid"], USER.uid)

    def test_other_users_adoptions_do_not_count(self):
        self._fill(20, OTHER_UID)
        out = engine.adopt("wlan0", "/usr/bin/x", user=USER)
        self.assertEqual(out["adoption"]["uid"], USER.uid)


if __name__ == "__main__":
    unittest.main()
