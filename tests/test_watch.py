"""Tests for the root watcher. Teardown is replaced by fakes; no system access."""

from __future__ import annotations

import contextlib
import unittest
from unittest import mock

from intermux import watch


@contextlib.contextmanager
def _no_lock(*_args, **_kwargs):
    yield


class RunOnceTests(unittest.TestCase):
    def test_runs_teardown_under_the_state_lock(self) -> None:
        calls = []

        def fake_teardown():
            calls.append("teardown")
            return {"removed": ["routeA/wlan0"], "torn_down_ifaces": ["wlan0"]}

        with mock.patch.object(watch.state, "state_lock", _no_lock):
            report = watch.run_once(fake_teardown, lambda: calls.append("refresh") or {"refreshed": ["wlan0"]},
                                    check=lambda: True, routes=lambda: True)
        self.assertEqual(calls, ["teardown", "refresh"])
        self.assertEqual(report["refreshed"], ["wlan0"])
        self.assertEqual(report["removed"], ["routeA/wlan0"])

    def test_default_teardown_is_engine_teardown_idle(self) -> None:
        with mock.patch.object(watch.state, "state_lock", _no_lock), \
             mock.patch.object(watch.engine, "teardown_idle", return_value={"removed": []}) as idle, \
             mock.patch.object(watch.engine, "refresh_routes", return_value={"refreshed": []}) as refresh:
            watch.run_once(check=lambda: True, routes=lambda: True)
        idle.assert_called_once_with()
        refresh.assert_called_once_with()

    def test_nothing_idle_skips_the_lock_and_the_state_write(self) -> None:
        def no_lock(*_a, **_k):
            raise AssertionError("the state lock must not be taken")

        with mock.patch.object(watch.state, "state_lock", no_lock), \
             mock.patch.object(watch.engine, "teardown_idle") as idle:
            self.assertEqual(watch.run_once(check=lambda: False, routes=lambda: False), {})
        idle.assert_not_called()

    def test_busy_bindings_only_refresh_routes(self) -> None:
        with mock.patch.object(watch.state, "state_lock", _no_lock), \
             mock.patch.object(watch.engine, "teardown_idle") as idle, \
             mock.patch.object(watch.engine, "refresh_routes", return_value={"refreshed": []}) as refresh:
            watch.run_once(check=lambda: False, routes=lambda: True)
        idle.assert_not_called()
        refresh.assert_called_once_with()


class NeedsTeardownTests(unittest.TestCase):
    @staticmethod
    def _state(bindings=(), subnets=None, adoptions=()):
        from intermux import state

        return state.State(subnets=dict(subnets or {}), bindings=list(bindings), adoptions=list(adoptions))

    @staticmethod
    def _binding(iface, ns):
        from intermux import state

        return state.Binding(iface=iface, app="/usr/bin/x", app_name="x", namespace=ns,
                             subnet="10.200.1.0/24", table=1001, priority=1001)

    def test_busy_namespace_is_not_idle(self) -> None:
        st = self._state([self._binding("wlan0", "ns_a")], {"wlan0": "10.200.1.0/24"})
        self.assertFalse(watch.needs_teardown(st, {11}, {"ns_a": 11}.get, lambda g: []))

    def test_empty_or_missing_namespace_is_idle(self) -> None:
        st = self._state([self._binding("wlan0", "ns_a")], {"wlan0": "10.200.1.0/24"})
        self.assertTrue(watch.needs_teardown(st, {99}, {"ns_a": 11}.get, lambda g: []))
        self.assertTrue(watch.needs_teardown(st, {11}, {}.get, lambda g: []))

    def test_subnet_without_binding_is_idle(self) -> None:
        st = self._state([], {"usb0": "10.200.2.0/24"})
        self.assertTrue(watch.needs_teardown(st, set(), {}.get, lambda g: []))

    def test_adoption_with_empty_group_is_idle(self) -> None:
        from intermux import state

        adoption = state.Adoption(id="ab12cd34", iface="wlan0", app="/usr/bin/x", app_name="x", slot=1,
                                  group="intermux/adopt-ab12cd34", mark=0x101, table=2001, priority=2001, uid=1000)
        st = self._state(adoptions=[adoption])
        self.assertFalse(watch.needs_teardown(st, set(), {}.get, lambda g: [42]))
        self.assertTrue(watch.needs_teardown(st, set(), {}.get, lambda g: []))

    def test_live_netns_inodes_reads_one_stat_per_process(self) -> None:
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as proc:
            os.makedirs(os.path.join(proc, "12", "ns"))
            target = os.path.join(proc, "12", "ns", "net")
            open(target, "w").close()
            os.makedirs(os.path.join(proc, "self"))
            os.makedirs(os.path.join(proc, "13"))  # no ns/net: skipped
            self.assertEqual(watch.live_netns_inodes(proc), {os.stat(target).st_ino})


class LoopTests(unittest.TestCase):
    def test_loop_runs_the_requested_number_of_passes(self) -> None:
        with mock.patch.object(watch, "run_once", return_value={}) as once:
            rc = watch.run(interval=0, iterations=3, sleep=lambda _s: None)
        self.assertEqual(rc, 0)
        self.assertEqual(once.call_count, 3)

    def test_a_failing_pass_does_not_stop_the_watcher(self) -> None:
        results = [RuntimeError("state busy"), {}, {}]

        def flaky():
            outcome = results.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with mock.patch.object(watch, "run_once", side_effect=flaky) as once:
            watch.run(interval=0, iterations=3, sleep=lambda _s: None)
        self.assertEqual(once.call_count, 3)

    def test_sleeps_between_passes(self) -> None:
        slept = []
        with mock.patch.object(watch, "run_once", return_value={}):
            watch.run(interval=2.5, iterations=3, sleep=slept.append)
        self.assertEqual(slept, [2.5, 2.5])  # no sleep after the last pass


class DispatchTests(unittest.TestCase):
    def test_root_may_run_watch_but_not_other_commands(self) -> None:
        from intermux import __main__ as entry

        with mock.patch.object(entry.os, "geteuid", return_value=0), \
             mock.patch("intermux.watch.main", return_value=0) as watch_main:
            self.assertEqual(entry.main(["watch"]), 0)
        watch_main.assert_called_once_with()

        with mock.patch.object(entry.os, "geteuid", return_value=0), \
             mock.patch("intermux.cli.main") as cli_main:
            self.assertEqual(entry.main(["interfaces"]), 1)
        cli_main.assert_not_called()

    def test_watch_refused_when_started_through_pkexec(self) -> None:
        from intermux import __main__ as entry

        with mock.patch.object(entry.os, "geteuid", return_value=0), \
             mock.patch.dict(entry.os.environ, {"PKEXEC_UID": "1000"}), \
             mock.patch("sys.stderr"), \
             mock.patch("intermux.watch.main", return_value=0) as watch_main:
            self.assertEqual(entry.main(["watch"]), 1)
        watch_main.assert_not_called()


if __name__ == "__main__":
    unittest.main()
