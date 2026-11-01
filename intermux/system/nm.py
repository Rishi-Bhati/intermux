"""VPN connections through NetworkManager (nmcli), run as the user. No root needed.

InterMux routes single apps through a VPN, so a VPN connected from InterMux is set to never become the
system's default route ("ipv4.never-default"): the tunnel comes up with its address, the rest of the
system keeps its normal route, and only apps routed through the tunnel use it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List, Optional

from intermux.errors import InterMuxError, MissingToolError
from intermux.system import commands

VPN_TYPES = {"vpn": "VPN", "wireguard": "WireGuard"}
IMPORT_TYPES = {".conf": "wireguard", ".ovpn": "openvpn"}
UP_TIMEOUT = 90.0


@dataclass
class VpnConnection:
    name: str
    uuid: str
    type: str        # "vpn" (OpenVPN, WireGuard plugin, ...) or "wireguard"
    device: str      # interface while active, "" otherwise
    active: bool

    @property
    def kind_label(self) -> str:
        return VPN_TYPES.get(self.type, self.type)


def split_terse(line: str) -> List[str]:
    """Split one `nmcli -t` line on unescaped colons and unescape \\: and \\\\. Pure."""
    fields: List[str] = []
    current: List[str] = []
    escaped = False
    for ch in line:
        if escaped:
            current.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(ch)
    fields.append("".join(current))
    return fields


def parse_connections(output: str) -> List[VpnConnection]:
    """VPN and WireGuard rows of `nmcli -t -f NAME,UUID,TYPE,DEVICE,STATE connection show`. Pure."""
    found: List[VpnConnection] = []
    for line in output.splitlines():
        parts = split_terse(line)
        if len(parts) < 5:
            continue
        name, uuid, kind, device, state = parts[:5]
        if kind not in VPN_TYPES or not uuid:
            continue
        device = "" if device in ("", "--") else device
        found.append(VpnConnection(name=name, uuid=uuid, type=kind, device=device,
                                   active=state in ("activated", "activating")))
    return sorted(found, key=lambda c: (not c.active, c.name.lower()))


def _nmcli() -> str:
    found = commands.which("nmcli")
    if found is None:
        raise MissingToolError("NetworkManager (nmcli) is not installed. Bring the VPN up with its own "
                               "tool (for example wg-quick up wg0); it then appears in InterMux.")
    return found


def available() -> bool:
    return commands.which("nmcli") is not None


def _check(argv: List[str], what: str, timeout: float = 30.0) -> str:
    result = commands.run(argv, timeout=timeout)
    if not result.ok:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise InterMuxError(f"{what}: {detail[-1] if detail else 'nmcli failed'}", code="nm_failed")
    return result.stdout


def list_vpns() -> List[VpnConnection]:
    out = _check([_nmcli(), "-t", "-f", "NAME,UUID,TYPE,DEVICE,STATE", "connection", "show"],
                 "could not list connections")
    return parse_connections(out)


def private_args(kind: str) -> List[str]:
    """nmcli modify arguments that keep a VPN from taking over the system's default route. Pure."""
    args = ["ipv4.never-default", "yes", "ipv6.never-default", "yes"]
    if kind == "wireguard":
        args += ["wireguard.ip4-auto-default-route", "no", "wireguard.ip6-auto-default-route", "no"]
    return args


def connect(conn: VpnConnection, apps_only: bool = True) -> None:
    """Bring the connection up; with apps_only, first make it never the default route."""
    nmcli = _nmcli()
    if apps_only:
        _check([nmcli, "connection", "modify", conn.uuid] + private_args(conn.type),
               f"could not configure {conn.name}")
    _check([nmcli, "--wait", str(int(UP_TIMEOUT)), "connection", "up", conn.uuid],
           f"could not connect {conn.name}", timeout=UP_TIMEOUT + 10)


def disconnect(conn: VpnConnection) -> None:
    _check([_nmcli(), "connection", "down", conn.uuid], f"could not disconnect {conn.name}")


_ADDED = re.compile(r"\(([0-9a-fA-F-]{36})\)")


def import_kind(path: str) -> Optional[str]:
    """nmcli import type for a VPN file, from its extension. Pure."""
    return IMPORT_TYPES.get(os.path.splitext(path)[1].lower())


def import_file(path: str) -> str:
    """Import a WireGuard (.conf) or OpenVPN (.ovpn) file; returns the new connection's UUID."""
    kind = import_kind(path)
    if kind is None:
        raise InterMuxError("choose a WireGuard .conf or an OpenVPN .ovpn file", code="nm_failed")
    out = _check([_nmcli(), "connection", "import", "type", kind, "file", path], "could not import the file")
    match = _ADDED.search(out)
    if not match:
        raise InterMuxError("the file was imported but its connection was not found", code="nm_failed")
    return match.group(1)
