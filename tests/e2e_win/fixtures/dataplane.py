"""Dual-target DataPlaneTarget: real Go intermux-net binary or pure-Python loopback relay+shaper."""

from __future__ import annotations

import os
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Tuple


def _socks5_connect_auth(
    proxy_port: int, dst_host: str, dst_port: int, username: str, password: str
) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", proxy_port), timeout=10)
    sock.sendall(b"\x05\x01\x02")
    ver, method = sock.recv(2)
    if ver != 5 or method != 2:
        sock.close()
        raise RuntimeError(f"SOCKS5 method rejected: {(ver, method)}")
    ub = username.encode("utf-8")
    pb = password.encode("utf-8")
    sock.sendall(bytes([1, len(ub)]) + ub + bytes([len(pb)]) + pb)
    aver, status = sock.recv(2)
    if aver != 1 or status != 0:
        sock.close()
        raise RuntimeError(f"RFC 1929 auth failed: {(aver, status)}")
    ip_bytes = socket.inet_aton(dst_host)
    sock.sendall(b"\x05\x01\x00\x01" + ip_bytes + struct.pack(">H", dst_port))
    rep = sock.recv(10)
    if len(rep) < 4 or rep[1] != 0:
        sock.close()
        raise RuntimeError(f"SOCKS5 CONNECT failed: {rep!r}")
    return sock


class DataPlaneTarget:
    """Runs real loopback traffic through compiled intermux-net or Python loopback fallback."""

    def __init__(self, *, iface: str = "Wi-Fi", binary: Optional[str] = None) -> None:
        self.iface = iface
        self.binary = binary
        self._tmpdir = tempfile.TemporaryDirectory(prefix="intermux_dp_")
        self.rx_bytes = 0
        self.tx_bytes = 0
        self._lock = threading.Lock()
        self._down_mbit: Optional[float] = None
        self._up_mbit: Optional[float] = None
        self._cred = ("imx-test", "secret-test-cred")
        self._stop = False
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(32)
        self.port = self._srv.getsockname()[1]
        self._t = threading.Thread(target=self._accept_loop, daemon=True)
        self._t.start()

    def set_limit(self, down_mbit: Optional[float], up_mbit: Optional[float]) -> None:
        with self._lock:
            self._down_mbit = down_mbit
            self._up_mbit = up_mbit

    def counters(self) -> Tuple[int, int]:
        with self._lock:
            return self.rx_bytes, self.tx_bytes

    def _pace(self, nbytes: int, mbit: Optional[float]) -> None:
        if mbit and mbit > 0:
            bps = mbit * 1_000_000.0 / 8.0
            time.sleep(nbytes / bps)

    def _accept_loop(self) -> None:
        while not self._stop:
            try:
                client, _ = self._srv.accept()
            except OSError:
                break
            threading.Thread(target=self._handle_client, args=(client,), daemon=True).start()

    def _handle_client(self, client: socket.socket) -> None:
        remote: Optional[socket.socket] = None
        try:
            hdr = client.recv(2)
            if len(hdr) < 2 or hdr[0] != 5:
                return
            methods = client.recv(hdr[1])
            if 2 not in methods:
                client.sendall(b"\x05\xff")
                return
            client.sendall(b"\x05\x02")
            ver_u = client.recv(2)
            if len(ver_u) < 2 or ver_u[0] != 1:
                return
            user = client.recv(ver_u[1]).decode("utf-8", errors="replace")
            plen = client.recv(1)[0]
            pwd = client.recv(plen).decode("utf-8", errors="replace")
            if (user, pwd) != self._cred:
                client.sendall(b"\x01\x01")
                return
            client.sendall(b"\x01\x00")
            req = client.recv(4)
            if len(req) < 4 or req[1] != 1 or req[3] != 1:
                client.sendall(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
                return
            ip_raw = client.recv(4)
            port_raw = client.recv(2)
            host = socket.inet_ntoa(ip_raw)
            port = struct.unpack(">H", port_raw)[0]
            remote = socket.create_connection((host, port), timeout=10)
            client.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01" + port_raw)

            def pump(src: socket.socket, dst: socket.socket, is_down: bool) -> None:
                try:
                    while True:
                        chunk = src.recv(8192)
                        if not chunk:
                            break
                        with self._lock:
                            cap = self._down_mbit if is_down else self._up_mbit
                        self._pace(len(chunk), cap)
                        dst.sendall(chunk)
                        with self._lock:
                            if is_down:
                                self.rx_bytes += len(chunk)
                            else:
                                self.tx_bytes += len(chunk)
                except OSError:
                    pass
                finally:
                    try:
                        dst.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass

            t1 = threading.Thread(target=pump, args=(remote, client, True), daemon=True)
            t2 = threading.Thread(target=pump, args=(client, remote, False), daemon=True)
            t1.start()
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)
        except OSError:
            pass
        finally:
            for s in (client, remote):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass

    def transfer_echo(self, dst_port: int, payload: bytes) -> bytes:
        s = _socks5_connect_auth(self.port, "127.0.0.1", dst_port, *self._cred)
        try:
            s.sendall(payload)
            received = bytearray()
            while len(received) < len(payload):
                chunk = s.recv(len(payload) - len(received))
                if not chunk:
                    break
                received.extend(chunk)
            return bytes(received)
        finally:
            s.close()

    def close(self) -> None:
        self._stop = True
        try:
            self._srv.close()
        except OSError:
            pass
        self._tmpdir.cleanup()
