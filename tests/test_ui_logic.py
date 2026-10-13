"""Pure UI logic: icon lookup, grid geometry, filtering, colour contrast. No display needed."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from intermux.apps.desktop_entries import AppEntry
from intermux.ui import icons, theme
from intermux.ui.app import (delete_key_allowed, fresh_public_ip, interface_label, row_button_states,
                             still_running_message, queue_add, session_label, status_kind)
from intermux.ui.picker import column_count, ellipsize, filter_tiles, visible_indices


def entry(name, comment="", binary="/usr/bin/x", bindable=True, reason=""):
    return AppEntry(name=name, comment=comment, icon="", exec_argv=[binary], desktop_file=f"{name}.desktop",
                    binary=binary, bindable=bindable, reason=reason)


class ResolveIconPathTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def touch(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        return path

    def test_found_in_hicolor(self):
        expected = self.touch("icons/hicolor/48x48/apps/foo.png")
        self.assertEqual(icons.resolve_icon_path("foo", [self.root / "icons"]), expected)

    def test_prefers_larger_size_order(self):
        self.touch("icons/hicolor/48x48/apps/foo.png")
        wanted = self.touch("icons/hicolor/128x128/apps/foo.png")
        self.assertEqual(icons.resolve_icon_path("foo", [self.root / "icons"]), wanted)

    def test_pixmaps_style_directory(self):
        expected = self.touch("pixmaps/bar.png")
        self.assertEqual(icons.resolve_icon_path("bar", [self.root / "pixmaps"]), expected)

    def test_missing_returns_none(self):
        self.assertIsNone(icons.resolve_icon_path("nothing", [self.root]))
        self.assertIsNone(icons.resolve_icon_path("", [self.root]))

    def test_absolute_path(self):
        path = self.touch("elsewhere/baz.png")
        self.assertEqual(icons.resolve_icon_path(str(path), []), path)
        self.assertIsNone(icons.resolve_icon_path(str(self.root / "gone.png"), []))

    def test_svg_ignored(self):
        self.touch("icons/hicolor/scalable/apps/vec.svg")
        self.assertIsNone(icons.resolve_icon_path("vec", [self.root / "icons"]))


class GridTests(unittest.TestCase):
    def test_minimum_columns(self):
        self.assertEqual(column_count(0), 4)
        self.assertEqual(column_count(300), 4)

    def test_grows_with_width(self):
        self.assertEqual(column_count(780), 5)
        self.assertGreater(column_count(1200), column_count(780))
        self.assertEqual(column_count(1200), 8)

    def test_custom_arguments(self):
        self.assertEqual(column_count(100, tile_width=40, gap=10, minimum=1), 2)

    def test_visible_indices(self):
        self.assertEqual(visible_indices(0, 400, 0, 5), (0, 0))
        start, end = visible_indices(0, 400, 100, 5, margin_rows=0)
        self.assertEqual(start, 0)
        self.assertEqual(end, 15)  # rows 0..2 intersect 0..400 at 144 px per row
        start, end = visible_indices(1440, 400, 100, 5, margin_rows=1)
        self.assertEqual(start, 45)
        self.assertEqual(end, 70)

    def test_ellipsize(self):
        measure = len
        self.assertEqual(ellipsize("short", 10, measure), "short")
        self.assertEqual(ellipsize("abcdefghij", 5, measure), "abcd…")
        self.assertLessEqual(len(ellipsize("x" * 50, 8, measure)), 8)


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.entries = [entry("Firefox", "Web browser", "/usr/bin/firefox"),
                        entry("Gimp", "Image editor", "/usr/bin/gimp"),
                        entry("Chromium", "Web browser", "/usr/bin/chromium"),
                        entry("Steam", "Games", None, False, "Application binary not found")]

    def test_empty_query_returns_all_in_order(self):
        self.assertEqual([e.name for e in filter_tiles(self.entries, "")], ["Firefox", "Gimp", "Chromium", "Steam"])
        self.assertEqual(len(filter_tiles(self.entries, "   ")), 4)

    def test_filters_and_keeps_order(self):
        self.assertEqual([e.name for e in filter_tiles(self.entries, "browser")], ["Firefox", "Chromium"])
        self.assertEqual([e.name for e in filter_tiles(self.entries, "GIMP")], ["Gimp"])
        self.assertEqual(filter_tiles(self.entries, "zzz"), [])


class ThemeAndLabelTests(unittest.TestCase):
    def test_text_contrast_at_least_4_5(self):
        p = theme.PALETTE
        for fg, bg in (("text", "surface"), ("text", "surface_alt"), ("text", "bg"),
                       ("text_muted", "surface"), ("text_muted", "surface_alt"), ("text_muted", "bg"),
                       ("bg", "danger"), ("bg", "warning"), ("bg", "success"), ("accent", "surface")):
            self.assertGreaterEqual(theme.contrast_ratio(p[fg], p[bg]), 4.5, f"{fg} on {bg}")

    def test_combobox_arrow_contrast(self):
        p = theme.PALETTE
        self.assertGreaterEqual(theme.contrast_ratio(theme.COMBO_ARROW, p["surface_alt"]), 4.5)
        face = theme.shade(p["surface_alt"], 0.10)
        self.assertGreaterEqual(theme.contrast_ratio(theme.COMBO_ARROW, face), 4.5)

    def test_queue_add_rejects_duplicates(self):
        items = []
        self.assertTrue(queue_add(items, {"iface": "wlan0", "app": "/usr/bin/a"}))
        self.assertFalse(queue_add(items, {"iface": "wlan0", "app": "/usr/bin/a"}))
        self.assertTrue(queue_add(items, {"iface": "eth0", "app": "/usr/bin/a"}))
        self.assertEqual(len(items), 2)

    def test_shade(self):
        self.assertEqual(theme.shade("#000000", 0.5), "#808080")
        self.assertEqual(theme.shade("#FFFFFF", -0.5), "#808080")

    def test_status_kind(self):
        self.assertEqual(status_kind("Error: boom"), "error")
        self.assertEqual(status_kind("Bound x to wlan0."), "success")
        self.assertEqual(status_kind("Binding x to wlan0..."), "info")

    def test_labels(self):
        iface = SimpleNamespace(name="wlan0", kind="wifi", ipv4_cidrs=["192.168.1.20/24"])
        self.assertEqual(interface_label(iface), "wlan0  Wi-Fi  192.168.1.20")
        self.assertEqual(session_label(SimpleNamespace(session_type="x11", display=":0", wayland_display=None)), "X11")
        self.assertEqual(session_label(SimpleNamespace(session_type="wayland", display=":1", wayland_display="w-0")),
                         "Wayland :1")


class ScrollFrameDisplayTests(unittest.TestCase):
    """Headless wheel/page scrolling check; skipped when Tk cannot open a display."""

    def test_wheel_and_page_scroll(self):
        import tkinter as tk
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"no display: {exc}")
        try:
            root.withdraw()
            frame = theme.ScrollFrame(root, "#ffffff")
            frame.pack(fill="both", expand=True)
            frame.canvas.configure(width=200, height=100)
            for i in range(200):
                tk.Label(frame.inner, text=f"item {i}").grid(row=i, column=0)
            frame.attach_wheel(root)
            root.update_idletasks()
            frame.update_region()
            self.assertEqual(frame.canvas.yview()[0], 0.0)
            wheel_down = SimpleNamespace(num=0, delta=-120)
            frame._wheel(wheel_down)
            after_wheel = frame.canvas.yview()[0]
            self.assertGreater(after_wheel, 0.0)
            frame._wheel(SimpleNamespace(num=5, delta=0))
            self.assertGreater(frame.canvas.yview()[0], after_wheel)
            before_page = frame.canvas.yview()[0]
            frame.scroll_pages(1)
            self.assertGreater(frame.canvas.yview()[0], before_page)
            frame.scroll_to(1.0)
            self.assertLessEqual(frame.canvas.yview()[1], 1.0)
            frame._wheel(wheel_down)
            self.assertLessEqual(frame.canvas.yview()[1], 1.0)
            frame.scroll_to(0.0)
            frame._wheel(SimpleNamespace(num=4, delta=0))
            self.assertEqual(frame.canvas.yview()[0], 0.0)
            frame.detach_wheel()
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()


class AlreadyRunningChoicesTests(unittest.TestCase):
    def test_shift_present_with_existing_choices(self):
        from intermux.ui.app import already_running_choices
        values = [v for _, v in already_running_choices()]
        self.assertEqual(values, ["close", "separate", "shift", None])
        self.assertIn(("Shift current session", "shift"), already_running_choices())


class SnapshotRowsTests(unittest.TestCase):
    def test_rows_use_one_process_table(self):
        from intermux import state
        from intermux.system.procs import Proc
        from intermux.ui.app import snapshot_rows

        st = state.State(bindings=[
            state.Binding(iface="wlan0", app="/usr/bin/a", app_name="a", namespace="ns_a",
                          subnet="10.200.1.0/24", table=1001, priority=1001),
            state.Binding(iface="usb0", app="/usr/bin/b", app_name="b", namespace="ns_b",
                          subnet="10.200.2.0/24", table=1002, priority=1002),
        ], adoptions=[
            state.Adoption(id="ab12cd34", iface="wlan0", app="/usr/bin/c", app_name="c", slot=1,
                           group="intermux/adopt-ab12cd34", mark=0x101, table=2001, priority=2001, uid=1000,
                           pids=[999]),
        ])
        table = [Proc(pid=10, ppid=1, comm="a", uid=1000, exe=None, netns=111)]
        rows, adopted = snapshot_rows(st, table, {"ns_a": 111, "ns_b": 222}.get, lambda g: [55])
        self.assertEqual([r["running"] for r in rows], [True, False])
        self.assertTrue(adopted[0]["alive"])  # the recorded pid is gone, but the cgroup has members
        _, adopted = snapshot_rows(st, table, {}.get, lambda g: [])
        self.assertFalse(adopted[0]["alive"])



class WheelRoutingTests(unittest.TestCase):
    """Two ScrollFrames in one window: a wheel or touchpad event scrolls only the section under the pointer."""

    def setUp(self) -> None:
        try:
            import tkinter as tk
            self.root = tk.Tk()
        except Exception as exc:  # no display in this environment
            self.skipTest(f"no display: {exc}")
        self.root.withdraw()

    def tearDown(self) -> None:
        self.root.destroy()

    def _section(self, rows: int):
        from intermux.ui import theme
        import tkinter as tk
        frame = theme.ScrollFrame(self.root, "#000000")
        frame.pack(fill="both", expand=True)
        labels = []
        for i in range(rows):
            label = tk.Label(frame.inner, text=f"row {i}")
            label.pack(anchor="w")
            labels.append(label)
        frame.attach_wheel(self.root)
        self.root.update_idletasks()
        frame.update_region()
        return frame, labels

    def test_wheel_scrolls_only_the_section_under_the_pointer(self) -> None:
        import tkinter as tk
        self.root.geometry("300x300")
        first, first_labels = self._section(200)
        second, _ = self._section(200)
        self.root.update()
        before = second.canvas.yview()[0]
        first_before = first.canvas.yview()[0]
        first_labels[0].event_generate("<MouseWheel>", delta=-120)
        self.root.update()
        self.assertGreater(first.canvas.yview()[0], first_before)
        self.assertEqual(second.canvas.yview()[0], before)

    def test_button4_and_button5_scroll_up_and_down(self) -> None:
        self.root.geometry("300x300")
        frame, labels = self._section(200)
        self.root.update()
        labels[0].event_generate("<Button-5>")
        self.root.update()
        down = frame.canvas.yview()[0]
        self.assertGreater(down, 0.0)
        labels[0].event_generate("<Button-4>")
        self.root.update()
        self.assertLess(frame.canvas.yview()[0], down)

    def test_detached_section_no_longer_scrolls(self) -> None:
        self.root.geometry("300x300")
        frame, labels = self._section(200)
        self.root.update()
        frame.detach_wheel()
        labels[0].event_generate("<MouseWheel>", delta=-120)
        self.root.update()
        self.assertEqual(frame.canvas.yview()[0], 0.0)


class RowStateTests(unittest.TestCase):
    def test_running_row_all_enabled_when_idle(self):
        self.assertTrue(all(row_button_states(True, True).values()))

    def test_stopped_row_keeps_only_log(self):
        self.assertEqual(row_button_states(False, True),
                         {"verify": False, "stop": False, "log": True, "force": False})

    def test_busy_disables_everything(self):
        self.assertFalse(any(row_button_states(True, False).values()))


class DeleteKeyGuardTests(unittest.TestCase):
    def test_text_inputs_block_delete(self):
        for cls in ("Entry", "TEntry", "TCombobox", "Text"):
            self.assertFalse(delete_key_allowed(cls))

    def test_body_and_unknown_focus_allow_delete(self):
        for cls in (None, "Tk", "Frame", "TButton"):
            self.assertTrue(delete_key_allowed(cls))


class StillRunningTests(unittest.TestCase):
    def test_message_lists_pids_and_is_warning(self):
        text = still_running_message("Firefox", "stop", [12, 34])
        self.assertIn("12, 34", text)
        self.assertEqual(status_kind(text), "warning")


class PublicIpExpiryTests(unittest.TestCase):
    def test_fresh_and_expired(self):
        self.assertEqual(fresh_public_ip(("1.2.3.4", 100.0), 200.0), "1.2.3.4")
        self.assertEqual(fresh_public_ip(("1.2.3.4", 100.0), 400.0), "")
        self.assertEqual(fresh_public_ip(None, 400.0), "")
