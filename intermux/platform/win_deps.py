"""Windows prerequisite diagnostics and desktop opener."""

from __future__ import annotations

import os
import platform as std_platform
import subprocess
import sys
from dataclasses import dataclass
from typing import List

from intermux.platform import win_pipe


@dataclass
class WinOSInfo:
    id: str = "windows"
    name: str = "Windows"
    version_id: str = ""
    package_manager: str = "winget"


@dataclass
class WinDependency:
    tool: str
    required: bool
    present: bool
    purpose: str
    install_hint: str


@dataclass
class DesktopOpenResult:
    ok: bool
    stderr: str = ""


def os_info() -> WinOSInfo:
    return WinOSInfo(
        id="windows",
        name=f"Windows {std_platform.release()}",
        version_id=std_platform.version(),
        package_manager="winget",
    )


def check_dependencies() -> List[WinDependency]:
    pf_exe = win_pipe.installed_engine_path()
    wintun_dll = pf_exe.parent / "wintun.dll"
    engine_ok = pf_exe.is_file()
    wintun_ok = wintun_dll.is_file()
    if engine_ok and sys.platform == "win32":
        try:
            proc = subprocess.run(
                [str(pf_exe), "check"],
                capture_output=True,
                timeout=8,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode != 0:
                wintun_ok = False
        except Exception:
            pass
    return [
        WinDependency(
            tool="intermux-net.exe",
            required=True,
            present=engine_ok,
            purpose="elevated per-app Wintun networking engine",
            install_hint=f"Run the InterMux Windows installer to place intermux-net.exe in {pf_exe.parent}",
        ),
        WinDependency(
            tool="wintun.dll",
            required=True,
            present=wintun_ok,
            purpose="WireGuard Wintun L3 virtual adapter driver",
            install_hint=f"Run scripts/fetch-wintun.ps1 or reinstall InterMux into {pf_exe.parent}",
        ),
    ]


def missing_required() -> List[WinDependency]:
    return [d for d in check_dependencies() if d.required and not d.present]


def open_with_desktop(target: str, *, timeout: float = 5.0) -> DesktopOpenResult:
    if not target:
        raise ValueError("target must be non-empty")
    if sys.platform != "win32":
        return DesktopOpenResult(ok=False, stderr="not running on Windows")
    try:
        os.startfile(target)  # type: ignore[attr-defined]
        return DesktopOpenResult(ok=True)
    except OSError as exc:
        return DesktopOpenResult(ok=False, stderr=str(exc))
