"""Platform abstraction entrypoints for InterMux (Linux, Windows, future macOS/Android)."""

from __future__ import annotations

import ctypes
import os
import sys
from typing import Any, Optional

from intermux.platform.base import (
    AppDiscoveryBackend,
    DiagnosticsBackend,
    ElevationBackend,
    NetworkBackend,
    PlatformBackend,
    VpnBackend,
    is_abs_app_path,
    normalize_platform,
    validate_app_path,
    validate_iface_name,
)
from intermux.platform.paths import PlatformPaths, resolve_paths

__all__ = [
    "AppDiscoveryBackend",
    "DiagnosticsBackend",
    "ElevationBackend",
    "NetworkBackend",
    "PlatformBackend",
    "PlatformPaths",
    "VpnBackend",
    "current_platform",
    "get_platform",
    "is_abs_app_path",
    "is_elevated",
    "is_linux",
    "is_windows",
    "normalize_platform",
    "get_paths",
    "reset_cached_platform",
    "validate_app_path",
    "validate_iface_name",
]

_CACHED_PLATFORM: Optional[Any] = None


def current_platform() -> str:
    """Return normalized platform identifier ('linux', 'windows', 'macos', ...)."""
    return normalize_platform(sys.platform)


def is_windows() -> bool:
    return current_platform() == "windows"


def is_linux() -> bool:
    return current_platform() == "linux"


def is_elevated() -> bool:
    """True when running as root on POSIX or Administrator on Windows."""
    if is_windows():
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except Exception:
            return False
    return hasattr(os, "geteuid") and os.geteuid() == 0


def get_platform(platform: Optional[str] = None) -> PlatformBackend:
    """Return the PlatformBackend for the active (or specified) OS."""
    global _CACHED_PLATFORM
    if platform is None and _CACHED_PLATFORM is not None:
        return _CACHED_PLATFORM

    target = normalize_platform(platform)
    backend: Any
    if target == "windows":
        from intermux.platform.windows import WindowsPlatform

        backend = WindowsPlatform()
    else:
        from intermux.platform.linux import LinuxPlatform

        backend = LinuxPlatform()

    if platform is None:
        _CACHED_PLATFORM = backend
    return backend


def reset_cached_platform() -> None:
    """Clear cached platform backend singleton (primarily for tests)."""
    global _CACHED_PLATFORM
    _CACHED_PLATFORM = None


def get_paths(platform: Optional[str] = None) -> PlatformPaths:
    """Return resolved OS paths for the active (or requested) platform."""
    if platform is not None:
        return resolve_paths(platform=platform)
    return resolve_paths(platform=current_platform())
