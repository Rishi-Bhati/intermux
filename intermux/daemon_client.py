"""Python client for the InterMux daemon (intermuxd).

Implements the contract defined in docs/daemon-protocol.md:
- AF_UNIX stream socket at $XDG_RUNTIME_DIR/intermux/daemon.sock
- Line-delimited UTF-8 JSON framing (lines > 64 KiB close connection)
- Protocol version 1 (hello, ping, subscribe, interfaces.list, bindings.list, gui.raise)
- Request-response correlation by id across threads
- Event subscription and auto-reconnect with backoff
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

log = logging.getLogger("intermux.daemon")

# Reconnect backoff schedule in seconds: 0.5s, 1s, 2s, then every 5s
RECONNECT_BACKOFF: Tuple[float, ...] = (0.5, 1.0, 2.0, 5.0)
BACKOFF = RECONNECT_BACKOFF
RECONNECT_DELAYS = RECONNECT_BACKOFF

MAX_LINE_BYTES = 64 * 1024  # 64 KiB = 65536 bytes


def socket_path() -> Optional[str]:
    """Return $XDG_RUNTIME_DIR/intermux/daemon.sock, or None if XDG_RUNTIME_DIR is unset."""
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if not xdg:
        return None
    return os.path.join(xdg, "intermux", "daemon.sock")


def daemon_binary() -> Optional[str]:
    """Return the path to intermuxd binary, or None if not found/executable.

    Order of preference:
    1. INTERMUX_DAEMON environment variable (if set and executable)
    2. /usr/lib/intermux/intermuxd (if executable)
    3. intermuxd on PATH (via shutil.which)
    """
    env_bin = os.environ.get("INTERMUX_DAEMON")
    if env_bin and os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
        return env_bin
    system_bin = "/usr/lib/intermux/intermuxd"
    if os.path.isfile(system_bin) and os.access(system_bin, os.X_OK):
        return system_bin
    which_bin = shutil.which("intermuxd")
    if which_bin and os.path.isfile(which_bin) and os.access(which_bin, os.X_OK):
        return which_bin
    return None


def _can_connect(path: str) -> bool:
    """Test whether an AF_UNIX stream socket connection can be established."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        try:
            sock.connect(path)
            return True
        finally:
            sock.close()
    except OSError:
        return False


def ensure_daemon(timeout: float = 3.0) -> bool:
    """Ensure the InterMux daemon is running.

    If a connection to the socket succeeds, returns True immediately.
    Otherwise, if daemon_binary() exists, starts it detached and retries connecting
    every 0.1s until timeout. Returns whether the daemon is up.
    """
    path = socket_path()
    if path is None:
        return False
    if _can_connect(path):
        return True

    binary = daemon_binary()
    if not binary:
        return False

    try:
        subprocess.Popen(
            [binary],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as exc:
        log.warning("Failed to start daemon %s: %s", binary, exc)
        return False

    deadline = time.monotonic() + timeout
    while True:
        if _can_connect(path):
            return True
        now = time.monotonic()
        if now >= deadline:
            break
        time.sleep(min(0.1, max(0.001, deadline - now)))
    return False


class DaemonError(Exception):
    """Protocol error reported by the daemon or client protocol error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


class DaemonConnectionError(DaemonError, OSError):
    """Raised when not connected or connection is lost."""

    def __init__(self, message: str = "Client is not connected") -> None:
        super().__init__("not_connected", message)


class DaemonTimeoutError(DaemonError, TimeoutError):
    """Raised when a request times out."""

    def __init__(self, message: str = "Request timed out") -> None:
        super().__init__("timeout", message)


class _InFlightRequest:
    """Synchronizes a waiting request caller with the reader thread."""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.response: Optional[dict] = None
        self.error: Optional[BaseException] = None

    def set_response(self, response: dict) -> None:
        self.response = response
        self.event.set()

    def set_error(self, error: BaseException) -> None:
        self.error = error
        self.event.set()


class DaemonClient:
    """Client for intermuxd over AF_UNIX stream socket."""

    def __init__(
        self,
        client_name: str,
        on_event: Optional[Callable[[str, dict], None]] = None,
        path: Optional[str] = None,
    ) -> None:
        self._client_name = client_name
        self._on_event = on_event
        self._path = path if path is not None else socket_path()

        self._sock: Optional[socket.socket] = None
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._pending_requests: Dict[int, _InFlightRequest] = {}
        self._next_id = 1

        self._closed = False
        self._reconnecting = False
        self._reconnect_topics: List[str] = []
        self._reconnect_backoff: Optional[Sequence[float]] = None

        self._stop_event = threading.Event()
        self._disconnect_event = threading.Event()

        self._reader_thread: Optional[threading.Thread] = None
        self._reconnect_thread: Optional[threading.Thread] = None

    @property
    def is_connected(self) -> bool:
        """Whether the client currently holds an open socket connection."""
        with self._state_lock:
            return self._sock is not None and not self._closed

    @property
    def reconnect_backoff(self) -> Optional[Sequence[float]]:
        return self._reconnect_backoff

    @reconnect_backoff.setter
    def reconnect_backoff(self, val: Optional[Sequence[float]]) -> None:
        self._reconnect_backoff = val

    def connect(self, timeout: float = 2.0) -> dict:
        """Open AF_UNIX stream socket, send hello handshake, return hello result."""
        if self._path is None:
            raise OSError("Daemon socket path is not available (XDG_RUNTIME_DIR unset)")

        self._close_socket()

        self._closed = False
        self._stop_event.clear()
        self._disconnect_event.clear()

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            sock.connect(self._path)
            sock.settimeout(None)
        except OSError:
            sock.close()
            raise

        with self._state_lock:
            self._sock = sock

        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name=f"intermux-daemon-reader-{self._client_name}",
            daemon=True,
        )
        self._reader_thread.start()

        try:
            res = self.request(
                "hello",
                {"client": self._client_name, "protocols": [1]},
                timeout=timeout,
            )
            if not isinstance(res, dict):
                raise DaemonError("invalid_response", f"Expected dict result from hello, got {type(res).__name__}")
            return res
        except Exception:
            self._close_socket()
            raise

    def request(self, method: str, params: Optional[dict] = None, timeout: float = 2.0) -> Any:
        """Send a request with a fresh integer id, wait for the response, return result or raise DaemonError."""
        inflight = _InFlightRequest()
        with self._state_lock:
            if self._sock is None or self._closed:
                raise DaemonConnectionError("Client is not connected")
            req_id = self._next_id
            self._next_id += 1
            self._pending_requests[req_id] = inflight

        payload = {
            "v": 1,
            "id": req_id,
            "method": method,
            "params": params if params is not None else {},
        }
        data = (json.dumps(payload) + "\n").encode("utf-8")

        with self._send_lock:
            with self._state_lock:
                sock = self._sock
            if sock is None or self._closed:
                with self._state_lock:
                    self._pending_requests.pop(req_id, None)
                raise DaemonConnectionError("Client is not connected")
            try:
                sock.sendall(data)
            except OSError as exc:
                with self._state_lock:
                    self._pending_requests.pop(req_id, None)
                raise DaemonConnectionError(f"Failed to send request: {exc}") from exc

        if not inflight.event.wait(timeout):
            with self._state_lock:
                self._pending_requests.pop(req_id, None)
            raise DaemonTimeoutError(f"Request {req_id} ({method}) timed out after {timeout} s")

        with self._state_lock:
            self._pending_requests.pop(req_id, None)

        if inflight.error is not None:
            raise inflight.error

        resp = inflight.response
        if resp is None:
            raise DaemonConnectionError("No response received")

        if "error" in resp:
            err = resp["error"]
            if isinstance(err, dict):
                raise DaemonError(
                    code=str(err.get("code", "unknown")),
                    message=str(err.get("message", "")),
                )
            raise DaemonError(code="unknown", message=str(err))

        return resp.get("result")

    def subscribe(self, topics: List[str]) -> List[str]:
        """Subscribe to daemon event topics, returning list of subscribed topics."""
        res = self.request("subscribe", {"topics": list(topics)})
        if isinstance(res, dict):
            return list(res.get("subscribed", []))
        return []

    def start_reconnecting(self, topics: Sequence[str]) -> None:
        """Start auto-reconnect thread that handles disconnects and re-subscribes."""
        self._reconnect_topics = list(topics)
        self._reconnecting = True
        if self._reconnect_thread is None or not self._reconnect_thread.is_alive():
            self._reconnect_thread = threading.Thread(
                target=self._reconnect_loop,
                name=f"intermux-daemon-reconnect-{self._client_name}",
                daemon=True,
            )
            self._reconnect_thread.start()

    def close(self) -> None:
        """Close connection and stop all background threads."""
        self._closed = True
        self._reconnecting = False
        self._stop_event.set()
        self._disconnect_event.set()

        self._close_socket()

        if self._reader_thread and self._reader_thread.is_alive():
            if threading.current_thread() != self._reader_thread:
                self._reader_thread.join(timeout=1.0)
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            if threading.current_thread() != self._reconnect_thread:
                self._reconnect_thread.join(timeout=1.0)

    def __enter__(self) -> "DaemonClient":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def _deliver_event(self, event_name: str, event_data: dict) -> None:
        if self._on_event is not None:
            try:
                self._on_event(event_name, event_data)
            except Exception:
                log.exception("Exception in on_event callback for event: %s", event_name)

    def _close_socket(self) -> None:
        with self._state_lock:
            sock = self._sock
            self._sock = None
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass

            for inflight in self._pending_requests.values():
                inflight.set_error(DaemonConnectionError("Daemon connection closed"))
            self._pending_requests.clear()

    def _handle_disconnect(self) -> None:
        was_connected = False
        with self._state_lock:
            if self._sock is not None:
                was_connected = True
                sock = self._sock
                self._sock = None
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass

            for inflight in self._pending_requests.values():
                inflight.set_error(DaemonConnectionError("Daemon connection closed"))
            self._pending_requests.clear()

        if was_connected and not self._closed:
            self._deliver_event("client.disconnected", {})
            if self._reconnecting:
                self._disconnect_event.set()

    def _reader_loop(self) -> None:
        with self._state_lock:
            sock = self._sock
        if sock is None:
            return

        buf = bytearray()
        while not self._closed:
            nl_idx = buf.find(b"\n")
            if nl_idx != -1:
                line_bytes = bytes(buf[:nl_idx])
                del buf[:nl_idx + 1]
                if len(line_bytes) > MAX_LINE_BYTES:
                    log.warning("Line exceeds 64 KiB (%d bytes); closing connection", len(line_bytes))
                    self._handle_disconnect()
                    break

                # Strip possible trailing CR
                if line_bytes.endswith(b"\r"):
                    line_bytes = line_bytes[:-1]

                try:
                    line_str = line_bytes.decode("utf-8")
                    msg = json.loads(line_str)
                except Exception as exc:
                    log.warning("Invalid JSON/UTF-8 from daemon (%s); closing connection", exc)
                    self._handle_disconnect()
                    break

                if not isinstance(msg, dict):
                    log.warning("Received non-dict JSON from daemon; closing connection")
                    self._handle_disconnect()
                    break

                self._dispatch_message(msg)
                continue

            if len(buf) > MAX_LINE_BYTES:
                log.warning("Buffer exceeds 64 KiB without newline; closing connection")
                self._handle_disconnect()
                break

            try:
                chunk = sock.recv(4096)
            except OSError as exc:
                if not self._closed:
                    log.debug("Socket recv error: %s", exc)
                self._handle_disconnect()
                break

            if not chunk:
                if not self._closed:
                    log.debug("Daemon socket closed by remote")
                self._handle_disconnect()
                break

            buf.extend(chunk)

    def _dispatch_message(self, msg: dict) -> None:
        if "id" in msg:
            req_id = msg["id"]
            with self._state_lock:
                inflight = self._pending_requests.get(req_id)
            if inflight is not None:
                inflight.set_response(msg)
            else:
                log.debug("Received response for unknown request id: %s", req_id)
        elif "event" in msg:
            event_name = msg["event"]
            event_data = msg.get("data")
            if not isinstance(event_data, dict):
                event_data = {}
            self._deliver_event(event_name, event_data)
        else:
            log.debug("Message without id or event ignored: %s", msg)

    def _reconnect_loop(self) -> None:
        while not self._stop_event.is_set() and self._reconnecting:
            if self.is_connected:
                self._disconnect_event.wait()
                if self._stop_event.is_set() or not self._reconnecting:
                    break
                self._disconnect_event.clear()

            backoff_idx = 0
            while not self._stop_event.is_set() and self._reconnecting:
                delays = self._reconnect_backoff or RECONNECT_BACKOFF
                delay = delays[min(backoff_idx, len(delays) - 1)]
                if self._stop_event.wait(delay):
                    break
                if not self._reconnecting:
                    break
                try:
                    self.connect()
                    self.subscribe(self._reconnect_topics)
                    self._deliver_event("client.reconnected", {})
                    break
                except Exception as exc:
                    log.debug("Reconnect attempt %d failed: %s", backoff_idx + 1, exc)
                    backoff_idx += 1
