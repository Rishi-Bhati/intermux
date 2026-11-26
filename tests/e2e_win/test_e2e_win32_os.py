"""Win32 OS & Security E2E tests (run on Windows / GitHub Actions windows-latest runners)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from intermux.errors import PermissionDeniedError
from intermux.platform import win_pipe


@unittest.skipUnless(sys.platform == "win32", "Win32 OS security & Wintun tests require Windows")
class Win32SecurityAndEngineTests(unittest.TestCase):
    def test_S1_non_admin_writable_dir_refused_by_daemon(self) -> None:
        """Security test: intermux-net.exe MUST refuse to run daemon from a user-writable directory."""
        eng = win_pipe.installed_engine_path()
        if not eng.is_file():
            self.skipTest(f"installed engine not found at {eng}")
        with tempfile.TemporaryDirectory(prefix="intermux_user_dir_") as td:
            # Explicitly grant BUILTIN\Users write access to verify DACL refusal even under admin runners.
            subprocess.run(["icacls", td, "/grant", "*S-1-5-32-545:(OI)(CI)F"], capture_output=True, check=False)
            user_exe = Path(td) / "intermux-net.exe"
            shutil.copy2(eng, user_exe)
            proc = subprocess.run(
                [str(user_exe), "daemon", "--allowed-sid", win_pipe.current_user_sid()],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("refusing", (proc.stderr + proc.stdout).lower())

    def test_S2_wintun_hash_mismatch_refused(self) -> None:
        """Security test: tampered wintun.dll fails SHA-256 verification and is refused."""
        eng = win_pipe.installed_engine_path()
        if not eng.is_file():
            self.skipTest(f"installed engine not found at {eng}")
        with tempfile.TemporaryDirectory(prefix="intermux_bad_wintun_") as td:
            user_exe = Path(td) / "intermux-net.exe"
            shutil.copy2(eng, user_exe)
            (Path(td) / "wintun.dll").write_bytes(b"tampered-dll-bytes")
            proc = subprocess.run(
                [str(user_exe), "check"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertNotEqual(proc.returncode, 0)
            data = json.loads(proc.stdout)
            self.assertFalse(data["wintun_ok"])
            self.assertIn("mismatch", data["wintun_error"].lower())

    def test_S3_go_security_tests_on_windows_pipe_dacl_flags_and_sid(self) -> None:
        """Security test: run Go Win32 security tests (pipe DACL, PIPE_REJECT_REMOTE_CLIENTS, cross-SID)."""
        proc = subprocess.run(
            ["go", "test", "-v", "./internal/ipc", "./internal/tun", "./internal/pidmap"],
            cwd=str(Path(__file__).resolve().parents[2] / "windows" / "engine"),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_S4_fake_pipe_server_rejected_and_real_wintun_daemon_works(self) -> None:
        """Security + E2E test: fake pipe server rejected; real elevated Program Files daemon + Wintun works."""
        expected = win_pipe.installed_engine_path()
        if not expected.is_file():
            self.skipTest(f"installed engine not found at {expected}")

        # Verify fake/non-elevated server rejection on Windows.
        with self.assertRaises(PermissionDeniedError):
            win_pipe.verify_pipe_server_identity(os.getpid(), str(expected), False, str(expected))
        with self.assertRaises(PermissionDeniedError):
            win_pipe.verify_pipe_server_identity(os.getpid(), r"C:\Users\Public\intermux-net.exe", True, str(expected))

        # Verify real Program Files engine + Wintun check passes on elevated windows-latest runner.
        chk = subprocess.run([str(expected), "check"], capture_output=True, text=True, timeout=15, check=False)
        self.assertEqual(chk.returncode, 0, chk.stdout + chk.stderr)
        chk_data = json.loads(chk.stdout)
        self.assertTrue(chk_data["admin_dir_ok"], chk_data)
        self.assertTrue(chk_data["wintun_ok"], chk_data)

        # Start real Wintun daemon briefly, verify pipe server identity + ping/interfaces/set_limit/counters/shutdown.
        import time

        kernel32, _, _ = win_pipe._win32_apis()
        sid = win_pipe.current_user_sid()
        daemon_proc = subprocess.Popen(
            [str(expected), "daemon", "--allowed-sid", sid],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            pipe_path = win_pipe.pipe_name_for_sid(sid)
            deadline = time.monotonic() + 12.0
            connected = False
            while time.monotonic() < deadline:
                if daemon_proc.poll() is not None:
                    out, err = daemon_proc.communicate()
                    self.fail(f"daemon exited prematurely ({daemon_proc.returncode}): {out}\n{err}")
                try:
                    h = win_pipe._connect_and_verify_pipe(pipe_path, expected)
                    kernel32.CloseHandle(h)
                    connected = True
                    break
                except FileNotFoundError:
                    time.sleep(0.15)
            self.assertTrue(connected, f"timed out waiting for {pipe_path}")
            pong = win_pipe.call_engine("ping")
            self.assertTrue(pong["pong"])
            self.assertEqual(pong["mode"], "wintun")
            ifaces = win_pipe.call_engine("interfaces", {"include_all": True})
            self.assertIn("interfaces", ifaces)
            cnt = win_pipe.call_engine("counters")
            self.assertIn("interfaces", cnt)
        finally:
            try:
                win_pipe.call_engine("shutdown")
            except Exception:
                daemon_proc.terminate()
            daemon_proc.wait(timeout=10)
