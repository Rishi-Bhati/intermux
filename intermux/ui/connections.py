"""The Connections view: one group per network connection, holding the apps routed through it.

This is the main view. Each group shows the connection, its DNS and speed limit (with buttons to change
them), what it is doing right now, and its apps with their controls. Live graphs live in the separate,
resizable Traffic panel.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence

from intermux import dnsconf
from intermux.system import shaping, traffic
from intermux.ui import graph, monitor, theme
from intermux.ui.tooltip import Tooltip, fit_text

try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:  # reported by main()
    tk = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]

APP_ICON = 26


def limit_text(limit: Optional[Dict[str, Optional[float]]]) -> str:
    """"none", "↓ 20 Mbit/s", "↓ 20 ↑ 5 Mbit/s" (one unit when both share it). Pure."""
    limit = limit or {}
    down, up = limit.get("down"), limit.get("up")
    if down and up and (down >= 1) == (up >= 1):
        unit = shaping.format_mbit(down).split()[-1]
        number = (lambda v: shaping.format_mbit(v).split()[0])
        return f"↓ {number(down)} ↑ {number(up)} {unit}"
    parts = []
    if down:
        parts.append(f"↓ {shaping.format_mbit(down)}")
    if up:
        parts.append(f"↑ {shaping.format_mbit(up)}")
    return "  ".join(parts) or "none"


def app_meta(data: Dict[str, Any], rate: Optional[tuple], public_ip: str, adopted: bool) -> str:
    """The muted line under an app name: processes, its namespace's speed, verified IP. Pure."""
    parts: List[str] = []
    if adopted:
        parts.append("moved while running")
    else:
        pids = data.get("pids") or []
        if pids:
            parts.append(f"{len(pids)} process{'es' if len(pids) != 1 else ''}")
        elif not data.get("running"):
            parts.append("not running")
    running = data.get("alive", True) if adopted else bool(data.get("running"))
    if running and rate is not None and (rate[0] or rate[1]):
        parts.append(f"↓ {traffic.format_rate(rate[0])}  ↑ {traffic.format_rate(rate[1])}")
    if public_ip:
        parts.append(f"IP {public_ip}")
    return "  ·  ".join(parts)


class AppRow:
    """One routed app inside a connection group."""

    def __init__(self, parent: Any, key: str, adopted: bool, actions: "Actions") -> None:
        p = theme.current_palette()
        bg = p["surface_alt"]
        self.key = key
        self.adopted = adopted
        self.actions = actions
        self.icon_widget: Any = None
        self.frame = tk.Frame(parent, background=bg, highlightthickness=1, highlightbackground=p["border"])
        self.frame.columnconfigure(2, weight=1)
        self.stripe = tk.Frame(self.frame, background=p["line_hi"], width=3)
        self.stripe.grid(row=0, column=0, rowspan=2, sticky="ns")
        self.icon_host = tk.Frame(self.frame, background=bg, width=APP_ICON, height=APP_ICON)
        self.icon_host.grid(row=0, column=1, rowspan=2, padx=(10, 10), pady=8)
        self.icon_host.grid_propagate(False)

        title = tk.Frame(self.frame, background=bg)
        title.grid(row=0, column=2, sticky="sw", pady=(7, 0))
        self.name = tk.Label(title, text="", background=bg, foreground=p["text"], font=theme.font("label"))
        self.name.pack(side="left")
        self.pill = tk.Label(title, text="", font=theme.font("eyebrow"), padx=6, pady=0, background=bg)
        self.pill.pack(side="left", padx=(8, 0))
        self.meta = tk.Label(self.frame, text="", background=bg, foreground=p["text_muted"],
                             font=theme.font("small"), anchor="w", width=1)
        self.meta.grid(row=1, column=2, sticky="new", pady=(1, 7))
        self.meta_tip = Tooltip(self.meta)

        buttons = tk.Frame(self.frame, background=bg)
        buttons.grid(row=0, column=3, rowspan=2, sticky="e", padx=(8, 8))
        if adopted:
            self.buttons = [ttk.Button(buttons, text="Release", style="Small.Secondary.TButton",
                                       command=lambda: actions.release(key))]
        else:
            self.buttons = [
                ttk.Button(buttons, text="Restart", style="Small.Secondary.TButton",
                           command=lambda: actions.run(key, "restart")),
                ttk.Button(buttons, text="Stop", style="Small.Secondary.TButton",
                           command=lambda: actions.run(key, "stop")),
            ]
        self.more = ttk.Button(buttons, text="•••", style="Small.CardGhost.TButton")
        self.more.bind("<ButtonRelease-1>", lambda e: None if self.more.instate(["disabled"])
                       else actions.menu(key, e))
        for index, button in enumerate(self.buttons + [self.more]):
            button.grid(row=0, column=index, padx=(0 if index == 0 else 6, 0))
        if adopted:
            self.more.grid_remove()
        for widget in (self.frame, self.icon_host, title, self.name, self.pill, self.meta, self.stripe):
            widget.bind("<Button-3>", lambda e: actions.menu(key, e))

    def set_icon(self, widget: Any) -> None:
        if self.icon_widget is not None:
            self.icon_widget.destroy()
        self.icon_widget = widget
        widget.place(relx=0.5, rely=0.5, anchor="center")
        widget.bind("<Button-3>", lambda e: self.actions.menu(self.key, e))

    def update(self, data: Dict[str, Any], colour: str, meta: str) -> None:
        p = theme.current_palette()
        alive = data.get("alive", True) if self.adopted else bool(data.get("running"))
        self.name.configure(text=data["app_name"])
        word = ("live" if alive else "exited") if self.adopted else ("running" if alive else "stopped")
        if alive:
            self.pill.configure(text=word.upper(), background=graph.mix(p["surface_alt"], p["success"], 0.16),
                                foreground=p["success"])
        else:
            self.pill.configure(text=word.upper(), background=p["surface"], foreground=p["text_muted"])
        self.stripe.configure(background=colour if alive else p["line_hi"])
        fit_text(self.meta, meta, theme.font("small"))
        self.meta_tip.text = meta

    def set_enabled(self, states: Dict[str, bool]) -> None:
        if self.adopted:
            self.buttons[0].state(["!disabled"] if states.get("idle") else ["disabled"])
            return
        for button, key in zip(self.buttons, ("restart", "stop")):
            button.state(["!disabled"] if states.get(key) else ["disabled"])
        self.more.state(["!disabled"] if states.get("idle") else ["disabled"])

    def flash(self) -> None:
        """Briefly outline the row, after a chip or search pointed at it."""
        p = theme.current_palette()
        self.frame.configure(highlightbackground=p["accent"])
        self.frame.after(1400, lambda: self.frame.winfo_exists() and
                         self.frame.configure(highlightbackground=p["border"]))


class Actions:
    """Callbacks from rows and groups to the main window."""

    def __init__(self, run: Callable[[str, str], None], release: Callable[[str], None],
                 menu: Callable[[str, Any], None], add_app: Callable[[str], None],
                 dns: Callable[[str], None], limit: Callable[[str], None],
                 group_menu: Callable[[str, Any], None]) -> None:
        self.run = run
        self.release = release
        self.menu = menu
        self.add_app = add_app
        self.dns = dns
        self.limit = limit
        self.group_menu = group_menu


class ConnectionGroup:
    """A connection with its settings line and its app rows."""

    def __init__(self, parent: Any, iface: Any, actions: Actions) -> None:
        p = theme.current_palette()
        bg = p["surface"]
        self.p = p
        self.name = iface.name
        self.actions = actions
        self.rows: Dict[str, AppRow] = {}
        self.frame = tk.Frame(parent, background=bg)
        self.frame.columnconfigure(0, weight=1)

        head = tk.Frame(self.frame, background=bg)
        head.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 0))
        head.columnconfigure(2, weight=1)
        self.glyph = tk.Canvas(head, width=20, height=20, background=bg, highlightthickness=0, borderwidth=0)
        self.glyph.grid(row=0, column=0, sticky="w")
        self.title = tk.Label(head, text="", background=bg, foreground=p["text"], font=theme.font("section"))
        self.title.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.detail = tk.Label(head, text="", background=bg, foreground=p["text_muted"], font=theme.font("mono"),
                               anchor="w", width=1)
        self.detail.grid(row=0, column=2, sticky="ew", padx=(10, 8))
        self.speed = tk.Label(head, text="", background=bg, foreground=p["text_muted"], font=theme.font("small"))
        self.speed.grid(row=0, column=3, sticky="e", padx=(0, 12))
        self.detail_tip = Tooltip(self.detail)
        self.add_btn = ttk.Button(head, text="+ Add app", style="Small.Link.TButton",
                                  command=lambda: actions.add_app(self.name))
        self.add_btn.grid(row=0, column=4, sticky="e")
        self.menu_btn = ttk.Button(head, text="•••", style="Small.CardGhost.TButton")
        self.menu_btn.grid(row=0, column=5, sticky="e", padx=(4, 0))
        self.menu_btn.bind("<ButtonRelease-1>", lambda e: None if self.menu_btn.instate(["disabled"])
                           else actions.group_menu(self.name, e))

        # State on the left, then the DNS and speed limit buttons; they drop to their own line when narrow.
        self.line = tk.Frame(self.frame, background=bg)
        self.line.grid(row=1, column=0, sticky="ew", padx=(44, 16), pady=(4, 0))
        state_box = tk.Frame(self.line, background=bg)
        state_box.grid(row=0, column=0, sticky="w")
        self.state_box = state_box
        self.dot = graph.status_dot(state_box, p["text_muted"], bg)
        self.dot.pack(side="left")
        self.state = tk.Label(state_box, text="", background=bg, foreground=p["text_muted"],
                              font=theme.font("small"))
        self.state.pack(side="left", padx=(6, 0))
        self.settings = tk.Frame(self.line, background=bg)
        self._has_settings = False
        self.line.bind("<Configure>", lambda _e: self._place_settings())
        self.dns_btn = ttk.Button(self.settings, text="", style="Small.CardGhost.TButton",
                                  command=lambda: actions.dns(self.name))
        self.dns_btn.pack(side="left")
        self.limit_btn = ttk.Button(self.settings, text="", style="Small.CardGhost.TButton",
                                    command=lambda: actions.limit(self.name))
        self.limit_btn.pack(side="left", padx=(4, 0))

        self.body = tk.Frame(self.frame, background=bg)
        self.body.grid(row=2, column=0, sticky="ew", padx=(44, 16), pady=(10, 14))
        self.body.columnconfigure(0, weight=1)
        self.empty = tk.Label(self.body, text="", background=bg, foreground=p["text_muted"],
                              font=theme.font("small"), anchor="w", justify="left")
        self.separator = tk.Frame(parent, background=p["border"], height=1)
        self.set_interface(iface)

    def set_interface(self, iface: Any) -> None:
        self.iface = iface
        self.title.configure(text=monitor.lane_title(iface))
        detail = monitor.lane_detail(iface)
        fit_text(self.detail, detail, theme.font("mono"))
        self.detail_tip.text = detail
        if getattr(iface, "bindable", False):
            self.add_btn.grid()
        else:
            self.add_btn.grid_remove()

    def set_namespace(self, info: Optional[Dict[str, Any]], idle: bool) -> None:
        """DNS and speed limit buttons exist once the connection has a namespace (an app was routed)."""
        if info is None:
            self._has_settings = False
            self.settings.grid_remove()
            self.menu_btn.grid_remove()
            return
        self._has_settings = True
        self.menu_btn.grid()
        self.dns_btn.configure(text=f"DNS: {dnsconf.describe(info.get('dns') or [])}  ▾")
        self.limit_btn.configure(text=f"Speed limit: {limit_text(info.get('limit'))}  ▾")
        for button in (self.dns_btn, self.limit_btn, self.menu_btn):
            button.state(["!disabled"] if idle else ["disabled"])
        self._place_settings()

    def _place_settings(self) -> None:
        if not self._has_settings:
            return
        try:
            need = self.state_box.winfo_reqwidth() + self.settings.winfo_reqwidth() + 24
            wide = self.line.winfo_width() <= 1 or self.line.winfo_width() >= need
        except tk.TclError:
            return
        if wide:
            self.settings.grid(row=0, column=1, sticky="w", padx=(14, 0), pady=0)
        else:
            self.settings.grid(row=1, column=0, columnspan=2, sticky="w", padx=(0, 0), pady=(4, 0))

    def set_state(self, tone: str, words: str, rate: tuple) -> None:
        p = self.p
        colour = monitor.tone_colour(tone, p)
        graph.set_dot(self.dot, colour)
        self.state.configure(text=words, foreground=colour if tone in ("ok", "hot") else p["text_muted"])
        self.glyph.delete("all")
        graph.draw_kind_glyph(self.glyph, graph.kind_for(self.iface), 10, 10, 18,
                              colour if tone in ("ok", "hot", "active") else p["text_muted"], p["surface"])
        self.speed.configure(text=f"↓ {traffic.format_rate(rate[0])}   ↑ {traffic.format_rate(rate[1])}")

    def set_apps(self, keys: Sequence[str], make_row: Callable[[Any, str], AppRow]) -> None:
        """Keep one row per key, in order; create missing rows with make_row(parent, key)."""
        for key in list(self.rows):
            if key not in keys:
                self.rows.pop(key).frame.destroy()
        for index, key in enumerate(keys):
            row = self.rows.get(key)
            if row is None:
                row = make_row(self.body, key)
                self.rows[key] = row
            row.frame.grid(row=index, column=0, sticky="ew", pady=(0, 6))
        if keys:
            self.empty.grid_remove()
        else:
            bindable = getattr(self.iface, "bindable", False)
            self.empty.configure(text="No apps use this connection yet." if bindable else
                                 "Apps cannot be routed through this connection right now.")
            self.empty.grid(row=0, column=0, sticky="w")

    def destroy(self) -> None:
        self.frame.destroy()
        self.separator.destroy()
