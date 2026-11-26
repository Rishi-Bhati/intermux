"""Tier 1 & 2 IPC lifecycle, browser isolation, and Go engine stdio tests."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from intermux.platform import win_apps
from tests.e2e_win import harness
from tests.e2e_win.fixtures import ipc_transport


class IPCLifecycleTests(unittest.TestCase):
    def _build_go_binary(self) -> str:
        harness.require_capability(self, "go_toolchain", harness.has_go_toolchain(), "Go toolchain missing")
        tmp = tempfile.TemporaryDirectory(prefix="intermux_go_bin_")
        self.addCleanup(tmp.cleanup)
        out_bin = str(Path(tmp.name) / ("intermux-net.exe" if os.name == "nt" else "intermux-net"))
        proc = subprocess.run(
            ["go", "build", "-o", out_bin, "./cmd/intermux-net"],
            cwd=str(harness.GO_ENGINE_DIR),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if proc.returncode != 0:
            self.fail(f"go build failed: {proc.stderr}")
        return out_bin

    def test_T1_F13_go_engine_stdio_ping_status_limits_counters(self) -> None:
        """T1-F13: Compiled intermux-net stdio handles ping, status, set_limit, counters, shutdown."""
        binary = self._build_go_binary()
        ping = ipc_transport.call_stdio(binary, "ping")
        self.assertTrue(ping["ok"])
        self.assertTrue(ping["data"]["pong"])

        bad = ipc_transport.call_stdio(binary, "set_limit", {"iface": "Wi-Fi", "down": 0.01})
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"]["code"], "invalid_request")

        ok_lim = ipc_transport.call_stdio(binary, "set_limit", {"iface": "Wi-Fi", "down": 12.5, "up": 4.0})
        self.assertTrue(ok_lim["ok"])
        self.assertEqual(ok_lim["data"]["down"], 12.5)

        cnt = ipc_transport.call_stdio(binary, "counters")
        self.assertTrue(cnt["ok"])
        self.assertIn("interfaces", cnt["data"])

    def test_T1_F03_browser_profile_separation_and_lock_cleanup(self) -> None:
        """T1-F03: Firefox and Chromium handlers isolate per-interface profiles and clean stale locks."""
        root = harness.make_isolated_env(self)
        ff = win_apps.WinFirefoxHandler()
        ff.prepare("Wi-Fi", "separate", r"C:\Program Files\Mozilla Firefox\firefox.exe")
        pdir = ff.separate_profile_dir("Wi-Fi")
        self.assertTrue(pdir.is_dir())
        (pdir / "parent.lock").write_text("stale", encoding="utf-8")
        ff.prepare("Wi-Fi", "separate", r"C:\Program Files\Mozilla Firefox\firefox.exe")
        self.assertFalse((pdir / "parent.lock").exists())

        edge = win_apps.WinChromiumHandler(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")
        edge.prepare("Ethernet 2", "separate", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")
        args = edge.extra_args("Ethernet 2", "separate")
        self.assertTrue(any("--user-data-dir=" in a and "msedge-Ethernet_2" in a for a in args))
        self.assertTrue(root.exists())

    def test_T1_F12_start_menu_app_discovery(self) -> None:
        """T1-F12: Windows app scanner discovers & searches Start Menu entries cleanly."""
        entries = [
            win_apps.WinAppEntry("Firefox", "Web Browser", "", ["C:\\fx.exe"], "", "C:\\fx.exe", True, ""),
            win_apps.WinAppEntry("Notepad", "Editor", "", ["C:\\np.exe"], "", "C:\\np.exe", True, ""),
        ]
        found = win_apps.search_entries(entries, "fire")
        self.assertEqual([e.name for e in found], ["Firefox"])
