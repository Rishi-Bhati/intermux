"""Release regressions using a temporary cgroup tree, including vanished desktop scopes."""

from __future__ import annotations

import builtins
import contextlib
import errno
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from intermux import engine, state
from intermux.system import adopt
from intermux.system.session import UserInfo

ID = "0123456789abcdef"
UID = 1000
SLICE = "/user.slice/user-1000.slice/user@1000.service/app.slice"
ORIGIN_A = SLICE + "/app-gnome-a-1.scope"
ORIGIN_B = SLICE + "/app-gnome-b-2.scope"


class ReleaseRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.root_procs = self.root / "cgroup.procs"
        self.root_procs.write_text("SENTINEL\n")
        patch = mock.patch.object(adopt, "CGROUP_ROOT", self.root)
        patch.start()
        self.addCleanup(patch.stop)
        self.rel = adopt.group_rel(ID)
        self.group = self.root / self.rel
        self.group.mkdir(parents=True)
        (self.group / "cgroup.procs").write_text("10\n")
        self.leaf = SLICE + "/intermux-released-adopt-" + ID

    def tearDown(self):
        self.assertEqual(self.root_procs.read_text(), "SENTINEL\n")

    def _path(self, rel):
        return self.root / rel.lstrip("/")

    def _scope(self, rel):
        path = self._path(rel)
        path.mkdir(parents=True, exist_ok=True)
        (path / "cgroup.procs").write_text("")
        return path

    @contextlib.contextmanager
    def _write_errors(self, errors):
        real_open = builtins.open

        def open_file(path, *args, **kwargs):
            code = errors.get(Path(path))
            if code is not None:
                raise OSError(code, "simulated cgroup write failure")
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=open_file):
            yield

    def test_missing_own_scope_uses_an_existing_other_origin_before_recreating(self):
        other = self._scope(ORIGIN_B)
        self.assertEqual(adopt.release_group(self.rel, origins={"10": ORIGIN_A, "11": ORIGIN_B}, uid=UID), [10])
        self.assertEqual((other / "cgroup.procs").read_text(), "10")
        self.assertFalse(self._path(ORIGIN_A).exists())
        self.assertFalse(self._path(self.leaf).exists())

    def test_vanished_own_scope_is_recreated_one_level_with_explicit_mode(self):
        self._path(SLICE).mkdir(parents=True)
        real_mkdir = Path.mkdir
        calls = []

        def mkdir(path, *args, **kwargs):
            calls.append((path, args, kwargs))
            return real_mkdir(path, *args, **kwargs)

        with mock.patch.object(Path, "mkdir", new=mkdir):
            self.assertEqual(adopt.release_group(self.rel, origins={"10": ORIGIN_A}, uid=UID), [10])
        self.assertEqual((self._path(ORIGIN_A) / "cgroup.procs").read_text(), "10")
        self.assertIn((self._path(ORIGIN_A), (), {"mode": 0o755}), calls)
        self.assertFalse(self._path(self.leaf).exists())

    def test_child_without_own_origin_recreates_the_first_safe_origin(self):
        self._path(SLICE).mkdir(parents=True)
        origins = {"11": "/user.slice/user-2000.slice/x.scope", "12": ORIGIN_A, "13": ORIGIN_B}
        self.assertEqual(adopt.release_group(self.rel, origins=origins, uid=UID), [10])
        self.assertEqual((self._path(ORIGIN_A) / "cgroup.procs").read_text(), "10")
        self.assertFalse(self._path(ORIGIN_B).exists())

    def test_missing_scope_parent_uses_a_leaf_in_existing_app_slice(self):
        self._path(SLICE).mkdir(parents=True)
        origin = "/user.slice/user-1000.slice/gone.slice/app.scope"
        self.assertEqual(adopt.release_group(self.rel, origins={"10": origin}, uid=UID), [10])
        self.assertFalse(self._path(origin).parent.exists())
        self.assertEqual((self._path(self.leaf) / "cgroup.procs").read_text(), "10")

    def test_old_state_without_origins_uses_the_release_leaf(self):
        self._path(SLICE).mkdir(parents=True)
        self.assertEqual(adopt.release_group(self.rel, uid=UID), [10])
        self.assertEqual((self._path(self.leaf) / "cgroup.procs").read_text(), "10")

    def test_nonexistent_app_slice_is_never_created_recursively(self):
        self._path(SLICE).parent.mkdir(parents=True)
        with self.assertLogs(adopt.log, level="WARNING"):
            self.assertEqual(adopt.release_group(self.rel, uid=UID), [])
        self.assertFalse(self._path(SLICE).exists())
        self.assertEqual((self.group / "cgroup.procs").read_text(), "10\n")

    def test_write_errors_try_the_next_existing_origin(self):
        own = self._scope(ORIGIN_A)
        other = self._scope(ORIGIN_B)
        for code in (errno.ENOENT, errno.EBUSY, errno.EINVAL):
            with self.subTest(errno=code), self._write_errors({own / "cgroup.procs": code}):
                self.assertEqual(adopt.release_group(self.rel, origins={"10": ORIGIN_A, "11": ORIGIN_B},
                                                     uid=UID), [10])
            self.assertEqual((other / "cgroup.procs").read_text(), "10")
        self.assertFalse(self._path(self.leaf).exists())

    def test_scope_vanishing_during_the_write_is_recreated_and_retried(self):
        own = self._scope(ORIGIN_A)
        real_open = builtins.open
        vanished = False

        def open_file(path, *args, **kwargs):
            nonlocal vanished
            if Path(path) == own / "cgroup.procs" and not vanished:
                vanished = True
                (own / "cgroup.procs").unlink()
                own.rmdir()
                raise OSError(errno.ENOENT, "scope vanished before the write")
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=open_file):
            self.assertEqual(adopt.release_group(self.rel, origins={"10": ORIGIN_A}, uid=UID), [10])
        self.assertEqual((own / "cgroup.procs").read_text(), "10")
        self.assertFalse(self._path(self.leaf).exists())

    def test_recreated_scope_that_refuses_processes_falls_back_to_a_leaf(self):
        self._path(SLICE).mkdir(parents=True)
        with self._write_errors({self._path(ORIGIN_A) / "cgroup.procs": errno.EBUSY}):
            self.assertEqual(adopt.release_group(self.rel, origins={"10": ORIGIN_A}, uid=UID), [10])
        self.assertTrue(self._path(ORIGIN_A).is_dir())
        self.assertEqual((self._path(self.leaf) / "cgroup.procs").read_text(), "10")

    def test_no_writable_target_keeps_the_process_and_warns(self):
        self._scope(ORIGIN_A)
        errors = {self._path(ORIGIN_A) / "cgroup.procs": errno.EBUSY,
                  self._path(self.leaf) / "cgroup.procs": errno.EINVAL}
        with self._write_errors(errors), self.assertLogs(adopt.log, level="WARNING"):
            self.assertEqual(adopt.release_group(self.rel, origins={"10": ORIGIN_A}, uid=UID), [])
        self.assertTrue(self.group.is_dir())
        self.assertEqual((self.group / "cgroup.procs").read_text(), "10\n")

    def test_foreign_or_unsafe_origins_never_receive_processes(self):
        for origin in ("/", "/system.slice/x.scope", "/user.slice/user-2000.slice/x.scope",
                       "/user.slice/user-1000.slice/../user-2000.slice/x.scope",
                       "/user.slice/user-1000.slice/intermux/adopt-" + ID):
            with self.subTest(origin=origin), mock.patch.object(adopt, "_write_pid") as write, \
                    self.assertLogs(adopt.log, level="WARNING"):
                self.assertEqual(adopt.release_group(self.rel, origins={"10": origin}, uid=UID), [])
                write.assert_not_called()

    def test_unknown_uid_never_creates_or_writes_a_release_target(self):
        self._scope(ORIGIN_A)
        with mock.patch.object(Path, "mkdir") as mkdir, mock.patch.object(adopt, "_write_pid") as write, \
                self.assertLogs(adopt.log, level="WARNING"):
            self.assertEqual(adopt.release_group(self.rel, origins={"10": ORIGIN_A}), [])
        mkdir.assert_not_called()
        write.assert_not_called()

    def test_engine_removes_the_record_after_recovering_a_vanished_scope(self):
        self._path(SLICE).mkdir(parents=True)
        a = state.Adoption(id=ID, iface="wlan0", app="/usr/bin/x", app_name="x", slot=1,
                           group=self.rel, mark=257, table=2001, priority=2001, uid=UID,
                           pids=[10], origins={"10": ORIGIN_A})
        st = state.State(adoptions=[a])
        real_write = adopt._write_pid

        def move(path, pid):
            if not real_write(path, pid):
                return False
            # A real cgroup.procs write removes the pid from its old group automatically.
            (self.group / "cgroup.procs").unlink()
            return True

        with mock.patch.object(adopt, "_write_pid", side_effect=move), \
                mock.patch.object(adopt, "remove_policy"), mock.patch.object(adopt, "delete_nft"), \
                mock.patch.object(adopt, "forget_marked_connections"), \
                mock.patch.object(engine.state, "load_state", return_value=st), \
                mock.patch.object(engine.state, "save_state") as save, \
                mock.patch.object(engine.procs, "list_procs", return_value=[mock.Mock(pid=10, uid=UID)]):
            out = engine.release_adoption(ID, user=UserInfo(UID, UID, "u", "/home/u"))
        self.assertEqual(out, {"released": [10], "still_running": []})
        self.assertEqual(st.adoptions, [])
        self.assertFalse(self.group.exists())
        save.assert_called_once()


if __name__ == "__main__":
    unittest.main()
