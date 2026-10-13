"""Network interface discovery from `ip -j` output and sysfs."""
from __future__ import annotations

import ipaddress
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from intermux.config import VIRTUAL_PREFIXES
from intermux.system import commands

log = logging.getLogger("intermux.netinfo")

_DOWN_STATES = ("DOWN", "LOWERLAYERDOWN")
_DEFAULT_DSTS = ("default", "0.0.0.0/0")


@dataclass
class Address:
    family: str                  # "inet" | "inet6"
    local: str
    prefixlen: int
    peer: Optional[str]          # point-to-point peer; None otherwise
    scope: str


@dataclass
class Interface:
    name: str
    kind: str                    # "wifi" | "ethernet" | "usb" | "bluetooth" | "mobile" | "virtual" | "other"
    up: bool
    operstate: str
    flags: List[str]
    mac: Optional[str]
    mtu: Optional[int]
    addresses: List[Address]
    default_route: bool          # a default route uses this dev in the main table
    gateways: List[str]          # IPv4 gateways of default routes via this dev (may be empty for p2p)

    @property
    def ipv4_cidrs(self) -> List[str]:
        return [f"{a.local}/{a.prefixlen}" for a in self.addresses if a.family == "inet"]

    @property
    def is_virtual(self) -> bool:
        return is_virtual_name(self.name) or self.kind == "virtual"

    @property
    def bindable(self) -> bool:
        return self.up and bool(self.ipv4_cidrs) and not self.is_virtual and self.default_route

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def is_virtual_name(name: str) -> bool:
    """True when the interface name starts with one of config.VIRTUAL_PREFIXES."""
    return name.startswith(tuple(VIRTUAL_PREFIXES))


def classify_kind(name: str, sys_class_net: Path = Path("/sys/class/net")) -> str:
    """Classify an interface by its name and its sysfs entry."""
    if is_virtual_name(name):
        return "virtual"
    dev = Path(sys_class_net) / name
    if _exists(dev / "wireless") or _exists(dev / "phy80211"):
        return "wifi"
    if name.startswith("wwan"):
        return "mobile"
    if name.startswith("bnep"):
        return "bluetooth"
    if "/usb" in os.path.realpath(str(dev / "device")):
        return "usb"
    if name.startswith(("en", "eth")):
        return "ethernet"
    return "other"


def parse_ip_json(links: List[Dict[str, Any]], addrs: List[Dict[str, Any]], routes: List[Dict[str, Any]],
                  sys_class_net: Path = Path("/sys/class/net")) -> List[Interface]:
    """Build interfaces from `ip -j` link, address and main-table route output. Reads sysfs only for kind."""
    addresses: Dict[str, List[Address]] = {}
    for entry in _dict_items(addrs):
        ifname = _str_or_none(entry.get("ifname"))
        if ifname is None:
            continue
        for info in _dict_items(entry.get("addr_info")):
            address = _parse_address(info)
            if address is not None:
                addresses.setdefault(ifname, []).append(address)

    default_devs: Set[str] = set()
    gateways: Dict[str, List[str]] = {}
    for route in _dict_items(routes):
        if route.get("dst") not in _DEFAULT_DSTS:
            continue
        dev = _str_or_none(route.get("dev"))
        if dev is None:
            continue
        default_devs.add(dev)
        gateway = _str_or_none(route.get("gateway"))
        if gateway is not None and _is_ipv4(gateway):
            via = gateways.setdefault(dev, [])
            if gateway not in via:
                via.append(gateway)

    interfaces: List[Interface] = []
    for link in _dict_items(links):
        name = _str_or_none(link.get("ifname"))
        if name is None or name == "lo":
            continue
        raw_flags = link.get("flags")
        flags = [f for f in raw_flags if isinstance(f, str)] if isinstance(raw_flags, list) else []
        operstate = _str_or_none(link.get("operstate")) or "UNKNOWN"
        interfaces.append(Interface(
            name=name,
            kind=classify_kind(name, sys_class_net),
            up="UP" in flags and operstate not in _DOWN_STATES,
            operstate=operstate,
            flags=flags,
            mac=_str_or_none(link.get("address")),
            mtu=_int_or_none(link.get("mtu")),
            addresses=list(addresses.get(name, [])),
            default_route=name in default_devs,
            gateways=list(gateways.get(name, [])),
        ))
    return interfaces


def list_interfaces() -> List[Interface]:
    """Query `ip` for links, addresses and main-table routes. Returns [] when ip is not installed."""
    ip_bin = commands.which("ip")
    if ip_bin is None:
        log.warning("ip not found; no interfaces listed")
        return []
    links = commands.json_cmd([ip_bin, "-j", "link", "show"])
    addrs = commands.json_cmd([ip_bin, "-j", "addr", "show"])
    routes = commands.json_cmd([ip_bin, "-j", "route", "show", "table", "main"])
    return parse_ip_json(links, addrs, routes)


def _exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def _dict_items(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _str_or_none(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _int_or_none(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _is_ipv4(text: str) -> bool:
    try:
        return ipaddress.ip_address(text).version == 4
    except ValueError:
        return False


def _parse_address(info: Dict[str, Any]) -> Optional[Address]:
    family = info.get("family")
    local = _str_or_none(info.get("local"))
    if family not in ("inet", "inet6") or local is None:
        return None
    prefixlen = _int_or_none(info.get("prefixlen"))
    if prefixlen is None:
        prefixlen = 32 if family == "inet" else 128
    # ip reports the far end of a point-to-point link in "address"
    peer = _str_or_none(info.get("address"))
    if peer == local:
        peer = None
    return Address(family=family, local=local, prefixlen=prefixlen, peer=peer,
                   scope=_str_or_none(info.get("scope")) or "")
