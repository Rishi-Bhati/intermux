"""The "Add VPN" window: connect, disconnect and import VPN connections through NetworkManager."""

from __future__ import annotations

from typing import Any, Callable, List, Optional

from intermux.errors import InterMuxError
from intermux.system import nm
from intermux.ui import graph, theme
from intermux.ui.worker import Worker

try:
    import tkinter as tk
    from tkinter import filedialog, ttk
except ImportError:  # reported by main()
    tk = None  # type: ignore[assignment]
    filedialog = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]

WRAP = 460
READING = "Reading VPN connections…"


class VpnDialog:
    """Lists NetworkManager VPN and WireGuard connections with Connect/Disconnect, and imports new ones."""

    def __init__(self, parent: Any, worker: Worker, on_change: Callable[[], None]) -> None:
        p = theme.current_palette()
        self.p = p
        self.worker = worker
        self.on_change = on_change
        self.busy = False
        self.top = tk.Toplevel(parent)
        self.top.title("VPN connections")
        self.top.configure(background=p["bg"])
        self.top.transient(parent)
        self.top.minsize(520, 360)
        self.apps_only = tk.BooleanVar(self.top, value=True)
        self.status = tk.StringVar(self.top, value="")

        outer = tk.Frame(self.top, background=p["bg"])
        outer.pack(fill="both", expand=True, padx=theme.SPACE_OUTER, pady=theme.SPACE_OUTER)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)
        tk.Label(outer, text="VPN connections", background=p["bg"], foreground=p["text"],
                 font=theme.font("title"), anchor="w").grid(row=0, column=0, sticky="w")
        tk.Label(outer, text="Connect a VPN here, then route single apps through it. Everything else keeps "
                             "using your normal connection.",
                 background=p["bg"], foreground=p["text_muted"], font=theme.font("body"), justify="left",
                 anchor="w", wraplength=WRAP).grid(row=1, column=0, sticky="w", pady=(6, 14))

        box = tk.Frame(outer, background=p["surface"], highlightthickness=1, highlightbackground=p["border"])
        box.grid(row=2, column=0, sticky="nsew")
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        self.scroll = theme.ScrollFrame(box, p["surface"], height=160)
        self.scroll.grid(row=0, column=0, sticky="nsew", padx=(0, 2), pady=4)
        self.scroll.attach_wheel(self.top)
        self.rows = self.scroll.inner
        self.rows.columnconfigure(1, weight=1)

        ttk.Checkbutton(outer, text="Only for apps I route through it (keep the system's normal route)",
                        variable=self.apps_only, style="Dialog.TCheckbutton").grid(
            row=3, column=0, sticky="w", pady=(12, 0))
        tk.Label(outer, textvariable=self.status, background=p["bg"], foreground=p["text_muted"],
                 font=theme.font("small"), justify="left", anchor="w", wraplength=WRAP).grid(
            row=4, column=0, sticky="w", pady=(8, 0))
        buttons = tk.Frame(outer, background=p["bg"])
        buttons.grid(row=5, column=0, sticky="ew", pady=(14, 0))
        self.import_btn = ttk.Button(buttons, text="Import WireGuard or OpenVPN file…", style="Secondary.TButton",
                                     command=self._import)
        self.import_btn.pack(side="left")
        ttk.Button(buttons, text="Close", style="Ghost.TButton", command=self.top.destroy).pack(side="right")
        self.top.bind("<Escape>", lambda _e: self.top.destroy())
        self.reload()

    # -- data -----------------------------------------------------------------

    def reload(self) -> None:
        if not nm.available():
            self._render_message("NetworkManager (nmcli) is not installed. Bring the VPN up with its own tool, "
                                 "for example \"wg-quick up wg0\", and it appears in InterMux by itself.")
            self.import_btn.state(["disabled"])
            return
        self._set_busy(True, None if self.status.get() else READING)
        self.worker.submit(nm.list_vpns, on_done=self._on_list)

    def _on_list(self, result: Any, error: Optional[BaseException]) -> None:
        if not self._alive():
            return
        self._set_busy(False, None)
        if self.status.get() == READING:
            self.status.set("")
        if error is not None:
            self._render_message(f"Could not read the VPN connections: {error}")
            return
        self._render(result or [])

    # -- rendering ------------------------------------------------------------

    def _clear(self) -> None:
        for child in self.rows.winfo_children():
            child.destroy()

    def _render_message(self, text: str) -> None:
        self._clear()
        tk.Label(self.rows, text=text, background=self.p["surface"], foreground=self.p["text_muted"],
                 font=theme.font("body"), justify="left", anchor="w", wraplength=WRAP - 40).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=14, pady=14)

    def _render(self, conns: List[nm.VpnConnection]) -> None:
        self._clear()
        p = self.p
        bg = p["surface"]
        if not conns:
            self._render_message("No VPN connections yet. Import a WireGuard .conf or OpenVPN .ovpn file from "
                                 "your VPN provider below.")
            return
        for index, conn in enumerate(conns):
            colour = p["success"] if conn.active else p["text_muted"]
            g = tk.Canvas(self.rows, width=20, height=20, background=bg, highlightthickness=0, borderwidth=0)
            graph.draw_kind_glyph(g, "vpn", 10, 10, 18, colour, bg)
            g.grid(row=index * 2, column=0, rowspan=2, padx=(14, 10), pady=10)
            tk.Label(self.rows, text=conn.name, background=bg, foreground=p["text"], font=theme.font("label"),
                     anchor="w").grid(row=index * 2, column=1, sticky="sw", pady=(10, 0))
            where = f"Connected as {conn.device}" if conn.active and conn.device else (
                "Connected" if conn.active else "Not connected")
            tk.Label(self.rows, text=f"{conn.kind_label}  ·  {where}", background=bg, foreground=colour,
                     font=theme.font("small"), anchor="w").grid(row=index * 2 + 1, column=1, sticky="nw",
                                                                 pady=(0, 10))
            if conn.active:
                button = ttk.Button(self.rows, text="Disconnect", style="Small.Secondary.TButton",
                                    command=lambda c=conn: self._disconnect(c))
            else:
                button = ttk.Button(self.rows, text="Connect", style="Small.Primary.TButton",
                                    command=lambda c=conn: self._connect(c))
            button.grid(row=index * 2, column=2, rowspan=2, padx=14)

    # -- actions ----------------------------------------------------------------

    def _connect(self, conn: nm.VpnConnection) -> None:
        if self.busy:
            return
        self._set_busy(True, f"Connecting {conn.name}…")
        self.worker.submit(nm.connect, conn, self.apps_only.get(), on_done=lambda r, e: self._done(
            e, f"Connected {conn.name}. It is now listed under Connections; use \"+ Add app\" on it."))

    def _disconnect(self, conn: nm.VpnConnection) -> None:
        if self.busy:
            return
        self._set_busy(True, f"Disconnecting {conn.name}…")
        self.worker.submit(nm.disconnect, conn, on_done=lambda r, e: self._done(e, f"Disconnected {conn.name}."))

    def _import(self) -> None:
        if self.busy:
            return
        path = filedialog.askopenfilename(
            parent=self.top, title="Import a VPN file",
            filetypes=[("VPN files", "*.conf *.ovpn"), ("WireGuard", "*.conf"), ("OpenVPN", "*.ovpn"),
                       ("All files", "*")])
        if not path:
            return
        self._set_busy(True, "Importing…")
        self.worker.submit(nm.import_file, path, on_done=lambda r, e: self._done(
            e, "Imported. Press Connect to use it."))

    def _done(self, error: Optional[BaseException], message: str) -> None:
        if not self._alive():
            return
        self._set_busy(False, None)
        if error is not None:
            self.status.set(error.message if isinstance(error, InterMuxError) else str(error))
        else:
            self.status.set(message)
            self.on_change()
        self.reload()

    def _set_busy(self, busy: bool, text: Optional[str]) -> None:
        self.busy = busy
        if text is not None:
            self.status.set(text)
        state = ["disabled"] if busy else ["!disabled"]
        if nm.available():
            self.import_btn.state(state)

    def _alive(self) -> bool:
        try:
            return bool(self.top.winfo_exists())
        except tk.TclError:
            return False
