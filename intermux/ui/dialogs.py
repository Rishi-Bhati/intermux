"""Small modal dialogs for per-connection settings: bandwidth limit and DNS servers."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from intermux import dnsconf
from intermux.system import shaping
from intermux.ui import theme

try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:  # reported by main()
    tk = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]

AUTO_LABEL = "Automatic"
DNS_CHOICES = [AUTO_LABEL] + [f"{name}: {', '.join(servers)}" for name, servers in dnsconf.PRESETS.items()]


def dns_choice_value(text: str) -> Any:
    """What a DNS field means: dnsconf.AUTO for "Automatic", else a list of servers. Raises ValueError."""
    if not text.strip() or text.strip() == AUTO_LABEL:
        return dnsconf.AUTO
    return dnsconf.parse_servers(text)


def dns_choice_label(servers: List[str]) -> str:
    """The field text for a namespace's current servers."""
    if not servers:
        return AUTO_LABEL
    name = dnsconf.preset_name(servers)
    return f"{name}: {', '.join(servers)}" if name else ", ".join(servers)


def parse_limit(text: str) -> Optional[float]:
    """A limit field: empty or 0 means no limit; otherwise Mbit/s within range. Raises ValueError."""
    text = text.strip().replace(",", ".")
    if not text or text in ("0", "-"):
        return None
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"not a number: {text}") from None
    return shaping.check_mbit(value)


class _Modal:
    """A themed modal Toplevel with a title, a body frame, an error line and OK/Cancel."""

    def __init__(self, parent: Any, title: str, heading: str, text: str, ok_label: str) -> None:
        p = theme.current_palette()
        self.result: Any = None
        self.top = tk.Toplevel(parent)
        self.top.title(title)
        self.top.configure(background=p["bg"])
        self.top.transient(parent)
        self.top.resizable(False, False)
        outer = tk.Frame(self.top, background=p["bg"])
        outer.pack(fill="both", expand=True, padx=theme.SPACE_OUTER, pady=theme.SPACE_OUTER)
        tk.Label(outer, text=heading, background=p["bg"], foreground=p["text"], font=theme.font("title"),
                 anchor="w").pack(anchor="w")
        tk.Label(outer, text=text, background=p["bg"], foreground=p["text_muted"], font=theme.font("body"),
                 wraplength=420, justify="left", anchor="w").pack(anchor="w", pady=(6, 14))
        self.body = tk.Frame(outer, background=p["bg"])
        self.body.pack(fill="x")
        self.error = tk.Label(outer, text="", background=p["bg"], foreground=p["danger"], font=theme.font("small"),
                              anchor="w", wraplength=420, justify="left")
        self.error.pack(anchor="w", pady=(8, 0))
        buttons = tk.Frame(outer, background=p["bg"])
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(buttons, text=ok_label, style="Primary.TButton", command=self._ok).pack(side="left")
        ttk.Button(buttons, text="Cancel", style="Ghost.TButton", command=self.top.destroy).pack(
            side="left", padx=(8, 0))
        self.top.bind("<Escape>", lambda _e: self.top.destroy())
        self.top.bind("<Return>", lambda _e: self._ok())
        self.top.protocol("WM_DELETE_WINDOW", self.top.destroy)

    def label(self, text: str, row: int) -> None:
        p = theme.current_palette()
        tk.Label(self.body, text=text, background=p["bg"], foreground=p["text_muted"], font=theme.font("small"),
                 anchor="w").grid(row=row, column=0, sticky="w", pady=(0, 4))

    def run(self) -> Any:
        try:
            self.top.grab_set()
        except tk.TclError:
            pass
        self.top.wait_window()
        return self.result

    def _ok(self) -> None:
        try:
            self.result = self.collect()
        except ValueError as exc:
            self.error.configure(text=str(exc))
            return
        self.top.destroy()

    def collect(self) -> Any:  # pragma: no cover - overridden
        raise NotImplementedError


class LimitDialog(_Modal):
    """Ask for download and upload limits of a connection's namespace. Returns (down, up) or None."""

    def __init__(self, parent: Any, iface: str, current: Optional[Dict[str, Optional[float]]]) -> None:
        super().__init__(parent, "Bandwidth limit", f"Limit {iface}",
                         f"Caps the speed of every app InterMux routes through {iface}, together. "
                         "Leave a field empty for no limit.", "Apply")
        current = current or {}
        self.down = tk.StringVar(self.top, value=_fmt(current.get("down")))
        self.up = tk.StringVar(self.top, value=_fmt(current.get("up")))
        self.body.columnconfigure(0, weight=1, uniform="limit")
        self.body.columnconfigure(1, weight=1, uniform="limit")
        for column, (text, var) in enumerate((("Download, Mbit/s", self.down), ("Upload, Mbit/s", self.up))):
            p = theme.current_palette()
            tk.Label(self.body, text=text, background=p["bg"], foreground=p["text_muted"],
                     font=theme.font("small"), anchor="w").grid(row=0, column=column, sticky="w", pady=(0, 4),
                                                                padx=(0 if column == 0 else 8, 0))
            entry = ttk.Entry(self.body, textvariable=var, width=14)
            entry.grid(row=1, column=column, sticky="ew", padx=(0 if column == 0 else 8, 0))
            if column == 0:
                entry.focus_set()

    def collect(self) -> Tuple[Optional[float], Optional[float]]:
        return parse_limit(self.down.get()), parse_limit(self.up.get())


class DnsDialog(_Modal):
    """Ask for the DNS servers of a connection's namespace. Returns dnsconf.AUTO, a list, or None."""

    def __init__(self, parent: Any, iface: str, current: List[str]) -> None:
        super().__init__(parent, "DNS servers", f"DNS for {iface}",
                         "Automatic uses the connection's router, then public resolvers. Pick a preset or type "
                         "up to four IPv4 servers separated by commas. Apps that are running pick up the change; "
                         "apps with their own DNS-over-HTTPS keep using that.", "Apply")
        self.var = tk.StringVar(self.top, value=dns_choice_label(current))
        self.label("Servers", 0)
        combo = ttk.Combobox(self.body, textvariable=self.var, values=DNS_CHOICES, width=42)
        combo.grid(row=1, column=0, sticky="ew")
        combo.focus_set()

    def collect(self) -> Any:
        return dns_choice_value(self.var.get())


def _fmt(value: Optional[float]) -> str:
    return f"{value:g}" if value else ""
