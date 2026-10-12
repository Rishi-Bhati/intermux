"""Tests for .desktop discovery and Exec cleaning (temp files only)."""
from __future__ import annotations

import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from intermux.apps.desktop_entries import (
    FLATPAK_REASON,
    MISSING_BINARY_REASON,
    AppEntry,
    clean_exec,
    desktop_dirs,
    parse_desktop_file,
    scan_desktop_entries,
    search_entries,
)

PY = sys.executable


def _entry_text(exec_line: str, extra: str = "", entry_type: str = "Application",
                name: str = "Demo") -> str:
    return (
        "[Desktop Entry]\n"
        f"Type={entry_type}\n"
        f"Name={name}\n"
        "Comment=Demo app\n"
        "Icon=demo-icon\n"
        f"Exec={exec_line}\n"
        f"{extra}"
    )


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def write(self, name: str, text: str, directory: Path = None) -> Path:
        target = (directory or self.dir) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target


class CleanExecTests(unittest.TestCase):
    def test_drops_field_codes(self):
        self.assertEqual(clean_exec("firefox %u"), ["firefox"])
        self.assertEqual(clean_exec("app %F --x %U"), ["app", "--x"])

    def test_drops_every_listed_field_code(self):
        codes = "%f %F %u %U %i %c %k %d %D %n %N %v %m"
        self.assertEqual(clean_exec("a " + codes), ["a"])

    def test_percent_escape_becomes_literal_percent(self):
        self.assertEqual(clean_exec("sh -c 'echo 100%%'"), ["sh", "-c", "echo 100%"])
        self.assertEqual(clean_exec("tool --fmt=%%u"), ["tool", "--fmt=%u"])

    def test_quoted_arguments(self):
        self.assertEqual(clean_exec('"my app" --name "x y" %U'),
                         ["my app", "--name", "x y"])

    def test_empty_input(self):
        self.assertEqual(clean_exec(""), [])
        self.assertEqual(clean_exec("   "), [])

    def test_unbalanced_quotes_return_empty(self):
        self.assertEqual(clean_exec('"unterminated'), [])


class ParseDesktopFileTests(TempDirTestCase):
    def test_bindable_application(self):
        path = self.write("demo.desktop", _entry_text(f"{shlex.quote(PY)} %u"))
        entry = parse_desktop_file(path)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.name, "Demo")
        self.assertEqual(entry.comment, "Demo app")
        self.assertEqual(entry.icon, "demo-icon")
        self.assertEqual(entry.exec_argv, [PY])
        self.assertEqual(entry.binary, PY)
        self.assertTrue(entry.bindable)
        self.assertEqual(entry.reason, "")
        self.assertEqual(entry.desktop_file, str(path))

    def test_rejected_entries_return_none(self):
        cases = {
            "not application": _entry_text(f"{PY}", entry_type="Link"),
            "hidden": _entry_text(f"{PY}", extra="Hidden=true\n"),
            "no display": _entry_text(f"{PY}", extra="NoDisplay=true\n"),
            "no exec": "[Desktop Entry]\nType=Application\nName=Demo\n",
            "no name": "[Desktop Entry]\nType=Application\nExec=" + PY + "\n",
        }
        for label, text in cases.items():
            with self.subTest(label):
                path = self.write(f"{label.replace(' ', '_')}.desktop", text)
                self.assertIsNone(parse_desktop_file(path))

    def test_localized_name_is_ignored(self):
        text = (
            "[Desktop Entry]\nType=Application\nName[de]=Demo DE\nName=Demo\n"
            f"Exec={PY}\n"
        )
        entry = parse_desktop_file(self.write("loc.desktop", text))
        self.assertEqual(entry.name, "Demo")

    def test_other_groups_are_ignored(self):
        text = (
            "[Desktop Entry]\nType=Application\nExec=" + PY + "\n"
            "[Desktop Action New]\nName=Action Name\n"
        )
        self.assertIsNone(parse_desktop_file(self.write("act.desktop", text)))

    def test_flatpak_not_bindable(self):
        path = self.write("fp.desktop", _entry_text("flatpak run org.example.App %U"))
        entry = parse_desktop_file(path)
        self.assertFalse(entry.bindable)
        self.assertEqual(entry.reason, FLATPAK_REASON)
        self.assertEqual(entry.exec_argv, ["flatpak", "run", "org.example.App"])

    def test_missing_binary_not_bindable(self):
        path = self.write("gone.desktop",
                          _entry_text(str(self.dir / "no-such-dir" / "tool")))
        entry = parse_desktop_file(path)
        self.assertIsNone(entry.binary)
        self.assertFalse(entry.bindable)
        self.assertEqual(entry.reason, MISSING_BINARY_REASON)

    def test_unreadable_file_returns_none(self):
        self.assertIsNone(parse_desktop_file(self.dir / "does-not-exist.desktop"))


class ScanDesktopEntriesTests(TempDirTestCase):
    def test_first_directory_wins_and_hidden_shadows(self):
        first = self.dir / "first"
        second = self.dir / "second"
        # Hidden in the first directory shadows the visible copy in the second.
        self.write("shadow.desktop", _entry_text(PY, extra="Hidden=true\n", name="Shadow"), first)
        self.write("shadow.desktop", _entry_text(PY, name="Shadow"), second)
        self.write("beta.desktop", _entry_text(PY, name="beta"), second)

        names = [entry.name for entry in scan_desktop_entries([first, second])]
        self.assertEqual(names, ["beta"])

    def test_duplicate_visible_entry_appears_once_and_sorted(self):
        first = self.dir / "first"
        second = self.dir / "second"
        self.write("zeta.desktop", _entry_text(PY, name="Zeta"), first)
        self.write("alpha.desktop", _entry_text(PY, name="alpha"), first)
        self.write("zeta.desktop", _entry_text(PY, name="Zeta copy"), second)

        names = [entry.name for entry in scan_desktop_entries([first, second])]
        self.assertEqual(names, ["alpha", "Zeta"])

    def test_missing_directory_ignored(self):
        self.assertEqual(scan_desktop_entries([self.dir / "nope"]), [])


class DesktopDirsTests(unittest.TestCase):
    def test_respects_xdg_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"XDG_DATA_HOME": tmp, "XDG_DATA_DIRS": "/opt/x/share:/srv/share"}
            with mock.patch.dict(os.environ, env):
                dirs = desktop_dirs()
        self.assertEqual(dirs[0], Path(tmp) / "applications")
        self.assertIn(Path("/opt/x/share/applications"), dirs)
        self.assertIn(Path("/srv/share/applications"), dirs)
        self.assertIn(Path("/var/lib/snapd/desktop/applications"), dirs)


class SearchEntriesTests(unittest.TestCase):
    def setUp(self):
        self.firefox = AppEntry(
            name="Firefox", comment="Web Browser", icon="", exec_argv=["firefox"],
            desktop_file="/x/firefox.desktop", binary="/usr/bin/firefox",
            bindable=True, reason="")
        self.gimp = AppEntry(
            name="GIMP", comment="Image editor", icon="", exec_argv=["gimp"],
            desktop_file="/x/gimp.desktop", binary="/usr/bin/gimp",
            bindable=True, reason="")
        self.entries = [self.firefox, self.gimp]

    def test_match_name_case_insensitive(self):
        self.assertEqual(search_entries(self.entries, "FIREFOX"), [self.firefox])

    def test_match_comment(self):
        self.assertEqual(search_entries(self.entries, "image"), [self.gimp])

    def test_match_binary(self):
        self.assertEqual(search_entries(self.entries, "/usr/bin/gimp"), [self.gimp])

    def test_empty_query_returns_all(self):
        self.assertEqual(search_entries(self.entries, ""), self.entries)

    def test_no_match(self):
        self.assertEqual(search_entries(self.entries, "nomatch"), [])


if __name__ == "__main__":
    unittest.main()
