"""Design system: colour tokens, spacing, fonts and the ttk "clam" styles used by every window."""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:  # reported by main()
    tk = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]

log = logging.getLogger("intermux.ui")

# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

PALETTE: Dict[str, str] = {
    "bg": "#0E1117",
    "surface": "#161B22",
    "surface_alt": "#1C2330",
    "border": "#2A3140",
    "text": "#E6EAF2",
    "text_muted": "#9AA4B5",
    "accent": "#5B8CFF",
    "accent_text": "#FFFFFF",
    "success": "#3FD69A",
    "warning": "#F2B84B",
    "danger": "#FF6B6B",
    "focus": "#8AB4FF",
}

COMBO_ARROW = PALETTE["text"]  # dropdown arrow colour; contrasts with surface_alt

SPACE_OUTER = 24   # window padding
SPACE_CARD_GAP = 16  # between cards
SPACE_INNER = 12   # inside cards
BUTTON_MIN_HEIGHT = 34
BUTTON_PAD_X = 14
CHECK_SIZE = 16
CHECK_GAP = 8  # transparent strip right of the indicator, so the label does not touch the box

_current: Dict[str, str] = dict(PALETTE)
_fonts: Dict[str, Any] = {}
_images: List[Any] = []  # keeps PhotoImages referenced for the life of the process

FONT_SIZES: Dict[str, Tuple[int, str]] = {
    "title": (18, "bold"),
    "section": (12, "bold"),
    "body": (11, "normal"),
    "small": (10, "normal"),
}


# ---------------------------------------------------------------------------
# Pure colour helpers
# ---------------------------------------------------------------------------

def _rgb(colour: str) -> Tuple[int, int, int]:
    value = colour.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def shade(colour: str, amount: float) -> str:
    """Lighten (amount > 0) or darken (amount < 0) a #RRGGBB colour by moving it toward white or black."""
    target = 255 if amount > 0 else 0
    factor = min(1.0, abs(amount))
    parts = [round(c + (target - c) * factor) for c in _rgb(colour)]
    return "#{:02X}{:02X}{:02X}".format(*parts)


def relative_luminance(colour: str) -> float:
    def channel(c: int) -> float:
        s = c / 255.0
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in _rgb(colour))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(foreground: str, background: str) -> float:
    """WCAG contrast ratio between two #RRGGBB colours (1.0 to 21.0)."""
    a, b = relative_luminance(foreground), relative_luminance(background)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def current_palette() -> Dict[str, str]:
    """Return a copy of the palette applied by the last apply_theme call."""
    return dict(_current)


def font(kind: str = "body") -> Any:
    """Return the named font ("title", "section", "body", "small"); falls back to TkDefaultFont."""
    if kind in _fonts:
        return _fonts[kind]
    import tkinter.font as tkfont
    return tkfont.nametofont("TkDefaultFont")


# ---------------------------------------------------------------------------
# Check box indicator (drawn pixel by pixel so it is always visible)
# ---------------------------------------------------------------------------

_CHECK_POINTS = [(4, 8), (5, 9), (6, 10), (7, 9), (8, 8), (9, 7), (10, 6), (11, 5)]


def _indicator_image(root: Any, box: str, fill: str, mark: Optional[str]) -> Any:
    size = CHECK_SIZE
    img = tk.PhotoImage(master=root, width=size + CHECK_GAP, height=size)
    img.put(fill, to=(0, 0, size, size))
    for i in range(size):
        img.put(box, to=(i, 0, i + 1, 1))
        img.put(box, to=(i, size - 1, i + 1, size))
        img.put(box, to=(0, i, 1, i + 1))
        img.put(box, to=(size - 1, i, size, i + 1))
    if mark:
        for x, y in _CHECK_POINTS:
            img.put(mark, to=(x, y, x + 1, y + 1))
            img.put(mark, to=(x, y + 1, x + 1, y + 2))
    _images.append(img)
    return img


def _build_checkbutton(style: Any, root: Any, p: Dict[str, str], name: str, surface: str) -> None:
    """Create style `name` (e.g. "Card.TCheckbutton") with a 16 px bordered box and an accent check mark."""
    off = _indicator_image(root, p["text_muted"], surface, None)
    off_focus = _indicator_image(root, p["focus"], surface, None)
    on = _indicator_image(root, p["accent"], p["accent"], p["accent_text"])
    on_focus = _indicator_image(root, p["focus"], p["accent"], p["accent_text"])
    off_dis = _indicator_image(root, p["border"], surface, None)
    on_dis = _indicator_image(root, p["border"], p["border"], p["text_muted"])
    element = f"{name}.indicator"
    try:
        style.element_create(
            element, "image", off,
            ("disabled selected", on_dis),
            ("disabled", off_dis),
            ("selected focus", on_focus),
            ("selected", on),
            ("focus", off_focus),
            sticky="w",
        )
    except tk.TclError:
        return  # element already exists (second apply_theme call)
    style.layout(name, [
        ("Checkbutton.padding", {"sticky": "nswe", "children": [
            (element, {"side": "left", "sticky": ""}),
            ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
                ("Checkbutton.label", {"sticky": "nswe"}),
            ]}),
        ]}),
    ])
    style.configure(name, background=surface, foreground=p["text"], padding=(0, 4), focuscolor=surface)
    style.map(name,
              background=[("active", surface), ("disabled", surface)],
              foreground=[("disabled", p["text_muted"]), ("active", p["text"])])


# ---------------------------------------------------------------------------
# apply_theme
# ---------------------------------------------------------------------------

def apply_theme(root: Any) -> Dict[str, str]:
    """Configure fonts, ttk styles and Tk defaults on root; return the palette used."""
    global _current
    import tkinter.font as tkfont

    p = dict(PALETTE)
    _current = p

    base = tkfont.nametofont("TkDefaultFont")
    family = base.cget("family")
    for kind, (size, weight) in FONT_SIZES.items():
        _fonts[kind] = tkfont.Font(root=root, family=family, size=size, weight=weight)
    body, small, section, title = _fonts["body"], _fonts["small"], _fonts["section"], _fonts["title"]

    style = ttk.Style(root)
    style.theme_use("clam")

    root.configure(background=p["bg"])
    root.option_add("*Background", p["bg"])
    root.option_add("*Foreground", p["text"])
    root.option_add("*Font", body)
    root.option_add("*Entry.background", p["surface_alt"])
    root.option_add("*Entry.foreground", p["text"])
    root.option_add("*TCombobox*Listbox.background", p["surface_alt"])
    root.option_add("*TCombobox*Listbox.foreground", p["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", p["accent"])
    root.option_add("*TCombobox*Listbox.selectForeground", p["accent_text"])

    style.configure(
        ".",
        background=p["bg"], foreground=p["text"], fieldbackground=p["surface_alt"],
        bordercolor=p["border"], lightcolor=p["border"], darkcolor=p["border"],
        troughcolor=p["surface"], selectbackground=p["accent"], selectforeground=p["accent_text"],
        insertcolor=p["text"], focuscolor=p["focus"], font=body,
    )

    # Frames and labels
    style.configure("TFrame", background=p["bg"])
    style.configure("Card.TFrame", background=p["surface"], bordercolor=p["border"],
                    lightcolor=p["border"], darkcolor=p["border"], borderwidth=1, relief="solid")
    style.configure("Inner.TFrame", background=p["surface"])
    style.configure("Alt.TFrame", background=p["surface_alt"])
    style.configure("TLabel", background=p["bg"], foreground=p["text"], font=body)
    style.configure("Title.TLabel", background=p["bg"], foreground=p["text"], font=title)
    style.configure("Muted.TLabel", background=p["bg"], foreground=p["text_muted"], font=body)
    style.configure("Small.TLabel", background=p["bg"], foreground=p["text_muted"], font=small)
    style.configure("Card.TLabel", background=p["surface"], foreground=p["text"], font=body)
    style.configure("CardMuted.TLabel", background=p["surface"], foreground=p["text_muted"], font=body)
    style.configure("CardSmall.TLabel", background=p["surface"], foreground=p["text_muted"], font=small)
    style.configure("Section.TLabel", background=p["surface"], foreground=p["text"], font=section)
    style.configure("Sub.TLabel", background=p["surface"], foreground=p["text_muted"], font=small)
    style.configure("Chip.TLabel", background=p["surface_alt"], foreground=p["text"], font=small, padding=(10, 3))
    style.configure("Banner.TFrame", background=p["warning"], bordercolor=p["warning"])
    style.configure("Banner.TLabel", background=p["warning"], foreground=p["bg"], font=body)
    style.configure("BannerTitle.TLabel", background=p["warning"], foreground=p["bg"], font=section)
    style.configure("Footer.TFrame", background=p["bg"])
    style.configure("Status.TLabel", background=p["bg"], foreground=p["text_muted"], font=body)

    # Buttons
    def button(name: str, bg: str, fg: str, border: str, hover: str, pressed: str,
               disabled_bg: str, disabled_fg: str) -> None:
        style.configure(
            name, background=bg, foreground=fg, bordercolor=border, lightcolor=bg, darkcolor=bg,
            focuscolor=bg, focusthickness=0, borderwidth=1, relief="flat", font=body,
            padding=(BUTTON_PAD_X, 8), anchor="center",
        )
        style.map(
            name,
            background=[("disabled", disabled_bg), ("pressed", pressed), ("active", hover)],
            foreground=[("disabled", disabled_fg)],
            bordercolor=[("focus", p["focus"]), ("disabled", disabled_bg)],
            lightcolor=[("focus", p["focus"]), ("pressed", pressed), ("active", hover), ("disabled", disabled_bg)],
            darkcolor=[("focus", p["focus"]), ("pressed", pressed), ("active", hover), ("disabled", disabled_bg)],
        )

    dis_bg, dis_fg = p["surface"], p["text_muted"]
    button("Primary.TButton", p["accent"], p["accent_text"], p["accent"],
           shade(p["accent"], 0.15), shade(p["accent"], -0.18), p["surface_alt"], dis_fg)
    button("Secondary.TButton", p["surface_alt"], p["text"], p["border"],
           shade(p["surface_alt"], 0.10), shade(p["surface_alt"], -0.25), dis_bg, dis_fg)
    button("Danger.TButton", p["danger"], p["bg"], p["danger"],
           shade(p["danger"], 0.15), shade(p["danger"], -0.18), p["surface_alt"], dis_fg)
    button("Ghost.TButton", p["bg"], p["text_muted"], p["bg"],
           p["surface_alt"], p["surface"], p["bg"], p["border"])
    style.map("Ghost.TButton", foreground=[("disabled", p["border"]), ("active", p["text"])])
    # Inside a card the ghost/secondary buttons sit on the surface colour
    button("CardGhost.TButton", p["surface"], p["text_muted"], p["surface"],
           p["surface_alt"], p["bg"], p["surface"], p["border"])
    style.map("CardGhost.TButton", foreground=[("disabled", p["border"]), ("active", p["text"])])
    for variant in ("Secondary", "CardGhost", "Danger"):
        style.configure(f"Small.{variant}.TButton", padding=(10, 4), font=small)

    # Entry and combobox
    style.configure("TEntry", fieldbackground=p["surface_alt"], foreground=p["text"], bordercolor=p["border"],
                    lightcolor=p["border"], darkcolor=p["border"], insertcolor=p["text"], padding=8)
    style.map("TEntry",
              bordercolor=[("focus", p["focus"])], lightcolor=[("focus", p["focus"])],
              darkcolor=[("focus", p["focus"])], fieldbackground=[("disabled", p["surface"])],
              foreground=[("disabled", p["text_muted"])])
    # The arrow button gets its own lighter face and a bright arrow (text on surface_alt is >= 4.5:1).
    combo_face = shade(p["surface_alt"], 0.10)
    style.configure("TCombobox", fieldbackground=p["surface_alt"], foreground=p["text"],
                    background=combo_face, arrowcolor=COMBO_ARROW, arrowsize=18,
                    bordercolor=p["text_muted"], lightcolor=p["border"], darkcolor=p["border"],
                    padding=6, relief="flat")
    style.map("TCombobox",
              fieldbackground=[("readonly", p["surface_alt"]), ("disabled", p["surface"])],
              foreground=[("readonly", p["text"]), ("disabled", p["text_muted"])],
              background=[("disabled", p["surface"]), ("pressed", shade(combo_face, 0.10)),
                          ("active", shade(combo_face, 0.10)), ("readonly", combo_face)],
              selectbackground=[("readonly", p["surface_alt"])],
              selectforeground=[("readonly", p["text"])],
              bordercolor=[("focus", p["focus"]), ("active", p["focus"])],
              arrowcolor=[("disabled", p["text_muted"]), ("pressed", p["focus"]), ("active", p["focus"]),
                          ("readonly", COMBO_ARROW)])

    # Check buttons (custom 16 px indicator)
    style.configure("TCheckbutton", background=p["bg"], foreground=p["text"])
    style.map("TCheckbutton", background=[("active", p["bg"])], foreground=[("disabled", p["text_muted"])])
    _build_checkbutton(style, root, p, "Card.TCheckbutton", p["surface"])
    _build_checkbutton(style, root, p, "Dialog.TCheckbutton", p["bg"])

    # Scrollbar: slim, no arrow buttons
    for orient in ("Vertical", "Horizontal"):
        side = "ns" if orient == "Vertical" else "we"
        style.layout(f"{orient}.TScrollbar", [
            (f"{orient}.Scrollbar.trough", {"sticky": side, "children": [
                (f"{orient}.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"}),
            ]}),
        ])
        style.configure(
            f"{orient}.TScrollbar", background=p["surface_alt"], troughcolor=p["surface"],
            bordercolor=p["border"], lightcolor=p["surface_alt"], darkcolor=p["surface_alt"],
            arrowcolor=p["text_muted"], arrowsize=10, gripcount=0, relief="flat",
        )
        style.map(
            f"{orient}.TScrollbar",
            background=[("pressed", p["border"]), ("active", shade(p["surface_alt"], 0.12))],
        )

    style.configure("TSeparator", background=p["border"])
    return dict(p)


# ---------------------------------------------------------------------------
# Shared widget: vertically scrolling frame
# ---------------------------------------------------------------------------

_WHEEL_SEQUENCES = ("<MouseWheel>", "<Button-4>", "<Button-5>")
_REGISTRY_ATTR = "_intermux_scrollframes"


def _route_wheel(toplevel: Any, event: Any) -> None:
    """Send a wheel or touchpad event to the nearest ScrollFrame that contains its widget."""
    registry = getattr(toplevel, _REGISTRY_ATTR, None) or []
    if not registry:
        return
    widget = getattr(event, "widget", None)
    if isinstance(widget, str):
        try:
            widget = toplevel.nametowidget(widget)
        except (tk.TclError, KeyError, AttributeError):
            return
    while widget is not None:
        for frame in registry:
            if frame is widget:
                frame._wheel(event)
                return
        widget = getattr(widget, "master", None)


class ScrollFrame(tk.Frame if tk is not None else object):  # type: ignore[misc]
    """A frame whose `inner` child scrolls vertically inside a Canvas with a ttk scrollbar."""

    def __init__(self, master: Any, background: str, on_view: Optional[Callable[[], None]] = None) -> None:
        super().__init__(master, background=background, highlightthickness=0, borderwidth=0)
        self._on_view = on_view
        self.canvas = tk.Canvas(self, background=background, highlightthickness=0, borderwidth=0,
                                yscrollincrement=20)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self._yview)
        self.canvas.configure(yscrollcommand=self._set_view)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.inner = tk.Frame(self.canvas, background=background, highlightthickness=0, borderwidth=0)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        self._wheel_target: Any = None
        self._wheel_ids: Dict[str, str] = {}

    def _yview(self, *args: Any) -> None:
        self.canvas.yview(*args)
        if self._on_view:
            self._on_view()

    def _set_view(self, first: str, last: str) -> None:
        self.scrollbar.set(first, last)
        if self._on_view:
            self._on_view()

    def _on_inner(self, _event: Any = None) -> None:
        self.update_region()

    def update_region(self) -> None:
        """Make the whole inner frame reachable by the scrollbar and wheel."""
        try:
            width = max(self.canvas.winfo_width(), 1)
            height = max(self.inner.winfo_reqheight(), 1)
            self.canvas.configure(scrollregion=(0, 0, width, height))
        except tk.TclError:
            pass

    def _on_canvas(self, event: Any) -> None:
        # Only the width follows the canvas; the height is the natural height of all rows.
        self.canvas.itemconfigure(self._window, width=event.width)
        self.update_region()

    def attach_wheel(self, toplevel: Any) -> None:
        """Route wheel and touchpad scrolling to this frame when the pointer is over it.

        One handler per toplevel serves every ScrollFrame in that window. It finds the nearest registered
        frame that contains the widget receiving the event, so each section scrolls independently and an
        event is never applied twice. No bind_all and no pointer queries.
        """
        self.detach_wheel()
        registry = getattr(toplevel, _REGISTRY_ATTR, None)
        if registry is None:
            registry = []
            setattr(toplevel, _REGISTRY_ATTR, registry)
            for sequence in _WHEEL_SEQUENCES:
                toplevel.bind(sequence, lambda e, tl=toplevel: _route_wheel(tl, e), add="+")
        registry.append(self)
        self._wheel_target = toplevel

    def detach_wheel(self) -> None:
        target, self._wheel_target = self._wheel_target, None
        self._wheel_ids = {}
        if target is None:
            return
        registry = getattr(target, _REGISTRY_ATTR, None) or []
        if self in registry:
            registry.remove(self)

    def _notify(self) -> None:
        if self._on_view:
            self._on_view()

    def scroll_units(self, units: int) -> None:
        self.canvas.yview_scroll(units, "units")
        self._clamp()
        self._notify()

    def scroll_pages(self, direction: int) -> None:
        """Scroll by about 80% of the visible height (direction -1 up, +1 down)."""
        first, last = self.canvas.yview()
        span = last - first
        try:
            total = float(self.canvas.cget("scrollregion").split()[3]) or 1.0
        except (tk.TclError, IndexError, ValueError):
            total = 1.0
        step = max(span * 0.8, 20.0 / total)  # at least one scroll unit
        self.scroll_to(first + direction * step)

    def scroll_to(self, fraction: float) -> None:
        first, last = self.canvas.yview()
        span = last - first
        self.canvas.yview_moveto(min(max(fraction, 0.0), max(0.0, 1.0 - span)))
        self._notify()

    def _clamp(self) -> None:
        first, last = self.canvas.yview()
        if first < 0.0:
            self.canvas.yview_moveto(0.0)
        elif last > 1.0:
            self.canvas.yview_moveto(max(0.0, 1.0 - (last - first)))

    def _wheel(self, event: Any) -> None:
        num = getattr(event, "num", 0)
        if num == 4:
            units = -3
        elif num == 5:
            units = 3
        else:
            delta = getattr(event, "delta", 0)
            if not delta:
                return
            units = -3 if delta > 0 else 3
        first, last = self.canvas.yview()
        if first <= 0.0 and last >= 1.0:
            return
        self.scroll_units(units)
