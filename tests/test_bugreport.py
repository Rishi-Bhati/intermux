"""Tests for local bug report bundles. Uses temp directories only; no system access."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from intermux import bugreport


def _fake_diagnostics(**overrides):
    base = {
        "intermux": "2.0.0",
        "python": "3.11.4",
        "kernel": "6.5.0",
        "machine": "x86_64",
        "distribution": "Ubuntu 22.04",
        "session": "wayland",
        "interfaces": [{"name": "wlan0", "kind": "wifi", "up": True, "bindable": True}],
        "bindings": [{"iface": "wlan0", "app": "firefox", "profile_mode": "default", "namespace": "ns_1a2b3c4d"}],
        "tools": [{"tool": "ip", "required": True, "present": True}],
        "recent_logs": [{"file": "firefox-wlan0.log", "text": "started in ~/bin/x"}],
    }
    base.update(overrides)
    return base


class MarkdownTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = {"title": "Firefox drops", "description": "It stops", "report_type": "bug",
                        "impact": "high", "frequency": "daily", "steps": "1. open"}

    def test_sections_and_fields_present(self) -> None:
        text = bugreport.build_markdown(self.payload, _fake_diagnostics(), "2026-10-13 10:00:00 UTC")
        self.assertTrue(text.startswith("# Firefox drops"))
        for heading in ("## Description", "## Steps to reproduce", "## Environment", "## Interfaces",
                        "## Bindings", "## Tools", "## Recent logs"):
            self.assertIn(heading, text)
        self.assertIn("- **Impact:** high", text)
        self.assertIn("| wlan0 | wifi | yes | yes |", text)

    def test_missing_steps_and_logs_are_explained(self) -> None:
        payload = {"title": "T", "description": "D", "report_type": "feature"}
        diag = _fake_diagnostics(recent_logs=[])
        text = bugreport.build_markdown(payload, diag, "now")
        self.assertIn("Not provided.", text)
        self.assertIn("No log files found.", text)
        self.assertIn("- **Impact:** not given", text)

    def test_pipe_characters_do_not_break_tables(self) -> None:
        diag = _fake_diagnostics(bindings=[{"iface": "a|b", "app": "x", "profile_mode": "default", "namespace": "n"}])
        text = bugreport.build_markdown(self.payload, diag, "now")
        self.assertIn("| a/b | x | default | n |", text)


class DiagnosticsTests(unittest.TestCase):
    def test_logs_are_redacted_and_limited(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            log_dir = Path(home) / bugreport.LOG_DIR_REL
            log_dir.mkdir(parents=True)
            body = "\n".join(f"line {i} {home}/secret" for i in range(200))
            (log_dir / "app-wlan0.log").write_text(body, encoding="utf-8")
            with mock.patch.object(bugreport, "_interfaces", return_value=[]), \
                 mock.patch.object(bugreport, "_bindings", return_value=[]), \
                 mock.patch.object(bugreport, "_tools", return_value=[]):
                diag = bugreport.collect_diagnostics(home=home)
        entry = diag["recent_logs"][0]
        self.assertNotIn(home, entry["text"])
        self.assertIn("~/secret", entry["text"])
        self.assertLessEqual(len(entry["text"].splitlines()), bugreport.LOG_LINES_PER_FILE)

    def test_failing_sections_degrade_to_notes(self) -> None:
        with mock.patch("intermux.system.netinfo.list_interfaces", side_effect=RuntimeError("boom")):
            result = bugreport._interfaces()
        self.assertEqual(result, [{"error": "RuntimeError"}])


class WriteTests(unittest.TestCase):
    def test_zip_contains_system_report_and_redacted_logs(self) -> None:
        import zipfile

        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as out:
            log_dir = Path(home) / bugreport.LOG_DIR_REL
            log_dir.mkdir(parents=True)
            (log_dir / "firefox-wlan0.log").write_text(f"error at {home}/profile\n", encoding="utf-8")
            with mock.patch.object(bugreport, "_interfaces", return_value=[]), \
                 mock.patch.object(bugreport, "_bindings", return_value=[]), \
                 mock.patch.object(bugreport, "_tools", return_value=[]):
                path = bugreport.write_bug_zip(directory=Path(out), home=home)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
                self.assertIn("system-report.md", names)
                self.assertIn("logs/firefox-wlan0.log", names)
                log_text = archive.read("logs/firefox-wlan0.log").decode("utf-8")
                report_text = archive.read("system-report.md").decode("utf-8")
            self.assertNotIn(home, log_text)
            self.assertIn("~/profile", log_text)
            self.assertIn("# InterMux system report", report_text)

    def test_zip_without_logs_still_writes(self) -> None:
        import zipfile

        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as out:
            with mock.patch.object(bugreport, "_interfaces", return_value=[]), \
                 mock.patch.object(bugreport, "_bindings", return_value=[]), \
                 mock.patch.object(bugreport, "_tools", return_value=[]):
                path = bugreport.write_bug_zip(directory=Path(out), home=home)
            with zipfile.ZipFile(path) as archive:
                self.assertIn("logs/README.txt", archive.namelist())

    def test_default_folder_is_under_home(self) -> None:
        self.assertEqual(bugreport.default_report_dir().name, bugreport.REPORT_DIR_NAME)
        self.assertEqual(bugreport.default_report_dir().parent, Path(os.path.expanduser("~")))


if __name__ == "__main__":
    unittest.main()


class ScrubTests(unittest.TestCase):
    def test_home_replaced_only_when_non_empty(self) -> None:
        self.assertEqual(bugreport.scrub_text("at /opt/h/x", "/opt/h", None), "at ~/x")
        self.assertEqual(bugreport.scrub_text("keep this text", "", None), "keep this text")

    def test_other_home_and_run_user_paths(self) -> None:
        out = bugreport.scrub_text("/home/bob/a /run/user/1000/bus", "/home/me", None)
        self.assertEqual(out, "/home/USER/a /run/user/UID/bus")

    def test_username(self) -> None:
        self.assertEqual(bugreport.scrub_text("login alice ok", "", "alice"), "login USER ok")
        self.assertEqual(bugreport.scrub_text("login a ok", "", "a"), "login a ok")

    def test_ipv4(self) -> None:
        self.assertEqual(bugreport.scrub_text("ip 192.168.1.20 and 10.0.0.1.", "", None),
                         "ip x.x.x.x and x.x.x.x.")
        self.assertEqual(bugreport.scrub_text("999.1.1.1", "", None), "999.1.1.1")

    def test_ipv6(self) -> None:
        self.assertEqual(bugreport.scrub_text("a fe80::1 b", "", None), "a x:x:x b")
        self.assertEqual(bugreport.scrub_text("2001:db8:1:2:3:4:5:6", "", None), "x:x:x")
        self.assertEqual(bugreport.scrub_text("time 12:34:56", "", None), "time 12:34:56")

    def test_mac(self) -> None:
        self.assertEqual(bugreport.scrub_text("aa:bb:cc:dd:ee:ff", "", None), "xx:xx:xx:xx:xx:xx")
        self.assertEqual(bugreport.scrub_text("AA-BB-CC-DD-EE-FF", "", None), "xx:xx:xx:xx:xx:xx")


class UniqueZipTests(unittest.TestCase):
    def test_second_save_does_not_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as out, \
             mock.patch.object(bugreport, "_interfaces", return_value=[]), \
             mock.patch.object(bugreport, "_bindings", return_value=[]), \
             mock.patch.object(bugreport, "_tools", return_value=[]), \
             mock.patch.object(bugreport.time, "strftime", return_value="20240101-000000"):
            first = bugreport.write_bug_zip(directory=Path(out), home=home)
            second = bugreport.write_bug_zip(directory=Path(out), home=home)
            self.assertNotEqual(first, second)
            self.assertTrue(second.name.endswith("-2.zip"))
            self.assertEqual(stat.S_IMODE(os.stat(second).st_mode), 0o600)


class LogFileListingTests(unittest.TestCase):
    def test_vanished_log_does_not_break_the_listing(self):
        import os
        import tempfile
        from pathlib import Path

        from intermux import bugreport

        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "good.log").write_text("x")
            os.symlink(os.path.join(tmp, "gone"), os.path.join(tmp, "dangling.log"))  # stat() fails
            names = sorted(p.name for p in bugreport._log_files(Path(tmp)))
        self.assertEqual(names, ["dangling.log", "good.log"])
        self.assertEqual(bugreport._log_files(Path("/nonexistent/intermux-logs")), [])



class ScrubUrlTokenTests(unittest.TestCase):
    def test_url_query_removed_keeping_host_and_path(self) -> None:
        out = bugreport.scrub_text("GET https://example.com/a/b?user=x&key=abc#frag ok", "", None)
        self.assertEqual(out, "GET https://example.com/a/b?QUERY ok")
        self.assertEqual(bugreport.scrub_text("http://h.test/p?q=1", "", None), "http://h.test/p?QUERY")

    def test_url_without_query_is_kept(self) -> None:
        self.assertEqual(bugreport.scrub_text("see https://example.com/docs", "", None),
                         "see https://example.com/docs")

    def test_report_tokens_removed(self) -> None:
        out = bugreport.scrub_text("tok rsk_0123abcdEF99 and rpk_deadbeef.", "", None)
        self.assertNotIn("0123abcd", out)
        self.assertNotIn("deadbeef", out)
        self.assertEqual(out, "tok TOKEN and TOKEN.")


class LogTailTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = self._tmp.name
        self.log_dir = Path(self.home) / bugreport.LOG_DIR_REL
        self.log_dir.mkdir(parents=True)

    def _logs(self):
        with mock.patch.object(bugreport, "_interfaces", return_value=[]), \
             mock.patch.object(bugreport, "_bindings", return_value=[]), \
             mock.patch.object(bugreport, "_tools", return_value=[]):
            return bugreport.collect_diagnostics(home=self.home)["recent_logs"]

    def test_large_file_only_tail_is_read(self) -> None:
        big = self.log_dir / "big.log"
        with open(big, "wb") as fh:
            fh.write(b"HEAD-MARKER\n")
            fh.write(b"x" * 20 * 1024 * 1024)
            fh.write(b"\nlast line one\nlast line two\n")
        real_read = os.read
        total = []

        def counting_read(fd, n):
            data = real_read(fd, n)
            total.append(len(data))
            return data

        with mock.patch("intermux.bugreport.os.read", side_effect=counting_read):
            logs = self._logs()
        self.assertLessEqual(sum(total), bugreport.LOG_TAIL_BYTES)
        self.assertIn("last line two", logs[0]["text"])
        self.assertNotIn("HEAD-MARKER", logs[0]["text"])

    def test_fifo_is_skipped_without_hanging(self) -> None:
        os.mkfifo(self.log_dir / "pipe.log")
        (self.log_dir / "ok.log").write_text("fine\n", encoding="utf-8")
        logs = self._logs()
        self.assertEqual([e["file"] for e in logs], ["ok.log"])

    def test_symlink_is_skipped(self) -> None:
        secret = Path(self.home) / "secret.txt"
        secret.write_text("TOPSECRET\n", encoding="utf-8")
        os.symlink(secret, self.log_dir / "link.log")
        logs = self._logs()
        self.assertEqual(logs, [])

    def test_zip_skips_fifo_and_symlink(self) -> None:
        import zipfile
        os.mkfifo(self.log_dir / "pipe.log")
        secret = Path(self.home) / "secret.txt"
        secret.write_text("TOPSECRET\n", encoding="utf-8")
        os.symlink(secret, self.log_dir / "link.log")
        (self.log_dir / "ok.log").write_text("fine\n", encoding="utf-8")
        out = Path(self.home) / "out"
        with mock.patch.object(bugreport, "_interfaces", return_value=[]), \
             mock.patch.object(bugreport, "_bindings", return_value=[]), \
             mock.patch.object(bugreport, "_tools", return_value=[]):
            path = bugreport.write_bug_zip(directory=out, home=self.home)
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.startswith("logs/")]
        self.assertEqual(names, ["logs/ok.log"])
