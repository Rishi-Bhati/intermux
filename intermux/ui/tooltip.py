"""Hover tooltips, and labels that shorten their text to fit instead of being cut off."""

from __future__ import annotations

from typing import Any, Callable

from intermux.ui import theme

try:
    import tkinter as tk
except ImportError:  # reported by main()
    tk = None  # type: ignore[assignment]

DELAY_MS = 450


def ellipsize_to(text: str, width: int, measure: Callable[[str], int]) -> str:
    """text, shortened with "…" so that measure(result) <= width. Pure."""
    if width <= 0 or measure(text) <= width:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if measure(text[:mid].rstrip() + "…") <= width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + "…" if lo else "…"


def fit_text(label: Any, text: str, font: Any) -> None:
    """Show text in label, shortened to the label's width; it refits whenever the label is resized.

    The label should be created with width=1 and a sticky "ew" grid, so its size comes from the layout and
    not from the text: then long text is shortened with "…" instead of pushing other widgets out of view.
    """
    label._fit_full = text  # type: ignore[attr-defined]

    def refit(_event: Any = None) -> None:
        try:
            width = label.winfo_width()
        except tk.TclError:
            return
        if width <= 1:
            shown = label._fit_full
        else:
            shown = ellipsize_to(label._fit_full, width - 4, font.measure)
        if label.cget("text") != shown:
            label.configure(text=shown)

    if not getattr(label, "_fit_bound", False):
        label._fit_bound = True  # type: ignore[attr-defined]
        label.bind("<Configure>", refit, add="+")
    refit()


class Tooltip:
    """Shows `text` near the pointer after a short hover. An empty text shows nothing."""

    def __init__(self, widget: Any, text: str = "") -> None:
        self.widget = widget
        self.text = text
        self._after: Any = None
        self._top: Any = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event: Any = None) -> None:
        self._cancel()
        if self.text:
            self._after = self.widget.after(DELAY_MS, self._show)

    def _cancel(self) -> None:
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None

    def _show(self) -> None:
        self._after = None
        if not self.text or self._top is not None:
            return
        p = theme.current_palette()
        try:
            x = self.widget.winfo_pointerx() + 12
            y = self.widget.winfo_pointery() + 16
            top = tk.Toplevel(self.widget)
        except tk.TclError:
            return
        top.wm_overrideredirect(True)
        top.configure(background=p["line_hi"])
        tk.Label(top, text=self.text, background=p["surface_alt"], foreground=p["text"], font=theme.font("small"),
                 justify="left", wraplength=420, padx=8, pady=5).pack(padx=1, pady=1)
        top.wm_geometry(f"+{x}+{y}")
        self._top = top

    def _hide(self, _event: Any = None) -> None:
        self._cancel()
        if self._top is not None:
            try:
                self._top.destroy()
            except tk.TclError:
                pass
            self._top = None
