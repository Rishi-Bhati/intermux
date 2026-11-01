"""The network view: one lane per connection with its live traffic and the apps InterMux routes over it.

Modelled on the "InterMux on / off" monitor on the website: each lane shows the connection, how fast it is
going right now, whether it is coping, the apps riding on it as coloured chips, and a scrolling graph.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from intermux.system import traffic
from intermux.ui import graph, theme

try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:  # reported by main()
    tk = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]

KIND_NAMES = {
    "wifi": "Wi-Fi", "ethernet": "Ethernet", "usb": "Tethering", "bluetooth": "Bluetooth",
    "mobile": "Mobile data", "vpn": "VPN", "virtual": "Virtual", "other": "Network",
}
VPN_PREFIXES = ("tun", "tap", "wg", "ppp", "tailscale", "nordlynx", "proton")
KIND_ORDER = ("ethernet", "wifi", "usb", "mobile", "bluetooth", "vpn", "other")
SATURATED = 0.9      # share of the link speed at which a lane turns red
GRAPH_HEIGHT = 56


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def is_vpn_name(name: str) -> bool:
    return name.startswith(VPN_PREFIXES)


def lane_title(iface: Any) -> str:
    """Friendly name such as "Wi-Fi" or "VPN". Pure."""
    return KIND_NAMES.get(graph.kind_for(iface), "Network")


def lane_detail(iface: Any) -> str:
    """Second line such as "wlan0 · 192.168.1.20". Pure."""
    cidrs = list(getattr(iface, "ipv4_cidrs", []) or [])
    addr = cidrs[0].split("/")[0] if cidrs else "no IPv4 address"
    return f"{iface.name} · {addr}"


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


class ColourBook:
    """Gives every app a lasting colour, in order of first appearance, so it is easy to follow."""

    def __init__(self, colours: Sequence[str] = theme.APP_COLOURS) -> None:
        self.colours = tuple(colours)
        self._given: Dict[str, str] = {}

    def colour(self, key: str) -> str:
        if key not in self._given:
            self._given[key] = self.colours[len(self._given) % len(self.colours)]
        return self._given[key]


def graph_scale(history: Sequence[Tuple[float, float]]) -> float:
    """A round ceiling over both directions of a lane's history. Pure."""
    peak = max((max(d, u) for d, u in history), default=0.0)
    return traffic.nice_ceiling(peak * 1.1)


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------

class TrafficRow:
    """One connection in the Traffic panel: its name and live speed, and a two-minute graph."""

    def __init__(self, parent: Any, iface: Any) -> None:
        p = theme.current_palette()
        self.p = p
        bg = p["surface"]
        self.name = iface.name
        self.iface = iface
        self.frame = tk.Frame(parent, background=bg)
        self.frame.columnconfigure(1, weight=1)
        label_width = theme.font("small").measure("↓ 999 KB/s    ↑ 999 KB/s") + 8
        left = tk.Frame(self.frame, background=bg)
        left.grid(row=0, column=0, sticky="nw", padx=(16, 12), pady=(8, 6))
        left.columnconfigure(1, weight=1)
        tk.Frame(left, background=bg, width=label_width, height=1).grid(row=3, column=0, columnspan=2)
        self.dot = graph.status_dot(left, p["text_muted"], bg)
        self.dot.grid(row=0, column=0, sticky="w")
        self.title = tk.Label(left, text="", background=bg, foreground=p["text"], font=theme.font("label"),
                              anchor="w")
        self.title.grid(row=0, column=1, sticky="w", padx=(6, 0))
        self.down = tk.Label(left, text="", background=bg, foreground=p["text"], font=theme.font("small"), anchor="w")
        self.down.grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 0))
        self.up = tk.Label(left, text="", background=bg, foreground=p["violet"], font=theme.font("small"), anchor="w")
        self.up.grid(row=2, column=0, columnspan=2, sticky="w")
        self.graph = graph.TrafficGraph(self.frame, bg, height=GRAPH_HEIGHT)
        self.graph.canvas.grid(row=0, column=1, sticky="nsew", padx=(0, 16), pady=(4, 6))
        self.separator = tk.Frame(parent, background=p["border"], height=1)
        self.set_interface(iface)

    def set_interface(self, iface: Any) -> None:
        self.iface = iface
        self.title.configure(text=f"{lane_title(iface)}  ·  {iface.name}")

    def set_traffic(self, history: Sequence[Tuple[float, float]], tone: str, link_speed: Optional[float]) -> None:
        p = self.p
        down, up = history[-1] if history else (0.0, 0.0)
        colour = tone_colour(tone, p)
        graph.set_dot(self.dot, colour)
        self.down.configure(text=f"↓ {traffic.format_rate(down)}")
        self.up.configure(text=f"↑ {traffic.format_rate(up)}")
        scale = graph_scale(history)
        if link_speed and scale > link_speed:
            scale = link_speed
        line = colour if tone not in ("idle", "off") else p["text_muted"]
        self.graph.update([d for d, _ in history], [u for _, u in history], scale, line,
                          traffic.scale_label(scale))

    def destroy(self) -> None:
        self.frame.destroy()
        self.separator.destroy()
