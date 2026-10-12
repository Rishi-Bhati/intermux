from __future__ import annotations

import ipaddress
from typing import Iterable, List, Optional

from intermux import config
from intermux.errors import InterMuxError

IPv4Network = ipaddress.IPv4Network
IPv4Address = ipaddress.IPv4Address

POOL = ipaddress.IPv4Network(f"10.{config.SUBNET_BASE_OCTET}.0.0/16")


def subnet_for_index(n: int) -> IPv4Network:
    """Return 10.200.n.0/24 for n in 1..254."""
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 254:
        raise ValueError(f"subnet index must be in 1..254, got {n!r}")
    return ipaddress.IPv4Network(f"10.{config.SUBNET_BASE_OCTET}.{n}.0/24")


def index_of(net: IPv4Network) -> int:
    """Inverse of subnet_for_index; ValueError if net is not a 10.200.N.0/24 pool subnet."""
    if not isinstance(net, ipaddress.IPv4Network) or net.prefixlen != 24:
        raise ValueError(f"not a /24 IPv4 subnet: {net}")
    if net.network_address not in POOL:
        raise ValueError(f"{net} is outside the {POOL} pool")
    n = net.network_address.packed[2]
    if not 1 <= n <= 254:
        raise ValueError(f"{net} has an index outside 1..254")
    return n


def host_ip(net: IPv4Network) -> IPv4Address:
    """Host side of the veth pair (network address + 1)."""
    return net.network_address + 1


def ns_ip(net: IPv4Network) -> IPv4Address:
    """Namespace side of the veth pair (network address + 2)."""
    return net.network_address + 2


def used_networks(cidrs: Iterable[str]) -> List[IPv4Network]:
    """Parse CIDR strings into IPv4 networks, skipping anything unparsable or IPv6."""
    nets: List[IPv4Network] = []
    for raw in cidrs:
        try:
            nets.append(ipaddress.IPv4Network(str(raw).strip(), strict=False))
        except (TypeError, ValueError):
            continue
    return nets


def pick_subnet(used: Iterable[IPv4Network], preferred: Optional[IPv4Network] = None) -> IPv4Network:
    """Return preferred if it is a free pool subnet, else the lowest free 10.200.N.0/24."""
    taken = list(used)

    def is_free(net: IPv4Network) -> bool:
        return not any(net.overlaps(u) for u in taken)

    if preferred is not None:
        try:
            index_of(preferred)
        except ValueError:
            pass
        else:
            if is_free(preferred):
                return preferred

    for n in range(1, 255):
        candidate = subnet_for_index(n)
        if is_free(candidate):
            return candidate

    raise InterMuxError("no free subnet left in the 10.200.0.0/16 pool", code="no_subnet")
