"""Report issue section: save a bug report zip, or open the reporting portal."""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Optional

from intermux import bugreport, report
from intermux.errors import InterMuxError
from intermux.system import commands
from intermux.ui import theme
from intermux.ui.worker import Worker


LINK_CHARS_PER_LINE = 48  # the link box wraps the URL at this many characters


class ReportDialog(tk.Toplevel):
    """Two actions: save a zip with app logs and system info, and open the web portal."""

    def __init__(self, master: Any, worker: Worker) -> None:
        super().__init__(master)
        self.worker = worker
        self.saved_path: Optional[Path] = None
        self.title("Report issue")
        self.configure(background=theme.current_palette()["bg"])
        self.transient(master)
        self.resizable(False, False)
        self._status = tk.StringVar(self, value="")
        self._build()
        self.bind("<Escape>", lambda _e: self.destroy())

    def _build(self) -> None:
        body = ttk.Frame(self, padding=theme.SPACE_OUTER)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text="Report an issue", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            body,
            text=("Save a bug report zip with your app logs and system details. "
                  "Then open the reporting portal and upload the zip there with your description."),
            wraplength=440, justify="left",
        ).pack(anchor="w", pady=(theme.SPACE_INNER, theme.SPACE_CARD_GAP))

        buttons = ttk.Frame(body)
        buttons.pack(fill="x")
        self.save_btn = ttk.Button(buttons, text="Save bug report", style="Primary.TButton",
                                   command=self._save)
        self.save_btn.pack(side="left")
        ttk.Button(buttons, text="Open reporting portal", style="Secondary.TButton", command=self._open_portal).pack(side="left", padx=(8, 0))
        self.folder_btn = ttk.Button(buttons, text="Open folder", style="Ghost.TButton", command=self._open_folder, state="disabled")
        self.folder_btn.pack(side="left", padx=(8, 0))

        self._build_link(body)

        ttk.Label(body, textvariable=self._status, style="Muted.TLabel", wraplength=440, justify="left").pack(anchor="w", pady=(theme.SPACE_CARD_GAP, 0))

    def _build_link(self, body: ttk.Frame) -> None:
        """The portal URL as a clickable, selectable link with a Copy link button."""
        p = theme.current_palette()
        ttk.Label(body, text="Reporting portal link", style="Small.TLabel").pack(
            anchor="w", pady=(theme.SPACE_CARD_GAP, 4))
        row = ttk.Frame(body)
        row.pack(fill="x")

        link_font = theme.font("small").copy()
        link_font.configure(underline=True)
        self._link_font = link_font  # keep a reference; Tk drops unreferenced fonts
        url = report.REPORT_PAGE_URL
        lines = max(1, -(-len(url) // LINK_CHARS_PER_LINE))
        text = tk.Text(
            row, height=lines, width=LINK_CHARS_PER_LINE, wrap="char", font=link_font,
            background=p["surface_alt"], foreground=p["accent"], relief="flat", borderwidth=0,
            highlightthickness=1, highlightbackground=p["border"], highlightcolor=p["focus"],
            selectbackground=p["accent"], selectforeground=p["accent_text"],
            inactiveselectbackground=p["accent"], insertwidth=0, padx=8, pady=6, cursor="hand2",
            takefocus=True,
        )
        text.insert("1.0", url, ("link",))
        text.configure(state="disabled")  # read-only, but still selectable and copyable
        text.pack(side="left", fill="x", expand=True)
        text.bind("<Button-1>", self._on_link_press, add="+")
        text.bind("<ButtonRelease-1>", self._on_link_release, add="+")
        text.bind("<Button-3>", self._show_link_menu)
        text.bind("<Control-c>", self._copy_selection)
        text.bind("<Control-a>", self._select_all_link)
        self._link_text = text
        self._link_press: Optional[tuple] = None

        self._link_menu = tk.Menu(self, tearoff=False, background=p["surface_alt"], foreground=p["text"],
                                  activebackground=p["accent"], activeforeground=p["accent_text"])
        self._link_menu.add_command(label="Open link", command=self._open_portal)
        self._link_menu.add_command(label="Copy link", command=self._copy_link)

        ttk.Button(row, text="Copy link", style="Ghost.TButton", command=self._copy_link).pack(
            side="left", padx=(8, 0), anchor="n")

    def _on_link_press(self, event: Any) -> None:
        self._link_text.focus_set()  # a disabled Text does not take focus by itself; Ctrl+C needs it
        self._link_press = (event.x, event.y)

    def _on_link_release(self, event: Any) -> None:
        """Open the portal on a plain click; a drag that selected text only selects."""
        start, self._link_press = self._link_press, None
        if start is None or abs(event.x - start[0]) > 3 or abs(event.y - start[1]) > 3:
            return
        if self._link_text.tag_ranges("sel"):
            return
        self._open_portal()

    def _select_all_link(self, _event: Any = None) -> str:
        self._link_text.tag_add("sel", "1.0", "end-1c")
        return "break"

    def _copy_selection(self, _event: Any = None) -> str:
        """Ctrl+C copies the selected part of the link, or the whole link when nothing is selected."""
        try:
            chosen = self._link_text.get("sel.first", "sel.last")
        except tk.TclError:
            chosen = ""
        self._copy_to_clipboard(chosen or report.REPORT_PAGE_URL)
        return "break"

    def _copy_link(self) -> None:
        self._copy_to_clipboard(report.REPORT_PAGE_URL)

    def _copy_to_clipboard(self, value: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(value)
        self._status.set("Copied the link to the clipboard." if value == report.REPORT_PAGE_URL
                         else "Copied the selected text to the clipboard.")

    def _show_link_menu(self, event: Any) -> None:
        try:
            self._link_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._link_menu.grab_release()

    def _save(self) -> None:
        self.save_btn.configure(state="disabled")
        self._status.set("Collecting logs and system details...")
        self.worker.submit(bugreport.write_bug_zip, on_done=self._on_saved)

    def _on_saved(self, result: Any, error: Optional[BaseException]) -> None:
        if not self.winfo_exists():
            return
        self.save_btn.configure(state="normal")
        if error is not None:
            message = str(error) if isinstance(error, InterMuxError) else f"unexpected error: {error}"
            messagebox.showerror("Could not save the report", message, parent=self)
            self._status.set("")
            return
        self.saved_path = Path(result)
        self.folder_btn.configure(state="normal")
        self._status.set(f"Saved: {self.saved_path}\nOpen the reporting portal and upload this file.")

    def _open_folder(self) -> None:
        if self.saved_path is None:
            return
        self.worker.submit(commands.open_with_desktop, str(self.saved_path.parent),
                           on_done=self._on_folder_opened)

    def _on_folder_opened(self, result: Any, error: Optional[BaseException]) -> None:
        if not self.winfo_exists():
            return
        if error is not None or not getattr(result, "ok", False):
            self._status.set(f"Could not open the folder. It is at: {self.saved_path.parent if self.saved_path else ''}")

    def _open_portal(self) -> None:
        self._status.set("Opening the reporting portal...")
        self.worker.submit(report.open_report_page, on_done=self._on_portal_opened)

    def _on_portal_opened(self, result: Any, error: Optional[BaseException]) -> None:
        if not self.winfo_exists():
            return
        if error is not None or not result:
            self._status.set("Could not open a browser. Copy the link above and open it in your browser.")
        else:
            self._status.set("Opened the reporting portal in your browser.")
