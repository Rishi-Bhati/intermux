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

        ttk.Label(body, textvariable=self._status, style="Muted.TLabel", wraplength=440, justify="left").pack(anchor="w", pady=(theme.SPACE_CARD_GAP, 0))

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
        self.worker.submit(commands.run, ["xdg-open", str(self.saved_path.parent)], timeout=10.0,
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
            self._status.set(f"Could not open a browser. Open this page yourself: {report.REPORT_PAGE_URL}")
        else:
            self._status.set("Opened the reporting portal in your browser.")
