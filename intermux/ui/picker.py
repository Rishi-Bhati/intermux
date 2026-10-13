"""Application picker: a searchable grid of installed desktop applications with icons."""

from __future__ import annotations

import logging
import tkinter as tk
from tkinter import filedialog, ttk
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from intermux.apps import desktop_entries
from intermux.apps.desktop_entries import AppEntry
from intermux.ui import icons, theme
from intermux.ui.worker import Worker

log = logging.getLogger("intermux.ui")

TILE_W = 136
TILE_H = 132
TILE_GAP = 12
MIN_COLUMNS = 4
ICON_SIZE = 48
PLACEHOLDER = "Search applications"


# ---------------------------------------------------------------------------
# Pure helpers (no Tk, unit tested)
# ---------------------------------------------------------------------------

def column_count(width: int, tile_width: int = TILE_W, gap: int = TILE_GAP, minimum: int = MIN_COLUMNS) -> int:
    """How many tiles of tile_width fit in width pixels with gap between them (never below minimum)."""
    fit = (int(width) + gap) // (tile_width + gap)
    return max(minimum, fit)


def filter_tiles(entries: Sequence[AppEntry], query: str) -> List[AppEntry]:
    """Entries matching query, in the original order. An empty query returns everything."""
    return desktop_entries.search_entries(entries, query)


def _safe_focus(widget: Any) -> Any:
    """focus_get() that returns None instead of raising (Tk raises KeyError for combobox popdowns)."""
    try:
        return widget.focus_get()
    except (KeyError, tk.TclError):
        return None


def ellipsize(text: str, max_width: int, measure: Callable[[str], int]) -> str:
    """Shorten text with an ellipsis so that measure(result) <= max_width."""
    if measure(text) <= max_width:
        return text
    ellipsis = "…"
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if measure(text[:mid].rstrip() + ellipsis) <= max_width:
            low = mid
        else:
            high = mid - 1
    return text[:low].rstrip() + ellipsis if low else ellipsis


def visible_indices(top: float, height: float, count: int, columns: int,
                    row_height: int = TILE_H + TILE_GAP, margin_rows: int = 1) -> Tuple[int, int]:
    """Half-open range of tile indexes whose rows intersect [top, top + height], plus margin_rows each side."""
    if count <= 0 or columns <= 0:
        return 0, 0
    first_row = max(0, int(top // row_height) - margin_rows)
    last_row = int((top + height) // row_height) + margin_rows
    return min(count, first_row * columns), min(count, (last_row + 1) * columns)


def caption_for(entry: AppEntry) -> str:
    """Second line of a tile: the reason when the app cannot be bound, else its description."""
    return entry.reason if not entry.bindable else entry.comment


# ---------------------------------------------------------------------------
# Picker dialog
# ---------------------------------------------------------------------------

class _Tile:
    """One grid cell. Widgets are created up front; the icon is loaded lazily."""

    def __init__(self, picker: "AppPicker", parent: Any, index: int, entry: AppEntry) -> None:
        p = picker.palette
        self.index = index
        self.entry = entry
        self.icon_loaded = False
        self.image: Any = None
        self.frame = tk.Frame(parent, width=TILE_W, height=TILE_H, background=p["surface"],
                              highlightthickness=1, highlightbackground=p["border"],
                              highlightcolor=p["focus"], takefocus=0)
        self.frame.pack_propagate(False)
        muted = not entry.bindable
        name_colour = p["text_muted"] if muted else p["text"]
        body, small = theme.font("body"), theme.font("small")
        self.icon_host = tk.Frame(self.frame, width=ICON_SIZE, height=ICON_SIZE, background=p["surface"])
        self.icon_host.pack(pady=(12, 8))
        self.icon_host.pack_propagate(False)
        self.name = tk.Label(self.frame, text=ellipsize(entry.name, TILE_W - 16, body.measure),
                             background=p["surface"], foreground=name_colour, font=body)
        self.name.pack()
        self.caption = tk.Label(self.frame, text=ellipsize(caption_for(entry), TILE_W - 16, small.measure),
                                background=p["surface"], foreground=p["text_muted"], font=small)
        self.caption.pack()
        for widget in (self.frame, self.icon_host, self.name, self.caption):
            widget.bind("<Button-1>", lambda _e: picker.select(self.index))
            widget.bind("<Double-Button-1>", lambda _e: picker.use_index(self.index))
            widget.bind("<Enter>", lambda _e: picker.hover(self.index))
            widget.bind("<Leave>", lambda _e: picker.hover(None))

    def set_selected(self, selected: bool, palette: Dict[str, str]) -> None:
        colour = palette["accent"] if selected else palette["border"]
        self.frame.configure(highlightbackground=colour, highlightthickness=2 if selected else 1,
                             background=palette["surface_alt"] if selected else palette["surface"])
        for widget in (self.icon_host, self.name, self.caption):
            widget.configure(background=palette["surface_alt"] if selected else palette["surface"])

    def load_icon(self, picker: "AppPicker") -> None:
        if self.icon_loaded:
            return
        self.icon_loaded = True
        p = picker.palette
        path = icons.resolve_icon_path(self.entry.icon) if self.entry.icon else None
        image = icons.load_tk_icon(picker, path, ICON_SIZE)
        if image is not None:
            self.image = image
            holder = tk.Label(self.icon_host, image=image, borderwidth=0,
                              background=self.icon_host.cget("background"))
            holder.image = image  # type: ignore[attr-defined]
        else:
            holder = icons.letter_tile(self.icon_host, self.entry.name, ICON_SIZE,
                                       background=self.icon_host.cget("background"),
                                       colour=p["border"] if self.entry.bindable else p["surface_alt"],
                                       foreground=p["text"] if self.entry.bindable else p["text_muted"])
        holder.place(relx=0.5, rely=0.5, anchor="center")
        for event, handler in (("<Button-1>", lambda _e: picker.select(self.index)),
                               ("<Double-Button-1>", lambda _e: picker.use_index(self.index)),
                               ("<Enter>", lambda _e: picker.hover(self.index)),
                               ("<Leave>", lambda _e: picker.hover(None))):
            holder.bind(event, handler)


class AppPicker(tk.Toplevel):
    """Modal grid of desktop applications. Calls on_choose(binary_path) for the chosen one.

    on_entry, when given, is called first with the chosen AppEntry (None when a file was browsed).
    """

    def __init__(self, master: Any, on_choose: Callable[[str], None], worker: Optional[Worker] = None,
                 on_entry: Optional[Callable[[Optional[AppEntry]], None]] = None) -> None:
        super().__init__(master)
        self.palette = theme.current_palette()
        p = self.palette
        self._on_choose = on_choose
        self._on_entry = on_entry
        self._owns_worker = worker is None
        self._worker = worker or Worker(self)
        self._entries: List[AppEntry] = []
        self._visible: List[AppEntry] = []
        self._tiles: List[_Tile] = []
        self._selected: Optional[int] = None
        self._columns = 0
        self._loaded = False
        self._closed = False

        self.title("Choose application")
        self.configure(background=p["bg"])
        self.transient(master)
        self.minsize(4 * (TILE_W + TILE_GAP) + 80, 420)
        self._center(master, 820, 580)

        outer = tk.Frame(self, background=p["bg"], padx=theme.SPACE_OUTER, pady=theme.SPACE_OUTER)
        outer.pack(fill="both", expand=True)
        outer.rowconfigure(1, weight=1)
        outer.columnconfigure(0, weight=1)

        top = tk.Frame(outer, background=p["bg"])
        top.grid(row=0, column=0, sticky="ew", pady=(0, theme.SPACE_INNER))
        top.columnconfigure(0, weight=1)
        self._query = tk.StringVar(self)
        self._search = ttk.Entry(top, textvariable=self._query)
        self._search.grid(row=0, column=0, sticky="ew")
        self._placeholder = tk.Label(self._search, text=PLACEHOLDER, background=p["surface_alt"],
                                     foreground=p["text_muted"], font=theme.font("body"), cursor="xterm")
        self._placeholder.place(x=10, rely=0.5, anchor="w")
        self._placeholder.bind("<Button-1>", lambda _e: self._search.focus_set())
        self._count_var = tk.StringVar(self, value="")
        ttk.Label(top, textvariable=self._count_var, style="Muted.TLabel").grid(
            row=0, column=1, padx=(theme.SPACE_INNER, 0))

        card = tk.Frame(outer, background=p["surface"], highlightthickness=1, highlightbackground=p["border"])
        card.grid(row=1, column=0, sticky="nsew")
        self._scroll = theme.ScrollFrame(card, p["surface"], on_view=self._load_visible_icons)
        self._scroll.pack(fill="both", expand=True, padx=theme.SPACE_INNER, pady=theme.SPACE_INNER)
        self._scroll.canvas.bind("<Configure>", self._on_canvas_configure, add="+")
        self._grid_host = self._scroll.inner
        self._loading = tk.Label(self._grid_host, text="Loading applications...", background=p["surface"],
                                 foreground=p["text_muted"], font=theme.font("body"), pady=40)
        self._loading.grid(row=0, column=0, sticky="w", padx=theme.SPACE_INNER)

        self._hint_var = tk.StringVar(self, value="")
        ttk.Label(outer, textvariable=self._hint_var, style="Muted.TLabel").grid(
            row=2, column=0, sticky="w", pady=(theme.SPACE_INNER, 0))

        buttons = tk.Frame(outer, background=p["bg"])
        buttons.grid(row=3, column=0, sticky="ew", pady=(theme.SPACE_INNER, 0))
        ttk.Button(buttons, text="Browse for a file...", style="Secondary.TButton",
                   command=self._browse).pack(side="left")
        self._use_btn = ttk.Button(buttons, text="Use", style="Primary.TButton", command=self._use_selected)
        self._use_btn.pack(side="right")
        ttk.Button(buttons, text="Cancel", style="Ghost.TButton", command=self._close).pack(
            side="right", padx=(0, theme.SPACE_INNER))
        self._use_btn.state(["disabled"])

        self._query.trace_add("write", lambda *_: self._on_query())
        self._search.bind("<FocusIn>", lambda _e: self._sync_placeholder())
        self._search.bind("<FocusOut>", lambda _e: self._sync_placeholder())
        self.bind("<Escape>", lambda _e: self._close())
        self.bind("<Return>", self._on_return)
        self.bind("<Down>", lambda _e: self._move(self._columns or 1))
        self.bind("<Up>", lambda _e: self._move(-(self._columns or 1)))
        self.bind("<Left>", lambda _e: self._move(-1) if _safe_focus(self) is not self._search else None)
        self.bind("<Right>", lambda _e: self._move(1) if _safe_focus(self) is not self._search else None)
        self.bind("<Prior>", lambda _e: self._scroll.scroll_pages(-1))
        self.bind("<Next>", lambda _e: self._scroll.scroll_pages(1))
        self.bind("<Home>", lambda _e: self._scroll.scroll_to(0.0) if _safe_focus(self) is not self._search else None)
        self.bind("<End>", lambda _e: self._scroll.scroll_to(1.0) if _safe_focus(self) is not self._search else None)
        self._scroll.attach_wheel(self)
        self.protocol("WM_DELETE_WINDOW", self._close)

        self._search.focus_set()
        self.after_idle(self._grab)
        self._worker.submit(desktop_entries.scan_desktop_entries, on_done=self._on_scanned)

    # -- window helpers -----------------------------------------------------

    def _center(self, master: Any, width: int, height: int) -> None:
        try:
            master.update_idletasks()
            x = master.winfo_rootx() + max(0, (master.winfo_width() - width) // 2)
            y = master.winfo_rooty() + max(0, (master.winfo_height() - height) // 3)
        except tk.TclError:
            x, y = 100, 100
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _grab(self) -> None:
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._scroll.detach_wheel()
        if self._owns_worker:
            self._worker.shutdown()
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()

    # -- loading and filtering ----------------------------------------------

    def _on_scanned(self, result: Any, error: Optional[BaseException]) -> None:
        if self._closed or not self.winfo_exists():
            return
        self._loaded = True
        self._loading.destroy()
        if error is not None:
            log.info("desktop scan failed: %s", error)
            self._entries = []
        else:
            self._entries = list(result)
        self._rebuild()

    def _on_query(self) -> None:
        self._sync_placeholder()
        if self._loaded:
            self._rebuild()

    def _sync_placeholder(self) -> None:
        if self._query.get():
            self._placeholder.place_forget()
        else:
            self._placeholder.place(x=10, rely=0.5, anchor="w")

    def _rebuild(self) -> None:
        query = self._query.get().strip()
        self._visible = filter_tiles(self._entries, query)
        for tile in self._tiles:
            tile.frame.destroy()
        self._tiles = []
        self._selected = None
        for child in self._grid_host.winfo_children():
            child.destroy()
        for index, entry in enumerate(self._visible):
            self._tiles.append(_Tile(self, self._grid_host, index, entry))
        total = len(self._visible)
        self._count_var.set(f"{total} application{'s' if total != 1 else ''}")
        if not self._visible:
            tk.Label(self._grid_host, text="No applications match your search." if self._entries
                     else "No desktop applications were found. Use Browse for a file...",
                     background=self.palette["surface"], foreground=self.palette["text_muted"],
                     font=theme.font("body"), pady=40).grid(row=0, column=0, sticky="w", padx=theme.SPACE_INNER)
        self._columns = 0
        self._layout(force=True)
        self._scroll.canvas.yview_moveto(0)
        self._update_hint()

    def _on_canvas_configure(self, _event: Any) -> None:
        if self._loaded:
            self._layout()

    def _layout(self, force: bool = False) -> None:
        width = max(self._scroll.canvas.winfo_width(), 1)
        columns = column_count(width)
        if columns == self._columns and not force:
            return
        self._columns = columns
        for tile in self._tiles:
            row, col = divmod(tile.index, columns)
            tile.frame.grid(row=row, column=col, padx=(0, TILE_GAP), pady=(0, TILE_GAP), sticky="nw")
        self._scroll.update_idletasks()
        self._scroll.update_region()
        self.after_idle(self._load_visible_icons)

    def _load_visible_icons(self) -> None:
        if self._closed or not self._tiles:
            return
        try:
            canvas = self._scroll.canvas
            top = canvas.canvasy(0)
            start, end = visible_indices(top, canvas.winfo_height(), len(self._tiles), self._columns or 1)
        except tk.TclError:
            return
        for tile in self._tiles[start:end]:
            tile.load_icon(self)

    # -- selection ----------------------------------------------------------

    def select(self, index: Optional[int]) -> None:
        if self._selected is not None and self._selected < len(self._tiles):
            self._tiles[self._selected].set_selected(False, self.palette)
        self._selected = index
        if index is not None and 0 <= index < len(self._tiles):
            self._tiles[index].set_selected(True, self.palette)
            self._tiles[index].load_icon(self)
            self._ensure_visible(index)
        entry = self._selected_entry()
        usable = entry is not None and entry.bindable and bool(entry.binary)
        self._use_btn.state(["!disabled"] if usable else ["disabled"])
        self._update_hint()

    def _ensure_visible(self, index: int) -> None:
        columns = self._columns or 1
        row = index // columns
        row_h = TILE_H + TILE_GAP
        canvas = self._scroll.canvas
        region = canvas.bbox("all")
        if not region:
            return
        total = max(region[3] - region[1], 1)
        top, height = canvas.canvasy(0), canvas.winfo_height()
        y0, y1 = row * row_h, row * row_h + TILE_H
        if y0 < top:
            canvas.yview_moveto(y0 / total)
        elif y1 > top + height:
            canvas.yview_moveto((y1 - height + TILE_GAP) / total)

    def _move(self, delta: int) -> None:
        if not self._tiles:
            return
        if self._selected is None:
            target = 0
        else:
            target = min(max(self._selected + delta, 0), len(self._tiles) - 1)
        self.select(target)
        if _safe_focus(self) is self._search and delta in (self._columns, -self._columns):
            self.focus_set()

    def _selected_entry(self) -> Optional[AppEntry]:
        if self._selected is None or self._selected >= len(self._visible):
            return None
        return self._visible[self._selected]

    def hover(self, index: Optional[int]) -> None:
        if index is None or index >= len(self._visible):
            self._update_hint()
            return
        entry = self._visible[index]
        self._hint_var.set(self._describe(entry))

    @staticmethod
    def _describe(entry: AppEntry) -> str:
        if not entry.bindable:
            return f"{entry.name} cannot be bound: {entry.reason}"
        return f"{entry.name} - {entry.comment}" if entry.comment else (entry.binary or entry.name)

    def _update_hint(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            self._hint_var.set("Click an application, then press Enter or double-click to use it.")
        else:
            self._hint_var.set(self._describe(entry))

    def _on_return(self, _event: Any) -> None:
        if isinstance(_safe_focus(self), ttk.Button):
            return  # Enter on a focused button activates that button instead
        self._use_selected()

    # -- choosing -------------------------------------------------------------

    def use_index(self, index: int) -> None:
        self.select(index)
        self._use_selected()

    def _use_selected(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        if not entry.bindable or not entry.binary:
            self._update_hint()
            return
        self._choose(entry.binary, entry)

    def _browse(self) -> None:
        path = filedialog.askopenfilename(parent=self, title="Choose application binary")
        if path:
            self._choose(path, None)

    def _choose(self, path: str, entry: Optional[AppEntry]) -> None:
        self._close()
        if self._on_entry is not None:
            self._on_entry(entry)
        self._on_choose(path)
