"""The native UI's unprivileged boundary and launch routing."""
from __future__ import annotations

import io
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from intermux import __main__ as entry, gui_bridge, state
from intermux.errors import ValidationError
from intermux.system.procs import Proc
from intermux.system.netinfo import Address, Interface


class BridgeProtocolTests(unittest.TestCase):
    def test_batch_uses_existing_helper_validation_and_disallows_injected_environment(self):
        req = {"v": 1, "id": 7, "op": "batch", "args": {"ops": [
            {"op": "bind", "iface": "wlan0", "app": "/usr/bin/firefox"},
            {"op": "adopt", "iface": "enp3s0", "app": "/usr/bin/sleep"},
        ]}}
        request_id, op, args = gui_bridge._parse_request(req)
        self.assertEqual((request_id, op), (7, "batch"))
        self.assertEqual([item["op"] for item in args["ops"]], ["bind", "adopt"])
        self.assertEqual(args["ops"][0]["running_policy"], "ask")
        req["args"]["ops"][0]["env"] = {"DISPLAY": ":500"}
        with self.assertRaisesRegex(ValidationError, "bridge supplies"):
            gui_bridge._parse_request(req)

    def test_only_explicit_operations_and_boolean_force_are_accepted(self):
        for op, args in [
            ("command", {"argv": ["anything"]}),
            ("snapshot", {"force": "yes"}),
            ("stop_app", {"iface": "wlan0", "app": "/usr/bin/firefox", "unexpected": True}),
        ]:
            with self.subTest(op=op, args=args), self.assertRaises(ValidationError):
                gui_bridge._parse_request({"v": 1, "id": 9, "op": op, "args": args})
        self.assertEqual(gui_bridge._parse_request({"v": 1, "id": 10, "op": "snapshot",
                                                    "args": {"force": True}}),
                         (10, "snapshot", {"force": True}))

    def test_ownership_and_remembered_interfaces(self):
        iface = Interface("wlan0", "wifi", True, "up", ["UP"], None, 1500,
                          [Address("inet", "192.168.1.4", 24, None, "global")], True, ["192.168.1.1"])
        other_user = state.Binding("wlan0", "/usr/bin/other", "other", "ns_other", "10.200.3.0/24",
                                   1003, 1003, uid=2000)
        own = state.Binding("usb0", "/usr/bin/mine", "mine", "ns_mine", "10.200.4.0/24", 1004, 1004,
                            uid=1000)
        data = state.State(subnets={"usb0": "10.200.4.0/24"}, bindings=[other_user, own])
        service = gui_bridge._Service(lambda _: None, threading.Event())
        with patch.object(gui_bridge.os, "getuid", return_value=1000), \
             patch.object(gui_bridge.netinfo, "list_interfaces", return_value=[iface]) as scan, \
             patch.object(gui_bridge.state, "load_state", return_value=data), \
             patch.object(gui_bridge.procs, "list_procs", return_value=[]), \
             patch.object(gui_bridge.procs, "netns_inode", return_value=None), \
             patch.object(gui_bridge.traffic, "read_counters", return_value={}), \
             patch.object(gui_bridge.deps, "missing_required", return_value=[Mock(tool="pkexec")]):
            result = service.snapshot()
            service.snapshot()
            self.assertEqual(scan.call_count, 1)
            service.snapshot(force=True)
            self.assertEqual(scan.call_count, 2)
        self.assertEqual([(item["name"], item["state"]) for item in result["interfaces"]],
                         [("wlan0", "up"), ("usb0", "missing")])
        self.assertEqual([item["name"] for item in result["bindings"]], ["mine"])
        self.assertEqual(result["dependencies"], ["pkexec"])
        self.assertTrue(result["interfaces"][1]["has_namespace"])

    def test_a_sibling_in_the_same_namespace_does_not_keep_a_stopped_app_running(self):
        own = state.Binding("eth0", "/usr/bin/firefox", "Firefox", "ns", "10.200.3.0/24",
                            1003, 1003, pid=101, uid=1000)
        stopped = state.Binding("eth0", "/usr/bin/other", "Other", "ns", "10.200.3.0/24",
                                1003, 1003, pid=102, uid=1000)
        data = state.State(bindings=[own, stopped])
        table = [Proc(101, 1, "firefox", 1000, own.app, 123),
                 Proc(104, 101, "renderer", 1000, "/usr/bin/renderer", 123)]
        with patch.object(gui_bridge.procs, "list_procs", return_value=table) as scan, \
             patch.object(gui_bridge.procs, "netns_inode", return_value=123):
            result = gui_bridge._bindings(data, 1000)
        scan.assert_called_once()
        self.assertEqual([(row["running"], row["pids"]) for row in result],
                         [(True, [101, 104]), (False, [])])

    def test_running_apps_counts_matching_processes_from_one_table(self):
        table = [Proc(201, 1, "firefox", gui_bridge.os.getuid(), "/usr/bin/firefox", None),
                 Proc(202, 1, "bash", gui_bridge.os.getuid(), "/usr/bin/bash", None)]
        with patch.object(gui_bridge.procs, "list_procs", return_value=table) as scan:
            result = gui_bridge._running_apps(["/usr/bin/firefox", "/usr/bin/other"])
        scan.assert_called_once()
        self.assertEqual(result, {"/usr/bin/firefox": 1, "/usr/bin/other": 0})

    def test_publishes_desktop_entry_icon_from_xdg_export(self):
        entry = gui_bridge.desktop_entries.AppEntry(
            "Example", "Description", "com.example.Client", ["/opt/example"],
            "/share/example.desktop", "/opt/example", True, "",
        )
        with tempfile.TemporaryDirectory() as directory:
            asset = Path(directory, "icons/hicolor/scalable/apps/com.example.Client.svg")
            asset.parent.mkdir(parents=True)
            asset.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
            with patch.dict(gui_bridge.os.environ, {"XDG_DATA_DIRS": directory}):
                self.assertEqual(gui_bridge._app_entry(entry, False)["icon"], str(asset))

    def test_stdio_can_take_snapshots_during_a_mutation_and_stops_on_eof(self):
        release = threading.Event()
        waiting = threading.Event()

        def operate(_self, op, _args):
            if op == "batch":
                waiting.set()
                self.assertTrue(release.wait(4))
                return [{"ok": True, "data": {}}, {"ok": False, "error": {"message": "Route unavailable"}}]
            return {"interfaces": []}

        peer, server = socket.socketpair()
        self.addCleanup(peer.close)
        self.addCleanup(server.close)
        peer.settimeout(5)
        source = server.makefile("rb")
        output = server.makefile("wb", buffering=0)
        self.addCleanup(source.close)
        self.addCleanup(output.close)
        peer_input = peer.makefile("rb")
        self.addCleanup(peer_input.close)

        def send(request_id, op, args):
            req = {"v": 1, "id": request_id, "op": op, "args": args}
            peer.sendall((json.dumps(req) + "\n").encode())

        with patch.object(gui_bridge._Service, "connect_daemon"), patch.object(gui_bridge._Service, "run", operate):
            service = threading.Thread(target=gui_bridge._serve, args=(source, output), daemon=True)
            service.start()
            try:
                send(1, "batch", {"ops": [{"op": "bind", "iface": iface, "app": "/usr/bin/test"}
                                          for iface in ("wlan0", "eth0")]})
                self.assertTrue(waiting.wait(4))
                send(2, "snapshot", {})
                snapshot = json.loads(peer_input.readline())
                self.assertEqual(snapshot, {"v": 1, "id": 2, "result": {"interfaces": []}})
                release.set()
                batch = json.loads(peer_input.readline())
                self.assertEqual(batch["id"], 1)
                self.assertEqual([row["ok"] for row in batch["result"]], [True, False])
            finally:
                release.set()
                peer.shutdown(socket.SHUT_WR)
                service.join(timeout=5)
            self.assertFalse(service.is_alive())

    def test_bridge_rejects_root_independently(self):
        with patch.object(gui_bridge.os, "getuid", return_value=0), \
             patch.object(gui_bridge.sys, "stderr", io.StringIO()) as stderr:
            self.assertEqual(gui_bridge.main(), 1)
        self.assertIn("regular user", stderr.getvalue())


class GuiDispatchTests(unittest.TestCase):
    def test_default_starts_native_and_falls_back_only_for_startup_error(self):
        for native_status, expected, fallback in [(75, 33, True), (1, 1, False), (0, 0, False)]:
            with self.subTest(status=native_status), patch.dict(entry.os.environ, {"INTERMUX_GUI": "auto"}), \
                 patch.object(entry, "_native_gui", return_value=Path("/test/intermux-ui")), \
                 patch.object(entry, "_raise_gui", return_value=False), \
                 patch.object(entry.subprocess, "run", return_value=Mock(returncode=native_status)) as run, \
                 patch("intermux.ui.app.main", return_value=33) as tk:
                self.assertEqual(entry._launch_gui(), expected)
                self.assertEqual(run.call_args.args[0], ["/test/intermux-ui"])
                self.assertEqual(tk.called, fallback)


if __name__ == "__main__":
    unittest.main()
