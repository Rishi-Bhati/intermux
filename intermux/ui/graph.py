"""Small drawing helpers: colour mixing, sample-to-point mapping for the inline speed graphs, and the
interface glyph used by the VPN dialog."""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

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


def kind_for(iface: Any) -> str:
    """Glyph kind for an Interface: VPN tunnels get their own shield. Pure."""
    name = getattr(iface, "name", "") or ""
    if name.startswith(("tun", "tap", "wg", "ppp", "tailscale", "nordlynx", "proton")):
        return "vpn"
    return getattr(iface, "kind", "other") or "other"
