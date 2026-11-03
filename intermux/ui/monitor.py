"""Connection helpers for the Bindings table: which interfaces to sample, and the state shown in Status."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from intermux.system import traffic
from intermux.ui import graph

VPN_PREFIXES = ("tun", "tap", "wg", "ppp", "tailscale", "nordlynx", "proton")
KIND_ORDER = ("ethernet", "wifi", "usb", "mobile", "bluetooth", "vpn", "other")
SATURATED = 0.9      # share of the link speed at which a lane turns red


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def is_vpn_name(name: str) -> bool:
    return name.startswith(VPN_PREFIXES)


def visible_lanes(interfaces: Iterable[Any], bound: Iterable[str]) -> List[Any]:
    """Interfaces worth a lane: physical or VPN links that are up, plus any link an app is bound to. Pure.

    Ordered with routable links first, then by kind and name, so lanes do not jump around between refreshes.
    """
    bound = set(bound)
    chosen = []
    for iface in interfaces:
        name = iface.name
        if name == "lo":
            continue
        vpn = is_vpn_name(name)
        if getattr(iface, "is_virtual", False) and not vpn:
            continue
        if iface.up or name in bound:
            chosen.append(iface)

    def key(iface: Any) -> Tuple[int, int, str]:
        kind = graph.kind_for(iface)
        rank = KIND_ORDER.index(kind) if kind in KIND_ORDER else len(KIND_ORDER)
        return (0 if getattr(iface, "bindable", False) else 1, rank, iface.name)

    return sorted(chosen, key=key)


def lane_state(up: bool, bindable: bool, routed: int, down: float, upload: float,
               link_speed: Optional[float]) -> Tuple[str, str]:
    """(tone, words) for a lane. Tones: "ok" (routing), "hot", "active", "idle", "off". Pure."""
    if not up:
        return "off", "Disconnected"
    busiest = max(down, upload)
    if link_speed and busiest >= SATURATED * link_speed:
        return "hot", "Saturated"
    if routed:
        return "ok", f"Routing {routed} app" + ("" if routed == 1 else "s")
    if not bindable:
        return ("active", "Active, no gateway") if busiest >= traffic.IDLE_RATE else ("idle", "No gateway")
    if busiest >= traffic.IDLE_RATE:
        return "active", "Active"
    return "idle", "Idle"


def tone_colour(tone: str, palette: Dict[str, str]) -> str:
    return {
        "ok": palette["success"], "hot": palette["danger"], "active": palette["accent_fg"],
        "idle": palette["text_muted"], "off": palette["line_hi"],
    }.get(tone, palette["text_muted"])
