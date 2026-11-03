"""DNS servers chosen per interface namespace: presets, parsing and validation. Pure.

A namespace has its own loopback and no IPv6, so only upstream IPv4 resolvers make sense: local stubs such
as systemd-resolved's 127.0.0.53, link-local, multicast and unspecified addresses are rejected.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any, Dict, List, Sequence, Tuple

MAX_SERVERS = 4

PRESETS: Dict[str, Tuple[str, ...]] = {
    "Cloudflare": ("1.1.1.1", "1.0.0.1"),
    "Google": ("8.8.8.8", "8.8.4.4"),
    "Quad9": ("9.9.9.9", "149.112.112.112"),
}

AUTO = "auto"      # pick servers for the interface (its gateway first, then public resolvers)
KEEP = "keep"      # keep what the namespace already uses; automatic for a new namespace


def valid_server(text: Any) -> bool:
    """True for a usable upstream IPv4 resolver address."""
    if not isinstance(text, str):
        return False
    try:
        addr = ipaddress.IPv4Address(text)
    except ValueError:
        return False
    return not (addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_unspecified
                or addr.is_reserved or str(addr) == "255.255.255.255")


def parse_servers(text: str) -> List[str]:
    """Servers from user input: "1.1.1.1, 9.9.9.9", "1.1.1.1 9.9.9.9" or a preset name. Raises ValueError."""
    if not isinstance(text, str):
        raise ValueError("DNS servers must be text")
    stripped = text.strip()
    for name, servers in PRESETS.items():
        if stripped.lower() == name.lower():
            return list(servers)
        if stripped.lower().startswith(f"{name.lower()}:"):
            # "Cloudflare: 1.1.1.1, 1.0.0.1" as the GUI lists it: the addresses after the name are what counts.
            stripped = stripped[len(name) + 1:].strip()
            if not stripped:
                return list(servers)
            break
    tokens = [t for t in re.split(r"[,;\s]+", stripped) if t]
    if not tokens:
        raise ValueError("no DNS servers given")
    servers: List[str] = []
    for token in tokens:
        if not valid_server(token):
            raise ValueError(f"not a usable IPv4 DNS server: {token}")
        canonical = str(ipaddress.IPv4Address(token))
        if canonical not in servers:
            servers.append(canonical)
    if len(servers) > MAX_SERVERS:
        raise ValueError(f"at most {MAX_SERVERS} DNS servers")
    return servers


def check_servers(value: Any) -> List[str]:
    """Validate a list of servers from a request. Raises ValueError."""
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_SERVERS:
        raise ValueError(f"DNS servers must be a list of 1 to {MAX_SERVERS} IPv4 addresses")
    servers: List[str] = []
    for item in value:
        if not valid_server(item):
            raise ValueError(f"not a usable IPv4 DNS server: {item!r}")
        canonical = str(ipaddress.IPv4Address(item))
        if canonical not in servers:
            servers.append(canonical)
    return servers


def preset_name(servers: Sequence[str]) -> str:
    """The preset these servers match, or "" ."""
    for name, preset in PRESETS.items():
        if list(servers) == list(preset):
            return name
    return ""


def describe(servers: Sequence[str]) -> str:
    """Short label: "Automatic", "Cloudflare" or "1.1.1.1, 9.9.9.9"."""
    if not servers:
        return "Automatic"
    return preset_name(servers) or ", ".join(servers)
