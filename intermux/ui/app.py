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
from intermux.ui import connections, dialogs, graph, icons, monitor, theme  # noqa: F401 (graph: sash_colour)
from intermux.ui.tooltip import Tooltip, fit_text
from intermux.ui.history import History, Item as HistoryItem
from intermux.ui.worker import Worker

try:
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import messagebox, ttk
except ImportError:  # reported by main()
    tk = None  # type: ignore[assignment]
    tkfont = None  # type: ignore[assignment]
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
            "pid": binding.pid if binding.pid in live_pids else None,
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
            "pids": members or [pid for pid in adoption.pids if pid in live_pids],
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
    """Enable or disable a ttk widget; leaves it alone when it is already right, so it is not redrawn."""
    if widget.instate(["disabled"]) == enabled:
        widget.state(["!disabled"] if enabled else ["disabled"])


KIND_LABELS = {
    "wifi": "Wi-Fi", "ethernet": "Ethernet", "usb": "USB", "bluetooth": "Bluetooth",
    "mobile": "Mobile", "vpn": "VPN", "virtual": "Virtual", "other": "Other",
}
NO_IFACE_TEXT = "No bindable interface found. Connect to a network and press Refresh."
TABLE_ICON = 16
SASH_WIDTH = 12              # the gaps between sections; drag them to resize
GROUP_PAD = 16               # inside each section's outline
FIELD_GAP = 14               # between fields in the form
FORM_MIN_WIDTH = 380
QUEUE_MIN_WIDTH = 360
TOP_MIN_HEIGHT = 200
BINDINGS_MIN_HEIGHT = 220
SPARK_SAMPLES = 60           # one minute of samples in each inline graph
SPARK_FLOOR = 32 * 1024      # inline graphs never zoom in below 32 KB/s, so idle noise stays flat
DESIGN_SIZE = (1360, 900)
MIN_SIZE = (1100, 640)


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
# Table rows (pure)
# ---------------------------------------------------------------------------

QUEUE_EMPTY = "Nothing queued. Pick an interface and an application above, then press Add to queue."
BINDINGS_EMPTY = "No apps are routed yet. Queued apps appear here after you press Start all."
APP_HINT_EMPTY = "Type a command such as firefox or a full path, pick a recent one, or press Choose."
POLICY_LABELS = {"ask": "ask me", "separate": "separate profile"}
WARN_TONES = ("off", "hot")


def pid_text(pids: Sequence[Any]) -> str:
    """"1234" for one process, "1234 +3" for four, "" for none. Pure."""
    live = sorted(p for p in pids if p)
    if not live:
        return ""
    return str(live[0]) if len(live) == 1 else f"{live[0]} +{len(live) - 1}"


def dns_text(servers: Sequence[str]) -> str:
    """DNS column of an interface: "auto" or the servers ("Cloudflare" for a preset). Pure."""
    return dnsconf.describe(list(servers)) if servers else "auto"


def limit_cell(limit: Optional[Dict[str, Optional[float]]]) -> str:
    """Limit column: "-" when there is none. Pure."""
    text = connections.limit_text(limit)
    return "-" if text == "none" else text


def queue_rows(items: Sequence[Dict[str, Any]], existing: Sequence[str]) -> List[Dict[str, Any]]:
    """Rows of the Queue table, in queue order. Pure.

    Columns: interface, DNS ("keep" when the interface's namespace exists and keeps its servers), speed limit,
    and what to do if the app is already running.
    """
    rows = []
    for index, item in enumerate(items):
        dns = item.get("dns", dnsconf.KEEP)
        if isinstance(dns, list):
            dns_cell = dnsconf.describe(dns)
        else:
            dns_cell = "keep" if item["iface"] in existing else "auto"
        rows.append({
            "iid": f"q{index}", "parent": "", "text": item["name"], "tags": (), "app": item["app"],
            "values": (item["iface"], dns_cell, limit_cell(item.get("limit")),
                       POLICY_LABELS.get(item["policy"], item["policy"])),
        })
    return rows


def active_rows(bindings: Sequence[Dict[str, Any]], adoptions: Sequence[Dict[str, Any]],
                interfaces: Dict[str, Any], namespaces: Dict[str, Dict[str, Any]],
                states: Dict[str, Tuple[str, str]], rates: Dict[str, Tuple[float, float]],
                public_ips: Dict[str, str]) -> List[Dict[str, Any]]:
    # rates: keyed by interface name (whole interface) and by ns_key(interface) (apps InterMux started there)
    """Rows of the Bindings table: one row per interface, with the apps routed through it under it. Pure.

    Columns: status, PID, download, upload, inline graph (drawn separately), limit, DNS, public IP.
    An interface row shows the whole interface's speed; an app row shows the speed of the apps InterMux
    started on that interface (one namespace, so apps sharing an interface share the figure). Shifted apps
    have no counter of their own and show none. Limit and DNS belong to the interface's namespace, so they
    sit on the interface row. Each row's "spark" key names the series its inline graph draws.
    """
    groups: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for row in bindings:
        groups.setdefault(row["iface"], {"bind": [], "adopt": []})["bind"].append(row)
    for row in adoptions:
        groups.setdefault(row["iface"], {"bind": [], "adopt": []})["adopt"].append(row)
    rows: List[Dict[str, Any]] = []
    for iface in sorted(groups):
        info = interfaces.get(iface)
        tone, words = states.get(iface, ("off", "Not found"))
        settings = namespaces.get(iface) or {}
        bound = groups[iface]["bind"]
        parent = f"if:{iface}"
        rate = rates.get(iface)
        ns_rate = rates.get(ns_key(iface))
        rows.append({
            "iid": parent, "parent": "", "kind": "iface", "iface": iface,
            "text": interface_label(info) if info is not None else iface,
            "values": (words.lower(), "",
                       traffic.format_rate(rate[0]) if rate else "", traffic.format_rate(rate[1]) if rate else "", "",
                       limit_cell(settings.get("limit")) if settings else "",
                       dns_text(settings.get("dns") or []) if settings else "",
                       public_ips.get(iface, "")),
            "tags": ("iface", "warn") if tone in WARN_TONES else ("iface",),
            "spark": iface, "tone": tone,
            "running": any(b["running"] for b in bound),
            "namespace": bool(settings) or bool(bound),
        })
        for b in bound:
            running = bool(b["running"])
            pids = [b["pid"]] if b.get("pid") else b.get("pids", [])
            rows.append({
                "iid": "b:" + _iid(iface, b["app"]), "parent": parent, "kind": "bind", "iface": iface,
                "text": b["app_name"], "app": b["app"],
                "values": ("running" if running else "stopped", pid_text(pids) if running else "",
                           traffic.format_rate(ns_rate[0]) if running and ns_rate else "",
                           traffic.format_rate(ns_rate[1]) if running and ns_rate else "", "", "", "", ""),
                "tags": () if running else ("muted",),
                "spark": ns_key(iface) if running and ns_rate else None, "tone": "ok",
                "running": running, "namespace": True, "data": b,
            })
        for a in groups[iface]["adopt"]:
            alive = bool(a.get("alive", True))
            rows.append({
                "iid": "a:" + str(a["id"]), "parent": parent, "kind": "adopt", "iface": iface,
                "text": a["app_name"], "app": a["app"],
                "values": ("running, shifted" if alive else "exited", pid_text(a.get("pids", [])),
                           "", "", "", "", "", ""),
                "tags": () if alive else ("muted",), "spark": None,
                "running": alive, "namespace": bool(settings) or bool(bound), "data": a,
            })
    return rows


APP_ACTIONS = ("verify", "restart", "stop", "log", "move", "force", "release")
IFACE_ACTIONS = ("dns", "limit", "remove")


def selection_actions(row: Optional[Dict[str, Any]], idle: bool, can_move: bool = True) -> Dict[str, bool]:
    """Which Bindings buttons are enabled for the selected table row. Pure.

    App buttons act on the selected app. Interface buttons (DNS, limit, remove) act on the interface of
    whatever row is selected, and need its namespace to exist.
    """
    states = {key: False for key in APP_ACTIONS + IFACE_ACTIONS}
    if not row:
        return states
    kind = row.get("kind")
    running = bool(row.get("running"))
    if kind == "bind":
        states.update(row_button_states(running, idle))
        states["move"] = idle and can_move
    elif kind == "iface":
        states["verify"] = idle and running
    elif kind == "adopt":
        states["release"] = idle
    if row.get("namespace"):
        for key in IFACE_ACTIONS:
            states[key] = idle
    return states


def visible_actions(row: Optional[Dict[str, Any]]) -> Tuple[str, ...]:
    """The buttons shown under the Bindings table for the selected row, in order. Pure."""
    kind = row.get("kind") if row else None
    if kind == "bind":
        return ("verify", "restart", "stop", "log", "move", "force")
    if kind == "adopt":
        return ("release",)
    if kind == "iface":
        return ("verify",) + IFACE_ACTIONS if row.get("namespace") else ("verify",)
    return ()


def selection_hint(row: Optional[Dict[str, Any]]) -> str:
    """One line under the Bindings table explaining what the buttons do for the selection. Pure."""
    kind = row.get("kind") if row else None
    if kind == "iface":
        if row.get("running"):
            return ("Check public IP asks a public service which address apps on this interface appear from. "
                    "DNS, Speed limit and Remove namespace apply to every app on it.")
        return "DNS, Speed limit and Remove namespace apply to every app on this interface."
    if kind == "bind":
        if row.get("running"):
            return ("Stop closes this app; Force quit closes every copy of it on this computer. "
                    "Select the interface row above it to change DNS or the speed limit.")
        return "This app has exited. Restart starts it again here; Remove stopped clears the row."
    if kind == "adopt":
        return "Release puts this app back on the default route without closing it."
    return "Select an app to stop, restart or move it, or an interface row to change its DNS or speed limit."


def unusable_text(interfaces: Sequence[Any], bound: Sequence[str]) -> str:
    """Hint under the Interface field naming connections that are up but cannot carry apps. Pure."""
    others = [i for i in monitor.visible_lanes(interfaces, bound) if not (i.bindable or i.name in bound)]
    if not others:
        return ""
    return "Not usable for routing: " + ", ".join(
        f"{i.name} ({'no gateway' if i.up else 'disconnected'})" for i in others)


def _set_var(var: Any, value: str) -> None:
    """Set a StringVar only when the value differs, so its label is not redrawn for nothing."""
    if var.get() != value:
        var.set(value)


def _sync_tree(tree: Any, rows: Sequence[Dict[str, Any]]) -> None:
    """Make `tree` show exactly `rows`, updating items in place so selection and scroll survive refreshes."""
    wanted = {row["iid"] for row in rows}
    for top in tree.get_children(""):
        for child in tree.get_children(top):
            if child not in wanted:
                tree.delete(child)
        if top not in wanted:
            tree.delete(top)
    positions: Dict[str, int] = {}
    for row in rows:
        parent = row.get("parent", "")
        index = positions.get(parent, 0)
        positions[parent] = index + 1
        image = row.get("image")
        options = {"text": (" " + row["text"]) if image else row["text"], "values": row["values"],
                   "tags": row.get("tags", ()), "image": image or ""}
        iid = row["iid"]
        # Remember what each item shows and only write what changed; rewriting unchanged items every second
        # makes the table repaint.
        shown = tree.__dict__.setdefault("_shown", {})
        if tree.exists(iid):
            if tree.parent(iid) != parent or tree.index(iid) != index:
                tree.move(iid, parent, index)
            if shown.get(iid) != options:
                tree.item(iid, **options)
        else:
            tree.insert(parent, index, iid=iid, open=True, **options)
        shown[iid] = options


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

def initial_geometry(screen_w: int, screen_h: int, wanted_h: int = 0) -> str:
    """Window size that fits the screen with a margin. Pure.

    The height is what the layout asks for (wanted_h), so both tables get their full number of rows,
    or the design height when that is not known; never more than the screen allows.
    """
    width = max(min(DESIGN_SIZE[0], screen_w - 80), MIN_SIZE[0])
    height = max(min(wanted_h or DESIGN_SIZE[1], screen_h - 80), MIN_SIZE[1])
    return f"{width}x{height}"


def sash_colour(palette: Dict[str, str]) -> str:
    """The resize handles between panels: a shade between the background and the panel border. Pure."""
    return graph.mix(palette["bg"], palette["line_hi"], 0.35)



class InterMuxApp:
    """Main window: a form to queue apps on an interface, a Queue table and a Bindings table."""

    def __init__(self, root: Any) -> None:
        self.root = root
        self.palette = theme.apply_theme(root)
        self.worker = Worker(root)
        self._busy = 0
        self._snapshot_inflight = False
        self._iface_map: Dict[str, str] = {}
        self._interfaces: List[Any] = []
        self._link_speeds: Dict[str, Optional[float]] = {}
        self._bindings: List[Dict[str, Any]] = []
        self._adoptions: List[Dict[str, Any]] = []
        self._namespaces: Dict[str, Dict[str, Any]] = {}
        self._rows: Dict[str, Dict[str, Any]] = {}  # Bindings table iid -> row from active_rows
        self._public_ips: Dict[str, Tuple[str, float]] = {}
        self._pending: List[Dict[str, Any]] = []
        self._picked_entries: Dict[str, AppEntry] = {}
        self._assigning = False
        self._picked_entry: Optional[AppEntry] = None
        self._desktop_info: Dict[str, AppEntry] = {}
        self._row_icons: Dict[str, Any] = {}
        self.tracker = traffic.RateTracker()
        self._sparks: Dict[str, Any] = {}  # Bindings iid -> Canvas drawn over its Traffic cell
        self.history = History()

        root.title(f"InterMux {config.VERSION}")
        from intermux.ui.appicon import set_window_icon
        set_window_icon(root)
        root.minsize(*MIN_SIZE)
        self.app_var = tk.StringVar(root, value="")
        self.iface_var = tk.StringVar(root, value="")
        self.separate_var = tk.BooleanVar(root, value=False)
        self.dns_var = tk.StringVar(root, value=dialogs.AUTO_LABEL)
        self.limit_down_var = tk.StringVar(root, value="")
        self.limit_up_var = tk.StringVar(root, value="")
        self.session_var = tk.StringVar(root, value="Detecting session")
        self.status_var = tk.StringVar(root, value="")
        self.hint_var = tk.StringVar(root, value="")
        self.app_hint_var = tk.StringVar(root, value="")
        self.iface_hint_var = tk.StringVar(root, value="")

        self._build(root)
        root.update_idletasks()
        # A paned window does not ask for its panes' full height, so add them up: the window opens tall
        # enough for the whole form and every Bindings row, when the screen allows.
        wanted = (root.winfo_reqheight() - self.vpane.winfo_reqheight() + self._form_box.winfo_reqheight()
                  + self._bindings_box.winfo_reqheight() + SASH_WIDTH)
        root.geometry(initial_geometry(root.winfo_screenwidth(), root.winfo_screenheight(), wanted))
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

        header = ttk.Frame(root)
        header.grid(row=0, column=0, sticky="ew", padx=pad, pady=(18, 14))
        ttk.Label(header, text="InterMux", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text=config.VERSION, style="Muted.TLabel").pack(side="left", padx=(8, 0))
        ttk.Label(header, textvariable=self.session_var, style="Muted.TLabel").pack(
            side="left", padx=(theme.SPACE_CARD_GAP, 0))
        ttk.Button(header, text="Refresh", style="Small.Secondary.TButton", command=self.refresh_all).pack(
            side="right")
        ttk.Button(header, text="VPN connections…", style="Small.Secondary.TButton", command=self._on_vpn).pack(
            side="right", padx=(0, 8))
        ttk.Button(header, text="Report issue", style="Small.Ghost.TButton", command=self._on_report).pack(
            side="right", padx=(0, 8))

        self.banner = ttk.Frame(root, padding=theme.SPACE_INNER, style="Banner.TFrame")

        # Every section can be resized: drag the gap between New binding and Queue sideways, and the gap
        # above Bindings up or down.
        sash = dict(background=p["bg"], sashwidth=SASH_WIDTH, sashrelief="flat", borderwidth=0,
                    opaqueresize=True, showhandle=False)
        self.vpane = tk.PanedWindow(root, orient="vertical", sashcursor="sb_v_double_arrow", **sash)
        self.vpane.grid(row=2, column=0, sticky="nsew", padx=pad)
        self.hpane = tk.PanedWindow(self.vpane, orient="horizontal", sashcursor="sb_h_double_arrow", **sash)
        form_box = self._group(self.hpane, "New binding")
        queue_box = self._group(self.hpane, "Queue")
        bindings_box = self._group(self.vpane, "Bindings")
        self._form_box, self._bindings_box = form_box, bindings_box
        self._build_form(form_box)
        self._build_queue(queue_box)
        self._build_bindings(bindings_box)
        self.hpane.add(form_box, minsize=FORM_MIN_WIDTH, stretch="never")
        self.hpane.add(queue_box, minsize=QUEUE_MIN_WIDTH, stretch="always")
        self.vpane.add(self.hpane, minsize=TOP_MIN_HEIGHT, stretch="never")
        self.vpane.add(bindings_box, minsize=BINDINGS_MIN_HEIGHT, stretch="always")

        footer = ttk.Frame(root)
        footer.grid(row=3, column=0, sticky="ew", padx=pad, pady=(14, 16))
        self.status_dot = tk.Label(footer, text="●", font=theme.font("body"), background=p["bg"])
        self.status_dot.pack(side="left")
        # Buttons are packed first so the status text can never push them out of view.
        self.reset_btn = ttk.Button(footer, text="Reset everything", style="Small.Danger.TButton",
                                    command=self._on_reset)
        self.reset_btn.pack(side="right")
        self.teardown_btn = ttk.Button(footer, text="Remove stopped", style="Small.Secondary.TButton",
                                       command=self._on_teardown)
        self.teardown_btn.pack(side="right", padx=(0, 8))
        self.reconnect_btn = ttk.Button(footer, text="Re-add routes", style="Small.Secondary.TButton",
                                        command=self._on_reconnect)
        self.reconnect_btn.pack(side="right", padx=(0, 8))
        self.status_label = tk.Label(footer, text="", font=theme.font("body"), anchor="w", width=1,
                                     background=p["bg"], foreground=p["text"])
        self.status_label.pack(side="left", padx=(6, 12), fill="x", expand=True)
        self.status_tip = Tooltip(self.status_label)
        self._set_status("")
        self._on_app_changed()
        self._render_pending()
        self._on_select()

    def _place_sashes(self) -> None:
        """Start with the form at its natural size and the rest of the height for Bindings."""
        try:
            width = self.hpane.winfo_width()
            if width < 50:
                self.root.after(80, self._place_sashes)
                return
            form_width = max(FORM_MIN_WIDTH, min(self._form_box.winfo_reqwidth(), int(width * 0.5)))
            self.hpane.sash_place(0, form_width, 0)
            self.root.update_idletasks()  # the hint lines re-wrap to the new width
            self.vpane.sash_place(0, 0, self._form_box.winfo_reqheight())
        except tk.TclError:
            pass

    def _group(self, parent: Any, title: str) -> Any:
        box = ttk.Labelframe(parent, text=f" {title} ", padding=(GROUP_PAD, 8, GROUP_PAD, GROUP_PAD))
        box.columnconfigure(0, weight=1)
        return box

    def _build_form(self, box: Any) -> None:
        """Fields stacked with their labels above them, so the column can be narrow."""
        box.columnconfigure(0, weight=1)

        def label(text: str, row: int, column: int = 0, top: int = FIELD_GAP) -> Any:
            widget = ttk.Label(box, text=text, style="Muted.TLabel")
            widget.grid(row=row, column=column, columnspan=2 if column == 0 else 1, sticky="w", pady=(top, 4))
            return widget

        label("Interface", 0, top=4)
        self.iface_combo = ttk.Combobox(box, textvariable=self.iface_var, state="readonly", values=[],
                                        font=theme.font("body"), height=10)
        self.iface_combo.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.iface_combo.bind("<<ComboboxSelected>>", lambda _e: self._update_buttons())
        self.iface_hint = ttk.Label(box, textvariable=self.iface_hint_var, style="Small.TLabel")
        self.iface_hint.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.iface_hint.bind("<Configure>", lambda e: self.iface_hint.configure(wraplength=max(e.width, 200)))

        label("Application", 3)
        self.path_entry = ttk.Combobox(box, textvariable=self.app_var, values=self._history_values(),
                                       font=theme.font("body"), height=12)
        self.path_entry.grid(row=4, column=0, sticky="ew")
        self.path_entry.bind("<<ComboboxSelected>>", lambda _e: self._on_history_pick())
        self.pick_btn = ttk.Button(box, text="Choose…", style="Secondary.TButton", command=self._on_pick)
        self.pick_btn.grid(row=4, column=1, sticky="ew", padx=(8, 0))
        self.app_hint = ttk.Label(box, textvariable=self.app_hint_var, style="Small.TLabel")
        self.app_hint.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.app_hint.bind("<Configure>", lambda e: self.app_hint.configure(wraplength=max(e.width, 200)))

        options = ttk.Frame(box)
        options.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(FIELD_GAP, 0))
        options.columnconfigure(0, weight=1)
        ttk.Label(options, text="DNS", style="Muted.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.dns_combo = ttk.Combobox(options, textvariable=self.dns_var, values=dialogs.DNS_CHOICES,
                                      font=theme.font("body"), width=22)
        self.dns_combo.grid(row=1, column=0, sticky="ew")
        self.dns_hint = ttk.Label(options, text="", style="Small.TLabel")
        self.dns_hint.grid(row=2, column=0, columnspan=5, sticky="ew", pady=(6, 0))
        self.dns_hint.bind("<Configure>", lambda e: self.dns_hint.configure(wraplength=max(e.width, 200)))
        dialogs.bind_custom_choice(self.dns_combo, self.dns_var, self.dns_hint)
        limit_label = ttk.Label(options, text="Speed limit, Mbit/s", style="Muted.TLabel")
        limit_label.grid(row=0, column=1, columnspan=4, sticky="w", padx=(FIELD_GAP, 0), pady=(0, 4))
        Tooltip(limit_label, "Leave empty for no limit. DNS and the speed limit apply to every app "
                             "routed through the interface.")
        for index, (text, var) in enumerate((("down", self.limit_down_var), ("up", self.limit_up_var))):
            ttk.Label(options, text=text, style="Small.TLabel").grid(
                row=1, column=1 + index * 2, padx=(FIELD_GAP if index == 0 else 10, 6))
            ttk.Entry(options, textvariable=var, width=6).grid(row=1, column=2 + index * 2)

        ttk.Checkbutton(
            box, text="Open a separate profile if the app is already running",
            variable=self.separate_var, style="Dialog.TCheckbutton",
        ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(FIELD_GAP, 0))
        self.add_btn = ttk.Button(box, text="Add to queue", style="Primary.TButton", command=self._on_add)
        self.add_btn.grid(row=8, column=0, columnspan=2, sticky="e", pady=(FIELD_GAP, 0))
        self.root.after(80, self._place_sashes)

    def _table(self, parent: Any, first: Tuple[str, int], columns: Sequence[Tuple[str, str, int, bool]],
               height: int) -> Tuple[Any, Any]:
        """A Treeview with a scrollbar in row 0 of `parent`. Returns (tree, empty-state label)."""
        p = self.palette
        holder = ttk.Frame(parent)
        holder.grid(row=0, column=0, sticky="nsew")
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)
        tree = ttk.Treeview(holder, columns=[key for key, *_ in columns], show="tree headings",
                            selectmode="browse", height=height)
        tree.heading("#0", text=first[0], anchor="w")
        tree.column("#0", width=first[1], minwidth=first[1] - 20, stretch=True, anchor="w")
        for key, title, width, stretch in columns:
            tree.heading(key, text=title, anchor="w")
            tree.column(key, width=width, minwidth=50, stretch=stretch, anchor="w")
        scroll = ttk.Scrollbar(holder, orient="vertical", command=tree.yview)

        def on_scroll(first: str, last: str) -> None:
            scroll.set(first, last)
            if tree in getattr(self, "_scroll_hooks", {}):
                self._scroll_hooks[tree]()

        tree.configure(yscrollcommand=on_scroll)
        tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        cell = theme.font("cell")
        if not hasattr(self, "_bold_cell"):
            self._bold_cell = tkfont.Font(root=self.root, family=cell.cget("family"), size=cell.cget("size"),
                                          weight="bold")
        tree.tag_configure("iface", background=theme.table_group(p), font=self._bold_cell)
        tree.tag_configure("muted", foreground=p["text_muted"])
        tree.tag_configure("warn", foreground=p["warning"])
        empty = tk.Label(tree, text="", background=p["surface"], foreground=p["text_muted"],
                         font=theme.font("body"), wraplength=560, justify="center")
        return tree, empty

    @staticmethod
    def _show_empty(empty: Any, text: str) -> None:
        if text:
            empty.configure(text=text)
            empty.place(relx=0.5, rely=0.58, anchor="center")
        else:
            empty.place_forget()

    def _build_queue(self, box: Any) -> None:
        box.rowconfigure(0, weight=1)
        self.queue_tree, self.queue_empty = self._table(
            box, ("Application", 190),
            [("iface", "Interface", 100, False), ("dns", "DNS", 110, True), ("limit", "Speed limit", 120, False),
             ("policy", "If running", 175, False)],
            height=4)
        self.queue_tree.bind("<<TreeviewSelect>>", lambda _e: self._update_buttons())
        self.queue_tree.bind("<Button-3>", self._on_queue_menu)

        row = ttk.Frame(box)
        row.grid(row=1, column=0, sticky="ew", pady=(FIELD_GAP, 0))
        self.assign_btn = ttk.Button(row, text="Start all", style="Small.Primary.TButton", command=self._on_assign)
        self.assign_btn.pack(side="left")
        self.remove_btn = ttk.Button(row, text="Remove", style="Small.Secondary.TButton",
                                     command=self._on_remove_queued)
        self.remove_btn.pack(side="left", padx=(8, 0))
        self.clear_btn = ttk.Button(row, text="Clear", style="Small.Secondary.TButton", command=self._on_clear)
        self.clear_btn.pack(side="left", padx=(8, 0))
        ttk.Label(box, text="Start all asks for your password once for the whole queue.",
                  style="Small.TLabel").grid(row=2, column=0, sticky="w", pady=(8, 0))

    def _build_bindings(self, box: Any) -> None:
        box.rowconfigure(0, weight=1)
        self.tree, self.tree_empty = self._table(
            box, ("Interface / application", 270),
            [("status", "Status", 150, False), ("pid", "PID", 85, False),
             ("down", "Download", 105, False), ("up", "Upload", 90, False), ("spark", "Last minute", 130, False),
             ("limit", "Speed limit", 150, False), ("dns", "DNS", 110, True), ("ip", "Public IP", 125, False)],
            height=10)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._on_select())
        # The inline graphs are canvases laid over the "Last minute" cells; keep them on their rows.
        self._scroll_hooks = {self.tree: self._place_sparks}
        for sequence in ("<Configure>", "<<TreeviewOpen>>", "<<TreeviewClose>>", "<B1-Motion>",
                         "<ButtonRelease-1>"):
            self.tree.bind(sequence, lambda _e: self.root.after_idle(self._place_sparks), add="+")
        self.tree.bind("<Button-3>", self._on_bindings_menu)

        # One row of buttons that follows the selection: app actions for an app, settings for an interface.
        self.actions_row = ttk.Frame(box)
        self.actions_row.grid(row=1, column=0, sticky="ew", pady=(FIELD_GAP, 0))
        self.action_buttons: Dict[str, Any] = {}
        for key, text in (
            ("verify", "Check public IP"), ("restart", "Restart"), ("stop", "Stop"), ("log", "Open log"),
            ("move", "Move to…"), ("force", "Force quit"), ("release", "Release"),
            ("dns", "DNS servers…"), ("limit", "Speed limit…"), ("remove", "Remove namespace…"),
        ):
            self.action_buttons[key] = ttk.Button(self.actions_row, text=text, style="Small.Secondary.TButton",
                                                  command=partial(self._on_action, key))
        self._shown_actions: Tuple[str, ...] = ()
        self.hint_label = ttk.Label(box, textvariable=self.hint_var, style="Small.TLabel")
        self.hint_label.grid(row=2, column=0, sticky="w", pady=(8, 0))

    def _show_actions(self, keys: Tuple[str, ...]) -> None:
        """Pack only the buttons that apply to the selected row, in a fixed order."""
        if keys == self._shown_actions:
            return
        for button in self.action_buttons.values():
            button.pack_forget()
        for key in keys:
            self.action_buttons[key].pack(side="left", padx=(0, 8))
        self._shown_actions = keys

    # -- actions on the selection -------------------------------------------

    def _menu(self) -> Any:
        p = self.palette
        return tk.Menu(self.root, tearoff=False, background=p["surface_alt"], foreground=p["text"],
                       activebackground=p["accent"], activeforeground=p["accent_text"],
                       disabledforeground=p["line_hi"], borderwidth=1, relief="flat")

    def _move_targets(self, iface: str) -> List[Tuple[str, str]]:
        return [(label, name) for label, name in self._iface_map.items() if name != iface]

    def _selection_states(self) -> Dict[str, bool]:
        item = self._selected_item()
        can_move = bool(item and self._move_targets(item["iface"]))
        return selection_actions(item, self._busy == 0, can_move)

    def _on_action(self, key: str, event: Any = None) -> None:
        item = self._selected_item()
        if item is None or not self._selection_states().get(key):
            return
        iface = item["iface"]
        if key == "move":
            self._popup_move_menu(item, event)
            return
        handler = {
            "verify": self._on_verify, "restart": self._on_restart, "stop": self._on_stop,
            "log": self._on_open_log, "force": self._on_force_quit,
            "release": lambda: self._on_release_adoption(item["data"]),
            "dns": lambda: self._on_dns(iface), "limit": lambda: self._on_limit(iface),
            "remove": lambda: self._on_remove_namespace(iface),
        }[key]
        handler()

    def _popup_move_menu(self, item: Dict[str, Any], event: Any = None) -> None:
        menu = self._menu()
        for label, name in self._move_targets(item["iface"]):
            menu.add_command(label=label, command=lambda n=name: self._on_move(n))
        if event is not None:
            x, y = event.x_root, event.y_root
        else:
            button = self.action_buttons["move"]
            x, y = button.winfo_rootx(), button.winfo_rooty() + button.winfo_height()
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _on_bindings_menu(self, event: Any, iid: str = "") -> None:
        iid = iid or self.tree.identify_row(event.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        self._on_select()
        item = self._rows.get(iid)
        if item is None:
            return
        states = self._selection_states()
        menu = self._menu()

        def add(key: str, label: str) -> None:
            menu.add_command(label=label, command=partial(self._on_action, key),
                             state="normal" if states[key] else "disabled")

        if item["kind"] == "bind":
            add("verify", "Check public IP")
            add("restart", "Restart")
            add("stop", "Stop")
            add("log", "Open log")
            move = self._menu()
            for label, name in self._move_targets(item["iface"]):
                move.add_command(label=label, command=lambda n=name: self._on_move(n))
            menu.add_cascade(label="Move to", menu=move, state="normal" if states["move"] else "disabled")
            add("force", "Force quit")
            menu.add_separator()
        elif item["kind"] == "adopt":
            add("release", "Release")
            menu.add_separator()
        else:
            add("verify", "Check public IP")
            menu.add_separator()
        add("dns", f"DNS servers of {item['iface']}…")
        add("limit", f"Speed limit of {item['iface']}…")
        add("remove", f"Remove the {item['iface']} namespace…")
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _on_queue_menu(self, event: Any) -> None:
        iid = self.queue_tree.identify_row(event.y)
        if not iid:
            return
        self.queue_tree.selection_set(iid)
        self._update_buttons()
        menu = self._menu()
        menu.add_command(label="Remove from queue", command=self._on_remove_queued,
                         state="normal" if self._busy == 0 and not self._assigning else "disabled")
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _update_buttons(self) -> None:
        idle = self._busy == 0
        has_iface = self._iface_name() is not None
        has_app = bool(self.app_var.get().strip())
        _set_enabled(self.add_btn, has_iface and has_app)
        _set_enabled(self.assign_btn, idle and bool(self._pending))
        _set_enabled(self.clear_btn, idle and bool(self._pending))
        _set_enabled(self.remove_btn, idle and not self._assigning and self._queued_index() is not None)
        _set_enabled(self.pick_btn, idle)
        _set_enabled(self.teardown_btn, idle)
        _set_enabled(self.reconnect_btn, idle)
        _set_enabled(self.reset_btn, idle)
        states = self._selection_states()
        self._show_actions(visible_actions(self._selected_item()))
        for key, button in self.action_buttons.items():
            _set_enabled(button, states[key])

    # -- icons and the application field ------------------------------------

    def _entry_for(self, path: str) -> Optional[AppEntry]:
        if self._picked_entry is not None and self._picked_entry.binary == path:
            return self._picked_entry
        return self._picked_entries.get(path) or self._desktop_info.get(path)

    def _table_icon(self, path: str) -> Any:
        """A small icon for the app at `path`, or None. Cached per path. Tk thread only."""
        if path in self._row_icons:
            return self._row_icons[path]
        image = None
        entry = self._entry_for(path)
        if entry is not None and entry.icon:
            image = icons.load_tk_icon(self.root, icons.resolve_icon_path(entry.icon), TABLE_ICON)
        self._row_icons[path] = image
        return image

    def _on_icon_scan(self, result: Any, error: Optional[BaseException]) -> None:
        if error is not None or not result:
            return
        self._desktop_info = result
        self._row_icons.clear()
        self._on_app_changed()
        self._render_pending()
        self._render_bindings()

    def _app_hint(self) -> str:
        text = self.app_var.get().strip()
        if not text:
            return APP_HINT_EMPTY
        entry = self._entry_for(text)
        if entry is not None:
            return f"{entry.name}   {text}"
        try:
            resolved = apps.resolve_app_path(text)
        except InterMuxError:
            return "Not found. Check the name, or use a full path."
        entry = self._entry_for(resolved)
        name = entry.name if entry is not None else os.path.basename(resolved)
        return f"{name}   {resolved}"

    def _on_app_changed(self) -> None:
        _set_var(self.app_hint_var, self._app_hint())
        self._update_buttons()

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
        self.status_dot.configure(foreground=colour)

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
        self._bindings = [dict(r) for r in result["bindings"]]
        self._adoptions = [dict(r) for r in result.get("adoptions", [])]
        self._namespaces = dict(result.get("namespaces") or {})
        _set_var(self.iface_hint_var, unusable_text(self._interfaces, self._bound_ifaces()))
        self._render_bindings()
        self._render_pending()

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
        self.banner.grid(row=1, column=0, sticky="ew", padx=theme.SPACE_OUTER, pady=(0, theme.SPACE_INNER))

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
        # Touch the field only when something changed: rewriting it on every refresh redraws its border.
        if self.iface_var.get() != (chosen or NO_IFACE_TEXT):
            self.iface_var.set(chosen or NO_IFACE_TEXT)
        if changed:
            self.iface_combo.configure(values=labels)
        if bool(labels) == self.iface_combo.instate(["disabled"]):
            self.iface_combo.state(["!disabled", "readonly"] if labels else ["disabled"])
        self._interfaces = list(interfaces)
        self._link_speeds = {i.name: traffic.link_speed_bytes(i.name) for i in interfaces}

    # -- traffic ------------------------------------------------------------

    def _bound_ifaces(self) -> List[str]:
        names = [row["iface"] for row in self._bindings] + [a["iface"] for a in self._adoptions]
        return list(dict.fromkeys(names))

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
        """Sample the bound interfaces and their namespaces' veths, for the rate columns and inline graphs."""
        lanes = self._bound_ifaces()
        veths = {iface: netns.veth_names(iface)[0] for iface in {row["iface"] for row in self._bindings}}
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
            self._render_bindings()

    # -- bindings table -----------------------------------------------------

    def _render_bindings(self) -> None:
        now = time.monotonic()
        bound = self._bound_ifaces()
        self._public_ips = {k: v for k, v in self._public_ips.items() if k in bound and fresh_public_ip(v, now)}
        ips = {k: fresh_public_ip(v, now) for k, v in self._public_ips.items()}
        by_name = {i.name: i for i in self._interfaces}
        routed: Dict[str, int] = {}
        for row in self._bindings:
            if row["running"]:
                routed[row["iface"]] = routed.get(row["iface"], 0) + 1
        for data in self._adoptions:
            if data.get("alive", True):
                routed[data["iface"]] = routed.get(data["iface"], 0) + 1
        states: Dict[str, Tuple[str, str]] = {}
        for name in bound:
            iface = by_name.get(name)
            if iface is None:
                continue
            down, up = self.tracker.rate(name)
            states[name] = monitor.lane_state(bool(iface.up), bool(iface.bindable), routed.get(name, 0), down, up,
                                              self._link_speeds.get(name))
        tracked = set(self.tracker.names())
        rates = {key: self.tracker.rate(key) for name in bound for key in (name, ns_key(name)) if key in tracked}
        rows = active_rows(self._bindings, self._adoptions, by_name, self._namespaces, states, rates, ips)
        for row in rows:
            if row["kind"] != "iface":
                row["image"] = self._table_icon(row["app"])
        _sync_tree(self.tree, rows)
        self._rows = {row["iid"]: row for row in rows}
        self._show_empty(self.tree_empty, "" if rows else BINDINGS_EMPTY)
        self._on_select()
        self._render_sparks()

    # -- inline graphs ----------------------------------------------------------

    def _spark_background(self, iid: str) -> str:
        p = self.palette
        if iid in self.tree.selection():
            return theme.table_selection(p)
        row = self._rows.get(iid) or {}
        return theme.table_group(p) if row.get("kind") == "iface" else p["surface"]

    def _render_sparks(self) -> None:
        """Draw every row's last minute of traffic into a small canvas over its "Last minute" cell."""
        p = self.palette
        tracked = set(self.tracker.names())
        wanted = {iid for iid, row in self._rows.items() if row.get("spark") in tracked}
        for iid in list(self._sparks):
            if iid not in wanted:
                self._sparks.pop(iid).destroy()
        for iid in wanted:
            canvas = self._sparks.get(iid)
            if canvas is None:
                canvas = tk.Canvas(self.tree, highlightthickness=0, borderwidth=0, height=1, width=1)
                canvas.bind("<Button-1>", lambda _e, i=iid: (self.tree.selection_set(i), self.tree.focus(i)))
                canvas.bind("<Button-3>", lambda e, i=iid: self._on_bindings_menu(e, i))
                self._sparks[iid] = canvas
            row = self._rows[iid]
            history = self.tracker.history(row["spark"])[-SPARK_SAMPLES:]
            colour = p["success"] if row.get("tone") == "ok" else monitor.tone_colour(row.get("tone", ""), p)
            if row.get("tone") in ("idle", "off"):
                colour = p["text_muted"]
            canvas._spark = (history, colour)  # type: ignore[attr-defined]
        self._place_sparks()

    def _place_sparks(self) -> None:
        for iid, canvas in self._sparks.items():
            try:
                box = self.tree.bbox(iid, "spark") if self.tree.exists(iid) else ""
            except tk.TclError:
                box = ""
            if not box:
                if getattr(canvas, "_geometry", None) is not None:
                    canvas._geometry = None  # type: ignore[attr-defined]
                    canvas.place_forget()
                continue
            x, y, width, height = box
            width, height = max(width - 16, 10), max(height - 10, 6)
            geometry = (x + 8, y + 5, width, height)
            if getattr(canvas, "_geometry", None) != geometry:
                canvas._geometry = geometry  # type: ignore[attr-defined]
                canvas.place(x=geometry[0], y=geometry[1], width=width, height=height)
            background = self._spark_background(iid)
            if canvas.cget("background") != background:
                canvas.configure(background=background)
            self._draw_spark(canvas, width, height)

    def _draw_spark(self, canvas: Any, width: int, height: int) -> None:
        history, colour = getattr(canvas, "_spark", ([], ""))
        canvas.delete("all")
        if len(history) < 2:
            return
        scale = max(SPARK_FLOOR, max(max(d, u) for d, u in history) * 1.15)
        up = graph.series_points([u for _, u in history], width - 1, 1, height - 1, scale)
        down = graph.series_points([d for d, _ in history], width - 1, 1, height - 1, scale)
        canvas.create_line(*[v for pt in up for v in pt], fill=self.palette["violet"], width=1)
        canvas.create_line(*[v for pt in down for v in pt], fill=colour, width=1.5)

    def _selected_item(self) -> Optional[Dict[str, Any]]:
        selection = self.tree.selection()
        return self._rows.get(selection[0]) if selection else None

    def _selected_row(self) -> Optional[Dict[str, Any]]:
        """The selected app that InterMux started, as read from state."""
        item = self._selected_item()
        return item["data"] if item and item["kind"] == "bind" else None

    def _on_select(self) -> None:
        _set_var(self.hint_var, selection_hint(self._selected_item()))
        self._update_buttons()
        for iid, canvas in self._sparks.items():
            background = self._spark_background(iid)
            if canvas.cget("background") != background:
                canvas.configure(background=background)

    def _on_return_key(self, _event: Any) -> None:
        if isinstance(safe_focus(self.root), ttk.Button):
            return  # Enter on a focused button activates that button
        if not self.add_btn.instate(["disabled"]):
            self._on_add()

    def _on_delete_key(self) -> None:
        focus = safe_focus(self.root)
        if not delete_key_allowed(widget_class_name(focus)):
            return
        if focus is self.queue_tree:
            self._on_remove_queued()
            return
        row = self._selected_row()
        if self._busy == 0 and row is not None and row.get("running"):
            self._on_stop()

    # -- queue ------------------------------------------------------------

    def _queued_index(self) -> Optional[int]:
        selection = self.queue_tree.selection()
        if not selection or not selection[0].startswith("q"):
            return None
        index = int(selection[0][1:])
        return index if index < len(self._pending) else None

    def _on_remove_queued(self) -> None:
        index = self._queued_index()
        if self._assigning or self._busy or index is None:
            return
        item = self._pending.pop(index)
        self.queue_tree.selection_set(())
        self._render_pending()
        self._update_buttons()
        self._set_status(f"Removed {item['name']} from the queue.")

    def _render_pending(self) -> None:
        rows = queue_rows(self._pending, list(self._namespaces))
        for row in rows:
            row["image"] = self._table_icon(row["app"])
        _sync_tree(self.queue_tree, rows)
        self._show_empty(self.queue_empty, "" if rows else QUEUE_EMPTY)

    # -- release ------------------------------------------------------------

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
            self.dns_combo.focus_set()
            return
        try:
            down = dialogs.parse_limit(self.limit_down_var.get())
            up = dialogs.parse_limit(self.limit_up_var.get())
        except ValueError as exc:
            self._set_status(f"Could not use the speed limit: {exc}", "error")
            return
        item = {"iface": iface, "app": app_path, "name": name,
                "policy": "separate" if self.separate_var.get() else "ask",
                # "Automatic" keeps what an existing namespace uses; a new one picks servers automatically.
                "dns": dnsconf.KEEP if dns_value == dnsconf.AUTO else dns_value,
                "dns_text": "" if dns_value == dnsconf.AUTO else self.dns_var.get().strip(),
                "limit": {"down": down, "up": up} if (down or up) else None}
        if queue_add(self._pending, item):
            self._set_status(f"Queued {name} on {iface}. Press Start all when the queue is ready.")
        else:
            self._set_status(f"{name} on {iface} is already queued.")
        self.app_var.set("")
        self._picked_entry = None
        self._render_pending()
        self._update_buttons()

    def _on_clear(self) -> None:
        if self._busy or not self._pending:
            return
        self._pending.clear()
        self._render_pending()
        self._update_buttons()
        self._set_status("Queue cleared.")

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
            self._set_status("Start cancelled.")
            return
        limit_ops = limit_ops_for([item for item, _ in plan])
        self._run_mutation(
            f"Starting {len(plan)} app(s)",
            _batch_task,
            [op for _, op in plan] + limit_ops,
            on_done=partial(self._on_batch_done, plan, limit_ops),
        )

    def _on_batch_done(self, plan: List[Tuple[Dict[str, Any], Dict[str, Any]]], limit_ops: List[Dict[str, Any]],
                       results: Any, error: Optional[BaseException]) -> None:
        self._assigning = False
        if error is not None:
            self._render_pending()
            self._fail("Start failed", error)
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
                done.append(f"{name} on {item['iface']}")
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
            prefix = f"Started {len(done)}. " if done else ""
            self._set_status(f"{prefix}Still queued after an error: " + "; ".join(failed), "error")
        elif done:
            self._set_status("Started " + ", ".join(done) + ".", "success")
        else:
            self._set_status("Nothing was started.")

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
        self._set_status(f"Moved {name} to {to}.", "success")

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
            self._set_status("Select an app in the Bindings table first.")
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
        item = self._selected_item()
        if item is None:
            return
        self._run_mutation(
            f"Checking the public IP of {item['iface']}",
            helper_client.call,
            "verify",
            {"iface": item["iface"]},
            on_done=self._on_verify_done,
        )

    def _on_verify_done(self, result: Any, error: Optional[BaseException]) -> None:
        if error is not None:
            self._fail("Public IP check failed", error)
            return
        iface = result.get("iface", "")
        ip = result.get("public_ip")
        if ip:
            self._public_ips[iface] = (ip, time.monotonic())
            self._set_status(f"{iface} public IP: {ip}")
        else:
            self._public_ips.pop(iface, None)
            self._set_status(f"Could not check the public IP of {iface}: {result.get('error') or 'unknown error'}")
        self._render_bindings()

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
        self._run_mutation("Removing stopped bindings", helper_client.call, "teardown_idle",
                           on_done=self._on_teardown_done)

    def _on_teardown_done(self, result: Any, error: Optional[BaseException]) -> None:
        if error is not None:
            self._fail("Remove stopped failed", error)
            return
        removed = result.get("removed", []) if isinstance(result, dict) else []
        if removed:
            self._set_status(f"Removed {len(removed)} stopped binding(s).")
        else:
            self._set_status("No stopped bindings to remove.")

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
                             + ", ".join(str(k) for k in kept) + ". Close them, then press Remove stopped.",
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
        self._set_status("Application selected. Press Add to queue.")

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
