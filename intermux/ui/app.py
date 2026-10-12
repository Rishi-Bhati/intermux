"""InterMux main window. Runs unprivileged; every mutation goes through helper.client.call in the Worker."""

from __future__ import annotations

import logging
import os
import sys
from functools import partial
from typing import Any, Dict, List, Optional, Sequence, Tuple

from intermux import apps, config, state
from intermux.apps import desktop_entries
from intermux.apps.desktop_entries import AppEntry
from intermux.errors import InterMuxError
from intermux.helper import client as helper_client
from intermux.system import commands, deps, netinfo, procs
from intermux.system import session as session_mod
from intermux.ui import icons, theme
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
OPEN_LOG_TIMEOUT = 10.0


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
    rows, adopted = snapshot_rows(state.load_state(), procs.list_procs(), procs.netns_inode,
                                  adopt_sys.read_group_pids)
    return {"interfaces": interfaces, "bindings": rows, "adoptions": adopted}


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


def _set_enabled(widget: Any, enabled: bool) -> None:
    widget.state(["!disabled"] if enabled else ["disabled"])


KIND_LABELS = {
    "wifi": "Wi-Fi", "ethernet": "Ethernet", "usb": "USB", "bluetooth": "Bluetooth",
    "mobile": "Mobile", "virtual": "Virtual", "other": "Other",
}
NO_IFACE_TEXT = "No bindable interface found. Connect to a network and press Refresh."
ROW_ICON = 32
TILE_ICON = 48
BINDINGS_MIN_HEIGHT = 260  # the Bindings card never shrinks below this


def status_kind(text: str) -> str:
    """Classify a status line as "info", "success" or "error" for the coloured dot. Pure."""
    low = text.lower()
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


class _PendingRow:
    """One row in the Pending section: icon, name, "via <iface>" and a remove control."""

    def __init__(self, app: "InterMuxApp", parent: Any, item: Dict[str, Any]) -> None:
        p = theme.current_palette()
        self.frame = tk.Frame(parent, background=p["surface"], highlightthickness=1,
                              highlightbackground=p["border"], takefocus=0)
        self.frame.columnconfigure(1, weight=1)
        host = tk.Frame(self.frame, background=p["surface"], width=ROW_ICON, height=ROW_ICON)
        host.grid(row=0, column=0, rowspan=2, padx=theme.SPACE_INNER, pady=theme.SPACE_INNER)
        host.grid_propagate(False)
        icon = app._icon_widget(host, item["app"], item["name"], ROW_ICON, p["surface"])
        icon.place(relx=0.5, rely=0.5, anchor="center")
        self.name = ttk.Label(self.frame, text=item["name"], style="Card.TLabel", font=theme.font("section"))
        self.name.grid(row=0, column=1, sticky="sw", pady=(theme.SPACE_INNER, 0))
        self.via = ttk.Label(self.frame, text=f"via {item['iface']}", style="CardSmall.TLabel")
        self.via.grid(row=1, column=1, sticky="nw", pady=(0, theme.SPACE_INNER))
        self.remove = ttk.Button(self.frame, text="Remove", style="Small.CardGhost.TButton",
                                 command=lambda: app._remove_pending(item))
        self.remove.grid(row=0, column=2, rowspan=2, padx=theme.SPACE_INNER)


class _AdoptRow:
    """One adopted app in the Active section: name, "Live" badge, interface and a Release button."""

    def __init__(self, app: "InterMuxApp", parent: Any, data: Dict[str, Any]) -> None:
        p = theme.current_palette()
        self.frame = tk.Frame(parent, background=p["surface"], highlightthickness=1,
                              highlightbackground=p["border"], takefocus=0)
        self.frame.columnconfigure(0, weight=1)
        self.name = ttk.Label(self.frame, text="", style="Card.TLabel", font=theme.font("section"))
        self.name.grid(row=0, column=0, sticky="sw", padx=theme.SPACE_INNER, pady=(theme.SPACE_INNER, 0))
        self.pill = tk.Label(self.frame, text="", font=theme.font("small"), padx=10, pady=2)
        self.pill.grid(row=0, column=1, sticky="e", padx=theme.SPACE_INNER, pady=(theme.SPACE_INNER, 0))
        self.via = ttk.Label(self.frame, text="", style="CardSmall.TLabel")
        self.via.grid(row=1, column=0, sticky="nw", padx=theme.SPACE_INNER, pady=(0, theme.SPACE_INNER))
        self.release = ttk.Button(self.frame, text="Release", style="Small.Secondary.TButton",
                                  command=lambda: app._on_release_adoption(self.data))
        self.release.grid(row=1, column=1, sticky="e", padx=theme.SPACE_INNER, pady=(0, theme.SPACE_INNER))
        self.data = data
        self.update(data)

    def update(self, data: Dict[str, Any]) -> None:
        p = theme.current_palette()
        self.data = data
        self.name.configure(text=data["app_name"])
        self.via.configure(text=f"via {data['iface']} (adopted)")
        if data.get("alive", True):
            self.pill.configure(text="Live", background=p["success"], foreground=p["bg"])
        else:
            self.pill.configure(text="exited", background=p["surface_alt"], foreground=p["text_muted"])


class _BindingRow:
    """One row card in the Active section. Updated in place on every refresh."""

    def __init__(self, app: "InterMuxApp", parent: Any, iid: str) -> None:
        p = theme.current_palette()
        self.app = app
        self.iid = iid
        self.selected = False
        self.icon_widget: Any = None
        self.frame = tk.Frame(parent, background=p["surface"], highlightthickness=1,
                              highlightbackground=p["border"], highlightcolor=p["focus"], takefocus=0)
        self.frame.columnconfigure(1, weight=1)
        self.icon_host = tk.Frame(self.frame, background=p["surface"], width=ROW_ICON, height=ROW_ICON)
        self.icon_host.grid(row=0, column=0, rowspan=2, padx=(theme.SPACE_INNER, theme.SPACE_INNER),
                            pady=theme.SPACE_INNER, sticky="n")
        self.icon_host.grid_propagate(False)
        self.name = ttk.Label(self.frame, text="", style="Card.TLabel", font=theme.font("section"))
        self.name.grid(row=0, column=1, sticky="sw", pady=(theme.SPACE_INNER, 0))
        self.pill = tk.Label(self.frame, text="", font=theme.font("small"), padx=10, pady=2)
        self.pill.grid(row=0, column=2, sticky="e", padx=theme.SPACE_INNER, pady=(theme.SPACE_INNER, 0))
        self.via = ttk.Label(self.frame, text="", style="CardSmall.TLabel")
        self.via.grid(row=1, column=1, sticky="nw")
        self.ip = ttk.Label(self.frame, text="", style="CardMuted.TLabel")
        self.ip.grid(row=1, column=2, sticky="e", padx=theme.SPACE_INNER)
        actions = tk.Frame(self.frame, background=p["surface"])
        actions.grid(row=2, column=1, columnspan=2, sticky="w", pady=(6, theme.SPACE_INNER))
        self.buttons = [
            ttk.Button(actions, text="Verify", style="Small.Secondary.TButton", command=lambda: self._act(app._on_verify)),
            ttk.Button(actions, text="Stop", style="Small.Secondary.TButton", command=lambda: self._act(app._on_stop)),
            ttk.Button(actions, text="Log", style="Small.Secondary.TButton", command=lambda: self._act(app._on_open_log)),
            ttk.Button(actions, text="Force quit", style="Small.CardGhost.TButton", command=lambda: self._act(app._on_force_quit)),
        ]
        for index, button in enumerate(self.buttons):
            button.grid(row=0, column=index, padx=(0 if index == 0 else 6, 0))
        for widget in (self.frame, self.icon_host, self.name, self.via, self.pill, self.ip, actions):
            widget.bind("<Button-1>", lambda _e: app.select_binding(self.iid))

    def _act(self, handler: Any) -> None:
        self.app.select_binding(self.iid)
        handler()

    def set_icon(self, widget: Any) -> None:
        if self.icon_widget is not None:
            self.icon_widget.destroy()
        self.icon_widget = widget
        widget.place(relx=0.5, rely=0.5, anchor="center")
        widget.bind("<Button-1>", lambda _e: self.app.select_binding(self.iid))

    def update(self, data: Dict[str, Any], public_ip: str) -> None:
        p = theme.current_palette()
        self.name.configure(text=data["app_name"])
        self.via.configure(text=f"via {data['iface']}")
        if data["running"]:
            self.pill.configure(text="running", background=p["success"], foreground=p["bg"])
        else:
            self.pill.configure(text="stopped", background=p["surface_alt"], foreground=p["text_muted"])
        self.ip.configure(text=public_ip)

    def set_selected(self, selected: bool) -> None:
        p = theme.current_palette()
        self.selected = selected
        self.frame.configure(highlightbackground=p["accent"] if selected else p["border"],
                             highlightthickness=2 if selected else 1)

    def set_enabled(self, enabled: bool) -> None:
        for button in self.buttons:
            _set_enabled(button, enabled)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class InterMuxApp:
    """Main window: bind an app to an interface, list bindings, run teardown actions."""

    def __init__(self, root: Any) -> None:
        self.root = root
        self.palette = theme.apply_theme(root)
        self.worker = Worker(root)
        self._busy = 0
        self._snapshot_inflight = False
        self._iface_map: Dict[str, str] = {}
        self._rows: Dict[str, Dict[str, Any]] = {}
        self._row_widgets: Dict[str, _BindingRow] = {}
        self._public_ips: Dict[str, str] = {}
        self._selected_iid: Optional[str] = None
        self._pending: List[Dict[str, Any]] = []
        self._pending_widgets: List[_PendingRow] = []
        self._picked_entries: Dict[str, AppEntry] = {}
        self._assigning = False
        self._picked_entry: Optional[AppEntry] = None
        self._desktop_info: Dict[str, AppEntry] = {}
        self._tile_icon: Any = None
        self._adoptions: List[Dict[str, Any]] = []
        self._adopt_widgets: Dict[str, _AdoptRow] = {}
        self._adopt_header: Any = None

        root.title(f"InterMux {config.VERSION}")
        root.geometry("1040x860")
        root.minsize(900, 760)
        self.app_var = tk.StringVar(root, value="")
        self.iface_var = tk.StringVar(root, value="")
        self.separate_var = tk.BooleanVar(root, value=False)
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

    # -- layout ---------------------------------------------------------

    def _build(self, root: Any) -> None:
        pad = theme.SPACE_OUTER
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        header = ttk.Frame(root)
        header.grid(row=0, column=0, sticky="ew", padx=pad, pady=(pad, theme.SPACE_CARD_GAP))
        ttk.Label(header, text="InterMux", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text=f"v{config.VERSION}", style="Muted.TLabel").pack(side="left", padx=(8, 0))
        self.session_chip = ttk.Label(header, textvariable=self.session_var, style="Chip.TLabel")
        self.session_chip.pack(side="left", padx=(theme.SPACE_CARD_GAP, 0))
        ttk.Button(header, text="Refresh", style="Ghost.TButton", command=self.refresh_all).pack(side="right")
        ttk.Button(header, text="Report issue", style="Secondary.TButton", command=self._on_report).pack(
            side="right", padx=(0, 8))

        self.banner = ttk.Frame(root, padding=theme.SPACE_INNER, style="Banner.TFrame")
        self.body = ttk.Frame(root)
        self.body.grid(row=2, column=0, sticky="nsew", padx=pad)
        self.body.columnconfigure(0, weight=1)
        self.body.rowconfigure(0, weight=0)
        self.body.rowconfigure(1, weight=1, minsize=BINDINGS_MIN_HEIGHT)

        self._build_bind_card(self.body)
        self._build_bindings_card(self.body)

        footer = ttk.Frame(root, style="Footer.TFrame")
        footer.grid(row=3, column=0, sticky="ew", padx=pad, pady=(theme.SPACE_CARD_GAP, pad))
        self.status_dot = tk.Label(footer, text="●", font=theme.font("body"))
        self.status_dot.pack(side="left")
        # Buttons are packed first so the status text can never push them out of view.
        self.reset_btn = ttk.Button(footer, text="Reset everything", style="Danger.TButton", command=self._on_reset)
        self.reset_btn.pack(side="right")
        self.teardown_btn = ttk.Button(footer, text="Teardown idle", style="Secondary.TButton",
                                       command=self._on_teardown)
        self.teardown_btn.pack(side="right", padx=(0, theme.SPACE_INNER))
        self.status_label = tk.Label(footer, textvariable=self.status_var, font=theme.font("body"), anchor="w",
                                     background=self.palette["bg"], foreground=self.palette["text"])
        self.status_label.pack(side="left", padx=(6, 0), fill="x", expand=True)
        self._set_status("")
        self._refresh_app_tile()
        self._update_buttons()

    def _build_bind_card(self, parent: Any) -> None:
        pad = theme.SPACE_INNER
        gap = theme.SPACE_CARD_GAP
        card = _card(parent)
        card.grid(row=0, column=0, sticky="ew")
        inner = tk.Frame(card, background=self.palette["surface"])
        inner.pack(fill="x", padx=pad, pady=pad)
        inner.columnconfigure(1, weight=1)
        p = self.palette

        ttk.Label(inner, text="New binding", style="Section.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")

        ttk.Label(inner, text="Interface", style="Sub.TLabel").grid(row=1, column=0, sticky="w",
                                                                    pady=(pad, 0), padx=(0, gap))
        self.iface_combo = ttk.Combobox(inner, textvariable=self.iface_var, state="readonly", values=[],
                                        font=theme.font("body"), height=10)
        self.iface_combo.grid(row=1, column=1, sticky="ew", pady=(pad, 0))
        self.iface_combo.bind("<<ComboboxSelected>>", lambda _e: self._update_buttons())

        self.app_tile = tk.Frame(inner, background=p["surface_alt"], highlightthickness=1,
                                 highlightbackground=p["border"], highlightcolor=p["focus"])
        self.app_tile.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(pad, 0))
        self.app_tile.columnconfigure(1, weight=1)
        tpad = pad // 2
        self.app_icon_host = tk.Frame(self.app_tile, background=p["surface_alt"], width=TILE_ICON, height=TILE_ICON)
        self.app_icon_host.grid(row=0, column=0, rowspan=2, padx=pad, pady=tpad)
        self.app_icon_host.grid_propagate(False)
        self.app_icon_widget: Any = None
        self.app_name_label = tk.Label(self.app_tile, text="", background=p["surface_alt"], foreground=p["text"],
                                       font=theme.font("section"), anchor="w", width=1)
        self.app_name_label.grid(row=0, column=1, sticky="sew", pady=(tpad, 0))
        self.app_path_label = tk.Label(self.app_tile, text="", background=p["surface_alt"],
                                       foreground=p["text_muted"], font=theme.font("small"), anchor="w", width=1)
        self.app_path_label.grid(row=1, column=1, sticky="new", pady=(0, tpad))
        self.pick_btn = ttk.Button(self.app_tile, text="Choose application", style="Secondary.TButton",
                                   command=self._on_pick)
        self.pick_btn.grid(row=0, column=2, rowspan=2, padx=pad)
        for widget in (self.app_tile, self.app_icon_host, self.app_name_label, self.app_path_label):
            widget.bind("<Button-1>", lambda _e: self._on_pick())

        entry_row = tk.Frame(inner, background=p["surface"])
        entry_row.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(pad, 0))
        entry_row.columnconfigure(1, weight=1)
        ttk.Label(entry_row, text="Or type a name or path", style="Sub.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, gap))
        self.path_entry = ttk.Entry(entry_row, textvariable=self.app_var)
        self.path_entry.grid(row=0, column=1, sticky="ew")
        self.add_btn = ttk.Button(entry_row, text="Add", style="Primary.TButton", command=self._on_add)
        self.add_btn.grid(row=0, column=2, padx=(pad, 0))

        ttk.Checkbutton(
            inner, text="Open a separate profile if the app is already running",
            variable=self.separate_var, style="Card.TCheckbutton",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(pad, 0))

    def _section(self, parent: Any, column: int, title: str, empty_text: str) -> Tuple[Any, Any, Any, Any]:
        """One half of the Bindings card. Returns (header_frame, scroll, rows_host, empty_label)."""
        p = self.palette
        pad = theme.SPACE_INNER
        panel = tk.Frame(parent, background=p["surface"], highlightthickness=1,
                         highlightbackground=p["border"], borderwidth=0)
        panel.grid(row=0, column=column, sticky="nsew", padx=(0, pad) if column == 0 else (0, 0))
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(1, weight=1)
        header = tk.Frame(panel, background=p["surface"])
        header.grid(row=0, column=0, sticky="ew", padx=pad, pady=(pad, 6))
        ttk.Label(header, text=title, style="Section.TLabel").pack(side="left")
        body = tk.Frame(panel, background=p["surface"], highlightthickness=0, borderwidth=0)
        body.grid(row=1, column=0, sticky="nsew", padx=(pad, 4), pady=(0, pad))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        scroll = theme.ScrollFrame(body, p["surface"])
        scroll.grid(row=0, column=0, sticky="nsew")
        host = scroll.inner
        host.columnconfigure(0, weight=1)
        # Empty state: a sibling of the ScrollFrame (created after it, so drawn above the canvas),
        # filling the whole body below the heading and centring the label.
        empty = tk.Frame(body, background=p["surface"], highlightthickness=0, borderwidth=0)
        empty.columnconfigure(0, weight=1)
        empty.rowconfigure(0, weight=1)
        ttk.Label(empty, text=empty_text, style="CardMuted.TLabel", justify="center",
                  anchor="center", wraplength=240).grid(row=0, column=0)
        empty.grid(row=0, column=0, sticky="nsew")
        empty.tkraise()
        return header, scroll, host, empty

    @staticmethod
    def _show_empty(empty: Any, show: bool) -> None:
        if show:
            empty.grid(row=0, column=0, sticky="nsew")
            empty.tkraise()
        else:
            empty.grid_remove()

    def _build_bindings_card(self, parent: Any) -> None:
        pad = theme.SPACE_INNER
        card = _card(parent)
        card.grid(row=1, column=0, sticky="nsew", pady=(theme.SPACE_CARD_GAP, 0))
        inner = tk.Frame(card, background=self.palette["surface"])
        inner.pack(fill="both", expand=True, padx=pad + 4, pady=pad + 4)
        inner.columnconfigure(0, weight=1)
        inner.rowconfigure(1, weight=1, minsize=BINDINGS_MIN_HEIGHT - 80)
        ttk.Label(inner, text="Bindings", style="Section.TLabel").grid(row=0, column=0, sticky="w",
                                                                       pady=(0, pad))
        sections = tk.Frame(inner, background=self.palette["surface"])
        sections.grid(row=1, column=0, sticky="nsew")
        sections.rowconfigure(0, weight=1)
        sections.columnconfigure(0, weight=1, uniform="half")
        sections.columnconfigure(1, weight=1, uniform="half")

        head, self.pending_scroll, self.pending_host, self.pending_empty = self._section(
            sections, 0, "Pending", "Nothing pending. Add an application above.")
        self.clear_btn = ttk.Button(head, text="Clear", style="Small.Secondary.TButton", command=self._on_clear)
        self.clear_btn.pack(side="right")
        self.assign_btn = ttk.Button(head, text="Assign", style="Primary.TButton", command=self._on_assign)
        self.assign_btn.pack(side="right", padx=(0, 6))

        _head, self.scroll, self.rows_host, self.empty_label = self._section(
            sections, 1, "Active", "No apps are bound yet.")

    def _update_buttons(self) -> None:
        idle = self._busy == 0
        has_iface = self._iface_name() is not None
        has_app = bool(self.app_var.get().strip())
        _set_enabled(self.add_btn, has_iface and has_app)
        _set_enabled(self.assign_btn, idle and bool(self._pending))
        _set_enabled(self.clear_btn, idle and bool(self._pending))
        _set_enabled(self.pick_btn, idle)
        for adopt_row in self._adopt_widgets.values():
            _set_enabled(adopt_row.release, idle)
        _set_enabled(self.teardown_btn, idle)
        _set_enabled(self.reset_btn, idle)
        for row in self._row_widgets.values():
            row.set_enabled(idle)

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
        for iid, widget in self._row_widgets.items():
            self._set_row_icon(widget, self._rows.get(iid))
        self._render_pending()

    def _set_row_icon(self, widget: _BindingRow, row: Optional[Dict[str, Any]]) -> None:
        if row is None:
            return
        widget.set_icon(self._icon_widget(widget.icon_host, row["app"], row["app_name"], ROW_ICON,
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
        self.status_dot.configure(foreground=colour, background=p["bg"])

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

    # -- bindings -----------------------------------------------------------

    def _apply_bindings(self, rows: Sequence[Dict[str, Any]]) -> None:
        self._rows = {}
        ordered: List[str] = []
        for row in rows:
            iid = _iid(row["iface"], row["app"])
            self._rows[iid] = dict(row, iid=iid)
            ordered.append(iid)
        for iid in list(self._row_widgets):
            if iid not in self._rows:
                self._row_widgets.pop(iid).frame.destroy()
        for index, iid in enumerate(ordered):
            row = self._rows[iid]
            widget = self._row_widgets.get(iid)
            if widget is None:
                widget = _BindingRow(self, self.rows_host, iid)
                self._row_widgets[iid] = widget
                self._set_row_icon(widget, row)
            widget.update(row, self._public_ips.get(row["iface"], ""))
            widget.frame.grid(row=index, column=0, sticky="ew", pady=(0, 8))
        self._public_ips = {k: v for k, v in self._public_ips.items() if any(r["iface"] == k for r in rows)}
        if self._selected_iid not in self._rows:
            self._selected_iid = None
        for iid, widget in self._row_widgets.items():
            widget.set_selected(iid == self._selected_iid)
        self._show_empty(self.empty_label, not (ordered or self._adoptions))

    def _apply_adoptions(self, rows: Sequence[Dict[str, Any]]) -> None:
        self._adoptions = [dict(r) for r in rows]
        wanted = {r["id"] for r in self._adoptions}
        for key in list(self._adopt_widgets):
            if key not in wanted:
                self._adopt_widgets.pop(key).frame.destroy()
        if self._adopt_header is None:
            self._adopt_header = ttk.Label(self.rows_host, text="Adopted apps", style="Sub.TLabel")
        for index, data in enumerate(self._adoptions):
            widget = self._adopt_widgets.get(data["id"])
            if widget is None:
                widget = _AdoptRow(self, self.rows_host, data)
                self._adopt_widgets[data["id"]] = widget
            else:
                widget.update(data)
            widget.frame.grid(row=1001 + index, column=0, sticky="ew", pady=(0, 8))
        if self._adoptions:
            self._adopt_header.grid(row=1000, column=0, sticky="w", pady=(4, 4))
        else:
            self._adopt_header.grid_remove()
        self._show_empty(self.empty_label, not (self._rows or self._adoptions))

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
        self._set_status(f"Stopped routing {name}; it uses the default route again.")

    def select_binding(self, iid: str) -> None:
        self._selected_iid = iid if iid in self._rows else None
        for key, widget in self._row_widgets.items():
            widget.set_selected(key == self._selected_iid)

    def _selected_row(self) -> Optional[Dict[str, Any]]:
        if self._selected_iid is None:
            return None
        return self._rows.get(self._selected_iid)

    def _on_return_key(self, _event: Any) -> None:
        if isinstance(self.root.focus_get(), ttk.Button):
            return  # Enter on a focused button activates that button
        if not self.add_btn.instate(["disabled"]):
            self._on_add()

    def _on_delete_key(self) -> None:
        if self._busy == 0 and self._selected_row() is not None:
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
        item = {"iface": iface, "app": app_path, "name": name,
                "policy": "separate" if self.separate_var.get() else "ask"}
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
                      "running_policy": policy, "allow_unsupported": False}
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
            else:
                err = result.get("error") if isinstance(result, dict) else None
                err = err if isinstance(err, dict) else {}
                failed.append(f"{name} ({err.get('code') or 'error'}: {err.get('message') or 'failed'})")
        self._render_pending()
        self._update_buttons()
        if failed:
            prefix = f"Assigned {len(done)}. " if done else ""
            self._set_status(f"{prefix}Failed, still pending: " + "; ".join(failed), "error")
        elif done:
            self._set_status("Assigned " + ", ".join(done) + ".", "success")
        else:
            self._set_status("Nothing was assigned.")

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
            self._set_status(f"{name}: some processes did not exit ({', '.join(str(p) for p in still)}).")
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
            self._public_ips[iface] = ip
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
            commands.run, ["xdg-open", row["log_path"]], timeout=OPEN_LOG_TIMEOUT,
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
            self._set_status(f"{name}: processes still running after force-quit.")
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
