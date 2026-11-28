"""Unprivileged NDJSON service for the native desktop UI."""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import sys
import threading
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any, BinaryIO, Callable

from intermux import apps, bugreport, daemon_client, dnsconf, report, state
from intermux.apps import desktop_entries
from intermux.errors import InterMuxError, NotFoundError, ValidationError
from intermux.helper import client as helper_client
from intermux.helper import protocol as helper_protocol
from intermux.system import adopt, commands, deps, netinfo, netns, nm, passwordless, procs, session, traffic
from intermux.ui.history import History, Item as HistoryItem
from intermux.ui.icons import resolve_desktop_icon_path
from intermux.ui.monitor import visible_lanes

log = logging.getLogger(__name__)
VERSION = 1
MAX_LINE = 512 * 1024
DAEMON_TOPICS = ["interfaces", "bindings", "gui"]
HELPER_OPS = {
    "stop_app", "restart_app", "move_app", "kill_app", "release_adoption",
    "verify", "set_dns", "set_limit", "remove_namespace", "reconnect", "teardown_idle", "reset",
}
EMPTY_OPS = {"apps", "vpn_list", "save_report", "open_report"}
READ_OPS = {"apps", "resolve_app", "running_apps", "vpn_list"}
UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def _fields(args: dict, *required: str) -> None:
    missing = set(required) - args.keys()
    extra = args.keys() - set(required)
    if missing:
        raise ValidationError("missing argument: " + min(missing))
    if extra:
        raise ValidationError("unknown argument: " + min(extra))


def _text(value: Any, name: str) -> str:
    if isinstance(value, str) and value and not any(char in value for char in "\0\r\n"):
        try:
            if len(value.encode("utf-8")) <= 4096:
                return value
        except UnicodeEncodeError:
            pass
    raise ValidationError(f"{name} must be a non-empty string of up to 4096 UTF-8 bytes without control characters")


def _helper_args(op: str, args: dict) -> dict:
    if "env" in args:
        raise ValidationError("the bridge supplies the desktop environment")
    dns = args.get("dns")
    if op in ("bind", "set_dns") and isinstance(dns, str) and dns not in (dnsconf.AUTO, dnsconf.KEEP):
        try:
            args = dict(args, dns=dnsconf.parse_servers(dns))
        except ValueError as exc:
            raise ValidationError(str(exc)) from None
    return helper_protocol.validate_request({
        "v": helper_protocol.PROTOCOL_VERSION, "action": op, "params": args,
    })[1]


def _validate_args(op: str, args: dict) -> dict:
    if op in HELPER_OPS:
        return _helper_args(op, args)
    if op in EMPTY_OPS:
        _fields(args)
    elif op == "snapshot":
        if args and (args.keys() != {"force"} or not isinstance(args["force"], bool)):
            raise ValidationError("snapshot accepts only a boolean force argument")
    elif op == "batch":
        _fields(args, "ops")
        ops = args["ops"]
        if not isinstance(ops, list) or not 1 <= len(ops) <= helper_protocol.MAX_BATCH_OPS:
            raise ValidationError(f"ops must be a list of 1 to {helper_protocol.MAX_BATCH_OPS} operations")
        clean = []
        for item in ops:
            if not isinstance(item, dict) or item.get("op") not in ("bind", "adopt"):
                raise ValidationError("each batch operation must be bind or adopt")
            kind = item["op"]
            fields = {key: value for key, value in item.items() if key != "op"}
            clean.append(dict(_helper_args(kind, fields), op=kind))
        return {"ops": clean}
    elif op in ("resolve_app", "vpn_import", "open_report_folder"):
        _fields(args, "path")
        path = _text(args["path"], "path")
        if op != "resolve_app" and not os.path.isabs(path):
            raise ValidationError("path must be absolute")
    elif op == "running_apps":
        _fields(args, "paths")
        paths = args["paths"]
        if not isinstance(paths, list) or len(paths) > helper_protocol.MAX_BATCH_OPS:
            raise ValidationError(f"paths must be a list of up to {helper_protocol.MAX_BATCH_OPS} paths")
        for path in paths:
            _helper_args("kill_app", {"app": path})
    elif op == "open_log":
        return _helper_args("stop_app", args)
    elif op == "passwordless":
        _fields(args, "on")
        if not isinstance(args["on"], bool):
            raise ValidationError("on must be a boolean")
    elif op in ("vpn_connect", "vpn_disconnect"):
        _fields(args, "uuid", "apps_only") if op == "vpn_connect" else _fields(args, "uuid")
        if not UUID.fullmatch(_text(args["uuid"], "uuid")):
            raise ValidationError("invalid VPN UUID")
        if op == "vpn_connect" and not isinstance(args["apps_only"], bool):
            raise ValidationError("apps_only must be a boolean")
    else:
        raise ValidationError("unknown operation")
    return args


def _parse_request(obj: Any) -> tuple[int, str, dict]:
    if not isinstance(obj, dict):
        raise ValidationError("request must be an object")
    _fields(obj, "v", "id", "op", "args")
    if type(obj["v"]) is not int or obj["v"] != VERSION:
        raise ValidationError(f"unsupported protocol version; expected {VERSION}")
    if type(obj["id"]) is not int:
        raise ValidationError("id must be an integer")
    if not isinstance(obj["op"], str) or not isinstance(obj["args"], dict):
        raise ValidationError("op must be a string and args must be an object")
    return obj["id"], obj["op"], _validate_args(obj["op"], obj["args"])


def _env() -> dict:
    return session.build_app_env(session.detect_session(probe=True))


def _app_entry(entry: desktop_entries.AppEntry, recent: bool) -> dict:
    icon = resolve_desktop_icon_path(entry.icon)
    return {
        "name": entry.name,
        "path": entry.binary or entry.desktop_file,
        "description": entry.comment,
        "icon": str(icon) if icon else None,
        "bindable": entry.bindable,
        "reason": entry.reason,
        "recent": recent,
    }


def _custom_entry(path: str, name: str = "", recent: bool = False) -> dict:
    reason = ""
    try:
        path = apps.resolve_app_path(path)
        _helper_args("kill_app", {"app": path})
    except (NotFoundError, ValidationError) as exc:
        reason = exc.message
    return {
        "name": name or os.path.basename(path),
        "path": path,
        "description": "",
        "icon": None,
        "bindable": not reason,
        "reason": reason,
        "recent": recent,
    }


def _installed_apps() -> list:
    entries = desktop_entries.scan_desktop_entries()
    recent = History().items
    recent_paths = {item.app: item for item in recent}
    installed = [_app_entry(entry, entry.binary in recent_paths) for entry in entries]
    installed_paths = {entry["path"] for entry in installed}
    installed.extend(_custom_entry(item.app, item.name, recent=True)
                     for item in recent if item.app not in installed_paths)
    for entry in installed:
        previous = recent_paths.get(entry["path"])
        if previous:
            entry.update(recent_iface=previous.iface, recent_dns=previous.dns)
    order = {item.app: index for index, item in enumerate(recent)}
    return sorted(installed, key=lambda entry: order.get(entry["path"], len(order)))


def _resolve_app(path: str) -> dict:
    entries = desktop_entries.scan_desktop_entries()
    history = History()
    for entry in entries:
        if path in (entry.desktop_file, entry.binary):
            return _app_entry(entry, history.find(entry.binary or path) is not None)
    resolved = _custom_entry(path)
    for entry in entries:
        if resolved["path"] == entry.binary:
            return _app_entry(entry, history.find(entry.binary) is not None)
    previous = history.find(resolved["path"])
    if previous:
        resolved.update(name=previous.name, recent=True)
    return resolved


def _bindings(st: state.State, uid: int) -> list:
    processes = {proc.pid: proc for proc in procs.list_procs() if proc.uid == uid}
    by_namespace: dict[int, list[procs.Proc]] = {}
    for proc in processes.values():
        if proc.netns is not None:
            by_namespace.setdefault(proc.netns, []).append(proc)

    namespace_bindings = Counter(binding.namespace for binding in st.bindings)
    bindings = []
    for binding in st.bindings:
        if binding.uid is not None and binding.uid != uid:
            continue
        inode = procs.netns_inode(binding.namespace)
        launch = processes.get(binding.pid)
        if binding.uid is None and (launch is None or inode is None or launch.netns != inode):
            continue
        members = by_namespace.get(inode, [])
        if members and namespace_bindings[binding.namespace] > 1:
            comm = apps.get_handler(binding.app).comm_names(binding.app)
            matching = {proc.pid for proc in procs.find_app_procs(binding.app, comm, uid, processes=members)}
            if binding.pid:
                descendants = set(procs.descendants([binding.pid], list(processes.values()), uid))
                matching.update(proc.pid for proc in members if proc.pid in descendants)
            pids = sorted(matching)
        else:
            pids = sorted(proc.pid for proc in members)
        bindings.append({
            "iface": binding.iface, "app": binding.app, "name": binding.app_name,
            "kind": "bound", "id": None, "running": bool(pids), "pids": pids,
            "log_path": binding.log_path or None,
        })
    for shifted in st.adoptions:
        if shifted.uid != uid:
            continue
        members = adopt.read_group_pids(shifted.group)
        pids = sorted(set(members or shifted.pids).intersection(processes))
        bindings.append({
            "iface": shifted.iface, "app": shifted.app, "name": shifted.app_name,
            "kind": "shifted", "id": shifted.id, "running": bool(pids), "pids": pids, "log_path": None,
        })
    return bindings


class _Service:
    def __init__(self, emit: Callable[[dict], None], closed: threading.Event) -> None:
        self._emit = emit
        self._closed = closed
        self._daemon: daemon_client.DaemonClient | None = None
        self._daemon_lock = threading.Lock()
        self._daemon_ready = threading.Event()
        self._rates = traffic.RateTracker()
        self._reports: dict[str, Path] = {}
        self._snapshot_dirty = threading.Event()
        self._snapshot_at = float("-inf")
        self._topology: tuple[dict, state.State, list] = ({}, state.State(), [])
        self._environment: dict = {}

    def _event(self, name: str, data: dict) -> None:
        if name == "gui.raise":
            self._emit({"v": VERSION, "event": name, "data": data})
            return
        if name == "client.reconnected":
            self._daemon_ready.set()
        elif name == "client.disconnected":
            self._daemon_ready.clear()
        if (name.startswith(("interface.", "route.", "binding.", "adoption."))
                or name in ("app.exited", "client.reconnected", "client.disconnected")):
            self._snapshot_dirty.set()
            self._emit({"v": VERSION, "event": "refresh", "data": {}})

    def connect_daemon(self) -> None:
        path = daemon_client.socket_path()
        if path is None or self._closed.is_set():
            return
        client = daemon_client.DaemonClient("gui", self._event, path=path)
        try:
            if daemon_client.ensure_daemon() and not self._closed.is_set():
                client.connect()
                client.subscribe(DAEMON_TOPICS)
                self._event("client.reconnected", {})
        except (OSError, daemon_client.DaemonError):
            log.debug("Initial GUI daemon connection failed", exc_info=True)
            client.close()
            client = daemon_client.DaemonClient("gui", self._event, path=path)
        with self._daemon_lock:
            if not self._closed.is_set():
                self._daemon = client
                client.start_reconnecting(DAEMON_TOPICS)
                return
        client.close()

    def close(self) -> None:
        with self._daemon_lock:
            client = self._daemon
        if client is not None:
            client.close()

    def snapshot(self, force: bool = False) -> dict:
        now = time.monotonic()
        if force or self._snapshot_dirty.is_set() or now - self._snapshot_at >= 4:
            self._snapshot_dirty.clear()
            interfaces = {interface.name: interface for interface in netinfo.list_interfaces()}
            st = state.load_state()
            bindings = _bindings(st, os.getuid())
            self._topology = interfaces, st, bindings
            self._environment = {
                "passwordless": bool(passwordless.status().get("enabled")),
                "dependencies": [dependency.tool for dependency in deps.missing_required()],
                "vpn_available": nm.available(),
            }
            self._snapshot_at = now
        interfaces, st, bindings = self._topology
        remembered = dict.fromkeys([*st.subnets, *(row["iface"] for row in bindings)])
        visible = visible_lanes(interfaces.values(), remembered)
        names = dict.fromkeys([*(interface.name for interface in visible), *remembered])
        veths = {iface: netns.veth_names(iface)[0] for iface in st.subnets}
        counters = traffic.read_counters([*names, *veths.values()])
        samples = {name: traffic.iface_sample(counters[name]) for name in names if name in counters}
        samples.update({f"namespace/{iface}": traffic.veth_sample(counters[veth])
                        for iface, veth in veths.items() if veth in counters})
        self._rates.update(samples, time.monotonic())

        def rate(name: str) -> dict | None:
            if name not in samples:
                return None
            down, up = self._rates.rate(name)
            return {"down": down, "up": up}

        rows = []
        for iface in names:
            info = interfaces.get(iface)
            gateways = info.gateways if info else []
            limit = st.limits.get(iface)
            rows.append({
                "name": iface,
                "kind": info.kind if info else "other",
                "state": ("up" if info.up else "down") if info else "missing",
                "bindable": info.bindable if info else False,
                "ipv4": info.ipv4_cidrs if info else [],
                "gateway": gateways[0] if gateways else None,
                "dns": list(st.dns.get(iface, [])),
                "limit": {key: limit.get(key) for key in ("down", "up")} if limit else None,
                "rates": rate(iface),
                "namespace_rates": rate(f"namespace/{iface}"),
                "has_namespace": iface in st.subnets,
            })
        return {
            "interfaces": rows,
            "bindings": bindings,
            "daemon_connected": bool(self._daemon_ready.is_set() and self._daemon and self._daemon.is_connected),
            **self._environment,
        }

    def _batch(self, ops: list) -> list:
        if any(op["op"] == "bind" for op in ops):
            env = _env()
            ops = [dict(op, env=env) if op["op"] == "bind" else op for op in ops]
        results = helper_client.batch(ops)
        if any(isinstance(result, dict) and result.get("ok") is True for result in results):
            history = History()
            entries = {entry.binary: entry.name for entry in desktop_entries.scan_desktop_entries()}
            for op, result in zip(ops, results):
                if isinstance(result, dict) and result.get("ok") is True:
                    app = op["app"]
                    dns = op.get("dns", dnsconf.KEEP)
                    dns_text = ", ".join(dns) if isinstance(dns, list) else dns if dns != dnsconf.KEEP else ""
                    history.add(HistoryItem(app, entries.get(app) or os.path.basename(app), op["iface"], dns_text))
        return results

    def _vpn_action(self, op: str, uuid: str, apps_only: bool = True) -> None:
        conn = next((conn for conn in nm.list_vpns() if conn.uuid.lower() == uuid.lower()), None)
        if conn is None:
            raise NotFoundError("VPN connection not found")
        if op == "vpn_connect":
            nm.connect(conn, apps_only)
        else:
            nm.disconnect(conn)

    def _open_log(self, iface: str, app: str) -> None:
        binding = next((row for row in _bindings(state.load_state(), os.getuid())
                        if row["kind"] == "bound" and row["iface"] == iface and row["app"] == app), None)
        if not binding or not binding["log_path"]:
            raise NotFoundError("No log file is recorded for this binding")
        path = Path(binding["log_path"])
        if not path.is_absolute() or not path.is_file():
            raise NotFoundError("The binding's log file could not be found")
        _open_file(path)

    def _save_report(self) -> dict:
        path = bugreport.write_bug_zip()
        self._reports[str(path)] = path.resolve()
        return {"path": str(path)}

    def _open_report_folder(self, path: str) -> None:
        known = self._reports.get(path)
        if known is None or Path(path).resolve() != known or not known.is_file():
            raise NotFoundError("No report saved by this bridge matches that path")
        _open_file(known.parent)

    def run(self, op: str, args: dict) -> Any:
        if op in HELPER_OPS:
            if op in ("restart_app", "move_app"):
                args = dict(args, env=_env())
            result = helper_client.call(op, args)
            self._snapshot_dirty.set()
            return result
        if op in ("vpn_connect", "vpn_disconnect"):
            result = self._vpn_action(op, **args)
            self._snapshot_dirty.set()
            return result
        dispatch = {
            "snapshot": self.snapshot,
            "apps": _installed_apps,
            "resolve_app": _resolve_app,
            "running_apps": _running_apps,
            "batch": self._batch,
            "passwordless": passwordless.set_from_gui,
            "vpn_list": lambda: [asdict(conn) for conn in nm.list_vpns()],
            "vpn_import": lambda path: {"uuid": nm.import_file(path)},
            "save_report": self._save_report,
            "open_log": self._open_log,
            "open_report": _open_report,
            "open_report_folder": self._open_report_folder,
        }
        result = dispatch[op](**args)
        if op in ("batch", "passwordless", "vpn_import"):
            self._snapshot_dirty.set()
        return result


def _running_apps(paths: list[str]) -> dict:
    uid = os.getuid()
    table = procs.list_procs()
    return {path: len(procs.find_app_procs(path, apps.get_handler(path).comm_names(path), uid=uid, processes=table))
            for path in dict.fromkeys(paths)}


def _open_file(path: Path) -> None:
    result = commands.open_with_desktop(str(path))
    if not result.ok:
        raise InterMuxError(result.stderr or "Could not open the file", code="open_failed")


def _open_report() -> None:
    if not report.open_report_page():
        raise InterMuxError("Could not open the report page", code="open_failed")


def _error(request_id: int | None, exc: InterMuxError) -> dict:
    error = {"code": exc.code, "message": exc.message}
    if exc.data is not None:
        error["data"] = exc.data
    return {"v": VERSION, "id": request_id, "error": error}


def _serve(source: BinaryIO, output: BinaryIO) -> None:
    closed = threading.Event()
    write_lock = threading.Lock()

    def emit(message: dict) -> None:
        if closed.is_set():
            return
        data = (json.dumps(message, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        try:
            with write_lock:
                if not closed.is_set():
                    remaining = memoryview(data)
                    while remaining:
                        remaining = remaining[output.write(remaining):]
                    output.flush()
        except OSError:
            closed.set()

    service = _Service(emit, closed)

    def worker(requests: queue.Queue) -> None:
        while not closed.is_set():
            request = requests.get()
            if request is None or closed.is_set():
                return
            request_id, op, args = request
            try:
                emit({"v": VERSION, "id": request_id, "result": service.run(op, args)})
            except InterMuxError as exc:
                emit(_error(request_id, exc))
            except Exception:
                log.exception("GUI bridge operation %s failed", op)
                emit(_error(request_id, InterMuxError("Operation failed. See the bridge log.", code="internal")))

    snapshots: queue.Queue = queue.Queue(maxsize=4)
    reads: queue.Queue = queue.Queue(maxsize=32)
    actions: queue.Queue = queue.Queue(maxsize=32)
    for name, tasks in (("snapshot", snapshots), ("read", reads), ("action", actions)):
        threading.Thread(target=worker, args=(tasks,), name=f"intermux-bridge-{name}", daemon=True).start()
    threading.Thread(target=service.connect_daemon, name="intermux-bridge-daemon", daemon=True).start()

    try:
        while not closed.is_set():
            line = source.readline(MAX_LINE + 1)
            if not line:
                break
            request_id = None
            try:
                if len(line) > MAX_LINE:
                    while line and not line.endswith(b"\n"):
                        line = source.readline(MAX_LINE + 1)
                    raise ValidationError("request exceeds the size limit")
                obj = json.loads(line.decode("utf-8"), parse_constant=_bad_constant)
                if isinstance(obj, dict) and type(obj.get("id")) is int:
                    request_id = obj["id"]
                request = _parse_request(obj)
                op = request[1]
                tasks = snapshots if op == "snapshot" else reads if op in READ_OPS else actions
                tasks.put_nowait(request)
            except queue.Full:
                emit(_error(request_id, InterMuxError("Too many pending requests", code="busy")))
            except ValidationError as exc:
                emit(_error(request_id, exc))
            except (ValueError, RecursionError):
                emit(_error(request_id, ValidationError("request must be valid UTF-8 JSON")))
    finally:
        closed.set()
        for tasks in (snapshots, reads, actions):
            try:
                tasks.put_nowait(None)
            except queue.Full:
                pass
        service.close()


def _bad_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def main() -> int:
    if os.getuid() == 0 or os.geteuid() == 0:
        print("The InterMux GUI bridge must run as a regular user.", file=sys.stderr)
        return 1
    with os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0) as output:
        _serve(sys.stdin.buffer, output)
    return 0
