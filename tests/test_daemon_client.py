"""Unit tests for intermux.daemon_client, CLI events command, and UI daemon integration."""

from __future__ import annotations

import io
import json
import os
import queue
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
from typing import Any
import unittest
from unittest import mock

from intermux import cli
from intermux import daemon_client
from intermux.daemon_client import (
    DaemonClient,
    DaemonConnectionError,
    DaemonError,
    DaemonTimeoutError,
    ensure_daemon,
    socket_path,
)
from intermux.ui import app as ui_app


class FakeDaemon:
    """Fake intermuxd daemon speaking the protocol on an AF_UNIX stream socket."""

    def __init__(self, sock_dir: str, socket_name: str = "daemon.sock") -> None:
        self.sock_dir = sock_dir
        self.sock_path = os.path.join(sock_dir, socket_name)
        if os.path.exists(self.sock_path):
            os.remove(self.sock_path)
        self.server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server_sock.bind(self.sock_path)
        self.server_sock.listen(5)
        self.clients: list[socket.socket] = []
        self.lock = threading.Lock()
        self.running = True
        self.gui_raised = True
        self.subscriptions: list[str] = []
        self.received_requests: list[dict] = []
        self.custom_handlers: dict[str, Any] = {}
        self.thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.thread.start()

    def _accept_loop(self) -> None:
        while self.running:
            try:
                self.server_sock.settimeout(0.2)
                client_sock, _ = self.server_sock.accept()
            except (socket.timeout, OSError):
                continue
            with self.lock:
                self.clients.append(client_sock)
            t = threading.Thread(target=self._client_loop, args=(client_sock,), daemon=True)
            t.start()

    def _client_loop(self, client_sock: socket.socket) -> None:
        buf = bytearray()
        while self.running:
            nl_idx = buf.find(b"\n")
            if nl_idx != -1:
                line = bytes(buf[:nl_idx])
                del buf[:nl_idx + 1]
                if not line:
                    continue
                try:
                    req = json.loads(line.decode("utf-8"))
                except Exception:
                    continue
                self._handle_request(client_sock, req)
                continue
            try:
                client_sock.settimeout(0.2)
                chunk = client_sock.recv(4096)
                if not chunk:
                    break
                buf.extend(chunk)
            except socket.timeout:
                continue
            except OSError:
                break
        with self.lock:
            if client_sock in self.clients:
                self.clients.remove(client_sock)
        try:
            client_sock.close()
        except OSError:
            pass

    def _handle_request(self, client_sock: socket.socket, req: dict) -> None:
        with self.lock:
            self.received_requests.append(req)
        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}

        if method in self.custom_handlers:
            resp = self.custom_handlers[method](req)
            self._send_raw(client_sock, resp)
            return

        if method == "hello":
            resp = {
                "v": 1,
                "id": req_id,
                "result": {
                    "daemon_version": "3.0.0-beta.1",
                    "protocol": 1,
                    "capabilities": ["interfaces", "bindings", "traffic", "gui"],
                },
            }
        elif method == "ping":
            resp = {"v": 1, "id": req_id, "result": {"pong": True}}
        elif method == "subscribe":
            topics = params.get("topics", [])
            with self.lock:
                self.subscriptions.extend(topics)
            resp = {"v": 1, "id": req_id, "result": {"subscribed": topics}}
        elif method == "gui.raise":
            resp = {"v": 1, "id": req_id, "result": {"raised": self.gui_raised}}
        elif method == "bindings.list":
            resp = {"v": 1, "id": req_id, "result": {"bindings": [], "adoptions": []}}
        else:
            resp = {
                "v": 1,
                "id": req_id,
                "error": {"code": "unknown_method", "message": f"no method named {method}"},
            }
        self._send_raw(client_sock, resp)

    def _send_raw(self, client_sock: socket.socket, obj: Any) -> None:
        try:
            if isinstance(obj, (bytes, bytearray)):
                data = bytes(obj)
            else:
                data = (json.dumps(obj) + "\n").encode("utf-8")
            client_sock.sendall(data)
        except OSError:
            pass

    def send_raw_all(self, data: bytes) -> None:
        with self.lock:
            for c in list(self.clients):
                try:
                    c.sendall(data)
                except OSError:
                    pass

    def send_event(self, event: str, data: dict) -> None:
        payload = {"v": 1, "event": event, "data": data}
        with self.lock:
            for c in list(self.clients):
                self._send_raw(c, payload)

    def drop_connections(self) -> None:
        with self.lock:
            for c in list(self.clients):
                try:
                    c.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    c.close()
                except OSError:
                    pass
            self.clients.clear()

    def close(self) -> None:
        self.running = False
        self.drop_connections()
        try:
            self.server_sock.close()
        except OSError:
            pass
        self.thread.join(timeout=1.0)


class TestDaemonClient(unittest.TestCase):
    """Test suite for DaemonClient."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.daemon = FakeDaemon(self.temp_dir.name)

    def tearDown(self) -> None:
        self.daemon.close()
        self.temp_dir.cleanup()

    def test_hello_result(self) -> None:
        """Client connects and receives expected hello result."""
        client = DaemonClient("test-client", path=self.daemon.sock_path)
        try:
            res = client.connect(timeout=2.0)
            self.assertEqual(res.get("protocol"), 1)
            self.assertEqual(res.get("daemon_version"), "3.0.0-beta.1")
            self.assertIn("interfaces", res.get("capabilities", []))
            self.assertTrue(client.is_connected)
        finally:
            client.close()
        self.assertFalse(client.is_connected)

    def test_concurrent_requests_id_matching(self) -> None:
        """Two concurrent requests from separate threads match responses by id."""
        client = DaemonClient("concurrent-test", path=self.daemon.sock_path)
        client.connect(timeout=2.0)

        # Introduce small artificial delay in custom handler to guarantee overlap
        def delay_handler(req: dict) -> dict:
            time.sleep(0.05)
            method = req.get("method")
            if method == "method_a":
                return {"v": 1, "id": req["id"], "result": {"val": "A"}}
            return {"v": 1, "id": req["id"], "result": {"val": "B"}}

        self.daemon.custom_handlers["method_a"] = delay_handler
        self.daemon.custom_handlers["method_b"] = delay_handler

        results: dict[str, Any] = {}
        errors: dict[str, Exception] = {}

        def call_a() -> None:
            try:
                results["a"] = client.request("method_a", timeout=2.0)
            except Exception as e:
                errors["a"] = e

        def call_b() -> None:
            try:
                results["b"] = client.request("method_b", timeout=2.0)
            except Exception as e:
                errors["b"] = e

        t1 = threading.Thread(target=call_a)
        t2 = threading.Thread(target=call_b)
        t1.start()
        t2.start()
        t1.join(timeout=3.0)
        t2.join(timeout=3.0)

        client.close()

        self.assertEqual(errors, {})
        self.assertEqual(results.get("a"), {"val": "A"})
        self.assertEqual(results.get("b"), {"val": "B"})

    def test_daemon_error_on_error_response(self) -> None:
        """DaemonError is raised on an error response with proper .code and .message."""
        client = DaemonClient("error-test", path=self.daemon.sock_path)
        client.connect(timeout=2.0)
        try:
            with self.assertRaises(DaemonError) as ctx:
                client.request("nonexistent.method", timeout=2.0)
            self.assertEqual(ctx.exception.code, "unknown_method")
            self.assertIn("no method named nonexistent.method", ctx.exception.message)
        finally:
            client.close()

    def test_events_delivered_to_on_event(self) -> None:
        """Subscribed events are delivered to the on_event callback from the reader thread."""
        received: list[tuple[str, dict]] = []
        event_arrived = threading.Event()

        def on_event(name: str, data: dict) -> None:
            received.append((name, data))
            event_arrived.set()

        client = DaemonClient("event-test", on_event=on_event, path=self.daemon.sock_path)
        client.connect(timeout=2.0)
        try:
            subscribed = client.subscribe(["interfaces"])
            self.assertEqual(subscribed, ["interfaces"])

            self.daemon.send_event("interface.status", {"interface": "wlan0", "state": "connected"})
            self.assertTrue(event_arrived.wait(timeout=2.0))
            self.assertEqual(len(received), 1)
            name, data = received[0]
            self.assertEqual(name, "interface.status")
            self.assertEqual(data.get("interface"), "wlan0")
            self.assertEqual(data.get("state"), "connected")
        finally:
            client.close()

    def test_requests_before_connect_fail_cleanly(self) -> None:
        """Requests and subscriptions before connect fail cleanly with DaemonError."""
        client = DaemonClient("unconnected", path=self.daemon.sock_path)
        with self.assertRaises(DaemonError) as ctx1:
            client.request("ping")
        self.assertEqual(ctx1.exception.code, "not_connected")

        with self.assertRaises(DaemonError) as ctx2:
            client.subscribe(["interfaces"])
        self.assertEqual(ctx2.exception.code, "not_connected")

    def test_oversize_line_closes_connection(self) -> None:
        """A line longer than 64 KiB closes the connection."""
        client = DaemonClient("oversize-test", path=self.daemon.sock_path)
        client.connect(timeout=2.0)
        try:
            # Send an oversized payload > 64 KiB
            oversized_padding = "x" * (65 * 1024)
            oversized_line = (f'{{"v": 1, "id": 999, "result": "{oversized_padding}"}}\n').encode("utf-8")
            self.daemon.send_raw_all(oversized_line)

            # Wait for reader thread to detect oversize line and close socket
            deadline = time.monotonic() + 2.0
            while client.is_connected and time.monotonic() < deadline:
                time.sleep(0.05)

            self.assertFalse(client.is_connected)
            # Subsequent requests should fail cleanly
            with self.assertRaises(DaemonError):
                client.request("ping", timeout=0.5)
        finally:
            client.close()

    def test_reconnect_after_server_drops_connection(self) -> None:
        """Reconnect backoff triggers after disconnect, calls client.reconnected and re-subscribes."""
        received: list[str] = []
        reconnected_event = threading.Event()

        def on_event(name: str, data: dict) -> None:
            received.append(name)
            if name == "client.reconnected":
                reconnected_event.set()

        client = DaemonClient("reconnect-test", on_event=on_event, path=self.daemon.sock_path)
        # Patch backoff to tiny delays for fast test execution
        client.reconnect_backoff = (0.02, 0.02, 0.05)

        client.connect(timeout=2.0)
        try:
            client.subscribe(["interfaces", "bindings"])
            client.start_reconnecting(["interfaces", "bindings"])

            # Verify initial subscription was registered
            with self.daemon.lock:
                self.assertIn("interfaces", self.daemon.subscriptions)
            with self.daemon.lock:
                self.daemon.subscriptions.clear()

            # Drop connection from server side
            self.daemon.drop_connections()

            # Wait for reconnection
            self.assertTrue(reconnected_event.wait(timeout=3.0))
            self.assertIn("client.disconnected", received)
            self.assertIn("client.reconnected", received)
            self.assertTrue(client.is_connected)

            # Re-subscribe should have happened
            with self.daemon.lock:
                self.assertIn("interfaces", self.daemon.subscriptions)
                self.assertIn("bindings", self.daemon.subscriptions)
        finally:
            client.close()

    def test_socket_path_with_and_without_xdg(self) -> None:
        """socket_path returns $XDG_RUNTIME_DIR/intermux/daemon.sock, or None when unset."""
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/1000"}):
            self.assertEqual(socket_path(), "/run/user/1000/intermux/daemon.sock")

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(socket_path())

        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": ""}):
            self.assertIsNone(socket_path())

    def test_ensure_daemon_when_nothing_available(self) -> None:
        """ensure_daemon returns False when daemon socket is not up and no binary exists."""
        with mock.patch.dict(os.environ, {"INTERMUX_DAEMON": "", "PATH": ""}):
            with mock.patch("intermux.daemon_client.socket_path", return_value=None):
                self.assertFalse(ensure_daemon(timeout=0.1))

    def test_ensure_daemon_starts_binary_from_intermux_daemon(self) -> None:
        """ensure_daemon executes INTERMUX_DAEMON binary detached and waits for socket."""
        sock_dir = os.path.join(self.temp_dir.name, "sub_xdg")
        os.makedirs(sock_dir, exist_ok=True)
        expected_sock = os.path.join(sock_dir, "intermux", "daemon.sock")

        # Create a tiny fake daemon python script
        fake_bin = os.path.join(self.temp_dir.name, "fake_intermuxd.py")
        pid_file = os.path.join(self.temp_dir.name, "fake_daemon.pid")
        script_code = f"""#!/usr/bin/env python3
import os, socket, json, time, sys

with open({repr(pid_file)}, "w") as f:
    f.write(str(os.getpid()))

sock_path = {repr(expected_sock)}
os.makedirs(os.path.dirname(sock_path), exist_ok=True)
if os.path.exists(sock_path):
    os.remove(sock_path)
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.bind(sock_path)
s.listen(5)

while True:
    try:
        conn, _ = s.accept()
        buf = conn.recv(1024)
        if not buf:
            conn.close()
            continue
        req = json.loads(buf.decode("utf-8").strip())
        resp = {{"v": 1, "id": req.get("id", 1), "result": {{"protocol": 1, "daemon_version": "fake"}}}}
        conn.sendall(json.dumps(resp).encode("utf-8") + b"\\n")
        conn.close()
    except Exception:
        break
"""
        with open(fake_bin, "w") as f:
            f.write(script_code)
        os.chmod(fake_bin, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

        env_patch = {
            "XDG_RUNTIME_DIR": sock_dir,
            "INTERMUX_DAEMON": fake_bin,
        }
        procs: list[subprocess.Popen] = []
        real_popen = subprocess.Popen

        def tracked_popen(*args: Any, **kwargs: Any) -> subprocess.Popen:
            p = real_popen(*args, **kwargs)
            procs.append(p)
            return p

        try:
            with mock.patch("subprocess.Popen", side_effect=tracked_popen):
                with mock.patch.dict(os.environ, env_patch):
                    # Initially socket is not present
                    self.assertFalse(os.path.exists(expected_sock))
                    result = ensure_daemon(timeout=3.0)
                    self.assertTrue(result)
                    self.assertTrue(os.path.exists(expected_sock))

                    # Calling again when socket is already connectable returns True immediately
                    self.assertTrue(ensure_daemon(timeout=0.5))
        finally:
            for p in procs:
                try:
                    p.terminate()
                    p.wait(timeout=1.0)
                except Exception:
                    pass


class TestCLIEvents(unittest.TestCase):
    """Test suite for 'intermux events' CLI sub-command."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.daemon = FakeDaemon(self.temp_dir.name)

    def tearDown(self) -> None:
        self.daemon.close()
        self.temp_dir.cleanup()

    def test_cli_events_json_output(self) -> None:
        """intermux events --json prints one JSON object per event from the daemon."""
        with mock.patch("intermux.daemon_client.socket_path", return_value=self.daemon.sock_path):
            with mock.patch("intermux.daemon_client.ensure_daemon", return_value=True):
                events_printed: list[str] = []
                stop_signal = threading.Event()

                def fake_stop() -> bool:
                    return stop_signal.is_set()

                # Send an event after a brief delay
                def send_delayed() -> None:
                    time.sleep(0.15)
                    self.daemon.send_event("interface.added", {"name": "wlan0"})
                    # Wait for event to be processed then signal stop
                    time.sleep(0.2)
                    stop_signal.set()

                t = threading.Thread(target=send_delayed)
                t.start()

                stdout_buf = io.StringIO()
                with mock.patch("sys.stdout", stdout_buf):
                    with mock.patch("intermux.cli._events_stop_condition", side_effect=fake_stop):
                        ret = cli.main(["events", "--json", "--topics", "interfaces"])

                t.join(timeout=2.0)
                self.assertEqual(ret, 0)

                output = stdout_buf.getvalue().strip()
                lines = [l for l in output.splitlines() if l.strip()]
                self.assertTrue(len(lines) >= 1)

                parsed = json.loads(lines[0])
                self.assertEqual(parsed.get("v"), 1)
                self.assertEqual(parsed.get("event"), "interface.added")
                self.assertEqual(parsed.get("data"), {"name": "wlan0"})

    def test_cli_events_daemon_unavailable(self) -> None:
        """intermux events reports error and returns 1 if daemon is not running."""
        with mock.patch("intermux.daemon_client.ensure_daemon", return_value=False):
            stderr_buf = io.StringIO()
            with mock.patch("sys.stderr", stderr_buf):
                ret = cli.main(["events"])
            self.assertEqual(ret, 1)
            self.assertIn("the InterMux daemon is not running and could not be started", stderr_buf.getvalue())


class TestUIAppIntegration(unittest.TestCase):
    """Test suite for GUI integration with intermuxd."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.daemon = FakeDaemon(self.temp_dir.name)

    def tearDown(self) -> None:
        self.daemon.close()
        self.temp_dir.cleanup()

    def test_gui_event_queue_debounce_schedules_single_refresh(self) -> None:
        """Several queued events schedule only a single debounced refresh 150ms later."""
        # Create an instance with dummy __init__ attributes
        app = object.__new__(ui_app.InterMuxApp)
        app._daemon_events = queue.Queue()
        app._debounce_after_id = None
        app._last_event_refresh = 0.0
        app._set_status = mock.MagicMock()

        scheduled_calls: list[tuple[int, Any]] = []

        fake_root = mock.MagicMock()

        def fake_after(ms: int, callback: Any) -> str:
            scheduled_calls.append((ms, callback))
            return f"after-{len(scheduled_calls)}"

        fake_root.after.side_effect = fake_after
        app.root = fake_root
        app.refresh_snapshot = mock.MagicMock()

        # Enqueue multiple events
        app._daemon_events.put(("interface.added", {"name": "wlan0"}))
        app._daemon_events.put(("route.lost", {"interface": "wlan0"}))
        app._daemon_events.put(("binding.started", {"iface": "wlan0"}))
        app._daemon_events.put(("adoption.started", {"id": "1"}))
        app._daemon_events.put(("app.exited", {"app_name": "firefox", "iface": "wlan0"}))
        app._daemon_events.put(("client.reconnected", {}))

        # Drain events directly
        app._drain_events()

        # Only one 150ms debounced refresh should be scheduled
        debounce_afters = [call for call in scheduled_calls if call[0] == 150]
        self.assertEqual(len(debounce_afters), 1)

        # Trigger the scheduled callback
        _, cb = debounce_afters[0]
        cb()

        # Verify refresh_snapshot was called once and last_event_refresh was updated
        app.refresh_snapshot.assert_called_once()
        self.assertGreater(app._last_event_refresh, 0.0)

        # Verify app.exited updated status line
        app._set_status.assert_called_with("firefox on wlan0 has exited")

    def test_gui_main_already_running_raises_and_exits_0(self) -> None:
        """main() exits 0 without creating Tk window if daemon reports raised=true."""
        self.daemon.gui_raised = True

        with mock.patch("intermux.daemon_client.socket_path", return_value=self.daemon.sock_path):
            with mock.patch("os.path.exists", return_value=True):
                with mock.patch("tkinter.Tk", side_effect=AssertionError("tk.Tk should not be called")):
                    stdout_buf = io.StringIO()
                    with mock.patch("sys.stdout", stdout_buf):
                        ret = ui_app.main()
                    self.assertEqual(ret, 0)
                    self.assertIn("InterMux is already open; bringing it to the front.", stdout_buf.getvalue())


if __name__ == "__main__":
    unittest.main()
