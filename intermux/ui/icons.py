"""Application icons: resolve a .desktop Icon= value to a PNG, load it for Tk, or draw a letter tile."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from intermux.ui import theme

log = logging.getLogger("intermux.ui")

ICON_SIZES = ("128x128", "64x64", "48x48", "256x256", "scalable")
PIXMAPS = Path("/usr/share/pixmaps")

_cache: Dict[Tuple[str, int], Any] = {}


def default_icon_dirs() -> List[Path]:
    """XDG icon base directories, highest priority first."""
    home = Path(os.path.expanduser("~"))
    data_home = os.environ.get("XDG_DATA_HOME") or str(home / ".local" / "share")
    candidates = [
        Path(data_home) / "icons",
        home / ".local" / "share" / "icons",
        Path("/usr/share/icons"),
        PIXMAPS,
        Path("/var/lib/flatpak/exports/share/icons"),
    ]
    unique: List[Path] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def resolve_icon_path(icon_name: str, search_dirs: Optional[Sequence[Path]] = None) -> Optional[Path]:
    """Return an existing PNG for icon_name, or None. Pure filesystem lookup, no Tk."""
    name = (icon_name or "").strip()
    if not name:
        return None
    if os.path.isabs(name):
        path = Path(name)
        return path if path.is_file() else None
    if name.lower().endswith(".png"):
        name = name[:-4]
    dirs = default_icon_dirs() if search_dirs is None else [Path(d) for d in search_dirs]
    filename = f"{name}.png"
    for base in dirs:
        for size in ICON_SIZES:
            candidate = base / "hicolor" / size / "apps" / filename
            if candidate.is_file():
                return candidate
        candidate = base / filename  # /usr/share/pixmaps style
        if candidate.is_file():
            return candidate
    if PIXMAPS not in dirs and search_dirs is None:
        candidate = PIXMAPS / filename
        if candidate.is_file():
            return candidate
    return None


def load_tk_icon(master: Any, path: Any, size: int = 48) -> Any:
    """Load a PNG as a PhotoImage scaled near `size`. Tk thread only. None on any error."""
    if path is None:
        return None
    key = (str(path), size)
    if key in _cache:
        return _cache[key]
    image = None
    try:
        import tkinter as tk

        image = tk.PhotoImage(master=master, file=str(path))
        width = max(image.width(), 1)
        if width > size:
            image = image.subsample(max(1, round(width / size)))
        elif width * 2 <= size + size // 4:
            image = image.zoom(max(1, size // width))
    except Exception as exc:  # tk.TclError, MemoryError, odd PNGs
        log.debug("cannot load icon %s: %s", path, exc)
        image = None
    _cache[key] = image  # None is cached too so broken files are not retried
    return image


def letter_tile(master: Any, text: str, size: int, colour: Optional[str] = None,
                background: Optional[str] = None, foreground: Optional[str] = None) -> Any:
    """A rounded-looking square Canvas with the first letter of `text` (palette colours by default)."""
    import tkinter as tk
    import tkinter.font as tkfont

    p = theme.PALETTE
    colour = colour or p["surface_alt"]
    background = background or p["surface"]
    foreground = foreground or p["text"]

    canvas = tk.Canvas(master, width=size, height=size, background=background,
                       highlightthickness=0, borderwidth=0)
    radius = max(4, size // 6)
    x0, y0, x1, y1 = 1, 1, size - 1, size - 1
    points = [x0 + radius, y0, x1 - radius, y0, x1, y0, x1, y0 + radius,
              x1, y1 - radius, x1, y1, x1 - radius, y1, x0 + radius, y1,
              x0, y1, x0, y1 - radius, x0, y0 + radius, x0, y0]
    canvas.create_polygon(points, smooth=True, fill=colour, outline=colour)
    letter = (text.strip()[:1] or "?").upper()
    letter_font = tkfont.Font(root=master, family=theme.font("body").cget("family"),
                              size=max(8, size // 3), weight="bold")
    canvas._letter_font = letter_font  # type: ignore[attr-defined]  # keep the font alive
    canvas.create_text(size // 2, size // 2, text=letter, fill=foreground, font=letter_font)
    return canvas
