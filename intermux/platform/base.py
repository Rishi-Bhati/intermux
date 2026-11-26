"""Cross-platform backend contracts and platform-parameterized input validators."""

from __future__ import annotations

import ntpath
import posixpath
import re
import sys
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple, runtime_checkable

from intermux.errors import ValidationError

LINUX_IFACE_RE = r"^[A-Za-z0-9_.:@-]{1,15}$"
_LINUX_IFACE_PATTERN = re.compile(LINUX_IFACE_RE)
_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/].+")
_WIN_UNC_RE = re.compile(r"^\\\\[^\\/?*<>|\x00-\x1f]+\\[^\\/?*<>|\x00-\x1f]+.*")

_MAX_PATH = 4096
_MAX_WIN_IFACE = 256
_FORBIDDEN_CONTROL = ("\x00", "\n", "\r")


def normalize_platform(platform: Optional[str] = None) -> str:
    """Normalize sys.platform or friendly names to 'linux', 'windows', 'macos', or raw string."""
    raw = (platform or sys.platform).lower()
    if raw.startswith("win"):
        return "windows"
    if raw.startswith("linux"):
        return "linux"
    if raw == "darwin" or raw.startswith("mac"):
        return "macos"
    return raw


def has_control_chars(value: str, *, reject_all_c0: bool = False) -> bool:
    """True for strings the OS cannot safely accept (NUL/CR/LF, invalid UTF-8, or C0 controls)."""
    if any(ch in value for ch in _FORBIDDEN_CONTROL):
        return True
    if reject_all_c0 and any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return True
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def validate_iface_name(value: Any, *, field: str = "iface", platform: Optional[str] = None) -> str:
    """Validate an interface identifier according to the target platform's rules."""
    plat = normalize_platform(platform)
    if plat == "windows":
        if not isinstance(value, str) or not value or not value.strip():
            raise ValidationError(f"invalid interface name for {field}: must be non-empty")
        if len(value) > _MAX_WIN_IFACE or has_control_chars(value, reject_all_c0=True):
            raise ValidationError(f"invalid interface name for {field}: too long or contains control characters")
        if value in (".", "..") or ".." in value or "/" in value or "\\" in value:
            raise ValidationError(f"invalid interface name for {field}: path separators and '..' are not allowed")
        return value

    if not isinstance(value, str) or value in (".", "..") or not _LINUX_IFACE_PATTERN.fullmatch(value):
        raise ValidationError(
            f"invalid interface name for {field} "
            "(1 to 15 characters: letters, digits and _ . : @ -)"
        )
    return value


def validate_app_path(value: Any, *, field: str = "app", platform: Optional[str] = None) -> str:
    """Validate an application executable path according to the target platform's rules."""
    plat = normalize_platform(platform)
    if plat == "windows":
        if not isinstance(value, str) or not value:
            raise ValidationError(f"{field} must be an absolute path to a file")
        if has_control_chars(value, reject_all_c0=True) or len(value.encode("utf-8")) > _MAX_PATH:
            raise ValidationError(f"invalid {field}: too long or contains control characters")
        if "=" in value:
            raise ValidationError(f"{field} cannot contain '='; rename the file or use a shortcut")
        if value.endswith(("/", "\\")):
            raise ValidationError(f"{field} must be an absolute path to a file")
        if not (_WIN_DRIVE_RE.match(value) or _WIN_UNC_RE.match(value)) or not ntpath.isabs(value):
            raise ValidationError(f"{field} must be an absolute Windows path to a file")
        return value

    if not isinstance(value, str) or not value.startswith("/") or value.endswith("/"):
        raise ValidationError(f"{field} must be an absolute path to a file")
    if has_control_chars(value) or len(value.encode("utf-8")) > _MAX_PATH:
        raise ValidationError(f"invalid {field}: too long or contains control characters")
    if "=" in value:
        raise ValidationError(f"{field} cannot contain '='; rename the file or use a link to it")
    return value


def is_abs_app_path(value: Any, platform: Optional[str] = None) -> bool:
    """True when value is an absolute executable path for the target platform."""
    if not isinstance(value, str) or not value:
        return False
    plat = normalize_platform(platform)
    if plat == "windows":
        return bool(_WIN_DRIVE_RE.match(value) or _WIN_UNC_RE.match(value)) and ntpath.isabs(value)
    return posixpath.isabs(value)


@runtime_checkable
class NetworkBackend(Protocol):
    """Per-app routing, interface discovery, shaping, DNS, and live traffic counters."""

    def list_interfaces(self, include_all: bool = False) -> List[Any]: ...
    def status(self) -> Dict[str, Any]: ...
    def bind(self, app: str, iface: str, **kwargs: Any) -> Dict[str, Any]: ...
    def adopt(self, iface: str, app: str, **kwargs: Any) -> Dict[str, Any]: ...
    def release_adoption(self, adopt_id: str, **kwargs: Any) -> Dict[str, Any]: ...
    def stop_app(self, iface: str, app: str, **kwargs: Any) -> Dict[str, Any]: ...
    def kill_app(self, app: str, *, force: bool = False, **kwargs: Any) -> Dict[str, Any]: ...
    def restart_app(self, iface: str, app: str, **kwargs: Any) -> Dict[str, Any]: ...
    def move_app(self, iface: str, app: str, to_iface: str, **kwargs: Any) -> Dict[str, Any]: ...
    def set_dns(self, iface: str, dns: Any, **kwargs: Any) -> Dict[str, Any]: ...
    def set_limit(self, iface: str, down: Optional[float], up: Optional[float], **kwargs: Any) -> Dict[str, Any]: ...
    def remove_namespace(self, iface: str, **kwargs: Any) -> Dict[str, Any]: ...
    def reconnect(self) -> Dict[str, Any]: ...
    def teardown_idle(self) -> Dict[str, Any]: ...
    def reset(self, *, kill_apps: bool = False, **kwargs: Any) -> Dict[str, Any]: ...
    def verify(self, iface: str) -> Dict[str, Any]: ...
    def read_traffic_counters(
        self, ifaces: Sequence[str], bound_ifaces: Sequence[str]
    ) -> Dict[str, Tuple[int, int]]: ...


@runtime_checkable
class AppDiscoveryBackend(Protocol):
    """Installed application discovery, path resolution, and browser profile handlers."""

    def scan_apps(self) -> List[Any]: ...
    def resolve_app_path(self, app: str) -> str: ...
    def get_handler(self, app_path: str) -> Any: ...


@runtime_checkable
class ElevationBackend(Protocol):
    """Privilege check, helper IPC client, and passwordless elevation status/toggle."""

    def is_elevated(self) -> bool: ...
    def helper_call(self, action: str, params: Optional[Dict[str, Any]] = None, *, timeout: float = 300.0) -> Any: ...
    def helper_batch(self, ops: Sequence[Dict[str, Any]], *, timeout: float = 600.0) -> list: ...
    def passwordless_status(self) -> Dict[str, Any]: ...


@runtime_checkable
class VpnBackend(Protocol):
    """VPN connection listing, connect/disconnect, and config import."""

    def is_available(self) -> bool: ...
    def list_connections(self) -> List[Any]: ...
    def connect(self, name: str, *, apps_only: bool = True) -> Any: ...
    def disconnect(self, name: str) -> Any: ...
    def import_config(self, path: str) -> Any: ...


@runtime_checkable
class DiagnosticsBackend(Protocol):
    """OS dependency checks, desktop opener, and OS environment metadata."""

    def check_dependencies(self) -> List[Any]: ...
    def missing_required(self) -> List[Any]: ...
    def os_info(self) -> Any: ...
    def open_with_desktop(self, target: str, *, timeout: float = 5.0) -> Any: ...


@runtime_checkable
class PlatformBackend(Protocol):
    """Aggregated platform capabilities for Linux, Windows, and future OS ports."""

    name: str
    paths: Any
    network: NetworkBackend
    apps: AppDiscoveryBackend
    elevation: ElevationBackend
    vpn: VpnBackend
    diagnostics: DiagnosticsBackend

    def is_elevated(self) -> bool: ...
    def detect_app_env(self) -> Dict[str, str]: ...
    def local_proc_count(self, app_path: str, comm_names: Sequence[str]) -> int: ...
    def format_system_env(self, environ: Optional[Mapping[str, str]] = None) -> Dict[str, str]: ...
