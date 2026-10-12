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


if __name__ == "__main__":
    unittest.main()
