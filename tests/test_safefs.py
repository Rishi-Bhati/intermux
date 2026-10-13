"""Tests for symlink-safe directory creation and stale Firefox lock removal. Uses temp dirs; runs without root."""
from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from intermux.apps.firefox import remove_stale_locks
from intermux.errors import ValidationError
from intermux.system import safefs
from intermux.system.safefs import ensure_user_dir

IS_ROOT = os.geteuid() == 0


class _TempTreeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.base = self.root / "home"
        self.base.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()


class EnsureUserDirTests(_TempTreeCase):
    def setUp(self) -> None:
        super().setUp()
        self.uid = os.getuid()
        self.gid = os.getgid()

    def test_refuses_symlinked_component_pointing_outside(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        os.symlink(outside, self.base / ".mozilla")
        with self.assertRaises(ValidationError):
            ensure_user_dir(self.base / ".mozilla" / "firefox" / "intermux-x", self.base, self.uid, self.gid)
        self.assertFalse((outside / "firefox").exists())

    def test_refuses_symlinked_component_pointing_inside(self) -> None:
        real = self.base / "real"
        real.mkdir()
        os.symlink(real, self.base / "link")
        with self.assertRaisesRegex(ValidationError, "symlink"):
            ensure_user_dir(self.base / "link" / "p", self.base, self.uid, self.gid)
        self.assertFalse((real / "p").exists())

    def test_refuses_path_outside_base(self) -> None:
        with self.assertRaises(ValidationError):
            ensure_user_dir(self.root / "elsewhere" / "p", self.base, self.uid, self.gid)
        self.assertFalse((self.root / "elsewhere").exists())

    def test_refuses_base_itself(self) -> None:
        with self.assertRaises(ValidationError):
            ensure_user_dir(self.base, self.base, self.uid, self.gid)

    def test_creates_nested_directory_with_mode(self) -> None:
        target = self.base / ".mozilla" / "firefox" / "intermux-wlan0"
        ensure_user_dir(target, self.base, self.uid, self.gid, 0o700)
        self.assertTrue(target.is_dir())
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o700)

    def test_tightens_existing_directory_mode(self) -> None:
        target = self.base / "existing"
        target.mkdir(mode=0o755)
        os.chmod(target, 0o755)
        ensure_user_dir(target, self.base, self.uid, self.gid, 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o700)

    @unittest.skipUnless(IS_ROOT, "chown assertions need root")
    def test_root_chowns_created_components_and_target(self) -> None:
        target = self.base / "a" / "b"
        ensure_user_dir(target, self.base, 65534, 65534, 0o700)
        for created in (self.base / "a", target):
            self.assertEqual(os.stat(created).st_uid, 65534)
            self.assertEqual(os.stat(created).st_gid, 65534)


class RemoveStaleLocksSafetyTests(_TempTreeCase):
    def test_removes_lock_symlink_inside_home(self) -> None:
        profile = self.base / "prof"
        profile.mkdir()
        os.symlink("IP:1234+", profile / "lock")
        (profile / "prefs.js").write_text("", encoding="utf-8")

        self.assertEqual(remove_stale_locks(profile, str(self.base)), ["lock"])
        self.assertFalse(os.path.lexists(profile / "lock"))
        self.assertTrue((profile / "prefs.js").exists())

    def test_refuses_profile_dir_outside_home(self) -> None:
        outside = self.root / "outside-profile"
        outside.mkdir()
        os.symlink("IP:1234+", outside / "lock")

        self.assertEqual(remove_stale_locks(outside, str(self.base)), [])
        self.assertTrue(os.path.lexists(outside / "lock"))

    def test_refuses_profile_symlink_that_points_outside_home(self) -> None:
        outside = self.root / "outside-profile"
        outside.mkdir()
        os.symlink("IP:1234+", outside / "lock")
        link = self.base / "prof-link"
        os.symlink(outside, link)

        self.assertEqual(remove_stale_locks(link, str(self.base)), [])
        self.assertTrue(os.path.lexists(outside / "lock"))

    def test_never_removes_directories_named_as_locks(self) -> None:
        profile = self.base / "prof"
        (profile / "lock").mkdir(parents=True)

        self.assertEqual(remove_stale_locks(profile, str(self.base)), [])
        self.assertTrue((profile / "lock").is_dir())


if __name__ == "__main__":
    unittest.main()


class NoFollowHelperTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = os.path.realpath(self._tmp.name)

    def test_open_dir_walks_plain_directories(self):
        os.makedirs(os.path.join(self.base, "a", "b"))
        fd = safefs.open_dir_nofollow(os.path.join(self.base, "a", "b"), self.base)
        os.close(fd)

    def test_open_dir_refuses_symlink_and_outside(self):
        os.makedirs(os.path.join(self.base, "real"))
        os.symlink("real", os.path.join(self.base, "ln"))
        with self.assertRaises(ValidationError):
            safefs.open_dir_nofollow(os.path.join(self.base, "ln"), self.base)
        with self.assertRaises(ValidationError):
            safefs.open_dir_nofollow("/tmp", self.base)

    def test_read_file_cap_and_symlink(self):
        p = os.path.join(self.base, "f")
        with open(p, "wb") as fh:
            fh.write(b"abc")
        self.assertEqual(safefs.read_file_nofollow(p, self.base, 10), b"abc")
        with self.assertRaises(ValidationError):
            safefs.read_file_nofollow(p, self.base, 2)
        os.symlink(p, os.path.join(self.base, "l"))
        with self.assertRaises(ValidationError):
            safefs.read_file_nofollow(os.path.join(self.base, "l"), self.base, 10)
