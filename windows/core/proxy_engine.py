#!/usr/bin/env python3
"""
InterMux Windows — SOCKS5 Proxy Engine (Phase 1: TCP only)
Runs a local SOCKS5 proxy server bound to a specific network adapter's IP address.
All connections made through this proxy will originate from the chosen interface.

Architecture:
  Client app → SOCKS5 proxy (127.0.0.1:<port>) → bound socket on <iface_ip> → Internet

SOCKS5 TCP (RFC 1928) support only in Phase 1.
UDP relay is Phase 2.
"""

import asyncio
import logging
import socket
import struct
import threading
from typing import Optional, Tuple

log = logging.getLogger(__name__)

# SOCKS5 constants
_SOCKS5_VERSION  = 5
_AUTH_NONE       = 0
_CMD_CONNECT     = 1
_ATYP_IPV4       = 1
_ATYP_DOMAIN     = 3
_ATYP_IPV6       = 4
_REP_SUCCESS     = 0
_REP_FAILURE     = 1
_REP_NOT_ALLOWED = 2
_REP_NET_UNREACH = 3
_REP_HOST_UNREACH = 4
_REP_REFUSED     = 5

_CHUNK = 65536  # read/write buffer size


class Socks5ProxyServer:
    """
    Asyncio-based SOCKS5 proxy server that binds outgoing connections to
    a specific local IP address, forcing traffic through a given network adapter.
    """

    def __init__(self, bind_ip: str, listen_port: int = 0):
        """
        Args:
            bind_ip: The IP address of the target network adapter.
                     All outgoing connections will be bound to this IP.
            listen_port: Local port to listen on. 0 = OS picks a free port.
        """
        self.bind_ip    = bind_ip
        self.listen_port = listen_port
        self._server: Optional[asyncio.AbstractServer] = None
        self._loop:   Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready   = threading.Event()
        self._port    = listen_port   # actual port after bind

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> int:
        """
        Start the proxy server in a background thread.
        Returns the actual port the server is listening on.
        """
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"intermux-proxy-{self.bind_ip}")
        self._thread.start()
        self._ready.wait(timeout=10)
        if not self._port:
            raise RuntimeError(f"SOCKS5 proxy for {self.bind_ip} failed to start.")
        log.info(f"[proxy] Listening on 127.0.0.1:{self._port} → bound to {self.bind_ip}")
        return self._port

    def stop(self):
        """Stop the proxy server."""
        if self._server and self._loop:
            self._loop.call_soon_threadsafe(self._server.close)
        log.info(f"[proxy] Stopped proxy for {self.bind_ip}")

    @property
    def port(self) -> int:
        return self._port

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        finally:
            self._loop.close()

    async def _serve(self):
        self._server = await asyncio.start_server(
            self._handle_client,
            host="127.0.0.1",
            port=self.listen_port,
            family=socket.AF_INET,
        )
        # Discover actual port if we asked for 0
        self._port = self._server.sockets[0].getsockname()[1]
        self._ready.set()

        async with self._server:
            await self._server.serve_forever()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ):
        peer = writer.get_extra_info("peername", ("?", 0))
        log.debug(f"[proxy] New client {peer}")
        try:
            await self._socks5_handshake(reader, writer)
        except Exception as e:
            log.debug(f"[proxy] Client {peer} error: {e}")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _socks5_handshake(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ):
        # ---- Greeting ----
        # +----+----------+----------+
        # |VER | NMETHODS | METHODS  |
        # +----+----------+----------+
        header = await reader.readexactly(2)
        ver, nmethods = header[0], header[1]
        if ver != _SOCKS5_VERSION:
            writer.close()
            return
        await reader.readexactly(nmethods)   # discard method list

        # We only support NO AUTH (0x00)
        writer.write(bytes([_SOCKS5_VERSION, _AUTH_NONE]))
        await writer.drain()

        # ---- Request ----
        # +----+-----+-------+------+----------+----------+
        # |VER | CMD |  RSV  | ATYP | DST.ADDR | DST.PORT |
        # +----+-----+-------+------+----------+----------+
        req_header = await reader.readexactly(4)
        ver, cmd, _, atyp = req_header

        if ver != _SOCKS5_VERSION or cmd != _CMD_CONNECT:
            await self._send_reply(writer, _REP_NOT_ALLOWED)
            return

        # Parse destination address
        if atyp == _ATYP_IPV4:
            addr_bytes = await reader.readexactly(4)
            dst_addr = socket.inet_ntoa(addr_bytes)
        elif atyp == _ATYP_DOMAIN:
            length = (await reader.readexactly(1))[0]
            dst_addr = (await reader.readexactly(length)).decode("utf-8", errors="replace")
        elif atyp == _ATYP_IPV6:
            addr_bytes = await reader.readexactly(16)
            dst_addr = socket.inet_ntop(socket.AF_INET6, addr_bytes)
        else:
            await self._send_reply(writer, _REP_FAILURE)
            return

        port_bytes = await reader.readexactly(2)
        dst_port   = struct.unpack("!H", port_bytes)[0]

        log.debug(f"[proxy] CONNECT {dst_addr}:{dst_port} via {self.bind_ip}")

        # ---- Open outgoing connection bound to the chosen interface IP ----
        try:
            remote_reader, remote_writer = await asyncio.wait_for(
                asyncio.open_connection(
                    host=dst_addr,
                    port=dst_port,
                    local_addr=(self.bind_ip, 0),   # KEY: force this adapter
                ),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            await self._send_reply(writer, _REP_HOST_UNREACH)
            return
        except ConnectionRefusedError:
            await self._send_reply(writer, _REP_REFUSED)
            return
        except OSError as e:
            log.debug(f"[proxy] Connect failed {dst_addr}:{dst_port}: {e}")
            await self._send_reply(writer, _REP_NET_UNREACH)
            return

        # Success reply
        await self._send_reply(writer, _REP_SUCCESS, self.bind_ip, 0)

        # ---- Bidirectional relay ----
        try:
            await asyncio.gather(
                _relay(reader, remote_writer),
                _relay(remote_reader, writer),
            )
        except Exception:
            pass
        finally:
            remote_writer.close()
            try:
                await remote_writer.wait_closed()
            except Exception:
                pass

    @staticmethod
    async def _send_reply(
        writer: asyncio.StreamWriter,
        rep: int,
        bind_addr: str = "0.0.0.0",
        bind_port: int = 0,
    ):
        try:
            addr_packed = socket.inet_aton(bind_addr)
        except OSError:
            addr_packed = b"\x00\x00\x00\x00"
        writer.write(
            bytes([_SOCKS5_VERSION, rep, 0x00, _ATYP_IPV4])
            + addr_packed
            + struct.pack("!H", bind_port)
        )
        await writer.drain()


async def _relay(src: asyncio.StreamReader, dst: asyncio.StreamWriter):
    """Pipe data from src to dst until EOF or error."""
    try:
        while True:
            data = await src.read(_CHUNK)
            if not data:
                break
            dst.write(data)
            await dst.drain()
    except Exception:
        pass
    finally:
        try:
            dst.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Registry: manage one proxy per interface
# ---------------------------------------------------------------------------

class ProxyRegistry:
    """
    Singleton-like registry that tracks one SOCKS5 proxy per interface IP.
    Ensures we don't start duplicate proxies for the same adapter.
    """

    def __init__(self):
        self._proxies: dict[str, Socks5ProxyServer] = {}   # ip -> server

    def get_or_start(self, bind_ip: str) -> int:
        """
        Returns the local SOCKS5 proxy port for the given adapter IP.
        Starts a new proxy server if one doesn't exist yet.
        """
        if bind_ip not in self._proxies:
            srv = Socks5ProxyServer(bind_ip=bind_ip, listen_port=0)
            port = srv.start()
            self._proxies[bind_ip] = srv
            log.info(f"[registry] Started proxy for {bind_ip} on port {port}")
        return self._proxies[bind_ip].port

    def stop_all(self):
        """Stop all running proxy servers."""
        for ip, srv in self._proxies.items():
            srv.stop()
        self._proxies.clear()
        log.info("[registry] All proxies stopped.")

    def stop_for_ip(self, bind_ip: str):
        """Stop the proxy for a specific interface IP."""
        if bind_ip in self._proxies:
            self._proxies.pop(bind_ip).stop()

    def running_proxies(self) -> dict:
        """Returns {ip: port} for all running proxies."""
        return {ip: srv.port for ip, srv in self._proxies.items()}


# Module-level registry shared by the GUI/CLI
proxy_registry = ProxyRegistry()


if __name__ == "__main__":
    import time, sys
    logging.basicConfig(level=logging.DEBUG)

    # Quick smoke test: pick the first usable local IP (not loopback)
    import socket as _s
    test_ip = "0.0.0.0"
    with _s.socket(_s.AF_INET, _s.SOCK_DGRAM) as sock:
        try:
            sock.connect(("8.8.8.8", 80))
            test_ip = sock.getsockname()[0]
        except Exception:
            pass

    print(f"Starting test proxy bound to {test_ip}...")
    port = proxy_registry.get_or_start(test_ip)
    print(f"Proxy listening on 127.0.0.1:{port}")
    print(f"Test: curl --proxy socks5://127.0.0.1:{port} https://httpbin.org/ip")
    try:
        time.sleep(60)
    except KeyboardInterrupt:
        proxy_registry.stop_all()
