"""Windows VPN connection management (RAS / WireGuard / OpenVPN) with graceful fallback."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, List, Optional

from intermux.errors import MissingToolError, SystemCommandError


@dataclass
class WinVpnConnection:
    name: str
    uuid: str
    kind: str
    active: bool
    device: str = ""


def _ps_json(cmd: str) -> Any:
    if sys.platform != "win32":
        return None
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", cmd],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return None


def is_available() -> bool:
    return sys.platform == "win32"


def list_connections() -> List[WinVpnConnection]:
    if sys.platform != "win32":
        return []
    raw = _ps_json(
        "Get-VpnConnection -ErrorAction SilentlyContinue | "
        "Select-Object Name,ConnectionStatus,TunnelType,SplitTunneling | ConvertTo-Json -Compress"
    )
    rows = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
    conns: List[WinVpnConnection] = []
    for r in rows:
        name = str(r.get("Name") or "")
        if not name:
            continue
        conns.append(
            WinVpnConnection(
                name=name,
                uuid=name,
                kind=str(r.get("TunnelType") or "vpn").lower(),
                active=str(r.get("ConnectionStatus") or "").lower() == "connected",
                device=name,
            )
        )
    conns.sort(key=lambda c: (not c.active, c.name.casefold()))
    return conns


def connect(name: str, *, apps_only: bool = True) -> None:
    if sys.platform != "win32":
        raise MissingToolError("Windows VPN controls require Windows", code="missing_tool")
    if apps_only:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             f"Set-VpnConnection -Name '{name.replace(chr(39), chr(39)*2)}' -SplitTunneling $true -ErrorAction SilentlyContinue"],
            capture_output=True,
            timeout=8,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    proc = subprocess.run(
        ["rasdial.exe", name],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0:
        raise SystemCommandError(proc.stderr.strip() or proc.stdout.strip() or f"rasdial failed for {name}")


def disconnect(name: str) -> None:
    if sys.platform != "win32":
        raise MissingToolError("Windows VPN controls require Windows", code="missing_tool")
    proc = subprocess.run(
        ["rasdial.exe", name, "/DISCONNECT"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0:
        raise SystemCommandError(proc.stderr.strip() or proc.stdout.strip() or f"rasdial disconnect failed for {name}")


def import_config(path: str) -> WinVpnConnection:
    raise MissingToolError(
        "Automatic .conf/.ovpn import is not built into Windows RAS; import the tunnel in WireGuard or OpenVPN Connect and it will appear as a bindable adapter.",
        code="missing_tool",
    )
