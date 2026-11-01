"""Tests for the live traffic counters and the network view's pure helpers. No system access."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from intermux.system import traffic
from intermux.ui import app as appmod
from intermux.ui import graph, monitor, theme


def _fake_dev(base: Path, name: str, rx: int, tx: int, speed: str = "") -> None:
    stats = base / name / "statistics"
    stats.mkdir(parents=True)
    (stats / "rx_bytes").write_text(f"{rx}\n")
    (stats / "tx_bytes").write_text(f"{tx}\n")
    if speed:
        (base / name / "speed").write_text(speed + "\n")


class CounterTests(unittest.TestCase):
    def test_reads_existing_devices_and_skips_missing_or_bad_names(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            _fake_dev(base, "eth0", 100, 50)
            found = traffic.read_counters(["eth0", "nope", "../etc", ""], base=base)
        self.assertEqual(found, {"eth0": traffic.Counters(100, 50)})

    def test_link_speed(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            _fake_dev(base, "eth0", 0, 0, "1000")
            _fake_dev(base, "wlan0", 0, 0, "-1")
            self.assertEqual(traffic.link_speed_bytes("eth0", base), 125_000_000)
            self.assertIsNone(traffic.link_speed_bytes("wlan0", base))
            self.assertIsNone(traffic.link_speed_bytes("missing", base))

    def test_veth_counters_are_swapped(self):
        c = traffic.Counters(rx=10, tx=99)
        self.assertEqual(traffic.iface_sample(c), (10, 99))
        self.assertEqual(traffic.veth_sample(c), (99, 10))


class RateTrackerTests(unittest.TestCase):
    def test_rates_history_and_transferred(self):
        t = traffic.RateTracker(history=4)
        t.update({"eth0": (1000, 100)}, now=0.0)
        self.assertEqual(t.rate("eth0"), (0.0, 0.0))
        t.update({"eth0": (3000, 300)}, now=2.0)
        self.assertEqual(t.rate("eth0"), (1000.0, 100.0))
        self.assertEqual(len(t.history("eth0")), 4)
        self.assertEqual(t.transferred("eth0"), (2000, 200))
        self.assertEqual(t.peak("eth0"), 1000.0)

    def test_counter_reset_never_goes_negative(self):
        t = traffic.RateTracker(history=3)
        t.update({"veth": (5000, 5000)}, now=0.0)
        t.update({"veth": (10, 10)}, now=1.0)
        self.assertEqual(t.rate("veth"), (0.0, 0.0))
        t.update({"veth": (110, 60)}, now=2.0)
        self.assertEqual(t.rate("veth"), (100.0, 50.0))
        self.assertEqual(t.transferred("veth"), (100, 50))

    def test_missing_names_are_forgotten(self):
        t = traffic.RateTracker()
        t.update({"a": (1, 1), "b": (1, 1)}, now=0.0)
        t.update({"a": (2, 2)}, now=1.0)
        self.assertEqual(t.names(), ["a"])
        self.assertEqual(t.transferred("b"), (0, 0))


class FormatTests(unittest.TestCase):
    def test_nice_ceiling_uses_round_binary_units(self):
        self.assertEqual(traffic.nice_ceiling(0), traffic.MIN_SCALE)
        self.assertEqual(traffic.nice_ceiling(3 * 1024 * 1024), 5 * 1024 * 1024)
        self.assertEqual(traffic.nice_ceiling(150 * 1024), 200 * 1024)
        self.assertEqual(traffic.nice_ceiling(10 * 1024, floor=1024), 10 * 1024)

    def test_rates_and_sizes(self):
        self.assertEqual(traffic.split_rate(0), ("0", "B/s"))
        self.assertEqual(traffic.split_rate(2.5 * 1024 * 1024), ("2.5", "MB/s"))
        self.assertEqual(traffic.format_rate(512 * 1024), "512 KB/s")
        self.assertEqual(traffic.format_bytes(1536), "1.5 KB")
        self.assertEqual(traffic.scale_label(5 * 1024 * 1024), "5 MB/s")


def _iface(name, kind="ethernet", up=True, bindable=True, virtual=False, cidrs=("192.168.1.2/24",)):
    return SimpleNamespace(name=name, kind=kind, up=up, bindable=bindable, is_virtual=virtual,
                           ipv4_cidrs=list(cidrs))


class MonitorHelperTests(unittest.TestCase):
    def test_visible_lanes_order_and_filter(self):
        lanes = monitor.visible_lanes([
            _iface("wlan0", "wifi"), _iface("lo"), _iface("docker0", "virtual", virtual=True),
            _iface("tun0", "virtual", bindable=False, virtual=True), _iface("eth1", up=False, bindable=False),
            _iface("eth0"), _iface("usb0", "usb", up=False, bindable=False),
        ], bound=["usb0"])
        self.assertEqual([i.name for i in lanes], ["eth0", "wlan0", "usb0", "tun0"])

    def test_lane_state(self):
        self.assertEqual(monitor.lane_state(False, False, 0, 0, 0, None), ("off", "Disconnected"))
        self.assertEqual(monitor.lane_state(True, True, 2, 5e6, 0, None), ("ok", "Routing 2 apps"))
        self.assertEqual(monitor.lane_state(True, True, 1, 0, 0, None), ("ok", "Routing 1 app"))
        self.assertEqual(monitor.lane_state(True, True, 1, 120e6, 0, 125e6), ("hot", "Saturated"))
        self.assertEqual(monitor.lane_state(True, True, 0, 1e6, 0, None), ("active", "Active"))
        self.assertEqual(monitor.lane_state(True, True, 0, 10, 10, None), ("idle", "Idle"))
        self.assertEqual(monitor.lane_state(True, False, 0, 0, 0, None), ("idle", "No gateway"))

    def test_lane_labels(self):
        self.assertEqual(monitor.lane_title(_iface("wlan0", "wifi")), "Wi-Fi")
        self.assertEqual(monitor.lane_title(_iface("wg0", "other")), "VPN")
        self.assertEqual(monitor.lane_detail(_iface("eth0")), "eth0 · 192.168.1.2")
        self.assertEqual(monitor.lane_detail(_iface("eth0", cidrs=())), "eth0 · no IPv4 address")

    def test_colour_book_is_stable(self):
        book = monitor.ColourBook(("#1", "#2"))
        self.assertEqual([book.colour("a"), book.colour("b"), book.colour("a"), book.colour("c")],
                         ["#1", "#2", "#1", "#1"])

    def test_tone_colours_exist(self):
        for tone in ("ok", "hot", "active", "idle", "off"):
            self.assertTrue(monitor.tone_colour(tone, theme.PALETTE).startswith("#"))

    def test_series_points_and_mix(self):
        pts = graph.series_points([0, 5, 10], width=100, top=0, bottom=50, scale=10)
        self.assertEqual(pts, [(0.0, 50.0), (50.0, 25.0), (100.0, 0.0)])
        self.assertEqual(graph.series_points([20], 10, 0, 10, 10), [(0.0, 0.0)])
        self.assertEqual(graph.mix("#000000", "#FFFFFF", 0.5), "#808080")


class AppHelperTests(unittest.TestCase):
    def test_sum_series_and_share(self):
        self.assertEqual(appmod.sum_series([[(1, 2), (3, 4)], [(10, 20), (30, 40)]], 0), [11, 33])
        self.assertEqual(appmod.sum_series([], 1), [])
        self.assertEqual(appmod.routed_share(25, 100), "25%")
        self.assertEqual(appmod.routed_share(0.1, 100), "<1%")
        self.assertEqual(appmod.routed_share(0, 100), "")

    def test_initial_geometry_fits_the_screen(self):
        self.assertEqual(appmod.initial_geometry(1920, 1080), "1360x900")
        self.assertEqual(appmod.initial_geometry(1366, 768), "1286x700")


if __name__ == "__main__":
    unittest.main()
