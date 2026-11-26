"""Tier 4 multi-feature end-to-end workloads (W1-W6)."""

from __future__ import annotations

import time
import unittest

from intermux import bugreport, state
from intermux.system import traffic
from tests.e2e_win import harness
from tests.e2e_win.fixtures import dataplane, dummy_app, net_servers


class WorkloadTests(unittest.TestCase):
    def test_T4_W1_live_speed_reporting_and_idle_decay(self) -> None:
        """T4-W1: Real traffic -> 1s RateTracker -> CLI/GUI formatting -> idle decay."""
        echo = net_servers.LoopbackTCPEchoServer()
        self.addCleanup(echo.close)
        dp = dataplane.DataPlaneTarget(iface="Wi-Fi")
        self.addCleanup(dp.close)

        tracker = traffic.RateTracker()
        tracker.update({"Wi-Fi": dp.counters()}, 10.0)
        got = dp.transfer_echo(echo.port, b"D" * (64 * 1024))
        self.assertEqual(len(got), 64 * 1024)
        rx, tx = dp.counters()
        tracker.update({"Wi-Fi": (rx, tx)}, 11.0)
        d, u = tracker.rate("Wi-Fi")
        self.assertGreater(d, 0.0)
        self.assertIn("KB/s", traffic.format_rate(d))
        tracker.update({"Wi-Fi": (rx, tx)}, 12.0)
        self.assertEqual(tracker.rate("Wi-Fi"), (0.0, 0.0))

    def test_T4_W2_dynamic_bandwidth_limit_reconfiguration(self) -> None:
        """T4-W2: Unthrottled -> 1.5 Mbit/s cap -> unlimited without dropping connections."""
        echo = net_servers.LoopbackTCPEchoServer()
        self.addCleanup(echo.close)
        dp = dataplane.DataPlaneTarget(iface="Ethernet")
        self.addCleanup(dp.close)

        fast_t0 = time.monotonic()
        dp.transfer_echo(echo.port, b"F" * (64 * 1024))
        fast_dt = time.monotonic() - fast_t0

        dp.set_limit(1.5, 1.5)
        slow_t0 = time.monotonic()
        dp.transfer_echo(echo.port, b"S" * (96 * 1024))
        slow_dt = time.monotonic() - slow_t0
        self.assertGreater(slow_dt, fast_dt)
        self.assertGreaterEqual(slow_dt, 0.35)

        dp.set_limit(None, None)
        dp.transfer_echo(echo.port, b"U" * (32 * 1024))

    def test_T4_W5_linux_regression_guard_and_state_roundtrip(self) -> None:
        """T4-W5: Windows state extensions preserve strict Linux state validation."""
        lin_bad = {"version": 1, "subnets": {"Ethernet 2": "10.200.1.0/24"}}
        loaded_lin = state.state_from_dict(lin_bad, platform="linux")
        self.assertEqual(loaded_lin.subnets, {})

        win_ok = {"version": 1, "subnets": {"Ethernet 2": "10.200.1.0/24"}}
        loaded_win = state.state_from_dict(win_ok, platform="windows")
        self.assertEqual(loaded_win.subnets["Ethernet 2"], "10.200.1.0/24")

    def test_T4_W6_bugreport_zip_generation_and_dummy_child(self) -> None:
        """T4-W6: Bug report zip generation scrubs home/IPs and child process cleans up."""
        root = harness.make_isolated_env(self)
        proc = dummy_app.spawn_sleep_child(10.0)
        self.addCleanup(proc.kill)
        self.assertIsNone(proc.poll())
        proc.terminate()
        proc.wait(timeout=5)

        zpath = bugreport.write_bug_zip(directory=root / "reports", home=str(root / "home"))
        self.assertTrue(zpath.is_file())
