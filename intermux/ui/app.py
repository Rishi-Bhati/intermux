"""InterMux main window. Runs unprivileged; every mutation goes through helper.client.call in the Worker."""

from __future__ import annotations

import logging
import os
import sys
import time
from functools import partial
from typing import Any, Dict, List, Optional, Sequence, Tuple

from intermux import apps, config, dnsconf, state
from intermux.apps import desktop_entries
from intermux.apps.desktop_entries import AppEntry
from intermux.errors import InterMuxError
from intermux.helper import client as helper_client
from intermux.system import commands, deps, netinfo, netns, procs, traffic
from intermux.system import session as session_mod
from intermux.ui import dialogs, graph, icons, monitor, theme
from intermux.ui.history import History, Item as HistoryItem
from intermux.ui.worker import Worker

try:
    import tkinter as tk
    from tkinter import messagebox, ttk

    from intermux.ui.picker import ellipsize
except ImportError:  # reported by main()
    tk = None  # type: ignore[assignment]
    messagebox = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]

log = logging.getLogger("intermux.ui")

AUTO_REFRESH_MS = 4000
TRAFFIC_MS = 1000           # traffic counters are sampled once a second
OPEN_LOG_TIMEOUT = 5.0
PUBLIC_IP_TTL = 300.0  # a verified public IP is shown for five minutes, then cleared

# Widgets that take typed text: shortcut keys such as Delete must not act while one has focus.
TEXT_INPUT_CLASSES = frozenset({"Entry", "TEntry", "TCombobox", "Combobox", "Text", "Spinbox", "TSpinbox",
                                "Listbox", "TListbox"})


# ---------------------------------------------------------------------------
# Background tasks (run in Worker threads, never on the Tk thread)
# ---------------------------------------------------------------------------

def snapshot_rows(st: Any, table: Sequence[Any], ns_inode: Any, group_pids: Any) -> Tuple[List[Dict[str, Any]],
                                                                                     List[Dict[str, Any]]]:
    """Binding and adoption rows from one state read and one process table. Pure apart from the lookups."""
    live_netns = {proc.netns for proc in table if proc.netns is not None}
    live_pids = {proc.pid for proc in table}
    rows: List[Dict[str, Any]] = []
    for binding in st.bindings:
        ino = ns_inode(binding.namespace)
        rows.append({
            "iface": binding.iface,
            "app": binding.app,
            "app_name": binding.app_name,
            "log_path": binding.log_path,
            "running": ino is not None and ino in live_netns,
            "pids": sorted(proc.pid for proc in table if ino is not None and proc.netns == ino),
            "profile_mode": binding.profile_mode,
        })
    adopted: List[Dict[str, Any]] = []
    for adoption in st.adoptions:
        # The cgroup is the truth: the app may have replaced the pids recorded when it was adopted.
        members = list(group_pids(adoption.group))
        adopted.append({
            "id": adoption.id,
            "iface": adoption.iface,
            "app": adoption.app,
            "app_name": adoption.app_name,
            "alive": bool(members) or any(pid in live_pids for pid in adoption.pids),
        })
    return rows, adopted


def _snapshot_task() -> Dict[str, Any]:
    """Read interfaces and bindings (read-only, unprivileged). Scans /proc once per refresh."""
    from intermux.system import adopt as adopt_sys

    interfaces = netinfo.list_interfaces()
    st = state.load_state()
    rows, adopted = snapshot_rows(st, procs.list_procs(), procs.netns_inode, adopt_sys.read_group_pids)
    return {"interfaces": interfaces, "bindings": rows, "adoptions": adopted,
            "namespaces": namespace_settings(st)}


def namespace_settings(st: Any) -> Dict[str, Dict[str, Any]]:
    """Per interface namespace: its hand-picked DNS servers ([] = automatic) and bandwidth limit. Pure."""
    return {iface: {"dns": list(st.dns.get(iface, [])), "limit": dict(st.limits.get(iface, {}))}
            for iface in st.subnets}


def _env_task(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a helper call that starts an app, adding the invoking user's allowlisted desktop environment."""
    session = session_mod.detect_session(probe=True)
    return fn(*args, env=session_mod.build_app_env(session), **kwargs)


def _restart_call(iface: str, app: str, env: Dict[str, str]) -> Any:
    return helper_client.call("restart_app", {"iface": iface, "app": app, "env": env})


def _move_call(iface: str, app: str, to: str, env: Dict[str, str]) -> Any:
    return helper_client.call("move_app", {"iface": iface, "app": app, "to": to, "env": env})


def _environment_task() -> Tuple[Any, List[deps.Dependency]]:
    """Detect the desktop session and missing required tools."""
    session = session_mod.detect_session(probe=True)
    return session, deps.missing_required()


def _batch_task(ops: List[Dict[str, Any]]) -> Any:
    """Add the invoking user's allowlisted environment to every bind op and run one helper call."""
    session = session_mod.detect_session(probe=True)
    env = session_mod.build_app_env(session)
    full = [dict(op, env=dict(env)) if op["op"] == "bind" else dict(op) for op in ops]
    return helper_client.batch(full)


def _icon_scan_task() -> Dict[str, AppEntry]:
    """Map resolved binary path -> desktop entry, so bindings can show the app icon and name."""
    found: Dict[str, AppEntry] = {}
    for entry in desktop_entries.scan_desktop_entries():
        if entry.binary and entry.binary not in found:
            found[entry.binary] = entry
    return found


def _error_text(error: BaseException) -> str:
    text = getattr(error, "message", None) or str(error)
    return text or error.__class__.__name__


def _iid(iface: str, app: str) -> str:
    return f"{iface}|{app}"


NS_PREFIX = "ns:"


def ns_key(iface: str) -> str:
    """Tracker key of the namespace bound to iface (its traffic is counted on the host veth)."""
    return NS_PREFIX + iface


def sum_series(histories: Sequence[Sequence[Tuple[float, float]]], index: int) -> List[float]:
    """Element-wise sum of one direction (0 down, 1 up) over equally long histories. Pure."""
    if not histories:
        return []
    length = min(len(h) for h in histories)
    return [sum(h[len(h) - length + i][index] for h in histories) for i in range(length)]


def routed_share(routed: float, total: float) -> str:
    """"42%" of total that went through InterMux, or "" when there is no traffic to compare. Pure."""
    if routed <= 0 or total <= 0:
        return ""
    share = min(routed / total, 1.0) * 100
    return "<1%" if share < 1 else f"{share:.0f}%"


def safe_focus(root: Any) -> Any:
    """focus_get() that returns None instead of raising (Tk raises KeyError for combobox popdowns)."""
    try:
        return root.focus_get()
    except (KeyError, tk.TclError):
        return None


def widget_class_name(widget: Any) -> Optional[str]:
    if widget is None:
        return None
    try:
        return str(widget.winfo_class())
    except Exception:  # destroyed widget
        return None


def delete_key_allowed(focus_class: Optional[str]) -> bool:
    """Delete may stop the selected binding only when no text-input widget has focus. Pure."""
    return focus_class not in TEXT_INPUT_CLASSES


def row_button_states(running: bool, idle: bool) -> Dict[str, bool]:
    """Which buttons of a binding row are enabled. Stopped rows keep Log and Restart (start it again). Pure."""
    return {"verify": idle and running, "stop": idle and running,
            "log": idle, "force": idle and running, "restart": idle}


def still_running_message(name: str, action: str, pids: Sequence[Any]) -> str:
    """Warning shown when the helper reports processes that survived a stop, kill or release. Pure."""
    return f"Warning: {name}: processes still running after {action} (pids {', '.join(str(p) for p in pids)})."


def fresh_public_ip(stored: Optional[Tuple[str, float]], now: float, ttl: float = PUBLIC_IP_TTL) -> str:
    """The stored verified IP, or "" when it is older than ttl. Pure."""
    if not stored:
        return ""
    ip, stamp = stored
    return ip if now - stamp < ttl else ""


def _set_enabled(widget: Any, enabled: bool) -> None:
    widget.state(["!disabled"] if enabled else ["disabled"])


KIND_LABELS = {
    "wifi": "Wi-Fi", "ethernet": "Ethernet", "usb": "USB", "bluetooth": "Bluetooth",
    "mobile": "Mobile", "vpn": "VPN", "virtual": "Virtual", "other": "Other",
}
NO_IFACE_TEXT = "No bindable interface found. Connect to a network and press Refresh."
CARD_ICON = 32
PENDING_ICON = 26
TILE_ICON = 36
DESIGN_SIZE = (1360, 900)
MIN_SIZE = (1100, 640)
SIDEBAR_WIDTH = 360
NETWORK_MIN_HEIGHT = 200
APPS_MIN_HEIGHT = 150
ADOPT_PREFIX = "adopt:"


def status_kind(text: str) -> str:
    """Classify a status line as "info", "success" or "error" for the coloured dot. Pure."""
    low = text.lower()
    if low.startswith("warning"):
        return "warning"
    if low.startswith(("error", "could not", "verify failed")) or "did not exit" in low or "still running" in low:
        return "error"
    if low.startswith(("bound ", "stopped ", "removed ", "reset complete", "force-quit ")) or "public ip:" in low:
        return "success"
    return "info"


def session_label(session: Any) -> str:
    """Short text for the session chip, e.g. "Wayland :1" or "X11". Pure."""
    kind = getattr(session, "session_type", "") or ""
    if kind == "wayland":
        display = getattr(session, "display", None)
        return f"Wayland {display}" if display else "Wayland"
    if kind == "x11":
        return "X11"
    return "No session" if not (getattr(session, "display", None) or getattr(session, "wayland_display", None)) else "Session"


def interface_label(iface: Any) -> str:
    """Chip text such as "wlan0  Wi-Fi  192.168.1.20". Pure."""
    addr = iface.ipv4_cidrs[0].split("/")[0] if iface.ipv4_cidrs else ""
    kind = KIND_LABELS.get(iface.kind, iface.kind)
    return "  ".join(part for part in (iface.name, kind, addr) if part)


def pending_detail(item: Dict[str, Any], existing: Sequence[str]) -> str:
    """Second line of a queued app: "via wlan0 · new namespace · DNS Cloudflare". Pure."""
    where = "existing namespace" if item["iface"] in existing else "new namespace"
    dns = item.get("dns", dnsconf.KEEP)
    if isinstance(dns, list):
        dns_text = f"DNS {dnsconf.describe(dns)}"
    elif item["iface"] in existing:
        dns_text = "keeps its DNS"
    else:
        dns_text = "automatic DNS"
    return f"via {item['iface']} · {where} · {dns_text}"


def pids_text(pids: Sequence[int]) -> str:
    """"1 process (1234)" or "4 processes (1234, 1240, +2)". Pure."""
    if not pids:
        return ""
    shown = ", ".join(str(p) for p in list(pids)[:2]) + (f", +{len(pids) - 2}" if len(pids) > 2 else "")
    return f"{len(pids)} process{'es' if len(pids) != 1 else ''} ({shown})"


def queue_add(items: List[Dict[str, Any]], item: Dict[str, Any]) -> bool:
    """Append item to the pending list unless the same interface and application is already queued. Pure."""
    for existing in items:
        if existing["iface"] == item["iface"] and existing["app"] == item["app"]:
            return False
    items.append(item)
    return True


def _fit_label(label: Any, full_text: str, font: Any) -> None:
    """Keep `label` showing full_text, ellipsised to the label's current width."""
    label._full_text = full_text  # type: ignore[attr-defined]

    def refit(event: Any = None) -> None:
        width = label.winfo_width() if event is None else event.width
        text = ellipsize(label._full_text, max(width - 4, 20), font.measure)
        if label.cget("text") != text:
            label.configure(text=text)

    if not getattr(label, "_fit_bound", False):
        label._fit_bound = True  # type: ignore[attr-defined]
        label.bind("<Configure>", refit)
    refit()


def already_running_choices() -> List[Tuple[str, Optional[str]]]:
    """Buttons of the already-running prompt; the shift option exists only here."""
    return [("Close and reopen", "close"), ("Open separate profile", "separate"),
            ("Shift current session", "shift"), ("Cancel", None)]


def _ask_choice(
    parent: Any,
    title: str,
    message: str,
    buttons: Sequence[Tuple[str, Optional[str]]],
    checkbox: Optional[str] = None,
) -> Tuple[Optional[str], bool]:
    """Modal dialog. Returns (value of the clicked button or None, checkbox state)."""
    p = theme.current_palette()
    top = tk.Toplevel(parent)
    top.title(title)
    top.configure(background=p["bg"])
    top.transient(parent)
    top.resizable(False, False)

    result: Dict[str, Any] = {"value": None, "checked": False}
    check_var = tk.BooleanVar(top, value=False)

    body = ttk.Frame(top, padding=theme.SPACE_OUTER)
    body.pack(fill="both", expand=True)
    ttk.Label(body, text=message, justify="left", wraplength=420).pack(anchor="w")
    if checkbox:
        ttk.Checkbutton(body, text=checkbox, variable=check_var, style="Dialog.TCheckbutton").pack(
            anchor="w", pady=(theme.SPACE_CARD_GAP, 0))

    def pick(value: Optional[str]) -> None:
        result["value"] = value
        result["checked"] = bool(check_var.get())
        top.destroy()

    row = ttk.Frame(body)
    row.pack(fill="x", pady=(theme.SPACE_OUTER, 0))
    for index, (label, value) in enumerate(buttons):
        if index == 0:
            style = "Danger.TButton" if value in ("kill", "reset") else "Primary.TButton"
        else:
            style = "Secondary.TButton"
        ttk.Button(row, text=label, style=style, command=lambda v=value: pick(v)).pack(
            side="left", padx=(0, theme.SPACE_INNER))

    top.protocol("WM_DELETE_WINDOW", lambda: pick(None))
    top.bind("<Escape>", lambda _e: pick(None))
    try:
        top.grab_set()
    except tk.TclError:
        pass
    top.wait_window()
    return result["value"], bool(result["checked"])


# ---------------------------------------------------------------------------
# Small widgets
# ---------------------------------------------------------------------------

def _card(parent: Any) -> Any:
    p = theme.current_palette()
    return tk.Frame(parent, background=p["surface"], highlightthickness=1,
                    highlightbackground=p["border"], highlightcolor=p["border"], borderwidth=0)


def _rule(parent: Any) -> Any:
    """A one-pixel divider line."""
    return tk.Frame(parent, background=theme.current_palette()["border"], height=1, borderwidth=0,
                    highlightthickness=0)


def _panel(parent: Any, eyebrow: str) -> Tuple[Any, Any, Any]:
    """A website-style panel: a slim header with an uppercase label, a divider, then the body.

    Returns (panel, header, body). Widgets added to the header's right side should pack with side="right".
    """
    p = theme.current_palette()
    panel = _card(parent)
    panel.columnconfigure(0, weight=1)
    panel.rowconfigure(2, weight=1)
    header = tk.Frame(panel, background=p["surface"], height=42)
    header.grid(row=0, column=0, sticky="ew", padx=(16, 10))
    header.pack_propagate(False)
    tk.Label(header, text=eyebrow.upper(), background=p["surface"], foreground=p["text_muted"],
             font=theme.font("eyebrow")).pack(side="left")
    _rule(panel).grid(row=1, column=0, sticky="ew")
    body = tk.Frame(panel, background=p["surface"])
    body.grid(row=2, column=0, sticky="nsew")
    return panel, header, body


def _pill(parent: Any, background: str) -> Any:
    return tk.Label(parent, text="", font=theme.font("eyebrow"), padx=7, pady=1, background=background)


def set_pill(pill: Any, kind: str) -> None:
    """Style a status pill: "running"/"live" (green), "stopped"/"exited" (muted)."""
    p = theme.current_palette()
    if kind in ("running", "live"):
        pill.configure(text=kind.upper(), background=graph.mix(p["surface"], p["success"], 0.16),
                       foreground=p["success"])
    else:
        pill.configure(text=kind.upper(), background=p["surface_alt"], foreground=p["text_muted"])


class _PendingRow:
    """One queued app in the sidebar: icon, name, "via <iface>" and a remove control."""

    def __init__(self, app: "InterMuxApp", parent: Any, item: Dict[str, Any]) -> None:
        p = theme.current_palette()
        bg = p["surface_alt"]
        self.frame = tk.Frame(parent, background=bg, highlightthickness=1, highlightbackground=p["border"],
                              takefocus=0)
        self.frame.columnconfigure(1, weight=1)
        host = tk.Frame(self.frame, background=bg, width=PENDING_ICON, height=PENDING_ICON)
        host.grid(row=0, column=0, rowspan=2, padx=(10, 10), pady=8)
        host.grid_propagate(False)
        icon = app._icon_widget(host, item["app"], item["name"], PENDING_ICON, bg)
        icon.place(relx=0.5, rely=0.5, anchor="center")
        self.name = tk.Label(self.frame, text=item["name"], background=bg, foreground=p["text"],
                             font=theme.font("label"), anchor="w")
        self.name.grid(row=0, column=1, sticky="sw", pady=(8, 0))
        self.via = tk.Label(self.frame, text=pending_detail(item, list(app._namespaces)), background=bg,
                            foreground=p["text_muted"], font=theme.font("small"), anchor="w", justify="left",
                            wraplength=SIDEBAR_WIDTH - 120)
        self.via.grid(row=1, column=1, sticky="nw", pady=(0, 8))
        self.remove = tk.Label(self.frame, text="✕", background=bg, foreground=p["text_muted"],
                               font=theme.font("small"), cursor="hand2", padx=10)
        self.remove.grid(row=0, column=2, rowspan=2, sticky="ns")
        self.remove.bind("<Button-1>", lambda _e: app._remove_pending(item))
        self.remove.bind("<Enter>", lambda _e: self.remove.configure(foreground=p["danger"]))
        self.remove.bind("<Leave>", lambda _e: self.remove.configure(foreground=p["text_muted"]))


class _AppCard:
    """A routed app (a binding or an adoption) in the Routed apps panel. Updated in place on refresh.

    Row one: icon, name and state on the left; the live traffic of its connection on the right.
    Row two: which connection it uses and its verified public IP; the actions on the right.
    """

    def __init__(self, app: "InterMuxApp", parent: Any, key: str, adopted: bool) -> None:
        p = theme.current_palette()
        bg = p["surface"]
        self.app = app
        self.key = key
        self.adopted = adopted
        self.selected = False
        self.icon_widget: Any = None
        self.frame = tk.Frame(parent, background=bg, highlightthickness=1, highlightbackground=p["border"],
                              highlightcolor=p["focus"], takefocus=0)
        self.frame.columnconfigure(2, weight=1)
        self.stripe = tk.Frame(self.frame, background=p["line_hi"], width=3)
        self.stripe.grid(row=0, column=0, rowspan=3, sticky="ns")
        self.icon_host = tk.Frame(self.frame, background=bg, width=CARD_ICON, height=CARD_ICON)
        self.icon_host.grid(row=0, column=1, rowspan=2, padx=(12, 12), pady=12, sticky="n")
        self.icon_host.grid_propagate(False)

        title = tk.Frame(self.frame, background=bg)
        title.grid(row=0, column=2, sticky="sw", pady=(11, 0))
        self.name = tk.Label(title, text="", background=bg, foreground=p["text"], font=theme.font("section"))
        self.name.pack(side="left")
        self.pill = _pill(title, bg)
        self.pill.pack(side="left", padx=(8, 0), pady=(2, 0))
        self.kind = tk.Label(title, text="ADOPTED" if adopted else "", background=bg, foreground=p["text_muted"],
                             font=theme.font("eyebrow"))
        self.kind.pack(side="left", padx=(6, 0), pady=(2, 0))

        via = tk.Frame(self.frame, background=bg)
        via.grid(row=1, column=2, sticky="nw", pady=(3, 4))
        self.via_glyph = tk.Canvas(via, width=16, height=16, background=bg, highlightthickness=0, borderwidth=0)
        self.via_glyph.pack(side="left")
        self.via = tk.Label(via, text="", background=bg, foreground=p["text_muted"], font=theme.font("mono"))
        self.via.pack(side="left", padx=(5, 0))
        self.ip = tk.Label(via, text="", background=bg, foreground=p["text"], font=theme.font("mono"))
        self.ip.pack(side="left", padx=(10, 0))
        self.extra = tk.Label(self.frame, text="", background=bg, foreground=p["text_muted"],
                              font=theme.font("small"), anchor="w")
        self.extra.grid(row=2, column=2, columnspan=2, sticky="ew", pady=(0, 10))

        rate = tk.Frame(self.frame, background=bg)
        rate.grid(row=0, column=3, sticky="se", padx=(8, 14), pady=(10, 0))
        self.rate = tk.Label(rate, text="", background=bg, foreground=p["text"], font=theme.font("small"))
        self.rate.pack(side="left", padx=(0, 10))
        self.spark = graph.Sparkline(rate, bg, width=110, height=24)
        self.spark.canvas.pack(side="left")

        actions = tk.Frame(self.frame, background=bg)
        actions.grid(row=1, column=3, sticky="ne", padx=(8, 12), pady=(6, 4))
        if adopted:
            self.buttons = [ttk.Button(actions, text="Release", style="Small.Secondary.TButton",
                                       command=lambda: app._on_release_adoption(self.data))]
        else:
            self.buttons = [
                ttk.Button(actions, text="Verify IP", style="Small.Secondary.TButton",
                           command=lambda: self._act(app._on_verify)),
                ttk.Button(actions, text="Restart", style="Small.Secondary.TButton",
                           command=lambda: self._act(app._on_restart)),
                ttk.Button(actions, text="Stop", style="Small.Secondary.TButton",
                           command=lambda: self._act(app._on_stop)),
                ttk.Button(actions, text="More ▾", style="Small.CardGhost.TButton"),
            ]
            # "More" opens the same menu as a right-click: log, move to another connection, force quit.
            self.buttons[-1].bind("<ButtonRelease-1>", lambda e: (
                None if self.buttons[-1].instate(["disabled"]) else app._show_app_menu(self.key, e)))
        for index, button in enumerate(self.buttons):
            button.grid(row=0, column=index, padx=(0 if index == 0 else 6, 0))
        self.data: Dict[str, Any] = {}
        for widget in (self.frame, self.icon_host, self.name, self.via, self.pill, self.ip, title, via, rate,
                       self.rate, self.kind, self.stripe, self.via_glyph, self.spark.canvas, self.extra):
            widget.bind("<Button-1>", lambda _e: app.select_binding(self.key))
            widget.bind("<Button-3>", lambda e: app._show_app_menu(self.key, e))

    def _act(self, handler: Any) -> None:
        self.app.select_binding(self.key)
        handler()

    def set_icon(self, widget: Any) -> None:
        if self.icon_widget is not None:
            self.icon_widget.destroy()
        self.icon_widget = widget
        widget.place(relx=0.5, rely=0.5, anchor="center")
        widget.bind("<Button-1>", lambda _e: self.app.select_binding(self.key))

    def update(self, data: Dict[str, Any], public_ip: str, colour: str, kind: str) -> None:
        p = theme.current_palette()
        self.data = data
        alive = data.get("alive", True) if self.adopted else data["running"]
        self.name.configure(text=data["app_name"])
        set_pill(self.pill, ("live" if alive else "exited") if self.adopted else ("running" if alive else "stopped"))
        self.via.configure(text=f"via {data['iface']}")
        self.via_glyph.delete("all")
        graph.draw_kind_glyph(self.via_glyph, kind, 8, 8, 14, p["text_muted"], p["surface"])
        self.ip.configure(text=f"IP {public_ip}" if public_ip else "")
        self.stripe.configure(background=colour if alive else p["line_hi"])
        self._pids = pids_text(data.get("pids") or []) if not self.adopted else ""

    def set_traffic(self, history: Optional[Sequence[Tuple[float, float]]], shared: int,
                    totals: Optional[Tuple[int, int]] = None) -> None:
        p = theme.current_palette()
        parts = [getattr(self, "_pids", "")]
        if totals is not None:
            parts.append(f"total ↓ {traffic.format_bytes(totals[0])} ↑ {traffic.format_bytes(totals[1])}")
        text = "  ·  ".join(x for x in parts if x)
        self.extra.configure(text=text)
        if text:
            self.extra.grid()
        else:
            self.extra.grid_remove()
        if history is None:
            self.rate.configure(text="counted on its connection", foreground=p["text_muted"])
            self.spark.update([], 1.0, p["text_muted"])
            return
        down, up = history[-1]
        suffix = f"  · shared by {shared}" if shared > 1 else ""
        self.rate.configure(text=f"↓ {traffic.format_rate(down)}   ↑ {traffic.format_rate(up)}{suffix}",
                            foreground=p["text"])
        values = [d + u for d, u in history]
        self.spark.update(values, traffic.nice_ceiling(max(values, default=0.0) * 1.1, floor=16 * 1024),
                          p["success"])

    def set_selected(self, selected: bool) -> None:
        p = theme.current_palette()
        self.selected = selected
        self.frame.configure(highlightbackground=p["accent"] if selected else p["border"])

    def set_enabled(self, enabled: bool, running: bool = True) -> None:
        if self.adopted:
            _set_enabled(self.buttons[0], enabled)
            return
        states = row_button_states(running, enabled)
        for button, key in zip(self.buttons, ("verify", "restart", "stop", "log")):
            _set_enabled(button, states[key])


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

def initial_geometry(screen_w: int, screen_h: int) -> str:
    """Window size that fits the screen with a margin, never above the design size. Pure."""
    width = max(min(DESIGN_SIZE[0], screen_w - 80), MIN_SIZE[0])
    height = max(min(DESIGN_SIZE[1], screen_h - 80), MIN_SIZE[1])
    return f"{width}x{height}"


class InterMuxApp:
    """Main window: the live network view, routed apps, and a sidebar to route more apps."""

    def __init__(self, root: Any) -> None:
        self.root = root
        self.palette = theme.apply_theme(root)
        self.worker = Worker(root)
        self._busy = 0
        self._snapshot_inflight = False
        self._iface_map: Dict[str, str] = {}
        self._interfaces: List[Any] = []
        self._link_speeds: Dict[str, Optional[float]] = {}
        self._rows: Dict[str, Dict[str, Any]] = {}
        self._cards: Dict[str, _AppCard] = {}
        self._lanes: Dict[str, monitor.LaneRow] = {}
        self._public_ips: Dict[str, Tuple[str, float]] = {}
        self._selected_iid: Optional[str] = None
        self._pending: List[Dict[str, Any]] = []
        self._pending_widgets: List[_PendingRow] = []
        self._picked_entries: Dict[str, AppEntry] = {}
        self._assigning = False
        self._picked_entry: Optional[AppEntry] = None
        self._desktop_info: Dict[str, AppEntry] = {}
        self._adoptions: List[Dict[str, Any]] = []
        self.tracker = traffic.RateTracker()
        self.colours = monitor.ColourBook()
        self._namespaces: Dict[str, Dict[str, Any]] = {}
        self.history = History()

        root.title(f"InterMux {config.VERSION}")
        root.geometry(initial_geometry(root.winfo_screenwidth(), root.winfo_screenheight()))
        root.minsize(*MIN_SIZE)
        self.app_var = tk.StringVar(root, value="")
        self.iface_var = tk.StringVar(root, value="")
        self.separate_var = tk.BooleanVar(root, value=False)
        self.dns_var = tk.StringVar(root, value=dialogs.AUTO_LABEL)
        self.session_var = tk.StringVar(root, value="Detecting session")
        self.status_var = tk.StringVar(root, value="")

        self._build(root)
        self.app_var.trace_add("write", lambda *_: self._on_app_changed())
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.bind("<Return>", self._on_return_key)
        root.bind("<F5>", lambda _e: self.refresh_all())
        root.bind("<Delete>", lambda _e: self._on_delete_key())

        self.refresh_all()
        self.worker.submit(_icon_scan_task, on_done=self._on_icon_scan)
        self._schedule_auto_refresh()
        self.root.after(TRAFFIC_MS, self._traffic_tick)

    # -- layout ---------------------------------------------------------

    def _build(self, root: Any) -> None:
        pad = theme.SPACE_OUTER
        p = self.palette
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        header = tk.Frame(root, background=p["bg"])
        header.grid(row=0, column=0, sticky="ew", padx=pad, pady=(16, 14))
        graph.logo(header, 26, p["bg"]).pack(side="left")
        tk.Label(header, text="InterMux", background=p["bg"], foreground=p["text"],
                 font=theme.font("title")).pack(side="left", padx=(10, 0))
        tk.Label(header, text=f"v{config.VERSION}", background=p["bg"], foreground=p["text_muted"],
                 font=theme.font("mono")).pack(side="left", padx=(8, 0), pady=(3, 0))
        chip = tk.Frame(header, background=p["bg"], highlightthickness=1, highlightbackground=p["line_hi"])
        chip.pack(side="left", padx=(16, 0))
        self.live_dot = graph.status_dot(chip, p["success"], p["bg"], size=7)
        self.live_dot.pack(side="left", padx=(9, 0))
        self.session_chip = tk.Label(chip, textvariable=self.session_var, background=p["bg"],
                                     foreground=p["text_muted"], font=theme.font("mono"), padx=7, pady=3)
        self.session_chip.pack(side="left")
        ttk.Button(header, text="Refresh", style="Ghost.TButton", command=self.refresh_all).pack(side="right")
        ttk.Button(header, text="Report issue", style="Secondary.TButton", command=self._on_report).pack(
            side="right", padx=(0, 8))

        self.banner = ttk.Frame(root, padding=theme.SPACE_INNER, style="Banner.TFrame")
        self.body = tk.Frame(root, background=p["bg"])
        self.body.grid(row=2, column=0, sticky="nsew", padx=pad)
        self.body.columnconfigure(0, weight=1)
        self.body.columnconfigure(1, weight=0, minsize=SIDEBAR_WIDTH)
        self.body.rowconfigure(0, weight=1)

        main = tk.Frame(self.body, background=p["bg"])
        self.main = main
        main.grid(row=0, column=0, sticky="nsew", padx=(0, theme.SPACE_CARD_GAP))
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=5, minsize=NETWORK_MIN_HEIGHT)
        main.rowconfigure(2, weight=3, minsize=APPS_MIN_HEIGHT)
        self._build_tiles(main)
        self._build_network(main)
        self._build_apps(main)

        side = tk.Frame(self.body, background=p["bg"], width=SIDEBAR_WIDTH)
        side.grid(row=0, column=1, sticky="nsew")
        side.columnconfigure(0, weight=1)
        side.rowconfigure(0, weight=1)
        self._build_bind_card(side)

        footer = tk.Frame(root, background=p["bg"])
        footer.grid(row=3, column=0, sticky="ew", padx=pad, pady=(12, 12))
        self.status_dot = graph.status_dot(footer, p["text_muted"], p["bg"])
        self.status_dot.pack(side="left")
        # Buttons are packed first so the status text can never push them out of view.
        self.reset_btn = ttk.Button(footer, text="Reset everything", style="Small.Danger.TButton",
                                    command=self._on_reset)
        self.reset_btn.pack(side="right")
        self.teardown_btn = ttk.Button(footer, text="Teardown idle", style="Small.Secondary.TButton",
                                       command=self._on_teardown)
        self.teardown_btn.pack(side="right", padx=(0, 8))
        self.live_label = tk.Label(footer, text="", background=p["bg"], foreground=p["text_muted"],
                                   font=theme.font("mono"))
        self.live_label.pack(side="right", padx=(0, 16))
        self.status_label = tk.Label(footer, textvariable=self.status_var, font=theme.font("body"), anchor="w",
                                     background=p["bg"], foreground=p["text"])
        self.status_label.pack(side="left", padx=(8, 0), fill="x", expand=True)
        self._set_status("")
        self._refresh_app_tile()
        self._update_buttons()

    def _build_tiles(self, parent: Any) -> None:
        p = self.palette
        row = tk.Frame(parent, background=p["bg"])
        row.grid(row=0, column=0, sticky="ew", pady=(0, theme.SPACE_CARD_GAP))
        self.tiles = {
            "down": monitor.StatTile(row, "Download", p["accent_fg"]),
            "up": monitor.StatTile(row, "Upload", p["violet"]),
            "routed": monitor.StatTile(row, "Via InterMux", p["success"]),
            "apps": monitor.StatTile(row, "Routed apps", p["warning"], sparkline=False),
        }
        for index, tile in enumerate(self.tiles.values()):
            row.columnconfigure(index, weight=1, uniform="tile")
            tile.frame.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 12, 0))

    def _build_network(self, parent: Any) -> None:
        p = self.palette
        panel, header, body = _panel(parent, "Network")
        panel.grid(row=1, column=0, sticky="nsew")
        legend = tk.Frame(header, background=p["surface"])
        legend.pack(side="right")
        for text, colour in (("routed by intermux", p["success"]), ("not routed", p["accent_fg"]),
                             ("upload", p["violet"])):
            graph.status_dot(legend, colour, p["surface"], size=7).pack(side="left", padx=(12, 5))
            tk.Label(legend, text=text, background=p["surface"], foreground=p["text_muted"],
                     font=theme.font("eyebrow")).pack(side="left")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.lane_scroll = theme.ScrollFrame(body, p["surface"], height=60)
        self.lane_scroll.grid(row=0, column=0, sticky="nsew", pady=(0, 2))
        self.lane_scroll.attach_wheel(self.root)
        self.lanes_host = self.lane_scroll.inner
        self.lanes_host.columnconfigure(0, weight=1)
        self.lanes_empty = tk.Label(body, text="No network connections are up. Connect to a network and press "
                                               "Refresh.", background=p["surface"], foreground=p["text_muted"],
                                    font=theme.font("body"))

    def _build_apps(self, parent: Any) -> None:
        p = self.palette
        panel, header, body = _panel(parent, "Routed apps")
        panel.grid(row=2, column=0, sticky="nsew", pady=(theme.SPACE_CARD_GAP, 0))
        self.apps_count = tk.Label(header, text="", background=p["surface"], foreground=p["text_muted"],
                                   font=theme.font("mono"))
        self.apps_count.pack(side="right", padx=(0, 6))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.scroll = theme.ScrollFrame(body, p["surface"], height=60)
        self.scroll.grid(row=0, column=0, sticky="nsew", padx=(12, 2), pady=12)
        self.scroll.attach_wheel(self.root)
        self.rows_host = self.scroll.inner
        self.rows_host.columnconfigure(0, weight=1)
        self.empty_label = tk.Frame(body, background=p["surface"])
        self.empty_label.columnconfigure(0, weight=1)
        self.empty_label.rowconfigure(0, weight=1)
        inner = tk.Frame(self.empty_label, background=p["surface"])
        inner.grid(row=0, column=0)
        tk.Label(inner, text="No apps are routed yet", background=p["surface"], foreground=p["text"],
                 font=theme.font("section")).pack()
        tk.Label(inner, text="Pick a connection and an app on the right, add it to the queue and press Assign.",
                 background=p["surface"], foreground=p["text_muted"], font=theme.font("small")).pack(pady=(4, 0))
        self._show_empty(self.empty_label, True)

    def _build_bind_card(self, parent: Any) -> None:
        p = self.palette
        bg = p["surface"]
        panel, _header, outer = _panel(parent, "Route an app")
        panel.grid(row=0, column=0, sticky="nsew")
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)
        # The form and the queue scroll together, so a short window never hides queued apps;
        # the Assign row stays pinned below them.
        self.side_scroll = theme.ScrollFrame(outer, bg, height=60, width=SIDEBAR_WIDTH - 24)
        self.side_scroll.grid(row=0, column=0, sticky="nsew", padx=(0, 2))
        self.side_scroll.attach_wheel(self.root)
        body = self.side_scroll.inner
        body.columnconfigure(0, weight=1)
        self._side_outer = outer
        inner = tk.Frame(body, background=bg)
        inner.grid(row=0, column=0, sticky="ew", padx=(16, 12), pady=(10, 12))
        inner.columnconfigure(0, weight=1)

        def label(text: str, row: int, top: int = 0) -> None:
            tk.Label(inner, text=text, background=bg, foreground=p["text_muted"], font=theme.font("small"),
                     anchor="w").grid(row=row, column=0, sticky="w", pady=(top, 5))

        label("Connection", 0)
        self.iface_combo = ttk.Combobox(inner, textvariable=self.iface_var, state="readonly", values=[],
                                        font=theme.font("body"), height=10)
        self.iface_combo.grid(row=1, column=0, sticky="ew")
        self.iface_combo.bind("<<ComboboxSelected>>", lambda _e: self._update_buttons())

        tile_bg = p["bg"]
        self.app_tile = tk.Frame(inner, background=tile_bg, highlightthickness=1,
                                 highlightbackground=p["line_hi"], highlightcolor=p["focus"], cursor="hand2")
        self.app_tile.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self.app_tile.columnconfigure(1, weight=1)
        self.app_icon_host = tk.Frame(self.app_tile, background=tile_bg, width=TILE_ICON, height=TILE_ICON)
        self.app_icon_host.grid(row=0, column=0, rowspan=2, padx=10, pady=8)
        self.app_icon_host.grid_propagate(False)
        self.app_icon_widget: Any = None
        self.app_name_label = tk.Label(self.app_tile, text="", background=tile_bg, foreground=p["text"],
                                       font=theme.font("label"), anchor="w", width=1)
        self.app_name_label.grid(row=0, column=1, sticky="sew", pady=(8, 0))
        self.app_path_label = tk.Label(self.app_tile, text="", background=tile_bg,
                                       foreground=p["text_muted"], font=theme.font("small"), anchor="w", width=1)
        self.app_path_label.grid(row=1, column=1, sticky="new", pady=(0, 8))
        self.pick_btn = ttk.Button(self.app_tile, text="Choose", style="Small.Secondary.TButton",
                                   command=self._on_pick)
        self.pick_btn.grid(row=0, column=2, rowspan=2, padx=10)
        for widget in (self.app_tile, self.app_icon_host, self.app_name_label, self.app_path_label):
            widget.bind("<Button-1>", lambda _e: self._on_pick())

        label("Or type a command, or pick a recent app", 4, top=10)
        self.path_entry = ttk.Combobox(inner, textvariable=self.app_var, values=self._history_values(),
                                       font=theme.font("body"), height=12)
        self.path_entry.grid(row=5, column=0, sticky="ew")
        self.path_entry.bind("<<ComboboxSelected>>", lambda _e: self._on_history_pick())

        label("DNS for this connection", 6, top=10)
        self.dns_combo = ttk.Combobox(inner, textvariable=self.dns_var, values=dialogs.DNS_CHOICES,
                                      font=theme.font("body"))
        self.dns_combo.grid(row=7, column=0, sticky="ew")

        ttk.Checkbutton(
            inner, text="Separate profile if it is already running",
            variable=self.separate_var, style="Card.TCheckbutton",
        ).grid(row=8, column=0, sticky="w", pady=(10, 0))
        self.add_btn = ttk.Button(inner, text="Add to queue", style="Secondary.TButton", command=self._on_add)
        self.add_btn.grid(row=9, column=0, sticky="ew", pady=(12, 0))
        self._build_queue(body)

    def _build_queue(self, body: Any) -> None:
        """The queue lives in the same panel, under the form: it grows into whatever height is left."""
        p = self.palette
        _rule(body).grid(row=1, column=0, sticky="ew")
        head = tk.Frame(body, background=p["surface"])
        head.grid(row=2, column=0, sticky="ew", padx=16, pady=(10, 0))
        tk.Label(head, text="QUEUE", background=p["surface"], foreground=p["text_muted"],
                 font=theme.font("eyebrow")).pack(side="left")
        self.queue_count = tk.Label(head, text="", background=p["surface"], foreground=p["text_muted"],
                                    font=theme.font("mono"))
        self.queue_count.pack(side="right")
        self.pending_host = tk.Frame(body, background=p["surface"])
        self.pending_host.grid(row=3, column=0, sticky="ew", padx=(16, 12), pady=(8, 8))
        self.pending_host.columnconfigure(0, weight=1)
        self.pending_empty = tk.Frame(self.pending_host, background=p["surface"])
        self.pending_empty.columnconfigure(0, weight=1)
        tk.Label(self.pending_empty, text="Queued apps start together with one password prompt.",
                 background=p["surface"], foreground=p["text_muted"], font=theme.font("small"),
                 justify="left", anchor="w", wraplength=290).grid(row=0, column=0, sticky="w", pady=(0, 4))
        self._show_empty(self.pending_empty, True)
        _rule(self._side_outer).grid(row=1, column=0, sticky="ew")
        buttons = tk.Frame(self._side_outer, background=p["surface"])
        buttons.grid(row=2, column=0, sticky="ew", padx=16, pady=(12, 14))
        buttons.columnconfigure(0, weight=1)
        self.assign_btn = ttk.Button(buttons, text="Assign", style="Primary.TButton", command=self._on_assign)
        self.assign_btn.grid(row=0, column=0, sticky="ew")
        self.clear_btn = ttk.Button(buttons, text="Clear", style="Secondary.TButton", command=self._on_clear)
        self.clear_btn.grid(row=0, column=1, padx=(8, 0))

    @staticmethod
    def _show_empty(empty: Any, show: bool) -> None:
        if show:
            empty.grid(row=0, column=0, sticky="nsew")
            empty.tkraise()
        else:
            empty.grid_remove()

    def _update_buttons(self) -> None:
        idle = self._busy == 0
        has_iface = self._iface_name() is not None
        has_app = bool(self.app_var.get().strip())
        _set_enabled(self.add_btn, has_iface and has_app)
        _set_enabled(self.assign_btn, idle and bool(self._pending))
        _set_enabled(self.clear_btn, idle and bool(self._pending))
        _set_enabled(self.pick_btn, idle)
        _set_enabled(self.teardown_btn, idle)
        _set_enabled(self.reset_btn, idle)
        for key, card in self._cards.items():
            data = self._rows.get(key) or {}
            card.set_enabled(idle, bool(data.get("running")))
        self.queue_count.configure(text=str(len(self._pending)) if self._pending else "")

    # -- app tile ---------------------------------------------------------

    def _entry_for(self, path: str) -> Optional[AppEntry]:
        if self._picked_entry is not None and self._picked_entry.binary == path:
            return self._picked_entry
        return self._picked_entries.get(path) or self._desktop_info.get(path)

    def _icon_widget(self, parent: Any, path: str, name: str, size: int, background: str) -> Any:
        """An icon Label for the app at `path`, or a letter tile. Tk thread only."""
        entry = self._entry_for(path)
        if entry is not None and entry.icon:
            image = icons.load_tk_icon(self.root, icons.resolve_icon_path(entry.icon), size)
            if image is not None:
                label = tk.Label(parent, image=image, background=background, borderwidth=0)
                label.image = image  # type: ignore[attr-defined]
                return label
        return icons.letter_tile(parent, name, size, colour=self.palette["border"],
                                 background=background, foreground=self.palette["text"])

    def _refresh_app_tile(self) -> None:
        p = self.palette
        path = self.app_var.get().strip()
        if self.app_icon_widget is not None:
            self.app_icon_widget.destroy()
            self.app_icon_widget = None
        if not path:
            self.app_name_label.configure(text="No application chosen", foreground=p["text_muted"])
            _fit_label(self.app_path_label, "Choose the application to run through the selected interface.",
                       theme.font("small"))
            widget = icons.letter_tile(self.app_icon_host, "?", TILE_ICON, colour=p["border"],
                                       background=p["surface_alt"], foreground=p["text_muted"])
        else:
            entry = self._entry_for(path)
            name = entry.name if entry is not None else os.path.basename(path) or path
            self.app_name_label.configure(text=name, foreground=p["text"])
            _fit_label(self.app_path_label, path, theme.font("small"))
            widget = self._icon_widget(self.app_icon_host, path, name, TILE_ICON, p["surface_alt"])
        widget.place(relx=0.5, rely=0.5, anchor="center")
        widget.bind("<Button-1>", lambda _e: self._on_pick())
        self.app_icon_widget = widget

    def _on_app_changed(self) -> None:
        self._refresh_app_tile()
        self._update_buttons()

    def _on_icon_scan(self, result: Any, error: Optional[BaseException]) -> None:
        if error is not None or not result:
            return
        self._desktop_info = result
        self._refresh_app_tile()
        for key, card in self._cards.items():
            self._set_card_icon(card, self._card_data(key))
        self._render_pending()

    def _set_card_icon(self, card: _AppCard, row: Optional[Dict[str, Any]]) -> None:
        if row is None:
            return
        card.set_icon(self._icon_widget(card.icon_host, row["app"], row["app_name"], CARD_ICON,
                                        self.palette["surface"]))

    # -- status and errors ----------------------------------------------

    def _set_status(self, text: str, kind: Optional[str] = None) -> None:
        p = self.palette
        if not text:
            text, kind = "Ready", "idle"
        self.status_var.set(text)
        kind = kind or status_kind(text)
        colour = {"error": p["danger"], "success": p["success"], "warning": p["warning"]}.get(
            kind, p["text_muted"])
        graph.set_dot(self.status_dot, colour)

    def _fail(self, title: str, error: BaseException) -> None:
        text = _error_text(error)
        self._set_status(f"Error: {text}", "error")
        messagebox.showerror(title, text, parent=self.root)

    # -- refresh --------------------------------------------------------

    def refresh_all(self) -> None:
        """Re-detect the session and tools, then re-read interfaces and bindings."""
        self.worker.submit(_environment_task, on_done=self._on_environment)
        self.refresh_snapshot()

    def refresh_snapshot(self) -> None:
        if self._snapshot_inflight:
            return
        self._snapshot_inflight = True
        self.worker.submit(_snapshot_task, on_done=self._on_snapshot)

    def _on_snapshot(self, result: Any, error: Optional[BaseException]) -> None:
        self._snapshot_inflight = False
        if error is not None:
            self._set_status(f"Could not read interfaces: {_error_text(error)}", "error")
            return
        self._apply_interfaces(result["interfaces"])
        self._apply_bindings(result["bindings"])
        self._apply_adoptions(result.get("adoptions", []))
        self._namespaces = dict(result.get("namespaces") or {})
        self._render_cards()
        self._render_lanes()
        self._update_buttons()

    def _on_environment(self, result: Any, error: Optional[BaseException]) -> None:
        if error is not None:
            self.session_var.set("Session unknown")
            self._set_status(f"Could not detect session: {_error_text(error)}", "error")
            return
        session, missing = result
        self.session_var.set(session_label(session))
        self._apply_banner(missing)

    def _schedule_auto_refresh(self) -> None:
        self.root.after(AUTO_REFRESH_MS, self._auto_refresh_tick)

    def _auto_refresh_tick(self) -> None:
        self.refresh_snapshot()
        self._schedule_auto_refresh()

    def _apply_banner(self, missing: Sequence[deps.Dependency]) -> None:
        for child in self.banner.winfo_children():
            child.destroy()
        if not missing:
            self.banner.grid_remove()
            return
        ttk.Label(self.banner, text="Required tools are missing", style="BannerTitle.TLabel").pack(anchor="w")
        for dep in missing:
            ttk.Label(
                self.banner,
                text=f"{dep.tool}: {dep.purpose}. Install with: {dep.install_hint}",
                style="Banner.TLabel",
            ).pack(anchor="w", pady=(4, 0))
        self.banner.grid(row=1, column=0, sticky="ew", padx=theme.SPACE_OUTER, pady=(0, theme.SPACE_CARD_GAP))

    # -- interfaces -------------------------------------------------------

    def _iface_name(self) -> Optional[str]:
        return self._iface_map.get(self.iface_var.get())

    def _apply_interfaces(self, interfaces: Sequence[netinfo.Interface]) -> None:
        previous = self._iface_name()
        mapping: Dict[str, str] = {}
        for iface in interfaces:
            if not iface.bindable:
                continue
            mapping[interface_label(iface)] = iface.name
        changed = list(mapping) != list(self._iface_map)
        self._iface_map = mapping
        labels = list(mapping)
        chosen = next((lbl for lbl, name in mapping.items() if name == previous), labels[0]) if labels else ""
        self.iface_var.set(chosen or NO_IFACE_TEXT)
        if changed:
            self.iface_combo.configure(values=labels)
        if labels:
            self.iface_combo.state(["!disabled", "readonly"])
        else:
            self.iface_combo.state(["disabled"])
        self._interfaces = list(interfaces)
        self._link_speeds = {i.name: traffic.link_speed_bytes(i.name) for i in interfaces}

    # -- network view -------------------------------------------------------

    def _bound_ifaces(self) -> List[str]:
        names = [row["iface"] for row in self._rows.values()] + [a["iface"] for a in self._adoptions]
        return list(dict.fromkeys(names))

    def _render_lanes(self) -> None:
        """Create, update or remove one lane per visible connection, then refresh their chips."""
        wanted = monitor.visible_lanes(self._interfaces, self._bound_ifaces())
        names = [iface.name for iface in wanted]
        for name in list(self._lanes):
            if name not in names:
                self._lanes.pop(name).destroy()
        for index, iface in enumerate(wanted):
            lane = self._lanes.get(iface.name)
            if lane is None:
                lane = monitor.LaneRow(self.lanes_host, iface, self._on_route_here)
                self._lanes[iface.name] = lane
            else:
                lane.set_interface(iface)
            lane.frame.grid(row=index * 2, column=0, sticky="ew")
            if index < len(wanted) - 1:
                lane.separator.grid(row=index * 2 + 1, column=0, sticky="ew")
            else:
                lane.separator.grid_remove()
            lane.set_chips(self._chips_for(iface.name), self.select_binding, self._show_app_menu)
            lane.set_namespace(self._namespaces.get(iface.name), self._show_lane_menu)
        if wanted:
            self.lanes_empty.place_forget()
        else:
            self.lanes_empty.place(relx=0.5, rely=0.5, anchor="center")
        self._render_traffic()

    def _chips_for(self, iface: str) -> List[Dict[str, Any]]:
        chips = []
        for key, row in self._rows.items():
            if row["iface"] == iface:
                chips.append({"key": key, "name": row["app_name"], "colour": self.colours.colour(row["app"]),
                              "running": bool(row["running"]), "note": "" if row["running"] else "stopped"})
        for data in self._adoptions:
            if data["iface"] == iface:
                chips.append({"key": ADOPT_PREFIX + data["id"], "name": data["app_name"],
                              "colour": self.colours.colour(data["app"]), "running": bool(data.get("alive", True)),
                              "note": "adopted"})
        return chips

    def _on_route_here(self, iface: str) -> None:
        """"+ Route an app here" on a lane: choose that connection in the sidebar, then pick an app."""
        label = next((lbl for lbl, name in self._iface_map.items() if name == iface), None)
        if label is None:
            self._set_status(f"{iface} cannot be used for routing right now.", "warning")
            return
        self.iface_var.set(label)
        self._update_buttons()
        if self.app_var.get().strip():
            self._set_status(f"Connection set to {iface}. Press Add to queue.")
        else:
            self._set_status(f"Connection set to {iface}. Choose the app to route.")
            self._on_pick()

    def _traffic_tick(self) -> None:
        try:
            self._sample_traffic()
        except Exception:
            log.exception("traffic sampling failed")
        try:
            self.root.after(TRAFFIC_MS, self._traffic_tick)
        except tk.TclError:
            pass

    def _sample_traffic(self) -> None:
        lanes = [iface.name for iface in monitor.visible_lanes(self._interfaces, self._bound_ifaces())]
        veths = {iface: netns.veth_names(iface)[0] for iface in {row["iface"] for row in self._rows.values()}}
        counters = traffic.read_counters(lanes + list(veths.values()))
        samples: Dict[str, Tuple[int, int]] = {}
        for name in lanes:
            if name in counters:
                samples[name] = traffic.iface_sample(counters[name])
        for iface, veth in veths.items():
            if veth in counters:
                samples[ns_key(iface)] = traffic.veth_sample(counters[veth])
        self.tracker.update(samples, time.monotonic())
        try:
            visible = bool(self.root.winfo_viewable())
        except tk.TclError:
            return
        if visible:
            self._render_traffic()

    def _render_traffic(self) -> None:
        p = self.palette
        routed_counts: Dict[str, int] = {}
        for row in self._rows.values():
            if row["running"]:
                routed_counts[row["iface"]] = routed_counts.get(row["iface"], 0) + 1
        for data in self._adoptions:
            if data.get("alive", True):
                routed_counts[data["iface"]] = routed_counts.get(data["iface"], 0) + 1
        for name, lane in self._lanes.items():
            key = ns_key(name)
            routed = self.tracker.rate(key) if key in self.tracker.names() else None
            lane.set_traffic(self.tracker.history(name), routed, routed_counts.get(name, 0),
                             self._link_speeds.get(name))

        physical = [name for name in self._lanes if not monitor.is_vpn_name(name)]
        down_series = sum_series([self.tracker.history(n) for n in physical], 0)
        up_series = sum_series([self.tracker.history(n) for n in physical], 1)
        routed_names = [n for n in self.tracker.names() if n.startswith(NS_PREFIX)]
        routed_series = [d + u for d, u in zip(sum_series([self.tracker.history(n) for n in routed_names], 0),
                                               sum_series([self.tracker.history(n) for n in routed_names], 1))]
        down = down_series[-1] if down_series else 0.0
        up = up_series[-1] if up_series else 0.0
        routed_now = routed_series[-1] if routed_series else 0.0
        number, unit = traffic.split_rate(down)
        self.tiles["down"].set(number, unit, f"{len(physical)} link" + ("" if len(physical) == 1 else "s"),
                               down_series)
        number, unit = traffic.split_rate(up)
        self.tiles["up"].set(number, unit, "all apps", up_series)
        number, unit = traffic.split_rate(routed_now)
        share = routed_share(routed_now, down + up)
        self.tiles["routed"].set(number, unit, f"{share} of total" if share else "", routed_series)
        running = sum(1 for r in self._rows.values() if r["running"]) + sum(
            1 for a in self._adoptions if a.get("alive", True))
        total = len(self._rows) + len(self._adoptions)
        lanes_used = len({r["iface"] for r in self._rows.values()} | {a["iface"] for a in self._adoptions})
        self.tiles["apps"].set(str(running), f"of {total} running" if total else "running",
                               f"{lanes_used} link" + ("" if lanes_used == 1 else "s") if total else "")
        shared: Dict[str, int] = {}
        for row in self._rows.values():
            shared[row["iface"]] = shared.get(row["iface"], 0) + 1
        for key, card in self._cards.items():
            data = self._card_data(key)
            if data is None:
                continue
            if card.adopted:
                card.set_traffic(None, 0)
            else:
                name = ns_key(data["iface"])
                card.set_traffic(self.tracker.history(name) if name in self.tracker.names() else
                                 [(0.0, 0.0)] * self.tracker.size, shared.get(data["iface"], 1),
                                 self.tracker.totals(name))
        self.live_label.configure(text=f"live · {time.strftime('%H:%M:%S')}")

    # -- bindings -----------------------------------------------------------

    def _card_data(self, key: str) -> Optional[Dict[str, Any]]:
        if key.startswith(ADOPT_PREFIX):
            return next((a for a in self._adoptions if ADOPT_PREFIX + a["id"] == key), None)
        return self._rows.get(key)

    def _apply_bindings(self, rows: Sequence[Dict[str, Any]]) -> None:
        self._rows = {}
        for row in rows:
            iid = _iid(row["iface"], row["app"])
            self._rows[iid] = dict(row, iid=iid)
        now = time.monotonic()
        self._public_ips = {k: v for k, v in self._public_ips.items()
                            if any(r["iface"] == k for r in rows) and fresh_public_ip(v, now)}

    def _apply_adoptions(self, rows: Sequence[Dict[str, Any]]) -> None:
        self._adoptions = [dict(r) for r in rows]

    def _render_cards(self) -> None:
        """Bindings first, then adoptions; cards are kept and updated in place."""
        keys = list(self._rows) + [ADOPT_PREFIX + a["id"] for a in self._adoptions]
        for key in list(self._cards):
            if key not in keys:
                self._cards.pop(key).frame.destroy()
        now = time.monotonic()
        kinds = {iface.name: graph.kind_for(iface) for iface in self._interfaces}
        for index, key in enumerate(keys):
            data = self._card_data(key)
            if data is None:
                continue
            card = self._cards.get(key)
            if card is None:
                card = _AppCard(self, self.rows_host, key, adopted=key.startswith(ADOPT_PREFIX))
                self._cards[key] = card
                card.update(data, "", self.colours.colour(data["app"]), kinds.get(data["iface"], "other"))
                self._set_card_icon(card, data)
            public_ip = "" if card.adopted else fresh_public_ip(self._public_ips.get(data["iface"]), now)
            card.update(data, public_ip, self.colours.colour(data["app"]), kinds.get(data["iface"], "other"))
            card.frame.grid(row=index, column=0, sticky="ew", pady=(0, 8))
        if self._selected_iid not in self._cards:
            self._selected_iid = None
        for key, card in self._cards.items():
            card.set_selected(key == self._selected_iid)
        self._show_empty(self.empty_label, not keys)
        # With nothing routed the apps panel only needs room for its message; the network view takes the rest.
        self.main.rowconfigure(2, weight=3 if keys else 0)
        running = sum(1 for r in self._rows.values() if r["running"])
        self.apps_count.configure(text=f"{running} running · {len(keys)} total" if keys else "")

    def _show_app_menu(self, key: str, event: Any) -> None:
        """Right-click on a chip or card: the same actions as the card's buttons."""
        self.select_binding(key)
        data = self._card_data(key)
        if data is None:
            return
        p = self.palette
        menu = tk.Menu(self.root, tearoff=False, background=p["surface_alt"], foreground=p["text"],
                       activebackground=p["accent"], activeforeground=p["accent_text"],
                       disabledforeground=p["line_hi"], borderwidth=1, relief="flat")
        idle = self._busy == 0
        if key.startswith(ADOPT_PREFIX):
            menu.add_command(label=f"Release {data['app_name']}", state="normal" if idle else "disabled",
                             command=lambda: self._on_release_adoption(data))
        else:
            states = row_button_states(bool(data["running"]), idle)
            menu.add_command(label="Verify public IP", command=self._on_verify,
                             state="normal" if states["verify"] else "disabled")
            menu.add_command(label="Restart", command=self._on_restart,
                             state="normal" if states["restart"] else "disabled")
            menu.add_command(label="Stop", command=self._on_stop, state="normal" if states["stop"] else "disabled")
            menu.add_command(label="Open log", command=self._on_open_log,
                             state="normal" if states["log"] else "disabled")
            move = tk.Menu(menu, tearoff=False, background=p["surface_alt"], foreground=p["text"],
                           activebackground=p["accent"], activeforeground=p["accent_text"],
                           disabledforeground=p["line_hi"])
            targets = [(label, name) for label, name in self._iface_map.items() if name != data["iface"]]
            for label, name in targets:
                move.add_command(label=label, command=lambda n=name: self._on_move(n))
            menu.add_cascade(label="Move to (restarts it)", menu=move,
                             state="normal" if idle and targets else "disabled")
            menu.add_separator()
            menu.add_command(label="Force quit", command=self._on_force_quit,
                             state="normal" if states["force"] else "disabled")
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _on_release_adoption(self, data: Dict[str, Any]) -> None:
        if self._busy:
            return
        self._run_mutation(
            f"Releasing {data['app_name']}",
            helper_client.call,
            "release_adoption",
            {"id": data["id"]},
            on_done=partial(self._on_release_done, data["app_name"]),
        )

    def _on_release_done(self, name: str, result: Any, error: Optional[BaseException]) -> None:
        if error is not None:
            self._fail("Release failed", error)
            return
        still = result.get("still_running", []) if isinstance(result, dict) else []
        if still:
            self._set_status(still_running_message(name, "release", still), "warning")
        else:
            self._set_status(f"Stopped routing {name}; it uses the default route again.")

    def select_binding(self, iid: str) -> None:
        self._selected_iid = iid if iid in self._cards else None
        for key, card in self._cards.items():
            card.set_selected(key == self._selected_iid)
        card = self._cards.get(iid)
        if card is not None:
            self._scroll_into_view(card.frame)

    def _scroll_into_view(self, widget: Any) -> None:
        try:
            self.root.update_idletasks()
            total = max(self.rows_host.winfo_height(), 1)
            top = widget.winfo_y()
            first, last = self.scroll.canvas.yview()
            if top / total < first or (top + widget.winfo_height()) / total > last:
                self.scroll.scroll_to(top / total)
        except tk.TclError:
            pass

    def _selected_row(self) -> Optional[Dict[str, Any]]:
        if self._selected_iid is None:
            return None
        return self._rows.get(self._selected_iid)

    def _on_return_key(self, _event: Any) -> None:
        if isinstance(safe_focus(self.root), ttk.Button):
            return  # Enter on a focused button activates that button
        if not self.add_btn.instate(["disabled"]):
            self._on_add()

    def _on_delete_key(self) -> None:
        if not delete_key_allowed(widget_class_name(safe_focus(self.root))):
            return
        row = self._selected_row()
        if self._busy == 0 and row is not None and row.get("running"):
            self._on_stop()

    # -- mutations ------------------------------------------------------

    def _run_mutation(self, label: str, fn: Any, *args: Any, on_done: Any, **kwargs: Any) -> None:
        """Run a helper call in the Worker with the buttons locked; refresh bindings afterwards."""
        self._busy += 1
        self._set_status(f"{label}...")
        self._update_buttons()

        def done(result: Any, error: Optional[BaseException]) -> None:
            self._busy -= 1
            self._update_buttons()
            on_done(result, error)
            self.refresh_snapshot()

        self.worker.submit(fn, *args, on_done=done, **kwargs)

    # -- pending queue ----------------------------------------------------

    def _on_add(self) -> None:
        iface = self._iface_name()
        if not iface:
            self._set_status("No bindable interface is available.")
            return
        text = self.app_var.get().strip()
        if not text:
            self._set_status("Choose an application first.")
            return
        try:
            app_path = apps.resolve_app_path(text)
        except InterMuxError as exc:
            self._fail("Application not found", exc)
            return
        entry = self._entry_for(text) or self._entry_for(app_path)
        if entry is not None:
            self._picked_entries[app_path] = entry
        name = entry.name if entry is not None else os.path.basename(app_path)
        try:
            dns_value = dialogs.dns_choice_value(self.dns_var.get())
        except ValueError as exc:
            self._set_status(f"Could not use the DNS servers: {exc}", "error")
            return
        item = {"iface": iface, "app": app_path, "name": name,
                "policy": "separate" if self.separate_var.get() else "ask",
                # "Automatic" keeps what an existing namespace uses; a new one picks servers automatically.
                "dns": dnsconf.KEEP if dns_value == dnsconf.AUTO else dns_value,
                "dns_text": "" if dns_value == dnsconf.AUTO else self.dns_var.get().strip()}
        if queue_add(self._pending, item):
            self._set_status(f"Added {name} via {iface} to pending.")
        else:
            self._set_status(f"{name} via {iface} is already pending.")
        self.app_var.set("")
        self._picked_entry = None
        self._render_pending()
        self._update_buttons()

    def _remove_pending(self, item: Dict[str, Any]) -> None:
        if self._assigning:
            return
        if item in self._pending:
            self._pending.remove(item)
        self._render_pending()
        self._update_buttons()

    def _on_clear(self) -> None:
        if self._busy or not self._pending:
            return
        self._pending.clear()
        self._render_pending()
        self._update_buttons()
        self._set_status("Pending list cleared.")

    def _render_pending(self) -> None:
        for row in self._pending_widgets:
            row.frame.destroy()
        self._pending_widgets = []
        for index, item in enumerate(self._pending):
            row = _PendingRow(self, self.pending_host, item)
            row.frame.grid(row=index, column=0, sticky="ew", pady=(0, 8))
            self._pending_widgets.append(row)
        self._show_empty(self.pending_empty, not self._pending)

    def _running_instances(self, app_path: str) -> int:
        """Count running processes of the app owned by the invoking user, as the engine does."""
        try:
            handler = apps.get_handler(app_path)
            return len(procs.find_app_procs(app_path, handler.comm_names(app_path), uid=os.getuid()))
        except Exception:
            log.exception("could not check running instances of %s", app_path)
            return 0

    def _on_assign(self) -> None:
        if self._busy or not self._pending:
            return
        self._assigning = True
        plan: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        for item in list(self._pending):
            name = item.get("name") or os.path.basename(item["app"])
            policy = item["policy"]
            op_kind = "bind"
            count = self._running_instances(item["app"])
            if count and policy == "ask":
                choice, _ = _ask_choice(
                    self.root,
                    "Application already running",
                    f"{name} is already running ({count} process(es)).\n\n"
                    "Close and reopen stops the running copy first. "
                    "Open separate profile starts a second copy with its own profile. "
                    "Shift current session routes the running copy through the interface "
                    "without restarting it.",
                    already_running_choices(),
                )
                if choice is None:
                    continue  # cancelled: this item stays pending
                if choice == "shift":
                    op_kind = "adopt"
                else:
                    policy = choice
            if op_kind == "adopt":
                op = {"op": "adopt", "iface": item["iface"], "app": item["app"]}
            else:
                op = {"op": "bind", "iface": item["iface"], "app": item["app"],
                      "running_policy": policy, "dns": item.get("dns", dnsconf.KEEP)}
            plan.append((item, op))
        if not plan:
            self._assigning = False
            self._set_status("Assign cancelled.")
            return
        self._run_mutation(
            f"Assigning {len(plan)} app(s) (one password prompt)",
            _batch_task,
            [op for _, op in plan],
            on_done=partial(self._on_batch_done, plan),
        )

    def _on_batch_done(self, plan: List[Tuple[Dict[str, Any], Dict[str, Any]]],
                       results: Any, error: Optional[BaseException]) -> None:
        self._assigning = False
        if error is not None:
            self._render_pending()
            self._fail("Assign failed", error)
            return
        done: List[str] = []
        failed: List[str] = []
        for (item, _op), result in zip(plan, results or []):
            name = item.get("name") or os.path.basename(item["app"])
            if isinstance(result, dict) and result.get("ok"):
                if item in self._pending:
                    self._pending.remove(item)
                done.append(name)
                self.history.add(HistoryItem(app=item["app"], name=name, iface=item["iface"],
                                             dns=item.get("dns_text", "")))
            else:
                err = result.get("error") if isinstance(result, dict) else None
                err = err if isinstance(err, dict) else {}
                failed.append(f"{name} ({err.get('code') or 'error'}: {err.get('message') or 'failed'})")
        self._render_pending()
        self._update_buttons()
        self.path_entry.configure(values=self._history_values())
        if failed:
            prefix = f"Assigned {len(done)}. " if done else ""
            self._set_status(f"{prefix}Failed, still pending: " + "; ".join(failed), "error")
        elif done:
            self._set_status("Assigned " + ", ".join(done) + ".", "success")
        else:
            self._set_status("Nothing was assigned.")

    # -- history ------------------------------------------------------------

    def _history_values(self) -> List[str]:
        return [item.app for item in self.history.items]

    def _on_history_pick(self) -> None:
        """Picking a recent app restores the DNS it was routed with (and its connection, if still usable)."""
        item = self.history.find(self.app_var.get().strip())
        if item is None:
            return
        self.dns_var.set(item.dns or dialogs.AUTO_LABEL)
        label = next((lbl for lbl, name in self._iface_map.items() if name == item.iface), None)
        if label is not None:
            self.iface_var.set(label)
        self._update_buttons()

    # -- per-app and per-connection controls ------------------------------

    def _on_restart(self) -> None:
        row = self._selected_row()
        if row is None:
            return
        self._run_mutation(
            f"Restarting {row['app_name']} on {row['iface']}",
            _env_task, partial(_restart_call, row["iface"], row["app"]),
            on_done=partial(self._on_restart_done, row["app_name"]),
        )

    def _on_restart_done(self, name: str, result: Any, error: Optional[BaseException]) -> None:
        if error is not None:
            self._fail("Restart failed", error)
            return
        warnings = result.get("warnings", []) if isinstance(result, dict) else []
        self._set_status(f"Restarted {name}." + (f" Warning: {warnings[0]}" if warnings else ""),
                         "warning" if warnings else "success")

    def _on_move(self, to: str) -> None:
        row = self._selected_row()
        if row is None:
            return
        self._run_mutation(
            f"Moving {row['app_name']} from {row['iface']} to {to}",
            _env_task, partial(_move_call, row["iface"], row["app"], to),
            on_done=partial(self._on_move_done, row["app_name"], to),
        )

    def _on_move_done(self, name: str, to: str, result: Any, error: Optional[BaseException]) -> None:
        if error is not None:
            self._fail("Move failed", error)
            return
        self._selected_iid = None
        self._set_status(f"Moved {name} to {to}.", "success")

    def _show_lane_menu(self, iface: str, event: Any) -> None:
        """The settings of a connection's namespace: limit, DNS, reconnect, remove."""
        p = self.palette
        info = self._namespaces.get(iface) or {}
        idle = self._busy == 0
        state_ = "normal" if idle else "disabled"
        menu = tk.Menu(self.root, tearoff=False, background=p["surface_alt"], foreground=p["text"],
                       activebackground=p["accent"], activeforeground=p["accent_text"],
                       disabledforeground=p["line_hi"], borderwidth=1, relief="flat")
        menu.add_command(label="Limit bandwidth…", state=state_, command=lambda: self._on_limit(iface))
        menu.add_command(label="DNS servers…", state=state_, command=lambda: self._on_dns(iface))
        menu.add_command(label="Re-add routes", state=state_, command=self._on_reconnect)
        menu.add_separator()
        menu.add_command(label="Remove namespace…", state=state_, command=lambda: self._on_remove_namespace(iface))
        if not info:
            for index in (0, 1, 4):
                menu.entryconfigure(index, state="disabled")
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _on_limit(self, iface: str) -> None:
        current = (self._namespaces.get(iface) or {}).get("limit") or {}
        answer = dialogs.LimitDialog(self.root, iface, current).run()
        if answer is None:
            return
        down, up = answer
        self._run_mutation(f"Setting the bandwidth limit of {iface}", helper_client.call, "set_limit",
                           {"iface": iface, "down": down, "up": up},
                           on_done=partial(self._on_limit_done, iface))

    def _on_limit_done(self, iface: str, result: Any, error: Optional[BaseException]) -> None:
        if error is not None:
            self._fail("Bandwidth limit failed", error)
            return
        from intermux.system import shaping
        if not result.get("down") and not result.get("up"):
            self._set_status(f"Removed the bandwidth limit of {iface}.", "success")
        else:
            self._set_status(f"Limited {iface}: download {shaping.format_mbit(result.get('down'))}, "
                             f"upload {shaping.format_mbit(result.get('up'))}.", "success")

    def _on_dns(self, iface: str) -> None:
        current = (self._namespaces.get(iface) or {}).get("dns") or []
        answer = dialogs.DnsDialog(self.root, iface, current).run()
        if answer is None:
            return
        self._run_mutation(f"Changing the DNS of {iface}", helper_client.call, "set_dns",
                           {"iface": iface, "dns": answer}, on_done=partial(self._on_dns_done, iface))

    def _on_dns_done(self, iface: str, result: Any, error: Optional[BaseException]) -> None:
        if error is not None:
            self._fail("DNS change failed", error)
            return
        label = "automatic" if result.get("automatic") else dnsconf.describe(result.get("servers") or [])
        warnings = result.get("warnings") or []
        self._set_status(f"DNS of {iface}: {label}." + (f" Warning: {warnings[0]}" if warnings else ""),
                         "warning" if warnings else "success")

    def _on_reconnect(self) -> None:
        self._run_mutation("Re-adding routes", helper_client.call, "reconnect", on_done=self._on_reconnect_done)

    def _on_reconnect_done(self, result: Any, error: Optional[BaseException]) -> None:
        if error is not None:
            self._fail("Re-adding routes failed", error)
            return
        refreshed = result.get("refreshed") or []
        self._set_status(f"Routes re-added for {', '.join(refreshed)}." if refreshed
                         else "Every connection that is up already has its route.")

    def _on_remove_namespace(self, iface: str) -> None:
        names = [r["app_name"] for r in self._rows.values() if r["iface"] == iface]
        choice, _ = _ask_choice(
            self.root, "Remove namespace",
            f"Stop everything running through {iface} and remove its namespace, routes and rules?"
            + (f"\n\nApps: {', '.join(names)}." if names else "")
            + " Unsaved work in those apps may be lost.",
            [("Remove", "kill"), ("Cancel", None)],
        )
        if choice != "kill":
            return
        self._run_mutation(f"Removing the {iface} namespace", helper_client.call, "remove_namespace",
                           {"iface": iface}, on_done=partial(self._on_remove_namespace_done, iface))

    def _on_remove_namespace_done(self, iface: str, result: Any, error: Optional[BaseException]) -> None:
        if error is not None:
            self._fail("Remove failed", error)
            return
        if result.get("removed"):
            self._set_status(f"Removed the {iface} namespace.", "success")
        else:
            still = result.get("still_running") or []
            self._set_status(still_running_message(iface, "remove", still), "warning")

    def _on_stop(self) -> None:
        row = self._selected_row()
        if row is None:
            self._set_status("Select a binding first.")
            return
        self._run_mutation(
            f"Stopping {row['app_name']}",
            helper_client.call,
            "stop_app",
            {"iface": row["iface"], "app": row["app"]},
            on_done=partial(self._on_stop_done, row["app_name"]),
        )

    def _on_stop_done(self, name: str, result: Any, error: Optional[BaseException]) -> None:
        if error is not None:
            self._fail("Stop failed", error)
            return
        still = result.get("still_running", []) if isinstance(result, dict) else []
        if still:
            self._set_status(still_running_message(name, "stop", still), "warning")
        else:
            self._set_status(f"Stopped {name}.")

    def _on_verify(self) -> None:
        row = self._selected_row()
        if row is None:
            return
        self._run_mutation(
            f"Verifying {row['iface']}",
            helper_client.call,
            "verify",
            {"iface": row["iface"]},
            on_done=self._on_verify_done,
        )

    def _on_verify_done(self, result: Any, error: Optional[BaseException]) -> None:
        if error is not None:
            self._fail("Verify failed", error)
            return
        iface = result.get("iface", "")
        ip = result.get("public_ip")
        if ip:
            self._public_ips[iface] = (ip, time.monotonic())
            self._set_status(f"{iface} public IP: {ip}")
        else:
            self._public_ips.pop(iface, None)
            self._set_status(f"Verify failed for {iface}: {result.get('error') or 'unknown error'}")

    def _on_open_log(self) -> None:
        row = self._selected_row()
        if row is None:
            return
        if not row["log_path"]:
            self._set_status("No log file is recorded for this binding.")
            return
        self.worker.submit(
            commands.open_with_desktop, row["log_path"], timeout=OPEN_LOG_TIMEOUT,
            on_done=self._on_open_log_done,
        )

    def _on_open_log_done(self, result: Any, error: Optional[BaseException]) -> None:
        if error is not None:
            self._fail("Open log failed", error)
        elif not result.ok:
            self._set_status(f"Could not open log: {result.stderr.strip() or 'xdg-open failed'}")

    def _on_force_quit(self) -> None:
        row = self._selected_row()
        if row is None:
            return
        choice, _ = _ask_choice(
            self.root,
            "Force-quit application",
            f"Force-quit every running {row['app_name']} process on this computer? "
            "Unsaved work will be lost.",
            [("Force-quit", "kill"), ("Cancel", None)],
        )
        if choice != "kill":
            return
        self._run_mutation(
            f"Force-quitting {row['app_name']}",
            helper_client.call,
            "kill_app",
            {"app": row["app"], "force": True},
            on_done=partial(self._on_kill_done, row["app_name"]),
        )

    def _on_kill_done(self, name: str, result: Any, error: Optional[BaseException]) -> None:
        if error is not None:
            self._fail("Force-quit failed", error)
            return
        still = result.get("still_running", []) if isinstance(result, dict) else []
        if still:
            self._set_status(still_running_message(name, "force-quit", still), "warning")
        else:
            self._set_status(f"Force-quit {name}.")

    def _on_teardown(self) -> None:
        self._run_mutation("Removing idle bindings", helper_client.call, "teardown_idle",
                           on_done=self._on_teardown_done)

    def _on_teardown_done(self, result: Any, error: Optional[BaseException]) -> None:
        if error is not None:
            self._fail("Teardown failed", error)
            return
        removed = result.get("removed", []) if isinstance(result, dict) else []
        if removed:
            self._set_status(f"Removed {len(removed)} idle binding(s).")
        else:
            self._set_status("No idle bindings to remove.")

    def _on_reset(self) -> None:
        choice, kill_apps = _ask_choice(
            self.root,
            "Reset everything",
            "This removes every InterMux namespace, route and NAT rule, and clears all bindings.",
            [("Reset", "reset"), ("Cancel", None)],
            checkbox="Also close applications running through InterMux",
        )
        if choice != "reset":
            self._set_status("Reset cancelled.")
            return
        self._run_mutation("Resetting", helper_client.call, "reset", {"kill_apps": kill_apps},
                           on_done=self._on_reset_done)

    def _on_reset_done(self, result: Any, error: Optional[BaseException]) -> None:
        if error is not None:
            self._fail("Reset failed", error)
            return
        still = result.get("still_running", []) if isinstance(result, dict) else []
        kept = result.get("kept_apps", []) if isinstance(result, dict) else []
        if kept:
            self._set_status("Reset done, but routing was kept for apps that are still running: "
                             + ", ".join(str(k) for k in kept) + ". Close them, then press Teardown idle.",
                             "warning")
        elif still:
            self._set_status("Reset complete, but some applications are still running.")
        else:
            self._set_status("Reset complete.")

    # -- pickers and window -----------------------------------------------

    def _on_pick(self) -> None:
        if self._busy:
            return
        from intermux.ui.picker import AppPicker

        AppPicker(self.root, self._on_picked, worker=self.worker, on_entry=self._on_picked_entry)

    def _on_picked_entry(self, entry: Optional[AppEntry]) -> None:
        self._picked_entry = entry
        if entry is not None and entry.binary:
            self._picked_entries[entry.binary] = entry

    def _on_picked(self, path: str) -> None:
        self.app_var.set(path)
        self._refresh_app_tile()
        self._set_status("Application selected. Press Add to queue it.")

    def _on_report(self) -> None:
        from intermux.ui.report_dialog import ReportDialog

        ReportDialog(self.root, self.worker)

    def _on_close(self) -> None:
        self.worker.shutdown()
        self.root.destroy()


def main() -> int:
    """Start the GUI. Returns 0 on normal exit, 1 when tkinter or a display is unavailable."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        print("InterMux GUI must not run as root. The root helper is started through pkexec; "
              "run 'intermux' as your normal user.", file=sys.stderr)
        return 1
    if tk is None:
        print("InterMux GUI needs tkinter (install the python3-tk package).", file=sys.stderr)
        return 1
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"InterMux GUI could not open a display: {exc}", file=sys.stderr)
        return 1
    InterMuxApp(root)
    root.mainloop()
    return 0
