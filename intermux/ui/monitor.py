"""The network view: one lane per connection with its live traffic and the apps InterMux routes over it.

Modelled on the "InterMux on / off" monitor on the website: each lane shows the connection, how fast it is
going right now, whether it is coping, the apps riding on it as coloured chips, and a scrolling graph.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from intermux import dnsconf
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
GRAPH_HEIGHT = 60
ID_WIDTH = 196


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


def namespace_badges(info: Optional[Dict[str, Any]]) -> str:
    """"DNS Cloudflare · ↓ 20 Mbit/s ↑ 5 Mbit/s" for a namespace with hand-picked settings, else "". Pure."""
    if not info:
        return ""
    from intermux.system import shaping

    parts = []
    if info.get("dns"):
        parts.append(f"DNS {dnsconf.describe(info['dns'])}")
    limit = info.get("limit") or {}
    caps = []
    if limit.get("down"):
        caps.append(f"↓ {shaping.format_mbit(limit['down'])}")
    if limit.get("up"):
        caps.append(f"↑ {shaping.format_mbit(limit['up'])}")
    if caps:
        parts.append("limit " + " ".join(caps))
    return "  ·  ".join(parts)


def graph_scale(history: Sequence[Tuple[float, float]]) -> float:
    """A round ceiling over both directions of a lane's history. Pure."""
    peak = max((max(d, u) for d, u in history), default=0.0)
    return traffic.nice_ceiling(peak * 1.1)


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------

def _hline(parent: Any, colour: str) -> Any:
    return tk.Frame(parent, background=colour, height=1, highlightthickness=0, borderwidth=0)


class Chip:
    """An app on a lane: a coloured mark, the app name, and a click target."""

    def __init__(self, parent: Any, key: str, name: str, colour: str, running: bool,
                 on_click: Callable[[str], None], on_menu: Optional[Callable[[str, Any], None]] = None,
                 note: str = "") -> None:
        p = theme.current_palette()
        face = p["surface_alt"]
        self.key = key
        self.frame = tk.Frame(parent, background=face, highlightthickness=1, highlightbackground=p["line_hi"],
                              cursor="hand2")
        mark = tk.Canvas(self.frame, width=10, height=10, background=face, highlightthickness=0, borderwidth=0)
        mark.create_rectangle(1, 1, 9, 9, fill=colour if running else p["line_hi"], outline="")
        mark.pack(side="left", padx=(8, 6), pady=6)
        text = name + (f"  {note}" if note else "")
        label = tk.Label(self.frame, text=text, background=face, font=theme.font("small"),
                         foreground=p["text"] if running else p["text_muted"])
        label.pack(side="left", padx=(0, 9))
        for widget in (self.frame, mark, label):
            widget.bind("<Button-1>", lambda _e: on_click(key))
            if on_menu is not None:
                widget.bind("<Button-3>", lambda e: on_menu(key, e))


class LaneRow:
    """One connection: identity, live rate and state on the left; app chips and a graph on the right."""

    def __init__(self, parent: Any, iface: Any, on_route: Callable[[str], None]) -> None:
        p = theme.current_palette()
        self.p = p
        self.name = iface.name
        bg = p["surface"]
        self.frame = tk.Frame(parent, background=bg, highlightthickness=0, borderwidth=0)
        self.frame.columnconfigure(1, weight=1)
        self.frame.rowconfigure(0, weight=1)

        ident = tk.Frame(self.frame, background=bg)
        ident.grid(row=0, column=0, sticky="nsw", padx=(18, 12), pady=(12, 10))
        ident.columnconfigure(1, weight=1)
        tk.Frame(ident, background=bg, width=ID_WIDTH, height=1).grid(row=9, column=0, columnspan=2, sticky="w")
        self.glyph_canvas = tk.Canvas(ident, width=22, height=22, background=bg, highlightthickness=0,
                                      borderwidth=0)
        self.glyph_canvas.grid(row=0, column=0, sticky="w")
        self.title = tk.Label(ident, text="", background=bg, foreground=p["text"], font=theme.font("section"),
                              anchor="w")
        self.title.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.detail = tk.Label(ident, text="", background=bg, foreground=p["text_muted"], font=theme.font("mono"),
                               anchor="w")
        self.detail.grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 0))

        stat = tk.Frame(ident, background=bg)
        stat.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.rate = tk.Label(stat, text="0", background=bg, foreground=p["text"], font=theme.font("stat"))
        self.rate.pack(side="left")
        self.unit = tk.Label(stat, text="B/s", background=bg, foreground=p["text_muted"], font=theme.font("small"))
        self.unit.pack(side="left", padx=(4, 0), pady=(8, 0))
        self.up = tk.Label(stat, text="", background=bg, foreground=p["violet"], font=theme.font("small"))
        self.up.pack(side="left", padx=(10, 0), pady=(8, 0))

        state = tk.Frame(ident, background=bg)
        state.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.dot = graph.status_dot(state, p["text_muted"], bg)
        self.dot.pack(side="left")
        self.state = tk.Label(state, text="", background=bg, foreground=p["text_muted"], font=theme.font("small"))
        self.state.pack(side="left", padx=(6, 0))
        self.badges = tk.Label(ident, text="", background=bg, foreground=p["text_muted"], font=theme.font("small"),
                               justify="left", anchor="w")
        self.badges.grid(row=4, column=0, columnspan=2, sticky="w", pady=(2, 0))

        main = tk.Frame(self.frame, background=bg)
        main.grid(row=0, column=1, sticky="nsew", padx=(0, 18), pady=(10, 10))
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)
        strip = tk.Frame(main, background=bg)
        strip.grid(row=0, column=0, sticky="ew")
        strip.columnconfigure(0, weight=1)
        self.chips_host = tk.Frame(strip, background=bg)
        self.chips_host.grid(row=0, column=0, sticky="w")
        self.route_btn = ttk.Button(strip, text="+ Route an app here", style="Small.Link.TButton",
                                    command=lambda: on_route(self.name))
        self.route_btn.grid(row=0, column=2, sticky="e")
        self.menu_btn = ttk.Button(strip, text="Settings ▾", style="Small.CardGhost.TButton")
        self.menu_btn.grid(row=0, column=3, sticky="e", padx=(4, 0))
        self.menu_btn.grid_remove()
        self._on_menu: Optional[Callable[[str, Any], None]] = None
        self.menu_btn.bind("<ButtonRelease-1>", self._open_menu)
        self.graph = graph.TrafficGraph(main, bg, height=GRAPH_HEIGHT)
        self.graph.canvas.grid(row=1, column=0, sticky="nsew", pady=(4, 0))

        self.separator = _hline(parent, p["border"])
        self._chip_key: Tuple[Any, ...] = ()
        self._kind = ""
        self.chips: List[Chip] = []
        self.set_interface(iface)

    def set_interface(self, iface: Any) -> None:
        kind = graph.kind_for(iface)
        self.title.configure(text=lane_title(iface))
        self.detail.configure(text=lane_detail(iface))
        if getattr(iface, "bindable", False):
            self.route_btn.grid()
        else:
            self.route_btn.grid_remove()
        self._iface = iface
        self._kind = kind

    def _open_menu(self, event: Any) -> None:
        if self._on_menu is not None and not self.menu_btn.instate(["disabled"]):
            self._on_menu(self.name, event)

    def set_namespace(self, info: Optional[Dict[str, Any]], on_menu: Callable[[str, Any], None]) -> None:
        """Show the namespace's DNS and limit next to the chips, and its settings menu, when one exists."""
        self._on_menu = on_menu
        text = namespace_badges(info).replace("  ·  ", "\n")
        self.badges.configure(text=text)
        if text:
            self.badges.grid()
        else:
            self.badges.grid_remove()
        if info is None:
            self.menu_btn.grid_remove()
        else:
            self.menu_btn.grid()

    def set_chips(self, apps: Sequence[Dict[str, Any]], on_click: Callable[[str], None],
                  on_menu: Optional[Callable[[str, Any], None]] = None) -> None:
        """apps: dicts with key, name, colour, running, note. Rebuilt only when something changed."""
        key = tuple((a["key"], a["name"], a["colour"], a["running"], a.get("note", "")) for a in apps)
        if key == self._chip_key:
            return
        self._chip_key = key
        for child in self.chips_host.winfo_children():
            child.destroy()
        self.chips = []
        p = self.p
        if not apps:
            slot = tk.Canvas(self.chips_host, width=28, height=26, background=p["surface"], highlightthickness=0,
                             borderwidth=0)
            slot.create_rectangle(1, 1, 27, 25, outline=p["line_hi"], dash=(3, 3))
            slot.pack(side="left")
            tk.Label(self.chips_host, text="No apps routed here", background=p["surface"],
                     foreground=p["text_muted"], font=theme.font("small")).pack(side="left", padx=(8, 0))
            return
        for app in apps:
            chip = Chip(self.chips_host, app["key"], app["name"], app["colour"], app["running"], on_click, on_menu,
                        app.get("note", ""))
            chip.frame.pack(side="left", padx=(0, 6))
            self.chips.append(chip)

    def set_traffic(self, history: Sequence[Tuple[float, float]], routed_rate: Optional[Tuple[float, float]],
                    routed_count: int, link_speed: Optional[float]) -> None:
        p = self.p
        down, up = history[-1] if history else (0.0, 0.0)
        tone, words = lane_state(bool(getattr(self._iface, "up", False)), bool(getattr(self._iface, "bindable", False)),
                                 routed_count, down, up, link_speed)
        colour = tone_colour(tone, p)
        number, unit = traffic.split_rate(down)
        self.rate.configure(text=number)
        self.unit.configure(text=f"{unit} ↓")
        self.up.configure(text=f"↑ {traffic.format_rate(up)}")
        if routed_rate is not None and routed_count and tone == "ok":
            words = f"{words} · {traffic.format_rate(routed_rate[0] + routed_rate[1])}"
        self.state.configure(text=words, foreground=colour if tone in ("ok", "hot") else p["text_muted"])
        graph.set_dot(self.dot, colour)
        self.glyph_canvas.delete("all")
        graph.draw_kind_glyph(self.glyph_canvas, self._kind, 11, 11, 20, colour if tone != "off" else p["text_muted"],
                              p["surface"])
        scale = graph_scale(history)
        if link_speed and scale > link_speed:
            scale = link_speed
        line = colour if tone != "idle" else p["text_muted"]
        self.graph.update([d for d, _ in history], [u for _, u in history], scale, line,
                          traffic.scale_label(scale))

    def destroy(self) -> None:
        self.frame.destroy()
        self.separator.destroy()


class StatTile:
    """A headline number with a label, a short note and an optional sparkline along the bottom."""

    def __init__(self, parent: Any, label: str, accent: str, sparkline: bool = True) -> None:
        p = theme.current_palette()
        bg = p["surface"]
        self.accent = accent
        self.frame = tk.Frame(parent, background=bg, highlightthickness=1, highlightbackground=p["border"])
        self.frame.columnconfigure(1, weight=1)
        head = tk.Frame(self.frame, background=bg)
        head.grid(row=0, column=0, columnspan=2, sticky="ew", padx=16, pady=(12, 0))
        self.mark = graph.status_dot(head, accent, bg, size=7)
        self.mark.pack(side="left", pady=(1, 0))
        tk.Label(head, text=label.upper(), background=bg, foreground=p["text_muted"],
                 font=theme.font("eyebrow")).pack(side="left", padx=(7, 0))
        value = tk.Frame(self.frame, background=bg)
        value.grid(row=1, column=0, sticky="w", padx=(16, 8), pady=(2, 0))
        self.value = tk.Label(value, text="0", background=bg, foreground=p["text"], font=theme.font("stat"))
        self.value.pack(side="left")
        self.unit = tk.Label(value, text="", background=bg, foreground=p["text_muted"], font=theme.font("small"))
        self.unit.pack(side="left", padx=(4, 0), pady=(8, 0))
        self.note = tk.Label(self.frame, text="", background=bg, foreground=p["text_muted"],
                             font=theme.font("small"), anchor="e")
        self.note.grid(row=1, column=1, sticky="se", padx=(0, 16), pady=(0, 5))
        self._value_box = value
        self.frame.bind("<Configure>", lambda _e: self._fit_note(), add="+")
        self.spark: Optional[graph.Sparkline] = None
        if sparkline:
            self.spark = graph.Sparkline(self.frame, bg, width=120, height=26)
            self.spark.canvas.grid(row=2, column=0, columnspan=2, sticky="ew", padx=16, pady=(4, 12))
        else:
            self.extra = tk.Frame(self.frame, background=bg, height=26)
            self.extra.grid(row=2, column=0, columnspan=2, sticky="ew", padx=16, pady=(4, 12))

    def _fit_note(self) -> None:
        """Hide the note when the tile is too narrow to show it next to the number."""
        try:
            need = self._value_box.winfo_reqwidth() + self.note.winfo_reqwidth() + 16 + 8 + 16
            if self.note.cget("text") and self.frame.winfo_width() >= need:
                self.note.grid()
            else:
                self.note.grid_remove()
        except tk.TclError:
            pass

    def set(self, value: str, unit: str, note: str, series: Optional[Sequence[float]] = None) -> None:
        self.value.configure(text=value)
        self.unit.configure(text=unit)
        self.note.configure(text=note)
        self._fit_note()
        if self.spark is not None and series is not None:
            peak = max(series, default=0.0)
            self.spark.update(series, traffic.nice_ceiling(peak * 1.1, floor=16 * 1024), self.accent)
