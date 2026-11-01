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
from intermux.system import commands, deps, netinfo, netns, procs, shaping, traffic
from intermux.system import session as session_mod
from intermux.ui import connections, dialogs, graph, icons, monitor, theme
from intermux.ui.tooltip import Tooltip, fit_text
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
PENDING_ICON = 26
TRAFFIC_HEIGHT = 230         # starting height of the Live traffic panel; drag the gap to resize
TILE_ICON = 36
DESIGN_SIZE = (1360, 900)
MIN_SIZE = (1100, 640)
SIDEBAR_WIDTH = 360
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
    text = f"via {item['iface']} · {where} · {dns_text}"
    limit = item.get("limit") or {}
    caps = [f"{arrow} {shaping.format_mbit(limit[key])}" for key, arrow in (("down", "↓"), ("up", "↑"))
            if limit.get(key)]
    if caps:
        text += " · limit " + " ".join(caps)
    return text


def limit_ops_for(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """set_limit batch ops for queued items that carry a speed limit, one per interface (last wins). Pure."""
    chosen: Dict[str, Dict[str, Any]] = {}
    for item in items:
        if item.get("limit"):
            chosen[item["iface"]] = {"op": "set_limit", "iface": item["iface"],
                                     "down": item["limit"].get("down"), "up": item["limit"].get("up")}
    return list(chosen.values())


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


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

def initial_geometry(screen_w: int, screen_h: int) -> str:
    """Window size that fits the screen with a margin, never above the design size. Pure."""
    width = max(min(DESIGN_SIZE[0], screen_w - 80), MIN_SIZE[0])
    height = max(min(DESIGN_SIZE[1], screen_h - 80), MIN_SIZE[1])
    return f"{width}x{height}"


def sash_colour(palette: Dict[str, str]) -> str:
    """The resize handles between panels: a shade between the background and the panel border. Pure."""
    return graph.mix(palette["bg"], palette["line_hi"], 0.35)


class InterMuxApp:
    """Main window: connections with the apps routed through them, a form to route more, and live traffic."""

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
        self._groups: Dict[str, connections.ConnectionGroup] = {}
        self._traffic_rows: Dict[str, monitor.TrafficRow] = {}
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
        from intermux.ui.appicon import set_window_icon
        set_window_icon(root)
        root.minsize(*MIN_SIZE)
        root.geometry(initial_geometry(root.winfo_screenwidth(), root.winfo_screenheight()))
        self.app_var = tk.StringVar(root, value="")
        self.iface_var = tk.StringVar(root, value="")
        self.separate_var = tk.BooleanVar(root, value=False)
        self.dns_var = tk.StringVar(root, value=dialogs.AUTO_LABEL)
        self.limit_down_var = tk.StringVar(root, value="")
        self.limit_up_var = tk.StringVar(root, value="")
        self.session_var = tk.StringVar(root, value="Detecting session")
        self.status_var = tk.StringVar(root, value="")
        self.actions = connections.Actions(
            run=self._row_action, release=self._release_key, menu=self._show_app_menu,
            add_app=self._on_route_here, dns=self._on_dns, limit=self._on_limit, group_menu=self._show_group_menu)

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
        header.grid(row=0, column=0, sticky="ew", padx=pad, pady=(14, 12))
        graph.logo(header, 24, p["bg"]).pack(side="left")
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
        ttk.Button(header, text="Report issue", style="Ghost.TButton", command=self._on_report).pack(
            side="right", padx=(0, 4))

        self.banner = ttk.Frame(root, padding=theme.SPACE_INNER, style="Banner.TFrame")

        # Resizable layout: connections | form, and connections over live traffic. Drag the gaps.
        sash = dict(background=sash_colour(p), sashwidth=8, sashrelief="flat", borderwidth=0,
                    opaqueresize=True, showhandle=False)
        self.hpane = tk.PanedWindow(root, orient="horizontal", sashcursor="sb_h_double_arrow", **sash)
        self.hpane.grid(row=2, column=0, sticky="nsew", padx=pad)
        self.vpane = tk.PanedWindow(self.hpane, orient="vertical", sashcursor="sb_v_double_arrow", **sash)
        side = tk.Frame(self.hpane, background=p["bg"])
        self.hpane.add(self.vpane, minsize=480, stretch="always")
        self.hpane.add(side, minsize=self._sidebar_min(), width=self._sidebar_width(), stretch="never")
        self._build_connections()
        self._build_traffic()
        side.columnconfigure(0, weight=1)
        side.rowconfigure(0, weight=1)
        self._build_bind_card(side)

        footer = tk.Frame(root, background=p["bg"])
        footer.grid(row=3, column=0, sticky="ew", padx=pad, pady=(10, 12))
        self.status_dot = graph.status_dot(footer, p["text_muted"], p["bg"])
        self.status_dot.pack(side="left")
        # Buttons are packed first so the status text can never push them out of view.
        self.reset_btn = ttk.Button(footer, text="Reset everything", style="Small.Danger.TButton",
                                    command=self._on_reset)
        self.reset_btn.pack(side="right")
        self.teardown_btn = ttk.Button(footer, text="Teardown idle", style="Small.Secondary.TButton",
                                       command=self._on_teardown)
        self.teardown_btn.pack(side="right", padx=(0, 8))
        self.status_label = tk.Label(footer, text="", font=theme.font("body"), anchor="w", width=1,
                                     background=p["bg"], foreground=p["text"])
        self.status_label.pack(side="left", padx=(8, 12), fill="x", expand=True)
        self.status_tip = Tooltip(self.status_label)
        self._set_status("")
        self._refresh_app_tile()
        self._update_buttons()
        root.after(80, self._place_sashes)

    def _place_sashes(self) -> None:
        """Give Live traffic about a third of the height, at most TRAFFIC_HEIGHT, once the window has a size."""
        try:
            total = self.vpane.winfo_height()
            if total < 50:
                self.root.after(80, self._place_sashes)
                return
            traffic_height = min(TRAFFIC_HEIGHT, max(110, total // 3))
            self.vpane.sash_place(0, 0, total - traffic_height)
        except tk.TclError:
            pass

    def _sidebar_width(self) -> int:
        return max(SIDEBAR_WIDTH, theme.font("body").measure("Separate profile if already running") + 110)

    def _sidebar_min(self) -> int:
        return max(300, theme.font("body").measure("Separate profile if already running") + 70)

    def _build_connections(self) -> None:
        p = self.palette
        panel, header, body = _panel(self.vpane, "Connections")
        self.vpane.add(panel, minsize=220, stretch="always")
        ttk.Button(header, text="+ Add VPN", style="Small.Secondary.TButton", command=self._on_vpn).pack(
            side="right")
        self.apps_count = tk.Label(header, text="", background=p["surface"], foreground=p["text_muted"],
                                   font=theme.font("mono"))
        self.apps_count.pack(side="right", padx=(0, 12))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.scroll = theme.ScrollFrame(body, p["surface"], height=60)
        self.scroll.grid(row=0, column=0, sticky="nsew", padx=(0, 2))
        self.scroll.attach_wheel(self.root)
        self.groups_host = self.scroll.inner
        self.groups_host.columnconfigure(0, weight=1)
        self.rows_host = self.groups_host          # kept for code that scrolls rows into view
        self.other_label = tk.Label(self.groups_host, text="", background=p["surface"],
                                    foreground=p["text_muted"], font=theme.font("small"), anchor="w",
                                    justify="left")
        self.empty_label = tk.Label(body, text="No network connection is up. Connect to a network and press "
                                               "Refresh.", background=p["surface"], foreground=p["text_muted"],
                                    font=theme.font("body"))

    def _build_traffic(self) -> None:
        p = self.palette
        panel, header, body = _panel(self.vpane, "Live traffic")
        self.vpane.add(panel, minsize=110, height=TRAFFIC_HEIGHT, stretch="never")
        legend = tk.Frame(header, background=p["surface"])
        legend.pack(side="right")
        for text, colour in (("routed", p["success"]), ("not routed", p["accent_fg"]), ("upload", p["violet"])):
            graph.status_dot(legend, colour, p["surface"], size=7).pack(side="left", padx=(12, 5))
            tk.Label(legend, text=text, background=p["surface"], foreground=p["text_muted"],
                     font=theme.font("eyebrow")).pack(side="left")
        self.traffic_summary = tk.Label(header, text="", background=p["surface"], foreground=p["text"],
                                        font=theme.font("small"), anchor="w", width=1)
        self.traffic_summary.pack(side="left", padx=(14, 8), fill="x", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.traffic_scroll = theme.ScrollFrame(body, p["surface"], height=40)
        self.traffic_scroll.grid(row=0, column=0, sticky="nsew", padx=(0, 2))
        self.traffic_scroll.attach_wheel(self.root)
        self.traffic_host = self.traffic_scroll.inner
        self.traffic_host.columnconfigure(0, weight=1)

    def _build_bind_card(self, parent: Any) -> None:
        p = self.palette
        bg = p["surface"]
        panel, _header, outer = _panel(parent, "Route an app")
        panel.grid(row=0, column=0, sticky="nsew")
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)
        # The form and the queue scroll together, so a short window never hides queued apps;
        # the Assign row stays pinned below them.
        self.side_scroll = theme.ScrollFrame(outer, bg, height=60, width=200)
        self.side_scroll.grid(row=0, column=0, sticky="nsew", padx=(0, 2))
        self.side_scroll.attach_wheel(self.root)
        body = self.side_scroll.inner
        body.columnconfigure(0, weight=1)
        self._side_outer = outer
        inner = tk.Frame(body, background=bg)
        inner.grid(row=0, column=0, sticky="ew", padx=(16, 12), pady=(12, 12))
        inner.columnconfigure(0, weight=1)

        def label(text: str, row: int, top: int = 0, parent: Any = inner) -> None:
            tk.Label(parent, text=text, background=bg, foreground=p["text_muted"], font=theme.font("small"),
                     anchor="w").grid(row=row, column=0, columnspan=2, sticky="w", pady=(top, 5))

        label("1. Connection", 0)
        self.iface_combo = ttk.Combobox(inner, textvariable=self.iface_var, state="readonly", values=[],
                                        font=theme.font("body"), height=10)
        self.iface_combo.grid(row=1, column=0, sticky="ew")
        self.iface_combo.bind("<<ComboboxSelected>>", lambda _e: self._update_buttons())

        label("2. Application", 2, top=14)
        tile_bg = p["bg"]
        self.app_tile = tk.Frame(inner, background=tile_bg, highlightthickness=1,
                                 highlightbackground=p["line_hi"], highlightcolor=p["focus"], cursor="hand2")
        self.app_tile.grid(row=3, column=0, sticky="ew")
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
        self.app_path_tip = Tooltip(self.app_path_label)
        self.pick_btn = ttk.Button(self.app_tile, text="Choose", style="Small.Secondary.TButton",
                                   command=self._on_pick)
        self.pick_btn.grid(row=0, column=2, rowspan=2, padx=10)
        for widget in (self.app_tile, self.app_icon_host, self.app_name_label, self.app_path_label):
            widget.bind("<Button-1>", lambda _e: self._on_pick())

        label("or type a command, or pick a recent one", 4, top=10)
        self.path_entry = ttk.Combobox(inner, textvariable=self.app_var, values=self._history_values(),
                                       font=theme.font("body"), height=12)
        self.path_entry.grid(row=5, column=0, sticky="ew")
        self.path_entry.bind("<<ComboboxSelected>>", lambda _e: self._on_history_pick())

        # Options are folded away by default; the toggle names what is inside, so they are easy to find.
        self.options_open = False
        self.options_btn = ttk.Button(inner, text="", style="Small.CardGhost.TButton", command=self._toggle_options)
        self.options_btn.grid(row=6, column=0, sticky="w", pady=(10, 0))
        self.options = tk.Frame(inner, background=bg)
        self.options.columnconfigure(0, weight=1, uniform="limit")
        self.options.columnconfigure(1, weight=1, uniform="limit")
        label("DNS", 0, parent=self.options)
        self.dns_combo = ttk.Combobox(self.options, textvariable=self.dns_var, values=dialogs.DNS_CHOICES,
                                      font=theme.font("body"))
        self.dns_combo.grid(row=1, column=0, columnspan=2, sticky="ew")
        label("Speed limit for this connection, Mbit/s (empty: none)", 2, top=10, parent=self.options)
        down = tk.Frame(self.options, background=bg)
        down.grid(row=3, column=0, sticky="ew", padx=(0, 4))
        up = tk.Frame(self.options, background=bg)
        up.grid(row=3, column=1, sticky="ew", padx=(4, 0))
        for frame, arrow, var in ((down, "↓ down", self.limit_down_var), (up, "↑ up", self.limit_up_var)):
            frame.columnconfigure(1, weight=1)
            tk.Label(frame, text=arrow, background=bg, foreground=p["text_muted"], font=theme.font("small")).grid(
                row=0, column=0, padx=(0, 6))
            ttk.Entry(frame, textvariable=var, width=6).grid(row=0, column=1, sticky="ew")
        ttk.Checkbutton(self.options, text="Separate profile if already running", variable=self.separate_var,
                        style="Card.TCheckbutton").grid(row=4, column=0, columnspan=2, sticky="w", pady=(12, 0))
        self._sync_options()

        self.add_btn = ttk.Button(inner, text="Add to queue", style="Secondary.TButton", command=self._on_add)
        self.add_btn.grid(row=8, column=0, sticky="ew", pady=(14, 0))
        self._build_queue(body)

    def _toggle_options(self) -> None:
        self.options_open = not self.options_open
        self._sync_options()

    def _sync_options(self) -> None:
        if self.options_open:
            self.options.grid(row=7, column=0, sticky="ew", pady=(8, 0))
            self.options_btn.configure(text="▾ Fewer options")
        else:
            self.options.grid_remove()
            extras = []
            if self.dns_var.get().strip() not in ("", dialogs.AUTO_LABEL):
                extras.append("DNS set")
            if self.limit_down_var.get().strip() or self.limit_up_var.get().strip():
                extras.append("limit set")
            if self.separate_var.get():
                extras.append("separate profile")
            suffix = f"  ({', '.join(extras)})" if extras else ""
            self.options_btn.configure(text=f"▸ DNS, speed limit, profile{suffix}")

    def _build_queue(self, body: Any) -> None:
        """The queue lives in the same panel, under the form."""
        p = self.palette
        _rule(body).grid(row=1, column=0, sticky="ew")
        head = tk.Frame(body, background=p["surface"])
        head.grid(row=2, column=0, sticky="ew", padx=16, pady=(12, 0))
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
        self.pending_hint = tk.Label(self.pending_empty, text="Queued apps start together with one password prompt.",
                                     background=p["surface"], foreground=p["text_muted"], font=theme.font("small"),
                                     justify="left", anchor="w", wraplength=260)
        self.pending_hint.grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.pending_empty.bind("<Configure>", lambda e: self.pending_hint.configure(wraplength=max(e.width, 120)))
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
        for group in self._groups.values():
            for key, row in group.rows.items():
                data = self._card_data(key) or {}
                states = row_button_states(bool(data.get("running")), idle)
                states["idle"] = idle
                row.set_enabled(states)
            group.set_namespace(self._namespaces.get(group.name), idle)
        self.queue_count.configure(text=str(len(self._pending)) if self._pending else "")
        self.add_btn.configure(style="Primary.TButton" if (has_iface and has_app and not self._pending)
                               else "Secondary.TButton")


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
        for group in self._groups.values():
            for key, row in group.rows.items():
                data = self._card_data(key)
                if data is not None:
                    row.set_icon(self._icon_widget(row.icon_host, data["app"], data["app_name"],
                                                   connections.APP_ICON, self.palette["surface_alt"]))
        self._render_pending()

    # -- status and errors ----------------------------------------------

    def _set_status(self, text: str, kind: Optional[str] = None) -> None:
        p = self.palette
        if not text:
            text, kind = "Ready", "idle"
        self.status_var.set(text)
        fit_text(self.status_label, text, theme.font("body"))
        self.status_tip.text = text
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
        self._render_connections()
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

    # -- connections view ---------------------------------------------------

    def _bound_ifaces(self) -> List[str]:
        names = [row["iface"] for row in self._rows.values()] + [a["iface"] for a in self._adoptions]
        return list(dict.fromkeys(names))

    def _keys_for(self, iface: str) -> List[str]:
        keys = [key for key, row in self._rows.items() if row["iface"] == iface]
        keys += [ADOPT_PREFIX + a["id"] for a in self._adoptions if a["iface"] == iface]
        return keys

    def _render_connections(self) -> None:
        """One group per connection that can carry apps (or already does); the rest on one muted line."""
        visible = monitor.visible_lanes(self._interfaces, self._bound_ifaces())
        bound = set(self._bound_ifaces())
        groups = [i for i in visible if i.bindable or i.name in bound]
        others = [i for i in visible if not (i.bindable or i.name in bound)]
        names = [i.name for i in groups]
        for name in list(self._groups):
            if name not in names:
                self._groups.pop(name).destroy()
        now = time.monotonic()
        for index, iface in enumerate(groups):
            group = self._groups.get(iface.name)
            if group is None:
                group = connections.ConnectionGroup(self.groups_host, iface, self.actions)
                self._groups[iface.name] = group
            else:
                group.set_interface(iface)
            group.frame.grid(row=index * 2, column=0, sticky="ew")
            group.separator.grid(row=index * 2 + 1, column=0, sticky="ew")
            keys = self._keys_for(iface.name)
            group.set_apps(keys, lambda parent, key: self._make_row(parent, key))
            for key in keys:
                data = self._card_data(key)
                if data is None:
                    continue
                row = group.rows[key]
                public_ip = "" if row.adopted else fresh_public_ip(self._public_ips.get(data["iface"]), now)
                row._public_ip = public_ip  # type: ignore[attr-defined]
                row.update(data, self.colours.colour(data["app"]), self._row_meta(key, data, public_ip))
        if others:
            text = "Can't route apps through: " + ",  ".join(
                f"{monitor.lane_title(i)} {i.name} ({'no gateway' if i.up else 'disconnected'})" for i in others)
            self.other_label.configure(text=text, wraplength=max(self.groups_host.winfo_width() - 40, 300))
            self.other_label.grid(row=len(groups) * 2, column=0, sticky="w", padx=16, pady=(12, 14))
        else:
            self.other_label.grid_remove()
        if visible:
            self.empty_label.place_forget()
        else:
            self.empty_label.place(relx=0.5, rely=0.5, anchor="center")
        running = sum(1 for r in self._rows.values() if r["running"]) + sum(
            1 for a in self._adoptions if a.get("alive", True))
        total = len(self._rows) + len(self._adoptions)
        self.apps_count.configure(text=f"{running} of {total} apps running" if total else "")
        self._render_traffic()

    def _make_row(self, parent: Any, key: str) -> connections.AppRow:
        row = connections.AppRow(parent, key, key.startswith(ADOPT_PREFIX), self.actions)
        data = self._card_data(key)
        if data is not None:
            row.set_icon(self._icon_widget(row.icon_host, data["app"], data["app_name"], connections.APP_ICON,
                                           self.palette["surface_alt"]))
        return row

    def _row_meta(self, key: str, data: Dict[str, Any], public_ip: str) -> str:
        adopted = key.startswith(ADOPT_PREFIX)
        name = ns_key(data["iface"])
        rate = None if adopted or name not in self.tracker.names() else self.tracker.rate(name)
        return connections.app_meta(data, rate, public_ip, adopted)

    def _on_route_here(self, iface: str) -> None:
        """"+ Add app" on a connection: choose that connection in the form, then pick an app."""
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

    def _on_vpn(self) -> None:
        from intermux.ui.vpn import VpnDialog

        VpnDialog(self.root, self.worker, on_change=self.refresh_all)

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

    def _lane_tone(self, iface: Any, routed: int) -> Tuple[str, str]:
        down, up = self.tracker.rate(iface.name)
        return monitor.lane_state(bool(iface.up), bool(iface.bindable), routed, down, up,
                                  self._link_speeds.get(iface.name))

    def _render_traffic(self) -> None:
        routed_counts: Dict[str, int] = {}
        for row in self._rows.values():
            if row["running"]:
                routed_counts[row["iface"]] = routed_counts.get(row["iface"], 0) + 1
        for data in self._adoptions:
            if data.get("alive", True):
                routed_counts[data["iface"]] = routed_counts.get(data["iface"], 0) + 1
        visible = monitor.visible_lanes(self._interfaces, self._bound_ifaces())
        by_name = {i.name: i for i in visible}

        for name, group in self._groups.items():
            iface = by_name.get(name, group.iface)
            tone, words = self._lane_tone(iface, routed_counts.get(name, 0))
            group.set_state(tone, words, self.tracker.rate(name))
            for key, row in group.rows.items():
                data = self._card_data(key)
                if data is not None:
                    row.update(data, self.colours.colour(data["app"]),
                               self._row_meta(key, data, getattr(row, "_public_ip", "")))

        names = [i.name for i in visible]
        for name in list(self._traffic_rows):
            if name not in names:
                self._traffic_rows.pop(name).destroy()
        for index, iface in enumerate(visible):
            row = self._traffic_rows.get(iface.name)
            if row is None:
                row = monitor.TrafficRow(self.traffic_host, iface)
                self._traffic_rows[iface.name] = row
            else:
                row.set_interface(iface)
            row.frame.grid(row=index * 2, column=0, sticky="ew")
            if index < len(visible) - 1:
                row.separator.grid(row=index * 2 + 1, column=0, sticky="ew")
            else:
                row.separator.grid_remove()
            tone, _words = self._lane_tone(iface, routed_counts.get(iface.name, 0))
            row.set_traffic(self.tracker.history(iface.name), tone, self._link_speeds.get(iface.name))

        physical = [i.name for i in visible if graph.kind_for(i) != "vpn"]
        down = sum(self.tracker.rate(n)[0] for n in physical)
        up = sum(self.tracker.rate(n)[1] for n in physical)
        routed = sum(sum(self.tracker.rate(n)) for n in self.tracker.names() if n.startswith(NS_PREFIX))
        share = routed_share(routed, down + up)
        fit_text(self.traffic_summary, f"↓ {traffic.format_rate(down)}   ↑ {traffic.format_rate(up)}"
                 + (f"   ·   {share} through InterMux" if share else ""), theme.font("small"))

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

    def _row_action(self, key: str, action: str) -> None:
        self._selected_iid = key
        handler = {"restart": self._on_restart, "stop": self._on_stop, "verify": self._on_verify,
                   "log": self._on_open_log, "force": self._on_force_quit}[action]
        handler()

    def _release_key(self, key: str) -> None:
        data = self._card_data(key)
        if data is not None:
            self._on_release_adoption(data)

    def _menu(self) -> Any:
        p = self.palette
        return tk.Menu(self.root, tearoff=False, background=p["surface_alt"], foreground=p["text"],
                       activebackground=p["accent"], activeforeground=p["accent_text"],
                       disabledforeground=p["line_hi"], borderwidth=1, relief="flat")

    def _show_app_menu(self, key: str, event: Any) -> None:
        """••• on an app row, or a right-click: every action for that app."""
        self._selected_iid = key
        data = self._card_data(key)
        if data is None:
            return
        menu = self._menu()
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
            move = self._menu()
            targets = [(label, name) for label, name in self._iface_map.items() if name != data["iface"]]
            for label, name in targets:
                move.add_command(label=label, command=lambda n=name: self._on_move(n))
            menu.add_cascade(label="Move to another connection", menu=move,
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
        self._selected_iid = iid if self._card_data(iid) is not None else None
        for group in self._groups.values():
            row = group.rows.get(iid)
            if row is not None:
                row.flash()
                self._scroll_into_view(group.frame)

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
            self.options_open = True
            self._sync_options()
            return
        try:
            down = dialogs.parse_limit(self.limit_down_var.get())
            up = dialogs.parse_limit(self.limit_up_var.get())
        except ValueError as exc:
            self._set_status(f"Could not use the speed limit: {exc}", "error")
            self.options_open = True
            self._sync_options()
            return
        item = {"iface": iface, "app": app_path, "name": name,
                "policy": "separate" if self.separate_var.get() else "ask",
                # "Automatic" keeps what an existing namespace uses; a new one picks servers automatically.
                "dns": dnsconf.KEEP if dns_value == dnsconf.AUTO else dns_value,
                "dns_text": "" if dns_value == dnsconf.AUTO else self.dns_var.get().strip(),
                "limit": {"down": down, "up": up} if (down or up) else None}
        if queue_add(self._pending, item):
            self._set_status(f"Added {name} via {iface} to pending.")
        else:
            self._set_status(f"{name} via {iface} is already pending.")
        self.app_var.set("")
        self._picked_entry = None
        self._render_pending()
        self._sync_options()
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
        limit_ops = limit_ops_for([item for item, _ in plan])
        self._run_mutation(
            f"Assigning {len(plan)} app(s) (one password prompt)",
            _batch_task,
            [op for _, op in plan] + limit_ops,
            on_done=partial(self._on_batch_done, plan, limit_ops),
        )

    def _on_batch_done(self, plan: List[Tuple[Dict[str, Any], Dict[str, Any]]], limit_ops: List[Dict[str, Any]],
                       results: Any, error: Optional[BaseException]) -> None:
        self._assigning = False
        if error is not None:
            self._render_pending()
            self._fail("Assign failed", error)
            return
        done: List[str] = []
        failed: List[str] = []
        results = list(results or [])
        for op, result in zip(limit_ops, results[len(plan):]):
            if not (isinstance(result, dict) and result.get("ok")):
                err = result.get("error") if isinstance(result, dict) else None
                err = err if isinstance(err, dict) else {}
                failed.append(f"speed limit on {op['iface']} ({err.get('message') or 'failed'})")
        for (item, _op), result in zip(plan, results[:len(plan)]):
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

    def _show_group_menu(self, iface: str, event: Any) -> None:
        """••• on a connection: speed limit, DNS, re-add routes, remove its namespace."""
        idle = self._busy == 0
        state_ = "normal" if idle else "disabled"
        menu = self._menu()
        menu.add_command(label="Speed limit…", state=state_, command=lambda: self._on_limit(iface))
        menu.add_command(label="DNS servers…", state=state_, command=lambda: self._on_dns(iface))
        menu.add_command(label="Re-add routes", state=state_, command=self._on_reconnect)
        menu.add_separator()
        menu.add_command(label="Stop all apps and remove…", state=state_,
                         command=lambda: self._on_remove_namespace(iface))
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
