"""Windows network interface discovery via IP Helper APIs and intermux-net.exe."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

log = logging.getLogger("intermux.platform.win_netinfo")

_VPN_KEYWORDS = (
    "wireguard", "wintun", "openvpn", "tap-windows", "tap0901", "nordlynx",
    "mullvad", "proton", "tailscale", "zerotier", "cisco", "anyconnect",
    "fortinet", "globalprotect", "pulse", "ras", "wan miniport", "ikev2",
    "l2tp", "pptp", "sstp", "vpn", "cloudflare warp",
)
_VIRTUAL_KEYWORDS = (
    "hyper-v", "vethernet", "vmware", "virtualbox", "vbox", "wsl", "docker",
    "npcap", "loopback", "teredo", "isatap", "6to4", "kernel debug",
)
_WIFI_KEYWORDS = ("wi-fi", "wifi", "wlan", "802.11", "wireless")
_USB_KEYWORDS = ("rndis", "usb", "tether", "apple mobile", "android")
_BT_KEYWORDS = ("bluetooth", "bnep", "pan")
_MOBILE_KEYWORDS = ("cellular", "mobile broadband", "wwan", "4g lte", "5g nr")


@dataclass
class WindowsInterface:
    name: str
    kind: str
    up: bool
    ipv4_cidrs: List[str] = field(default_factory=list)
    ipv6_cidrs: List[str] = field(default_factory=list)
    gateways: List[str] = field(default_factory=list)
    dns_servers: List[str] = field(default_factory=list)
    default_route: bool = False
    is_vpn: bool = False
    if_index: int = 0
    link_speed_bytes: Optional[float] = None
    rx_bytes: int = 0
    tx_bytes: int = 0
    mac: str = ""
    description: str = ""
    _bindable_override: Optional[bool] = None

    @property
    def bindable(self) -> bool:
        if self._bindable_override is not None:
            return self._bindable_override
        if not self.up or not self.ipv4_cidrs or self.kind == "virtual":
            return False
        if self.is_vpn or self.kind == "vpn":
            return True
        return self.default_route or bool(self.gateways) or bool(self.ipv4_cidrs)

    @property
    def unusable_reason(self) -> str:
        if self.bindable:
            return ""
        if self.kind == "virtual":
            return "Virtual switch/adapter cannot be bound"
        if not self.up:
            return "Interface is down"
        if not self.ipv4_cidrs:
            return "No IPv4 address assigned"
        return "Not bindable"


def classify_win_kind(if_type: int, name: str, description: str = "") -> str:
    combined = f"{name} {description}".lower()
    if any(k in combined for k in _VPN_KEYWORDS) or if_type in (23, 131):
        return "vpn"
    if any(k in combined for k in _VIRTUAL_KEYWORDS) or if_type == 24:
        return "virtual"
    if if_type == 71 or any(k in combined for k in _WIFI_KEYWORDS):
        return "wifi"
    if if_type in (243, 244) or any(k in combined for k in _MOBILE_KEYWORDS):
        return "mobile"
    if any(k in combined for k in _USB_KEYWORDS):
        return "usb"
    if any(k in combined for k in _BT_KEYWORDS):
        return "bluetooth"
    if if_type == 6:
        return "ethernet"
    if if_type == 53:
        return "virtual"
    return "other"


def parse_engine_interfaces(items: List[Dict[str, Any]]) -> List[WindowsInterface]:
    """Convert JSON items returned by intermux-net.exe interfaces/status into WindowsInterface objects."""
    out: List[WindowsInterface] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or "other")
        bps = raw.get("link_speed_bps")
        speed_bytes = float(bps) if isinstance(bps, (int, float)) and bps > 0 else None
        out.append(
            WindowsInterface(
                name=str(raw.get("name") or ""),
                kind=kind,
                up=bool(raw.get("up")),
                ipv4_cidrs=[str(x) for x in (raw.get("ipv4_cidrs") or [])],
                ipv6_cidrs=[str(x) for x in (raw.get("ipv6_cidrs") or [])],
                gateways=[str(x) for x in (raw.get("gateways") or [])],
                dns_servers=[str(x) for x in (raw.get("dns_servers") or [])],
                default_route=bool(raw.get("default_route")),
                is_vpn=(kind == "vpn"),
                if_index=int(raw.get("index") or 0),
                link_speed_bytes=speed_bytes,
                rx_bytes=int(raw.get("rx_bytes") or 0),
                tx_bytes=int(raw.get("tx_bytes") or 0),
                mac=str(raw.get("mac") or ""),
                description=str(raw.get("description") or ""),
                _bindable_override=bool(raw["bindable"]) if "bindable" in raw else None,
            )
        )
    return out


def list_interfaces_from_engine(engine_exe: Optional[str], include_all: bool = True) -> Optional[List[WindowsInterface]]:
    if not engine_exe:
        return None
    args = [engine_exe, "interfaces"]
    if include_all:
        args.append("--all")
    try:
        kwargs: Dict[str, Any] = {"capture_output": True, "text": True, "timeout": 10, "check": False}
        if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        proc = subprocess.run(args, **kwargs)
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        data = json.loads(proc.stdout)
        items = data.get("interfaces") if isinstance(data, dict) else None
        if isinstance(items, list):
            return parse_engine_interfaces(items)
    except Exception as exc:
        log.debug("engine interfaces probe failed: %s", exc)
    return None


def list_interfaces_powershell(include_all: bool = True) -> List[WindowsInterface]:
    """Fallback adapter discovery via PowerShell Get-NetIPConfiguration when engine exe is absent."""
    if sys.platform != "win32":
        return []
    ps_cmd = (
        "Get-NetAdapter | ForEach-Object {"
        " $a = $_;"
        " $cfg = Get-NetIPConfiguration -InterfaceIndex $a.ifIndex -ErrorAction SilentlyContinue;"
        " [PSCustomObject]@{"
        "  name=$a.Name; desc=$a.InterfaceDescription; ifIndex=$a.ifIndex;"
        "  up=($a.Status -eq 'Up'); linkSpeed=$a.LinkSpeed; mac=$a.MacAddress;"
        "  ipv4=@($cfg.IPv4Address | ForEach-Object { \"$($_.IPAddress)/$($_.PrefixLength)\" });"
        "  gw=@($cfg.IPv4DefaultGateway | ForEach-Object { $_.NextHop });"
        "  dns=@($cfg.DNSServer | Where-Object { $_.AddressFamily -eq 2 } | ForEach-Object { $_.ServerAddresses })"
        " }"
        "} | ConvertTo-Json -Depth 4 -Compress"
    )
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return []
        raw = json.loads(proc.stdout)
        rows = raw if isinstance(raw, list) else [raw]
        result: List[WindowsInterface] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            desc = str(item.get("desc") or "")
            if name.lower() == "intermux" or "loopback" in name.lower():
                continue
            kind = classify_win_kind(6, name, desc)
            gw = [str(g) for g in (item.get("gw") or []) if g]
            v4 = [str(ip) for ip in (item.get("ipv4") or []) if ip]
            iface = WindowsInterface(
                name=name,
                kind=kind,
                up=bool(item.get("up")),
                ipv4_cidrs=v4,
                gateways=gw,
                dns_servers=[str(d) for d in (item.get("dns") or []) if d],
                default_route=bool(gw),
                is_vpn=(kind == "vpn"),
                if_index=int(item.get("ifIndex") or 0),
                mac=str(item.get("mac") or ""),
                description=desc,
            )
            if include_all or iface.bindable or iface.up:
                result.append(iface)
        return result
    except Exception as exc:
        log.debug("PowerShell interface scan failed: %s", exc)
        return []
