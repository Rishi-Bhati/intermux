"""Loopback TCP/UDP echo & sink servers, DNS stub, and HTTP ipify stub."""

from __future__ import annotations

import http.server
import socket
import socketserver
import struct
import threading
from typing import Tuple


class _TCPEchoHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        while True:
            data = self.request.recv(65536)
            if not data:
                break
            self.request.sendall(data)


class LoopbackTCPEchoServer:
    def __init__(self) -> None:
        self._srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _TCPEchoHandler)
        self._srv.daemon_threads = True
        self.port = self._srv.server_address[1]
        self._t = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._t.start()

    def close(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()


class LoopbackUDPEchoServer:
    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self._stop = False
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self) -> None:
        while not self._stop:
            try:
                data, addr = self.sock.recvfrom(65535)
                self.sock.sendto(data, addr)
            except OSError:
                break

    def close(self) -> None:
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass


class LoopbackDNSStub:
    """Responds to DNS queries on UDP 127.0.0.1:0 with QR=1 NOERROR."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.queries_seen = 0
        self._stop = False
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self) -> None:
        while not self._stop:
            try:
                data, addr = self.sock.recvfrom(4096)
            except OSError:
                break
            if len(data) >= 12:
                self.queries_seen += 1
                qid = data[:2]
                flags = struct.pack(">H", 0x8180)
                reply = qid + flags + data[4:]
                self.sock.sendto(reply, addr)

    def close(self) -> None:
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass


class LoopbackIpifyStub:
    def __init__(self, public_ip: str = "203.0.113.77") -> None:
        self.public_ip = public_ip

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(req_self) -> None:  # noqa: N802
                body = (public_ip + "\n").encode("ascii")
                req_self.send_response(200)
                req_self.send_header("Content-Length", str(len(body)))
                req_self.end_headers()
                req_self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self._srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/"
        self._t = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._t.start()

    def close(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()
