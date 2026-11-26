"""Windows platform backend integrating IP Helper, Start Menu/Registry apps, and intermux-net.exe."""

from __future__ import annotations

import hashlib
import logging
import ntpath
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from intermux.errors import ConflictError, HelperError, InterMuxError, NotFoundError
from intermux.platform import get_paths, win_apps, win_deps, win_netinfo, win_pipe, win_vpn

log = logging.getLogger("intermux.platform.windows")


def _safe_log_name(app: str, iface: str) -> str:
    app_stem = ntpath.splitext(ntpath.basename(app))[0] or "app"
    clean_app = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in app_stem)
    clean_iface = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in iface)
    return f"{clean_app}-{clean_iface}.log"


def _win_ns_id(iface: str) -> str:
    return "win_" + hashlib.md5(iface.encode("utf-8")).hexdigest()[:8]


class _WindowsNetworkBackend:
    def list_interfaces(self, include_all: bool = False) -> List[Any]:
        engine = win_pipe.find_unprivileged_engine()
        ifaces = win_netinfo.list_interfaces_from_engine(str(engine) if engine else None, include_all=True)
        if ifaces is None:
            ifaces = win_netinfo.list_interfaces_powershell(include_all=True)
        if include_all:
            return ifaces
        return [i for i in ifaces if i.bindable]

    def status(self) -> Dict[str, Any]:
        data = win_pipe.try_call_engine_if_running("status")
        if isinstance(data, dict):
            return data
        return {"mode": "wintun", "tun_up": False, "bindings": [], "adoptions": [], "dns": {}, "limits": {}}

    def bind(self, app: str, iface: str, **kwargs: Any) -> Dict[str, Any]:
        running_policy = str(kwargs.get("running_policy") or "ask")
        dns = kwargs.get("dns", "keep")
        limit = kwargs.get("limit")

        handler = win_apps.get_handler(app)
        comms = handler.comm_names(app)
        running_pids = win_apps.find_running_pids(app, comms)
        profile_mode = "default"
        if running_pids:
            if running_policy == "ask":
                raise ConflictError(f"{ntpath.basename(app)} is already running", code="already_running")
            if running_policy == "close":
                _, still = win_apps.terminate_pids(running_pids, force=False)
                if still:
                    raise ConflictError(
                        f"{ntpath.basename(app)} is still running",
                        code="still_running",
                        data={"still_running": still},
                    )
            elif running_policy == "separate":
                profile_mode = "separate"

        handler.prepare(iface, profile_mode, app)
        extra_args = handler.extra_args(iface, profile_mode)

        log_dir = get_paths("windows").app_log_dir
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / _safe_log_name(app, iface)

        # Ensure the elevated engine is running BEFORE spawning the app so its first packets hit Wintun.
        win_pipe.ensure_daemon()

        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        with open(log_path, "ab") as log_fh:
            try:
                proc = subprocess.Popen(
                    [app, *extra_args],
                    stdin=subprocess.DEVNULL,
                    stdout=log_fh,
                    stderr=log_fh,
                    creationflags=creation_flags,
                )
            except OSError as exc:
                raise HelperError(f"could not start {app}: {exc}", code="launch_failed") from exc

        try:
            reg = win_pipe.call_engine(
                "bind_register",
                {
                    "iface": iface,
                    "app": app,
                    "pids": [proc.pid],
                    "dns": dns,
                    "limit": limit,
                },
            ) or {}
        except BaseException:
            win_apps.terminate_pids([proc.pid], force=True)
            raise

        binding = dict(reg.get("binding") or {})
        binding.setdefault("iface", iface)
        binding.setdefault("app", app)
        binding.setdefault("app_name", ntpath.basename(app))
        binding.setdefault("namespace", _win_ns_id(iface))
        binding.setdefault("subnet", "wintun")
        binding["profile_mode"] = profile_mode
        binding["log_path"] = str(log_path)
        return {
            "binding": binding,
            "pid": proc.pid,
            "log_path": str(log_path),
            "warnings": list(reg.get("warnings") or []),
        }

    def adopt(self, iface: str, app: str, **kwargs: Any) -> Dict[str, Any]:
        handler = win_apps.get_handler(app)
        pids = win_apps.find_running_pids(app, handler.comm_names(app))
        if not pids:
            raise NotFoundError(f"no running instances of {ntpath.basename(app)} found")
        return win_pipe.call_engine("adopt_pids", {"iface": iface, "app": app, "pids": pids}) or {}

    def release_adoption(self, adopt_id: str, **kwargs: Any) -> Dict[str, Any]:
        return win_pipe.call_engine("release_adoption", {"id": adopt_id}) or {}

    def stop_app(self, iface: str, app: str, **kwargs: Any) -> Dict[str, Any]:
        st = self.status()
        pids: List[int] = []
        binding_id = ""
        for b in st.get("bindings") or []:
            if b.get("iface") == iface and b.get("app", "").lower() == app.lower():
                binding_id = str(b.get("id") or "")
                pids.extend(int(p) for p in (b.get("pids") or []))
        if not pids:
            handler = win_apps.get_handler(app)
            pids = win_apps.find_running_pids(app, handler.comm_names(app))
        stopped, still = win_apps.terminate_pids(pids, force=False) if pids else ([], [])
        if not still:
            win_pipe.call_engine("bind_unregister", {"id": binding_id, "iface": iface, "app": app})
        return {"stopped": stopped, "still_running": still}

    def kill_app(self, app: str, *, force: bool = False, **kwargs: Any) -> Dict[str, Any]:
        handler = win_apps.get_handler(app)
        pids = win_apps.find_running_pids(app, handler.comm_names(app))
        stopped, still = win_apps.terminate_pids(pids, force=force) if pids else ([], [])
        return {"stopped": stopped, "still_running": still}

    def restart_app(self, iface: str, app: str, **kwargs: Any) -> Dict[str, Any]:
        self.stop_app(iface, app)
        return self.bind(app, iface, running_policy="separate", **kwargs)

    def move_app(self, iface: str, app: str, to_iface: str, **kwargs: Any) -> Dict[str, Any]:
        self.stop_app(iface, app)
        return self.bind(app, to_iface, running_policy="separate", **kwargs)

    def set_dns(self, iface: str, dns: Any, **kwargs: Any) -> Dict[str, Any]:
        return win_pipe.call_engine("set_dns", {"iface": iface, "dns": dns}) or {}

    def set_limit(self, iface: str, down: Optional[float], up: Optional[float], **kwargs: Any) -> Dict[str, Any]:
        return win_pipe.call_engine("set_limit", {"iface": iface, "down": down, "up": up}) or {}

    def remove_namespace(self, iface: str, **kwargs: Any) -> Dict[str, Any]:
        st = self.status()
        apps_removed: List[str] = []
        still: List[int] = []
        for b in list(st.get("bindings") or []):
            if b.get("iface") == iface:
                res = self.stop_app(iface, str(b.get("app") or ""))
                apps_removed.append(str(b.get("app_name") or ""))
                still.extend(res.get("still_running") or [])
        return {"removed": not still, "apps": apps_removed, "still_running": still}

    def reconnect(self) -> Dict[str, Any]:
        return win_pipe.call_engine("reconnect") or {}

    def teardown_idle(self) -> Dict[str, Any]:
        return win_pipe.call_engine("teardown_idle") or {}

    def reset(self, *, kill_apps: bool = False, **kwargs: Any) -> Dict[str, Any]:
        st = self.status()
        still: List[int] = []
        if kill_apps:
            pids = [int(p) for b in (st.get("bindings") or []) for p in (b.get("pids") or [])]
            pids += [int(p) for a in (st.get("adoptions") or []) for p in (a.get("pids") or [])]
            _, still = win_apps.terminate_pids(pids, force=True) if pids else ([], [])
        res = win_pipe.call_engine("reset", {"kill_apps": False}) or {}
        if still:
            res["still_running"] = still
        return res

    def verify(self, iface: str) -> Dict[str, Any]:
        return win_pipe.call_engine("verify", {"iface": iface}) or {}

    def read_traffic_counters(
        self, ifaces: Sequence[str], bound_ifaces: Sequence[str]
    ) -> Dict[str, Tuple[int, int]]:
        samples: Dict[str, Tuple[int, int]] = {}
        snap = win_pipe.try_call_engine_if_running("counters")
        if isinstance(snap, dict):
            iface_stats = snap.get("interfaces") or {}
            for name in set(ifaces) | set(bound_ifaces):
                st = iface_stats.get(name)
                if isinstance(st, dict):
                    pair = (int(st.get("rx_bytes") or 0), int(st.get("tx_bytes") or 0))
                    samples[name] = pair
                    if name in bound_ifaces:
                        samples[f"ns:{name}"] = pair
        for nic in self.list_interfaces(include_all=True):
            if nic.name in ifaces and nic.name not in samples:
                samples[nic.name] = (int(nic.rx_bytes), int(nic.tx_bytes))
        return samples


class _WindowsAppsBackend:
    def scan_apps(self) -> List[Any]:
        return win_apps.scan_apps()

    def resolve_app_path(self, app: str) -> str:
        return win_apps.resolve_app_path(app)

    def get_handler(self, app_path: str) -> Any:
        return win_apps.get_handler(app_path)


class _WindowsElevationBackend:
    def is_elevated(self) -> bool:
        from intermux.platform import is_elevated

        return is_elevated()

    def helper_call(self, action: str, params: Optional[Dict[str, Any]] = None, *, timeout: float = 300.0) -> Any:
        net = WindowsPlatform().network
        p = dict(params or {})
        dispatch = {
            "status": lambda: net.status(),
            "bind": lambda: net.bind(
                p["app"], p["iface"], running_policy=p.get("running_policy", "ask"),
                env=p.get("env", {}), dns=p.get("dns", "keep"), limit=p.get("limit"),
            ),
            "adopt": lambda: net.adopt(p["iface"], p["app"]),
            "release_adoption": lambda: net.release_adoption(p["id"]),
            "stop_app": lambda: net.stop_app(p["iface"], p["app"]),
            "kill_app": lambda: net.kill_app(p["app"], force=bool(p.get("force"))),
            "restart_app": lambda: net.restart_app(p["iface"], p["app"]),
            "move_app": lambda: net.move_app(p["iface"], p["app"], p["to"]),
            "set_dns": lambda: net.set_dns(p["iface"], p["dns"]),
            "set_limit": lambda: net.set_limit(p["iface"], p.get("down"), p.get("up")),
            "remove_namespace": lambda: net.remove_namespace(p["iface"]),
            "reconnect": lambda: net.reconnect(),
            "teardown_idle": lambda: net.teardown_idle(),
            "reset": lambda: net.reset(kill_apps=bool(p.get("kill_apps"))),
            "verify": lambda: net.verify(p["iface"]),
        }
        if action == "batch":
            results = []
            for op in p.get("ops") or []:
                kind = op.get("op")
                try:
                    if kind == "bind":
                        data = net.bind(op["app"], op["iface"], running_policy=op.get("running_policy", "ask"),
                                        dns=op.get("dns", "keep"), limit=op.get("limit"))
                    elif kind == "adopt":
                        data = net.adopt(op["iface"], op["app"])
                    elif kind == "set_limit":
                        data = net.set_limit(op["iface"], op.get("down"), op.get("up"))
                    else:
                        raise HelperError(f"unknown batch op {kind}", code="invalid_request")
                    results.append({"ok": True, "data": data})
                except InterMuxError as exc:
                    results.append({"ok": False, "error": {"code": exc.code, "message": exc.message, "data": exc.data}})
            return results
        if action not in dispatch:
            raise HelperError(f"unsupported helper action {action}", code="invalid_request")
        return dispatch[action]()

    def helper_batch(self, ops: Sequence[Dict[str, Any]], *, timeout: float = 600.0) -> list:
        return self.helper_call("batch", {"ops": [dict(op) for op in ops]}, timeout=timeout)

    def passwordless_status(self) -> Dict[str, Any]:
        return win_pipe.passwordless_status()


class _WindowsVpnBackend:
    def is_available(self) -> bool:
        return win_vpn.is_available()

    def list_connections(self) -> List[Any]:
        return win_vpn.list_connections()

    def connect(self, name: str, *, apps_only: bool = True) -> Any:
        return win_vpn.connect(name, apps_only=apps_only)

    def disconnect(self, name: str) -> Any:
        return win_vpn.disconnect(name)

    def import_config(self, path: str) -> Any:
        return win_vpn.import_config(path)


class _WindowsDiagnosticsBackend:
    def check_dependencies(self) -> List[Any]:
        return win_deps.check_dependencies()

    def missing_required(self) -> List[Any]:
        return win_deps.missing_required()

    def os_info(self) -> Any:
        return win_deps.os_info()

    def open_with_desktop(self, target: str, *, timeout: float = 5.0) -> Any:
        return win_deps.open_with_desktop(target, timeout=timeout)


class WindowsPlatform:
    """Platform backend for Windows v2.2.1."""

    name = "windows"

    def __init__(self) -> None:
        self._paths = get_paths("windows")
        self.network = _WindowsNetworkBackend()
        self.apps = _WindowsAppsBackend()
        self.elevation = _WindowsElevationBackend()
        self.vpn = _WindowsVpnBackend()
        self.diagnostics = _WindowsDiagnosticsBackend()

    @property
    def paths(self) -> Any:
        return self._paths

    def is_elevated(self) -> bool:
        return self.elevation.is_elevated()

    def detect_app_env(self) -> Dict[str, str]:
        return {}

    def local_proc_count(self, app_path: str, comm_names: Sequence[str]) -> int:
        return len(win_apps.find_running_pids(app_path, comm_names))

    def format_system_env(self, environ: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
        return dict(os.environ if environ is None else environ)
