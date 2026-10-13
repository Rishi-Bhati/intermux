from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from intermux import config
from intermux.system import procs
from intermux.system.procs import Proc

PY = sys.executable
SLEEP_CODE = "import time; time.sleep(60)"


def _spawn(code: str = SLEEP_CODE, env=None) -> subprocess.Popen:
    return subprocess.Popen([PY, "-c", code], env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def _cleanup(child: subprocess.Popen) -> None:
    if child.poll() is None:
        child.kill()
    child.wait(timeout=5)
    if child.stdout:
        child.stdout.close()


def _proc(pid: int, ppid: int) -> Proc:
    return Proc(pid=pid, ppid=ppid, comm="x", uid=0, exe=None, netns=None)


class ListProcsTests(unittest.TestCase):
    def test_lists_current_process(self):
        me = [p for p in procs.list_procs() if p.pid == os.getpid()]
        self.assertEqual(len(me), 1)
        self.assertEqual(me[0].uid, os.getuid())
        self.assertEqual(me[0].ppid, os.getppid())
        self.assertTrue(me[0].comm)
        self.assertIsInstance(me[0].exe, (str, type(None)))
        self.assertIsInstance(me[0].netns, (int, type(None)))

    def test_lists_child_with_ppid(self):
        child = _spawn()
        try:
            found = [p for p in procs.list_procs() if p.pid == child.pid]
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0].ppid, os.getpid())
        finally:
            _cleanup(child)


class DescendantsTests(unittest.TestCase):
    def setUp(self):
        self.table = [
            _proc(1, 0),
            _proc(10, 1),
            _proc(11, 10),
            _proc(12, 11),
            _proc(20, 1),
        ]

    def test_includes_all_descendants(self):
        self.assertEqual(sorted(procs.descendants([10], self.table)), [10, 11, 12])

    def test_no_duplicates_when_parent_and_child_given(self):
        result = procs.descendants([10, 11], self.table)
        self.assertEqual(sorted(result), [10, 11, 12])
        self.assertEqual(len(result), len(set(result)))

    def test_given_pid_without_process_is_kept(self):
        self.assertEqual(procs.descendants([99], self.table), [99])

    def test_uid_filter_skips_other_users_descendants(self):
        table = [_proc(10, 1), _proc(11, 10), _proc(12, 10)]
        table[2].uid = 4242
        table[0].uid = table[1].uid = 1000
        self.assertEqual(sorted(procs.descendants([10], table, uid=1000)), [10, 11])

    def test_leaf_returns_only_itself(self):
        self.assertEqual(procs.descendants([12], self.table), [12])


class NetnsTests(unittest.TestCase):
    def test_missing_namespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(config, "NETNS_DIR", Path(tmp)):
                self.assertIsNone(procs.netns_inode("missing"))
                self.assertEqual(procs.pids_in_netns("missing"), [])

    def test_existing_handle_inode(self):
        with tempfile.TemporaryDirectory() as tmp:
            handle = Path(tmp) / "ns_1a2b3c4d"
            handle.write_text("")
            with mock.patch.object(config, "NETNS_DIR", Path(tmp)):
                self.assertEqual(procs.netns_inode("ns_1a2b3c4d"), os.stat(handle).st_ino)
                self.assertIsInstance(procs.pids_in_netns("ns_1a2b3c4d"), list)

    def test_rejects_path_traversal_names(self):
        self.assertIsNone(procs.netns_inode(""))
        self.assertIsNone(procs.netns_inode("../etc"))
        self.assertIsNone(procs.netns_inode(".."))


class FindAppProcsTests(unittest.TestCase):
    def test_finds_child_by_exe_and_excludes_self(self):
        child = _spawn()
        try:
            pids = [p.pid for p in procs.find_app_procs(PY)]
            self.assertIn(child.pid, pids)
            self.assertNotIn(os.getpid(), pids)
        finally:
            _cleanup(child)

    def test_explicit_comm_names_match(self):
        child = _spawn()
        try:
            pids = [p.pid for p in procs.find_app_procs("/nonexistent/intermux-app",
                                                        comm_names=[os.path.basename(PY)[:15]])]
            self.assertIn(child.pid, pids)
            self.assertNotIn(os.getpid(), pids)
        finally:
            _cleanup(child)

    def test_unknown_app_matches_nothing_of_ours(self):
        child = _spawn()
        try:
            pids = [p.pid for p in procs.find_app_procs("/nonexistent/intermux-app")]
            self.assertNotIn(child.pid, pids)
        finally:
            _cleanup(child)


class TerminateTests(unittest.TestCase):
    def test_sigterm_stops_child(self):
        child = _spawn()
        try:
            self.assertEqual(procs.terminate([child.pid], grace=5.0), [])
            self.assertEqual(child.wait(timeout=5), -signal.SIGTERM)
        finally:
            _cleanup(child)

    def test_survivor_is_reported_then_force_killed(self):
        code = ("import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "print('ready', flush=True); time.sleep(60)")
        child = _spawn(code)
        try:
            self.assertEqual(child.stdout.readline().strip(), b"ready")
            self.assertEqual(procs.terminate([child.pid], grace=0.5, force=False), [child.pid])
            self.assertEqual(procs.terminate([child.pid], grace=0.5, force=True), [])
            self.assertEqual(child.wait(timeout=5), -signal.SIGKILL)
        finally:
            _cleanup(child)

    def test_never_signals_self_or_init(self):
        self.assertEqual(procs.terminate([os.getpid(), 0, 1], grace=0.0), [])

    def test_empty_and_dead_pids(self):
        self.assertEqual(procs.terminate([]), [])
        child = _spawn("pass")
        child.wait(timeout=5)
        self.assertEqual(procs.terminate([child.pid], grace=0.0), [])
        if child.stdout:
            child.stdout.close()


class ProcEnvironTests(unittest.TestCase):
    def test_reads_child_environment(self):
        env = dict(os.environ, INTERMUX_TEST_VAR="abc")
        child = _spawn(env=env)
        try:
            # The child's environ can be empty for a few milliseconds after exec, so poll.
            value = None
            deadline = time.monotonic() + 5.0
            while value is None and time.monotonic() < deadline:
                value = procs.proc_environ(child.pid).get("INTERMUX_TEST_VAR")
                if value is None:
                    time.sleep(0.05)
            self.assertEqual(value, "abc")
        finally:
            _cleanup(child)

    def test_missing_process_returns_empty(self):
        child = _spawn("pass")
        child.wait(timeout=5)
        self.assertEqual(procs.proc_environ(child.pid), {})
        if child.stdout:
            child.stdout.close()


if __name__ == "__main__":
    unittest.main()


class ZombieTests(unittest.TestCase):
    def test_zombie_and_dead_processes_are_skipped(self) -> None:
        from intermux.system import procs as procs_mod
        import tempfile, os as _os
        with tempfile.TemporaryDirectory() as tmp:
            for state, expect_none in [("Z (zombie)", True), ("X (dead)", True), ("S (sleeping)", False)]:
                pid_dir = _os.path.join(tmp, "p")
                _os.makedirs(pid_dir, exist_ok=True)
                with open(_os.path.join(pid_dir, "status"), "w") as f:
                    f.write(f"Name:\tx\nState:\t{state}\nPPid:\t1\nUid:\t1000\t1000\t1000\t1000\n")
                orig = procs_mod._read_bytes
                procs_mod._read_bytes = lambda path, _d=pid_dir: open(_os.path.join(_d, "status"), "rb").read() if path.endswith("/status") else None
                try:
                    result = procs_mod._status_ids(4242)
                finally:
                    procs_mod._read_bytes = orig
                self.assertEqual(result is None, expect_none, (state, result))
