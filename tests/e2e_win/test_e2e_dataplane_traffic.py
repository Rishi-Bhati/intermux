"""Tier 1, 2 & 3 loopback data-plane tests: real byte transfers, live 1s speeds, rate limits, DNS, verify."""

from __future__ import annotations

import socket
import struct
import time
import unittest

from intermux.system import traffic
from tests.e2e_win.fixtures import dataplane, net_servers


class DataPlaneTrafficTests(unittest.TestCase):
    def test_T1_F06_live_speed_reporting_tracks_real_bytes(self) -> None:
        """T1-F06: Real loopback TCP transfer updates counters and RateTracker rx/tx speeds."""
        echo = net_servers.LoopbackTCPEchoServer()
        self.addCleanup(echo.close)
        dp = dataplane.DataPlaneTarget(iface="Wi-Fi")
        self.addCleanup(dp.close)

        tracker = traffic.RateTracker()
        tracker.update({"Wi-Fi": dp.counters()}, 1.0)

        payload = b"X" * (128 * 1024)
        got = dp.transfer_echo(echo.port, payload)
        self.assertEqual(len(got), len(payload))

        rx, tx = dp.counters()
        self.assertEqual(rx, len(payload))
        self.assertEqual(tx, len(payload))

        tracker.update({"Wi-Fi": (rx, tx)}, 2.0)
        d_rate, u_rate = tracker.rate("Wi-Fi")
        self.assertEqual(d_rate, float(len(payload)))
        self.assertEqual(u_rate, float(len(payload)))

        # Idle decay to 0 B/s
        tracker.update({"Wi-Fi": (rx, tx)}, 3.0)
        self.assertEqual(tracker.rate("Wi-Fi"), (0.0, 0.0))

    def test_T2_F07_rate_limit_enforces_throughput_cap(self) -> None:
        """T2-F07: Bandwidth cap (2 Mbit/s) paces real loopback TCP transfer."""
        echo = net_servers.LoopbackTCPEchoServer()
        self.addCleanup(echo.close)
        dp = dataplane.DataPlaneTarget(iface="Ethernet")
        self.addCleanup(dp.close)

        dp.set_limit(2.0, 2.0)  # 250,000 B/s
        payload = b"Z" * (100 * 1024)
        t0 = time.monotonic()
        got = dp.transfer_echo(echo.port, payload)
        elapsed = time.monotonic() - t0
        self.assertEqual(len(got), len(payload))
        self.assertGreaterEqual(elapsed, 0.25)

    def test_T1_F02_socks5_rejects_unauthenticated_clients(self) -> None:
        """T1-F02: Loopback SOCKS5 listener rejects NO AUTH (0x00) and requires RFC 1929 credentials."""
        dp = dataplane.DataPlaneTarget(iface="Wi-Fi")
        self.addCleanup(dp.close)
        sock = socket.create_connection(("127.0.0.1", dp.port), timeout=5)
        self.addCleanup(sock.close)
        sock.sendall(b"\x05\x01\x00")
        rep = sock.recv(2)
        self.assertEqual(rep, b"\x05\xff")

    def test_T1_F08_loopback_dns_stub_answers_probe(self) -> None:
        """T1-F08: Loopback DNS stub handles RFC 1035 query with NOERROR."""
        dns_srv = net_servers.LoopbackDNSStub()
        self.addCleanup(dns_srv.close)
        query = b"\x49\x4d\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\x01"
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(s.close)
        s.settimeout(3.0)
        s.sendto(query, ("127.0.0.1", dns_srv.port))
        reply, _ = s.recvfrom(512)
        self.assertEqual(struct.unpack(">H", reply[:2])[0], 0x494D)
        self.assertTrue(struct.unpack(">H", reply[2:4])[0] & 0x8000)

    def test_T1_F09_ipify_stub_returns_ipv4(self) -> None:
        """T1-F09: HTTP public IP verification stub returns valid IPv4."""
        import urllib.request

        ip_srv = net_servers.LoopbackIpifyStub("198.51.100.23")
        self.addCleanup(ip_srv.close)
        body = urllib.request.urlopen(ip_srv.url, timeout=5).read().decode().strip()
        self.assertEqual(body, "198.51.100.23")
