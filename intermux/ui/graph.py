"""Canvas drawings for the network view: live traffic graphs, sparklines, interface glyphs and the logo.

The traffic graph follows the website's monitor: a line over a fill that fades toward the bottom,
faint gridlines at 25/50/75 %, a dashed ceiling with its value, and a dot on the newest sample.
Tk has no alpha, so the fade is a cached gradient image with the area above the line painted over.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

try:
    import tkinter as tk
except ImportError:  # reported by main()
    tk = None  # type: ignore[assignment]

from intermux.ui import theme


def mix(colour: str, other: str, amount: float) -> str:
    """Blend colour toward other by amount (0..1). Pure."""
    a = theme._rgb(colour)
    b = theme._rgb(other)
    t = min(max(amount, 0.0), 1.0)
    return "#{:02X}{:02X}{:02X}".format(*(round(x + (y - x) * t) for x, y in zip(a, b)))


def series_points(values: Sequence[float], width: float, top: float, bottom: float,
                  scale: float) -> List[Tuple[float, float]]:
    """Map samples (oldest first) onto x from 0 to width and y from bottom (0) to top (scale). Pure."""
    n = len(values)
    if n == 0:
        return []
    step = width / max(n - 1, 1)
    span = bottom - top
    scale = scale if scale > 0 else 1.0
    points = []
    for index, value in enumerate(values):
        fraction = min(max(value / scale, 0.0), 1.0)
        points.append((index * step, bottom - fraction * span))
    return points


_gradients: Dict[Tuple[int, int, str, str], Any] = {}


def _gradient(master: Any, width: int, height: int, colour: str, background: str) -> Any:
    """A width x height image fading from 30 % colour at the top to the background at the bottom."""
    key = (width, height, colour, background)
    image = _gradients.get(key)
    if image is not None:
        return image
    if len(_gradients) > 64:
        _gradients.clear()
    image = tk.PhotoImage(master=master, width=width, height=height)
    for y in range(height):
        fade = 0.30 * (1.0 - y / max(height - 1, 1)) ** 1.4
        image.put(mix(background, colour, fade), to=(0, y, width, y + 1))
    _gradients[key] = image
    return image


class TrafficGraph:
    """A live download/upload graph on a Canvas. Call update() with samples oldest-first."""

    TOP = 18      # room for the ceiling label
    PAD_BOTTOM = 2

    def __init__(self, master: Any, background: str, height: int = 64, show_upload: bool = True,
                 show_label: bool = True) -> None:
        p = theme.current_palette()
        self.p = p
        self.background = background
        self.show_upload = show_upload
        self.show_label = show_label
        self.canvas = tk.Canvas(master, background=background, height=height, highlightthickness=0,
                                borderwidth=0)
        self._down: List[float] = []
        self._up: List[float] = []
        self._scale = 1.0
        self._colour = p["text_muted"]
        self._label = ""
        self.canvas.bind("<Configure>", lambda _e: self.redraw())

    def update(self, down: Sequence[float], up: Sequence[float], scale: float, colour: str, label: str) -> None:
        self._down = list(down)
        self._up = list(up)
        self._scale = scale
        self._colour = colour
        self._label = label
        self.redraw()

    def redraw(self) -> None:
        c = self.canvas
        try:
            width = c.winfo_width()
            height = c.winfo_height()
        except tk.TclError:
            return
        if width < 8 or height < 8:
            return
        c.delete("all")
        p = self.p
        top = self.TOP if self.show_label else 4
        bottom = height - self.PAD_BOTTOM
        colour = self._colour
        down = series_points(self._down, width - 6, top, bottom, self._scale)

        if len(down) > 1:
            image = _gradient(c, width, height, colour, self.background)
            c.create_image(0, 0, image=image, anchor="nw")
            # Paint the background over everything above the line, leaving the fade below it.
            cover = [(0, 0)] + down + [(down[-1][0], 0)]
            c.create_polygon(*[v for pt in cover for v in pt], fill=self.background, outline="")
            c.create_rectangle(down[-1][0], 0, width, height, fill=self.background, outline="")

        grid = mix(self.background, "#FFFFFF", 0.05)
        for fraction in (0.25, 0.5, 0.75):
            y = round(bottom - fraction * (bottom - top)) + 0.5
            c.create_line(0, y, width, y, fill=grid)
        c.create_line(0, bottom + 0.5, width, bottom + 0.5, fill=mix(self.background, "#FFFFFF", 0.09))
        c.create_line(0, top + 0.5, width, top + 0.5, fill=mix(self.background, "#FFFFFF", 0.2), dash=(4, 4))
        if self.show_label and self._label:
            c.create_text(width - 2, top - 2, text=self._label, anchor="se", fill=p["text_muted"],
                          font=theme.font("eyebrow"))

        if self.show_upload and len(self._up) > 1:
            up = series_points(self._up, width - 6, top, bottom, self._scale)
            c.create_line(*[v for pt in up for v in pt], fill=p["violet"], width=1.25)
        if len(down) > 1:
            c.create_line(*[v for pt in down for v in pt], fill=colour, width=2, joinstyle="round",
                          capstyle="round")
            x, y = down[-1]
            c.create_oval(x - 5, y - 5, x + 5, y + 5, fill=self.background, outline="")
            c.create_oval(x - 3.2, y - 3.2, x + 3.2, y + 3.2, fill=colour, outline="")


class Sparkline:
    """A small line without axes, for stat tiles and app cards."""

    def __init__(self, master: Any, background: str, width: int = 120, height: int = 28) -> None:
        self.background = background
        self.canvas = tk.Canvas(master, background=background, width=width, height=height,
                                highlightthickness=0, borderwidth=0)
        self._values: List[float] = []
        self._scale = 1.0
        self._colour = "#868B91"
        self.canvas.bind("<Configure>", lambda _e: self.redraw())

    def update(self, values: Sequence[float], scale: float, colour: str) -> None:
        self._values = list(values)
        self._scale = scale
        self._colour = colour
        self.redraw()

    def redraw(self) -> None:
        c = self.canvas
        try:
            width = c.winfo_width()
            height = c.winfo_height()
        except tk.TclError:
            return
        if width < 8 or height < 6:
            return
        c.delete("all")
        points = series_points(self._values, width - 4, 3, height - 2, self._scale)
        if len(points) > 1:
            image = _gradient(c, width, height, self._colour, self.background)
            c.create_image(0, 0, image=image, anchor="nw")
            cover = [(0, 0)] + points + [(points[-1][0], 0)]
            c.create_polygon(*[v for pt in cover for v in pt], fill=self.background, outline="")
            c.create_rectangle(points[-1][0], 0, width, height, fill=self.background, outline="")
            c.create_line(*[v for pt in points for v in pt], fill=self._colour, width=1.5)
            x, y = points[-1]
            c.create_oval(x - 2.5, y - 2.5, x + 2.5, y + 2.5, fill=self._colour, outline="")


# ---------------------------------------------------------------------------
# Glyphs
# ---------------------------------------------------------------------------

def draw_kind_glyph(c: Any, kind: str, x: float, y: float, size: float, colour: str, background: str) -> None:
    """Draw a simple line icon for an interface kind, centred on (x, y)."""
    s = size / 24.0
    w = max(1.6, 1.8 * s)

    def P(px: float, py: float) -> Tuple[float, float]:
        return x + (px - 12) * s, y + (py - 12) * s

    def line(*pts: Tuple[float, float], **kw: Any) -> None:
        flat = [v for pt in pts for v in P(*pt)]
        c.create_line(*flat, fill=colour, width=w, capstyle="round", joinstyle="round", **kw)

    if kind == "wifi":
        for r in (10, 6.5, 3):
            x0, y0 = P(12 - r, 19 - r)
            x1, y1 = P(12 + r, 19 + r)
            c.create_arc(x0, y0, x1, y1, start=45, extent=90, style="arc", outline=colour, width=w)
        cx, cy = P(12, 18.6)
        c.create_oval(cx - 1.6 * s, cy - 1.6 * s, cx + 1.6 * s, cy + 1.6 * s, fill=colour, outline="")
    elif kind == "ethernet":
        x0, y0 = P(3, 13)
        x1, y1 = P(21, 20)
        c.create_rectangle(x0, y0, x1, y1, outline=colour, width=w)
        for px in (7, 10.5, 14, 17.5):
            line((px, 16.5), (px, 16.6))
        line((12, 13), (12, 8))
        line((7, 8), (17, 8))
        line((7, 8), (7, 4))
        line((17, 8), (17, 4))
    elif kind == "usb":
        line((12, 3), (12, 18))
        line((12, 3), (10, 6))
        line((12, 3), (14, 6))
        line((12, 13), (7, 10), (7, 8))
        line((12, 11), (17, 8))
        x0, y0 = P(16, 6)
        x1, y1 = P(18.5, 8.5)
        c.create_rectangle(x0, y0, x1, y1, outline=colour, width=w)
        cx, cy = P(12, 19.5)
        c.create_oval(cx - 2.2 * s, cy - 2.2 * s, cx + 2.2 * s, cy + 2.2 * s, outline=colour, width=w)
        cx, cy = P(7, 7.2)
        c.create_oval(cx - 1.4 * s, cy - 1.4 * s, cx + 1.4 * s, cy + 1.4 * s, fill=colour, outline="")
    elif kind == "bluetooth":
        line((7, 7), (17, 16), (12, 21), (12, 3), (17, 8), (7, 17))
    elif kind == "mobile":
        for index, height in enumerate((5, 9, 13, 17)):
            px = 5 + index * 4.6
            x0, y0 = P(px, 20 - height)
            x1, y1 = P(px + 2.8, 20)
            c.create_rectangle(x0, y0, x1, y1, fill=colour, outline="")
    elif kind == "vpn":
        line((12, 3), (19, 6), (19, 12), (12, 21), (5, 12), (5, 6), (12, 3))
        line((9, 12), (11.3, 14.3), (15.5, 9.5))
    else:
        x0, y0 = P(3.5, 3.5)
        x1, y1 = P(20.5, 20.5)
        c.create_oval(x0, y0, x1, y1, outline=colour, width=w)
        line((3.5, 12), (20.5, 12))
        x0, y0 = P(8, 3.5)
        x1, y1 = P(16, 20.5)
        c.create_oval(x0, y0, x1, y1, outline=colour, width=w)


def glyph(master: Any, kind: str, size: int, colour: str, background: str) -> Any:
    """A Canvas showing an interface glyph."""
    c = tk.Canvas(master, width=size, height=size, background=background, highlightthickness=0, borderwidth=0)
    draw_kind_glyph(c, kind, size / 2, size / 2, size - 2, colour, background)
    return c


def draw_logo(c: Any, x: float, y: float, size: float) -> None:
    """The InterMux mark (website/public/brand/mark.svg) at (x, y), size pixels square."""
    p = theme.current_palette()
    s = size / 64.0

    def P(px: float, py: float) -> Tuple[float, float]:
        return x + px * s, y + py * s

    w = 8 * s
    c.create_line(*P(12, 52), *P(12, 12), fill=p["text_muted"], width=w, capstyle="round")
    c.create_line(*P(52, 52), *P(52, 12), fill=p["text_muted"], width=w, capstyle="round")
    c.create_line(*P(12, 12), *P(32, 36), *P(52, 12), fill=p["accent"], width=w, capstyle="round",
                  joinstyle="round")
    for cx, cy, r in ((12, 12, 8), (52, 12, 8), (32, 36, 9.5)):
        x0, y0 = P(cx - r, cy - r)
        x1, y1 = P(cx + r, cy + r)
        c.create_oval(x0, y0, x1, y1, fill=p["accent"], outline="")
    for rx in (4, 44):
        x0, y0 = P(rx, 44)
        x1, y1 = P(rx + 16, 60)
        c.create_rectangle(x0, y0, x1, y1, fill=p["text"], outline="")


def logo(master: Any, size: int, background: str) -> Any:
    c = tk.Canvas(master, width=size, height=size, background=background, highlightthickness=0, borderwidth=0)
    draw_logo(c, 0, 0, size)
    return c


def status_dot(master: Any, colour: str, background: str, size: int = 8) -> Any:
    c = tk.Canvas(master, width=size, height=size, background=background, highlightthickness=0, borderwidth=0)
    c.create_oval(0, 0, size - 1, size - 1, fill=colour, outline="", tags=("dot",))
    return c


def set_dot(c: Any, colour: str) -> None:
    c.itemconfigure("dot", fill=colour)


def kind_for(iface: Any) -> str:
    """Glyph kind for an Interface: VPN tunnels get their own shield. Pure."""
    name = getattr(iface, "name", "") or ""
    if name.startswith(("tun", "tap", "wg", "ppp", "tailscale", "nordlynx", "proton")):
        return "vpn"
    return getattr(iface, "kind", "other") or "other"
