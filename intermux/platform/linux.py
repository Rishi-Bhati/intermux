"""Linux platform backend wrapping existing intermux.system and intermux.apps modules untouched."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from intermux.platform.paths import PlatformPaths, resolve_paths


class _LinuxNetworkBackend:
    def list_interfaces(self, include_all: bool = False) -> List[Any]:
        from intermux.system import netinfo

        ifaces = netinfo.list_interfaces()
        if include_all:
            return ifaces
        return [iface for iface in ifaces if iface.bindable]

    def status(self) -> Dict[str, Any]:
        from intermux.helper import client

        return client.call("status") or {}

    def bind(self, app: str, iface: str, **kwargs: Any) -> Dict[str, Any]:
        from intermux.helper import client

        return client.call("bind", {"app": app, "iface": iface, **kwargs}) or {}

    def adopt(self, iface: str, app: str, **kwargs: Any) -> Dict[str, Any]:
        from intermux.helper import client

        return client.call("adopt", {"iface": iface, "app": app, **kwargs}) or {}

    def release_adoption(self, adopt_id: str, **kwargs: Any) -> Dict[str, Any]:
        from intermux.helper import client

        return client.call("release_adoption", {"id": adopt_id, **kwargs}) or {}

    def stop_app(self, iface: str, app: str, **kwargs: Any) -> Dict[str, Any]:
        from intermux.helper import client

        return client.call("stop_app", {"iface": iface, "app": app, **kwargs}) or {}

    def kill_app(self, app: str, *, force: bool = False, **kwargs: Any) -> Dict[str, Any]:
        from intermux.helper import client

        return client.call("kill_app", {"app": app, "force": force, **kwargs}) or {}

    def restart_app(self, iface: str, app: str, **kwargs: Any) -> Dict[str, Any]:
        from intermux.helper import client

        return client.call("restart_app", {"iface": iface, "app": app, **kwargs}) or {}

    def move_app(self, iface: str, app: str, to_iface: str, **kwargs: Any) -> Dict[str, Any]:
        from intermux.helper import client

        return client.call("move_app", {"iface": iface, "app": app, "to": to_iface, **kwargs}) or {}

    def set_dns(self, iface: str, dns: Any, **kwargs: Any) -> Dict[str, Any]:
        from intermux.helper import client

        return client.call("set_dns", {"iface": iface, "dns": dns, **kwargs}) or {}

    def set_limit(self, iface: str, down: Optional[float], up: Optional[float], **kwargs: Any) -> Dict[str, Any]:
        from intermux.helper import client

        return client.call("set_limit", {"iface": iface, "down": down, "up": up, **kwargs}) or {}

    def remove_namespace(self, iface: str, **kwargs: Any) -> Dict[str, Any]:
        from intermux.helper import client

        return client.call("remove_namespace", {"iface": iface, **kwargs}) or {}

    def reconnect(self) -> Dict[str, Any]:
        from intermux.helper import client

        return client.call("reconnect") or {}

    def teardown_idle(self) -> Dict[str, Any]:
        from intermux.helper import client

        return client.call("teardown_idle") or {}

    def reset(self, *, kill_apps: bool = False, **kwargs: Any) -> Dict[str, Any]:
        from intermux.helper import client

        return client.call("reset", {"kill_apps": kill_apps, **kwargs}) or {}

    def verify(self, iface: str) -> Dict[str, Any]:
        from intermux.helper import client

        return client.call("verify", {"iface": iface}) or {}

    def read_traffic_counters(
        self, ifaces: Sequence[str], bound_ifaces: Sequence[str]
    ) -> Dict[str, Tuple[int, int]]:
        from intermux.system import netns, traffic

        veth_map = {iface: netns.veth_names(iface)[0] for iface in bound_ifaces}
        raw = traffic.read_counters(list(ifaces) + list(veth_map.values()))
        samples: Dict[str, Tuple[int, int]] = {}
        for iface in ifaces:
            if iface in raw:
                samples[iface] = traffic.iface_sample(raw[iface])
        for iface, veth in veth_map.items():
            if veth in raw:
                samples[f"ns:{iface}"] = traffic.veth_sample(raw[veth])
        return samples


class _LinuxAppsBackend:
    def scan_apps(self) -> List[Any]:
        from intermux.apps import desktop_entries

        return desktop_entries.scan_desktop_entries()

    def resolve_app_path(self, app: str) -> str:
        from intermux import apps

        return apps.resolve_app_path(app)

    def get_handler(self, app_path: str) -> Any:
        from intermux import apps

        return apps.get_handler(app_path)


class _LinuxElevationBackend:
    def is_elevated(self) -> bool:
        return hasattr(os, "geteuid") and os.geteuid() == 0

    def helper_call(self, action: str, params: Optional[Dict[str, Any]] = None, *, timeout: float = 300.0) -> Any:
        from intermux.helper import client

        return client.call(action, params, timeout=timeout)

    def helper_batch(self, ops: Sequence[Dict[str, Any]], *, timeout: float = 600.0) -> list:
        from intermux.helper import client

        return client.batch(ops, timeout=timeout)

    def passwordless_status(self) -> Dict[str, Any]:
        from intermux.system import passwordless

        return passwordless.status()


class _LinuxVpnBackend:
    def is_available(self) -> bool:
        from intermux.system import nm

        return nm.nmcli_bin() is not None

    def list_connections(self) -> List[Any]:
        from intermux.system import nm

        return nm.list_connections()

    def connect(self, name: str, *, apps_only: bool = True) -> Any:
        from intermux.system import nm

        return nm.connect(name, apps_only=apps_only)

    def disconnect(self, name: str) -> Any:
        from intermux.system import nm

        return nm.disconnect(name)

    def import_config(self, path: str) -> Any:
        from intermux.system import nm

        return nm.import_connection(path)


class _LinuxDiagnosticsBackend:
    def check_dependencies(self) -> List[Any]:
        from intermux.system import deps

        return deps.check_dependencies()

    def missing_required(self) -> List[Any]:
        from intermux.system import deps

        return deps.missing_required()

    def os_info(self) -> Any:
        from intermux.system import deps

        return deps.distro_info()

    def open_with_desktop(self, target: str, *, timeout: float = 5.0) -> Any:
        from intermux.system import commands

        return commands.open_with_desktop(target, timeout=timeout)


class LinuxPlatform:
    """Platform backend for Linux v2.2.1."""

    name = "linux"

    def __init__(self) -> None:
        self._paths: Optional[PlatformPaths] = None
        self.network = _LinuxNetworkBackend()
        self.apps = _LinuxAppsBackend()
        self.elevation = _LinuxElevationBackend()
        self.vpn = _LinuxVpnBackend()
        self.diagnostics = _LinuxDiagnosticsBackend()

    @property
    def paths(self) -> PlatformPaths:
        if self._paths is None:
            self._paths = resolve_paths(platform="linux")
        return self._paths

    def is_elevated(self) -> bool:
        return self.elevation.is_elevated()

    def detect_app_env(self) -> Dict[str, str]:
        from intermux.system import session

        return session.build_app_env(session.detect_session())

    def local_proc_count(self, app_path: str, comm_names: Sequence[str]) -> int:
        from intermux.system import procs

        return len(procs.find_app_procs(app_path, comm_names, uid=os.getuid()))

    def format_system_env(self, environ: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
        from intermux.system import commands

        return commands.desktop_env(environ)
