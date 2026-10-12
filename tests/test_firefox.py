"""Tests for Firefox profile handling and handler dispatch (no system changes)."""
from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from intermux.apps import ChromiumHandler, LaunchContext, get_handler
from intermux.apps.firefox import (
    FirefoxHandler,
    default_profile_dir,
    parse_default_profile,
    profile_roots,
    remove_stale_locks,
    separate_profile_dir,
)
from intermux.system.session import UserInfo

ROOT = Path("/fake/root")

INI_INSTALL = """
[Install4F96D1932A9F858E]
Default=abc123.default-release
Locked=1

[Profile1]
Name=default-release
IsRelative=1
Path=abc123.default-release
Default=1

[Profile0]
Name=default
IsRelative=1
Path=old.default
"""

INI_TAGGED_PROFILE = """
[General]
StartWithLastProfile=1

[Profile0]
Name=default
IsRelative=1
Path=old.default

[Profile1]
Name=work
IsRelative=1
Path=work.profile
Default=1
"""

INI_ABSOLUTE = """
[Profile0]
Name=custom
IsRelative=0
Path=/data/ff/prof
Default=1
"""

INI_FIRST_PROFILE = """
[Profile0]
Path=first

[Profile1]
Path=second
"""


class ParseDefaultProfileTests(unittest.TestCase):
    def test_install_section_wins(self):
        self.assertEqual(parse_default_profile(INI_INSTALL, ROOT),
                         ROOT / "abc123.default-release")

    def test_tagged_profile_when_no_install(self):
        self.assertEqual(parse_default_profile(INI_TAGGED_PROFILE, ROOT),
                         ROOT / "work.profile")

    def test_absolute_path_not_joined(self):
        self.assertEqual(parse_default_profile(INI_ABSOLUTE, ROOT),
                         Path("/data/ff/prof"))

    def test_first_profile_fallback(self):
        self.assertEqual(parse_default_profile(INI_FIRST_PROFILE, ROOT),
                         ROOT / "first")

    def test_no_profiles_returns_none(self):
        self.assertIsNone(parse_default_profile("", ROOT))
        self.assertIsNone(parse_default_profile("[General]\nStartWithLastProfile=1\n", ROOT))


class PathHelperTests(unittest.TestCase):
    def test_profile_roots(self):
        roots = profile_roots("/home/u")
        self.assertEqual(roots, [
            Path("/home/u/.mozilla/firefox"),
            Path("/home/u/snap/firefox/common/.mozilla/firefox"),
            Path("/home/u/.var/app/org.mozilla.firefox/.mozilla/firefox"),
        ])

    def test_separate_profile_dir(self):
        self.assertEqual(separate_profile_dir("/home/u", "wlan0"),
                         Path("/home/u/.mozilla/firefox/intermux-wlan0"))

    def test_separate_profile_dir_sanitizes_iface(self):
        self.assertEqual(separate_profile_dir("/home/u", "wl an/0"),
                         Path("/home/u/.mozilla/firefox/intermux-wl_an_0"))


class TempHomeTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    def ctx(self, mode: str, iface: str = "wlan0") -> LaunchContext:
        user = UserInfo(uid=os.getuid(), gid=os.getgid(), username="tester", home=str(self.home))
        return LaunchContext(user=user, iface=iface, profile_mode=mode)


class DefaultProfileDirTests(TempHomeTestCase):
    def test_finds_profile_under_native_root(self):
        root = self.home / ".mozilla" / "firefox"
        (root / "abc.default").mkdir(parents=True)
        (root / "profiles.ini").write_text(
            "[Profile0]\nPath=abc.default\nDefault=1\n", encoding="utf-8")
        self.assertEqual(default_profile_dir(str(self.home)), root / "abc.default")

    def test_finds_profile_under_snap_root(self):
        root = self.home / "snap" / "firefox" / "common" / ".mozilla" / "firefox"
        (root / "snapprof").mkdir(parents=True)
        (root / "profiles.ini").write_text(
            "[Profile0]\nPath=snapprof\nDefault=1\n", encoding="utf-8")
        self.assertEqual(default_profile_dir(str(self.home)), root / "snapprof")

    def test_missing_profile_directory_returns_none(self):
        root = self.home / ".mozilla" / "firefox"
        root.mkdir(parents=True)
        (root / "profiles.ini").write_text(
            "[Profile0]\nPath=gone\nDefault=1\n", encoding="utf-8")
        self.assertIsNone(default_profile_dir(str(self.home)))

    def test_no_profiles_ini_returns_none(self):
        self.assertIsNone(default_profile_dir(str(self.home)))


class RemoveStaleLocksTests(TempHomeTestCase):
    def test_removes_dangling_lock_symlink_and_parentlock(self):
        profile = self.home / "prof"
        profile.mkdir()
        os.symlink("IP:1234+", profile / "lock")
        (profile / ".parentlock").write_text("", encoding="utf-8")
        (profile / "prefs.js").write_text("", encoding="utf-8")

        self.assertEqual(sorted(remove_stale_locks(profile, str(self.home))), [".parentlock", "lock"])
        self.assertFalse(os.path.lexists(profile / "lock"))
        self.assertFalse(os.path.lexists(profile / ".parentlock"))
        self.assertTrue((profile / "prefs.js").exists())

    def test_lock_name_that_is_a_directory_is_left_alone(self):
        profile = self.home / "dirprof"
        (profile / "lock").mkdir(parents=True)
        self.assertEqual(remove_stale_locks(profile, str(self.home)), [])
        self.assertTrue((profile / "lock").is_dir())

    def test_nothing_to_remove(self):
        profile = self.home / "empty"
        profile.mkdir()
        self.assertEqual(remove_stale_locks(profile, str(self.home)), [])
        self.assertEqual(remove_stale_locks(self.home / "missing", str(self.home)), [])


class FirefoxHandlerTests(TempHomeTestCase):
    def test_comm_names(self):
        self.assertEqual(FirefoxHandler().comm_names("/usr/bin/firefox"),
                         ["firefox", "firefox-bin"])

    def test_comm_names_include_the_binary_name(self):
        self.assertIn("firefox-esr", FirefoxHandler().comm_names("/usr/lib/firefox-esr/firefox-esr"))

    def test_extra_args_default(self):
        self.assertEqual(FirefoxHandler().extra_args(self.ctx("default")), ["--no-remote"])

    def test_extra_args_separate(self):
        args = FirefoxHandler().extra_args(self.ctx("separate", "wlan0"))
        self.assertEqual(args, [
            "--no-remote", "--profile",
            str(self.home / ".mozilla" / "firefox" / "intermux-wlan0"),
        ])

    def test_prepare_separate_creates_private_dir_and_clears_locks(self):
        pdir = separate_profile_dir(str(self.home), "wlan0")
        pdir.mkdir(parents=True)
        os.symlink("IP:999+", pdir / "lock")

        FirefoxHandler().prepare(self.ctx("separate", "wlan0"), "/usr/bin/firefox")

        self.assertTrue(pdir.is_dir())
        self.assertEqual(stat.S_IMODE(os.stat(pdir).st_mode), 0o700)
        self.assertFalse(os.path.lexists(pdir / "lock"))

    def test_prepare_default_removes_locks_when_not_running(self):
        profile = self.home / ".mozilla" / "firefox" / "p.default"
        profile.mkdir(parents=True)
        (self.home / ".mozilla" / "firefox" / "profiles.ini").write_text(
            "[Profile0]\nPath=p.default\nDefault=1\n", encoding="utf-8")
        os.symlink("IP:1+", profile / "lock")

        with mock.patch("intermux.system.procs.find_app_procs", return_value=[]):
            FirefoxHandler().prepare(self.ctx("default"), "/usr/bin/firefox")

        self.assertFalse(os.path.lexists(profile / "lock"))

    def test_prepare_default_keeps_locks_when_running(self):
        profile = self.home / ".mozilla" / "firefox" / "p.default"
        profile.mkdir(parents=True)
        (self.home / ".mozilla" / "firefox" / "profiles.ini").write_text(
            "[Profile0]\nPath=p.default\nDefault=1\n", encoding="utf-8")
        os.symlink("IP:1+", profile / "lock")

        with mock.patch("intermux.system.procs.find_app_procs", return_value=[mock.Mock()]):
            FirefoxHandler().prepare(self.ctx("default"), "/usr/bin/firefox")

        self.assertTrue(os.path.lexists(profile / "lock"))


class GetHandlerTests(unittest.TestCase):
    def test_firefox_dispatch(self):
        handler = get_handler("/usr/lib/firefox/firefox")
        self.assertIsInstance(handler, FirefoxHandler)
        self.assertEqual(handler.name, "firefox")
        self.assertTrue(handler.supported)

    def test_chromium_supported_and_recognises_running_instances(self):
        handler = get_handler("/opt/brave.com/brave/brave-browser")
        self.assertIsInstance(handler, ChromiumHandler)
        self.assertEqual(handler.name, "chromium")
        self.assertTrue(handler.supported)
        names = handler.comm_names("/usr/bin/brave-origin")
        self.assertIn("brave", names)
        self.assertIn("brave-origin", names)

    def test_chromium_separate_profile_uses_its_own_data_dir(self):
        from intermux.apps import LaunchContext
        from intermux.system.session import UserInfo

        user = UserInfo(uid=1000, gid=1000, username="tester", home="/home/tester")
        handler = ChromiumHandler()
        default = LaunchContext(user=user, iface="wlan0", profile_mode="default")
        separate = LaunchContext(user=user, iface="wlan0", profile_mode="separate")
        self.assertEqual(handler.extra_args(default), [])
        args = handler.extra_args(separate)
        self.assertEqual(len(args), 1)
        self.assertTrue(args[0].startswith("--user-data-dir=/home/tester/.local/state/intermux/profiles/"))
        self.assertIn("wlan0", args[0])

    def test_chromium_family_names_do_not_cover_other_browsers(self):
        brave = get_handler("/opt/brave.com/brave/brave-browser").comm_names("/opt/brave.com/brave/brave-browser")
        self.assertIn("brave", brave)
        self.assertNotIn("chrome", brave)
        self.assertNotIn("chromium", brave)
        chrome = get_handler("/usr/bin/google-chrome-stable")
        self.assertIsInstance(chrome, ChromiumHandler)
        self.assertIn("chrome", chrome.comm_names("/usr/bin/google-chrome-stable"))
        self.assertNotIn("brave", chrome.comm_names("/usr/bin/google-chrome-stable"))
        self.assertIsInstance(get_handler("/usr/bin/microsoft-edge-stable"), ChromiumHandler)

    def test_separate_profiles_are_per_browser(self):
        from intermux.apps import LaunchContext
        from intermux.system.session import UserInfo

        user = UserInfo(uid=1000, gid=1000, username="tester", home="/home/tester")
        ctx = LaunchContext(user=user, iface="wlan0", profile_mode="separate")
        brave = get_handler("/usr/bin/brave").profile_dir(ctx)
        chrome = get_handler("/usr/bin/google-chrome-stable").profile_dir(ctx)
        self.assertNotEqual(brave, chrome)
        self.assertEqual(brave.name, "brave-wlan0")
        self.assertEqual(chrome.name, "chrome-wlan0")

    def test_chromium_profile_dir_error_is_reported(self):
        from intermux.apps import LaunchContext
        from intermux.errors import ConflictError
        from intermux.system import safefs
        from intermux.system.session import UserInfo

        user = UserInfo(uid=1000, gid=1000, username="tester", home="/home/tester")
        ctx = LaunchContext(user=user, iface="wlan0", profile_mode="separate")
        with mock.patch.object(safefs, "ensure_user_dir", side_effect=PermissionError(13, "Permission denied")):
            with self.assertRaises(ConflictError) as caught:
                get_handler("/usr/bin/brave").prepare(ctx, "/usr/bin/brave")
        self.assertEqual(caught.exception.code, "profile_unwritable")


if __name__ == "__main__":
    unittest.main()
