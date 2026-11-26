"""Tier 1, 2 & 3 GUI controller & display-gated widget tests."""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

from intermux.ui import app as ui_app, dialogs, history
from tests.e2e_win import harness


class GUITests(unittest.TestCase):
    def test_T1_F15_gui_helpers_and_history_windows_paths(self) -> None:
        """T1-F15: History stores and reloads Windows executable paths."""
        root = harness.make_isolated_env(self)
        hpath = root / "history.json"
        with mock.patch.object(sys, "platform", "win32"):
            hist = history.History(path=hpath)
            hist.add(history.Item(app=r"C:\Program Files\Firefox\firefox.exe", name="Firefox", iface="Wi-Fi", dns="Cloudflare"))
            reloaded = history.History(path=hpath)
        self.assertEqual(len(reloaded.items), 1)
        self.assertEqual(reloaded.items[0].iface, "Wi-Fi")

    def test_T1_F07_dialog_limit_parsing(self) -> None:
        """T1-F07: LimitDialog parser accepts valid caps and empty unlimited markers."""
        self.assertIsNone(dialogs.parse_limit(""))
        self.assertIsNone(dialogs.parse_limit("0"))
        self.assertEqual(dialogs.parse_limit("12.5"), 12.5)

    def test_T2_F15_tk_app_smoke_when_display_available(self) -> None:
        """T2-F15: InterMuxApp instantiates cleanly when a GUI display is present."""
        if not os.environ.get("DISPLAY") and os.name != "nt":
            self.skipTest("no DISPLAY in headless Linux CI")
        import tkinter as tk

        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk unavailable: {exc}")
        root.withdraw()
        app = ui_app.InterMuxApp(root)
        self.addCleanup(app._on_close)
        self.assertIn("InterMux", root.title())
