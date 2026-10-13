from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from intermux.state import (
    Binding,
    State,
    load_state,
    save_state,
    state_from_dict,
    state_lock,
    state_to_dict,
)


def _binding(iface: str = "wlan0", app: str = "/usr/bin/firefox") -> Binding:
    return Binding(
        iface=iface,
        app=app,
        app_name=os.path.basename(app),
        namespace="ns_1a2b3c4d",
        subnet="10.200.7.0/24",
        table=1007,
        priority=1007,
        profile_mode="default",
        log_path="/home/u/.local/state/intermux/logs/firefox-wlan0.log",
        created_at="2026-10-13T00:00:00+00:00",
    )


class StateTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "sub" / "state.json"

    def tearDown(self):
        self._tmp.cleanup()


class RoundTripTests(StateTestBase):
    def test_save_then_load_round_trip(self):
        state = State(
            subnets={"wlan0": "10.200.7.0/24"},
            bindings=[_binding()],
            ip_forward_prev="0",
        )
        save_state(state, self.path)
        self.assertTrue(self.path.is_file())
        self.assertEqual(load_state(self.path), state)

    def test_saved_file_mode_and_no_leftover_temp_files(self):
        save_state(State(), self.path)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o644)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ["state.json"])

    def test_save_overwrites_existing(self):
        save_state(State(subnets={"eth0": "10.200.1.0/24"}), self.path)
        save_state(State(subnets={"wlan0": "10.200.2.0/24"}), self.path)
        self.assertEqual(load_state(self.path).subnets, {"wlan0": "10.200.2.0/24"})

    def test_missing_file_gives_empty_state(self):
        self.assertEqual(load_state(self.dir / "absent.json"), State())


class CorruptFileTests(StateTestBase):
    def test_invalid_json_is_quarantined(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertLogs("intermux.state", level="WARNING"):
            result = load_state(self.path)
        self.assertEqual(result, State())
        self.assertFalse(self.path.exists())
        moved = [p.name for p in self.path.parent.iterdir()]
        self.assertEqual(len(moved), 1)
        self.assertTrue(moved[0].startswith("state.json.corrupt-"), moved)

    def test_invalid_utf8_is_quarantined(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"\xff\xfe\x00garbage")
        with self.assertLogs("intermux.state", level="WARNING"):
            self.assertEqual(load_state(self.path), State())
        self.assertFalse(self.path.exists())

    def test_non_object_top_level_is_quarantined(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        with self.assertLogs("intermux.state", level="WARNING"):
            self.assertEqual(load_state(self.path), State())
        self.assertFalse(self.path.exists())


class TolerantParsingTests(unittest.TestCase):
    def test_unknown_keys_ignored(self):
        state = state_from_dict({"version": 1, "future_field": {"x": 1}, "subnets": {}})
        self.assertEqual(state, State())

    def test_missing_keys_defaulted(self):
        state = state_from_dict({})
        self.assertEqual(state.version, 1)
        self.assertEqual(state.subnets, {})
        self.assertEqual(state.bindings, [])
        self.assertIsNone(state.ip_forward_prev)

    def test_binding_defaults_and_unknown_keys(self):
        raw = {
            "bindings": [{
                "iface": "wlan0",
                "app": "/usr/bin/firefox",
                "namespace": "ns_1a2b3c4d",
                "subnet": "10.200.7.0/24",
                "table": "1007",
                "priority": 1007,
                "extra": True,
            }]
        }
        state = state_from_dict(raw)
        self.assertEqual(len(state.bindings), 1)
        b = state.bindings[0]
        self.assertEqual(b.app_name, "firefox")
        self.assertEqual(b.table, 1007)
        self.assertEqual(b.profile_mode, "default")
        self.assertEqual(b.log_path, "")
        self.assertEqual(b.created_at, "")

    def test_malformed_bindings_skipped(self):
        raw = {
            "bindings": [
                "not a dict",
                {"iface": "wlan0", "app": "/usr/bin/firefox", "namespace": "ns_1a2b3c4d",
                 "subnet": "10.200.7.0/24", "table": 1007},          # no priority
                {"iface": "", "app": "/x", "namespace": "ns", "subnet": "10.200.1.0/24",
                 "table": 1, "priority": 1},                          # empty iface
                {"iface": "wlan0", "app": "/x", "namespace": "ns", "subnet": "10.200.1.0/24",
                 "table": "abc", "priority": 1},                      # non-integer table
                _binding_dict(),
            ]
        }
        state = state_from_dict(raw)
        self.assertEqual([b.iface for b in state.bindings], ["wlan0"])

    def test_malformed_subnets_skipped(self):
        state = state_from_dict({"subnets": {"wlan0": "10.200.1.0/24", "eth0": None}})
        self.assertEqual(state.subnets, {"wlan0": "10.200.1.0/24"})

    def test_non_dict_raises_value_error(self):
        with self.assertRaises(ValueError):
            state_from_dict([])  # type: ignore[arg-type]

    def test_ip_forward_prev_coerced_to_str(self):
        self.assertEqual(state_from_dict({"ip_forward_prev": 0}).ip_forward_prev, "0")


def _binding_dict() -> dict:
    return state_to_dict(State(bindings=[_binding()]))["bindings"][0]


class StateHelpersTests(unittest.TestCase):
    def test_state_to_dict_is_json_safe(self):
        state = State(subnets={"wlan0": "10.200.7.0/24"}, bindings=[_binding()])
        data = state_to_dict(state)
        self.assertEqual(json.loads(json.dumps(data)), data)
        self.assertEqual(data["bindings"][0]["iface"], "wlan0")

    def test_bindings_for_iface(self):
        state = State(bindings=[_binding("wlan0"), _binding("eth0", "/usr/bin/chromium"), _binding("wlan0", "/usr/bin/vlc")])
        self.assertEqual([b.app for b in state.bindings_for_iface("wlan0")], ["/usr/bin/firefox", "/usr/bin/vlc"])
        self.assertEqual(state.bindings_for_iface("usb0"), [])

    def test_find_binding(self):
        state = State(bindings=[_binding("wlan0", "/usr/bin/firefox")])
        self.assertIsNotNone(state.find_binding("wlan0", "/usr/bin/firefox"))
        self.assertIsNone(state.find_binding("wlan0", "/usr/bin/vlc"))
        self.assertIsNone(state.find_binding("eth0", "/usr/bin/firefox"))


class StateLockTests(StateTestBase):
    def test_lock_creates_parent_and_file(self):
        lock_path = self.dir / "run" / "helper.lock"
        with state_lock(lock_path):
            self.assertTrue(lock_path.exists())
        self.assertTrue(lock_path.exists())

    def test_lock_reentry_after_release(self):
        lock_path = self.dir / "helper.lock"
        with state_lock(lock_path):
            pass
        with state_lock(lock_path):
            pass


class AdoptionStateTests(unittest.TestCase):
    def _item(self, **over):
        item = {"id": "0123456789abcdef", "iface": "wlan0", "app": "/usr/bin/firefox", "app_name": "firefox",
                "slot": 1, "group": "intermux/adopt-0123456789abcdef", "mark": 257, "table": 2001,
                "priority": 2001, "uid": 1000, "pids": [10, 11], "created_at": "2026-01-01T00:00:00+00:00"}
        item.update(over)
        return item

    def test_round_trip(self):
        from intermux.state import Adoption

        a = Adoption(**self._item())
        st = State(adoptions=[a])
        back = state_from_dict(json.loads(json.dumps(state_to_dict(st))))
        self.assertEqual(back.adoptions, [a])
        self.assertIs(back.find_adoption(a.id) is not None, True)
        self.assertIsNone(back.find_adoption("deadbeef"))
        self.assertEqual(back.adoptions_for_iface("wlan0"), [a])
        self.assertEqual(back.adoptions_for_iface("eth0"), [])

    def test_malformed_entries_skipped(self):
        data = {"adoptions": [
            self._item(),
            "junk",
            self._item(id="BAD"),
            self._item(slot="x"),
            {"id": "0123456789abcdef"},
            self._item(id="fedcba9876543210", pids="nope"),
        ]}
        st = state_from_dict(data)
        self.assertEqual([a.id for a in st.adoptions], ["0123456789abcdef"])

    def test_missing_or_invalid_uid_skipped(self):
        no_uid = self._item()
        del no_uid["uid"]
        data = {"adoptions": [no_uid, self._item(uid="x"), self._item(uid=None), self._item()]}
        st = state_from_dict(data)
        self.assertEqual(len(st.adoptions), 1)
        self.assertEqual(st.adoptions[0].uid, 1000)

    def test_missing_adoptions_key(self):
        self.assertEqual(state_from_dict({}).adoptions, [])


class StateValidationTests(unittest.TestCase):
    def _item(self, **over):
        item = {"iface": "wlan0", "app": "/usr/bin/x", "namespace": "ns_a", "subnet": "10.200.7.0/24",
                "table": 1007, "priority": 1007}
        item.update(over)
        return item

    def test_invalid_subnets_dropped(self):
        for bad in ("garbage", "192.168.5.0/24", "10.200.300.0/24", "10.200.7.0/16", "10.200.0.0/24",
                    "10.200.7.1/24", "10.200.255.0/24"):
            with self.subTest(bad=bad):
                st = state_from_dict({"subnets": {"wlan0": bad, "eth0": "10.200.2.0/24"},
                                      "bindings": [self._item(subnet=bad)]})
                self.assertEqual(st.subnets, {"eth0": "10.200.2.0/24"})
                self.assertEqual(st.bindings, [])

    def test_out_of_range_table_priority_dropped(self):
        for over in ({"table": 0}, {"table": -5}, {"priority": 0}, {"priority": 99999}):
            with self.subTest(over=over):
                self.assertEqual(state_from_dict({"bindings": [self._item(**over)]}).bindings, [])

    def test_valid_binding_kept(self):
        self.assertEqual(len(state_from_dict({"bindings": [self._item()]}).bindings), 1)


class BindingPidTests(unittest.TestCase):
    def _item(self, **over):
        item = {"iface": "wlan0", "app": "/usr/bin/x", "namespace": "ns_a", "subnet": "10.200.7.0/24",
                "table": 1007, "priority": 1007}
        item.update(over)
        return item

    def test_old_state_has_no_pid(self):
        self.assertIsNone(state_from_dict({"bindings": [self._item()]}).bindings[0].pid)

    def test_pid_round_trip(self):
        st = state_from_dict({"bindings": [self._item(pid=4321)]})
        self.assertEqual(st.bindings[0].pid, 4321)
        again = state_from_dict(state_to_dict(st))
        self.assertEqual(again.bindings[0].pid, 4321)

    def test_garbage_pid_tolerated(self):
        for bad in ("abc", True, 0, -3, [1]):
            with self.subTest(bad=bad):
                self.assertIsNone(state_from_dict({"bindings": [self._item(pid=bad)]}).bindings[0].pid)


if __name__ == "__main__":
    unittest.main()
