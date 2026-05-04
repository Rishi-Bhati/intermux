#!/usr/bin/env python3
"""
InterMux for Windows — GUI
Dark-themed Tkinter GUI that lets users bind applications to specific network
interfaces using an embedded SOCKS5 proxy engine.

Same UX as the Linux version — interface dropdown, app path, Assign button.
"""

import os
import sys
import shutil
import logging
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

# Allow running from the repo root or windows/ dir
_GUI_DIR = os.path.dirname(os.path.abspath(__file__))
_WIN_DIR = os.path.dirname(_GUI_DIR)
_ROOT    = os.path.dirname(_WIN_DIR)
sys.path.insert(0, _ROOT)

from windows.core.interface import get_active_interfaces
import windows.core.platform_utils as plat
from windows.core.proxy_engine import proxy_registry
from windows.core.app_launcher import launch_app, session_tracker


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _setup_logging() -> logging.Logger:
    user = plat.get_current_user()
    log_dir = os.path.join(user["localappdata"], "InterMux")
    try:
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, "intermux.log")
    except OSError:
        log_file = os.path.join(os.path.expanduser("~"), "intermux.log")

    fmt    = logging.Formatter("%(asctime)s [%(levelname)-7s] %(message)s", "%Y-%m-%d %H:%M:%S")
    logger = logging.getLogger("intermux-win")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)
        ch.setFormatter(fmt)
        logger.addHandler(ch)
        try:
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        except OSError as e:
            logger.warning(f"Cannot open log file: {e}")
    return logger


log = _setup_logging()
log.info("InterMux for Windows starting")


# ---------------------------------------------------------------------------
# UAC / Admin guard
# ---------------------------------------------------------------------------
# We don't hard-require admin for Phase 1 (proxy-based binding works as a
# regular user), but we show a soft warning if not elevated.
_IS_ADMIN = plat.is_admin()


# ---------------------------------------------------------------------------
# Colour palette (same dark theme as Linux)
# ---------------------------------------------------------------------------
BG          = "#0d1117"
FG          = "#c9d1d9"
ENTRY_BG    = "#161b22"
BTN_BG      = "#21262d"
BTN_FG      = "#58a6ff"
LB_BG       = "#161b22"
LB_FG       = "#c9d1d9"
BORDER      = "#30363d"
HIGHLIGHT   = "#1f6feb"
TITLE_CLR   = "#58a6ff"
WARN_CLR    = "#e3b341"
SUCCESS_CLR = "#57ab5a"
PROXY_CLR   = "#a371f7"   # purple for proxy status


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VIRTUAL_PREFIXES = ("veth", "br-", "docker", "tun", "tap", "virbr",
                     "vmnet", "vboxnet", "loopback", "pseudo", "isatap",
                     "teredo", "6to4", "vethernet", "hyper-v")


def _is_real(name: str) -> bool:
    n = name.lower()
    return not any(v in n for v in _VIRTUAL_PREFIXES)


def _get_real_interfaces():
    ifaces = get_active_interfaces()
    return [i for i in ifaces if i["flag"] == "UP" and i["ip_addresses"] and _is_real(i["name"])]


def _get_ipv4(iface_dict) -> str:
    """Extract the first IPv4 address (without /prefix) from iface dict."""
    for ip in iface_dict.get("ip_addresses", []):
        if ":" not in ip:
            return ip.split("/")[0]
    return ""


# ---------------------------------------------------------------------------
# Main Application Window
# ---------------------------------------------------------------------------

class InterMuxApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("InterMux — Network Interface Binding (Windows)")
        self.root.geometry("860x640")
        self.root.resizable(True, True)
        self.root.configure(bg=BG)

        self._iface_data: list[dict] = []        # full interface dicts
        self._pending: list[tuple[str, str]] = [] # [(app_path, iface_name)]
        self._active:  list[dict] = []            # [{app, iface, bind_ip, port, pid}]

        self._setup_styles()
        self._build_ui()
        self._refresh_interfaces()
        self._check_deps_async()

    # ------------------------------------------------------------------
    # Styles
    # ------------------------------------------------------------------

    def _setup_styles(self):
        style = ttk.Style()
        style.theme_use("default")
        cfg = {
            "Dark.TFrame":           {"background": BG},
            "Dark.TLabel":           {"background": BG, "foreground": FG},
            "Dark.TButton":          {"background": BTN_BG, "foreground": BTN_FG, "borderwidth": 1},
            "Dark.TLabelframe":      {"background": BG, "foreground": FG},
            "Dark.TLabelframe.Label":{"background": BG, "foreground": FG},
            "Dark.TEntry":           {"fieldbackground": ENTRY_BG, "foreground": FG},
            "TCombobox":             {"fieldbackground": ENTRY_BG, "background": BTN_BG,
                                      "foreground": FG, "arrowcolor": FG},
        }
        for widget, opts in cfg.items():
            style.configure(widget, **opts)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        main = ttk.Frame(self.root, padding="20", style="Dark.TFrame")
        main.pack(fill=tk.BOTH, expand=True)
        self._main = main

        # ---- Admin warning (soft) ----
        if not _IS_ADMIN:
            tk.Label(
                main,
                text="⚠  Not running as Administrator. Some adapters may not be bindable. "
                     "Right-click InterMux and choose 'Run as administrator' for best results.",
                bg="#2d1d00", fg=WARN_CLR,
                font=("Segoe UI", 9), padx=10, pady=6,
                anchor="w", justify=tk.LEFT, wraplength=800,
            ).pack(fill=tk.X, pady=(0, 10))

        # ---- Title row ----
        title_row = ttk.Frame(main, style="Dark.TFrame")
        title_row.pack(fill=tk.X, pady=(0, 14))

        tk.Label(
            title_row,
            text="🌐  InterMux for Windows",
            font=("Segoe UI", 15, "bold"),
            bg=BG, fg=TITLE_CLR,
        ).pack(side=tk.LEFT)

        tk.Label(
            title_row,
            text=f"  {'✔ Admin' if _IS_ADMIN else 'User mode'}",
            bg=BG, fg=SUCCESS_CLR if _IS_ADMIN else WARN_CLR,
            font=("Segoe UI", 9),
        ).pack(side=tk.LEFT, padx=8)

        ttk.Button(
            title_row, text="⟳ Refresh", width=12,
            command=self._refresh_interfaces, style="Dark.TButton",
        ).pack(side=tk.RIGHT)

        # ---- Dep warning banner (populated async) ----
        self._dep_banner_var = tk.StringVar(value="")
        self._dep_banner = tk.Label(
            main, textvariable=self._dep_banner_var,
            bg="#2d1d00", fg=WARN_CLR,
            font=("Segoe UI", 9), padx=10, pady=5, anchor="w",
            justify=tk.LEFT,
        )

        # ---- Interface selection ----
        iface_row = ttk.Frame(main, style="Dark.TFrame")
        iface_row.pack(fill=tk.X, pady=(0, 8))
        tk.Label(iface_row, text="Network Interface", width=20, bg=BG, fg=FG,
                 font=("Segoe UI", 10)).pack(side=tk.LEFT)
        self._iface_var = tk.StringVar()
        self._iface_combo = ttk.Combobox(iface_row, textvariable=self._iface_var, state="readonly")
        self._iface_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._iface_combo.bind("<<ComboboxSelected>>", self._on_iface_selected)

        # ---- Interface info label ----
        self._iface_info_var = tk.StringVar(value="")
        tk.Label(main, textvariable=self._iface_info_var, bg=BG, fg=PROXY_CLR,
                 font=("Segoe UI", 9), anchor="w").pack(fill=tk.X, pady=(0, 8))

        # ---- App path ----
        app_row = ttk.Frame(main, style="Dark.TFrame")
        app_row.pack(fill=tk.X, pady=(0, 8))
        tk.Label(app_row, text="Application / Path", width=20, bg=BG, fg=FG,
                 font=("Segoe UI", 10)).pack(side=tk.LEFT)
        self._app_var = tk.StringVar()
        ttk.Entry(app_row, textvariable=self._app_var, style="Dark.TEntry").pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        ttk.Button(app_row, text="Browse…", width=10,
                   command=self._browse_app, style="Dark.TButton").pack(side=tk.LEFT)

        # ---- Add + Assign buttons ----
        btn_add_row = ttk.Frame(main, style="Dark.TFrame")
        btn_add_row.pack(fill=tk.X, pady=(0, 14))
        ttk.Button(btn_add_row, text="+ Add to Queue", width=18,
                   command=self._add_to_queue, style="Dark.TButton").pack(anchor=tk.CENTER)

        # ---- Queued list ----
        lists_frame = ttk.Frame(main, style="Dark.TFrame")
        lists_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 14))

        # Pending (queue)
        pending_frame = ttk.LabelFrame(lists_frame, text="Queued", style="Dark.TLabelframe", padding=8)
        pending_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 8))
        self._pending_lb = tk.Listbox(
            pending_frame, bg=LB_BG, fg=LB_FG, selectmode=tk.SINGLE,
            borderwidth=1, highlightthickness=1, highlightbackground=BORDER,
            selectbackground=HIGHLIGHT, selectforeground="#ffffff",
            relief=tk.FLAT, font=("Consolas", 9),
        )
        self._pending_lb.pack(fill=tk.BOTH, expand=True)
        ttk.Button(pending_frame, text="✕ Remove", width=12,
                   command=self._remove_selected, style="Dark.TButton").pack(pady=(6, 0))

        # Active (running)
        active_frame = ttk.LabelFrame(lists_frame, text="Active (Running)", style="Dark.TLabelframe", padding=8)
        active_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._active_lb = tk.Listbox(
            active_frame, bg=LB_BG, fg=LB_FG, selectmode=tk.SINGLE,
            borderwidth=1, highlightthickness=1, highlightbackground=BORDER,
            selectbackground=HIGHLIGHT, selectforeground="#ffffff",
            relief=tk.FLAT, font=("Consolas", 9),
        )
        self._active_lb.pack(fill=tk.BOTH, expand=True)

        # ---- Bottom action buttons ----
        bottom = ttk.Frame(main, style="Dark.TFrame")
        bottom.pack(anchor=tk.CENTER, pady=(0, 4))
        for text, cmd, w in [
            ("⚡ Assign All", self._assign_all, 22),
            ("⌫ Clear Queue",  self._clear_queue,  18),
            ("↺ Stop All Proxies", self._stop_all, 20),
        ]:
            ttk.Button(bottom, text=text, width=w, command=cmd,
                       style="Dark.TButton").pack(side=tk.LEFT, padx=6)

        # ---- Proxy status bar ----
        status_frame = tk.Frame(main, bg="#0a0e14")
        status_frame.pack(fill=tk.X, pady=(8, 0))
        self._status_var = tk.StringVar(value="Ready")
        tk.Label(
            status_frame, textvariable=self._status_var,
            bg="#0a0e14", fg=PROXY_CLR,
            font=("Consolas", 9), anchor="w", padx=8, pady=4,
        ).pack(fill=tk.X)

    # ------------------------------------------------------------------
    # Interface handling
    # ------------------------------------------------------------------

    def _refresh_interfaces(self):
        self._iface_data = _get_real_interfaces()
        names = [i["name"] for i in self._iface_data]
        self._iface_combo["values"] = names
        if names:
            self._iface_combo.set(names[0])
            self._update_iface_info(names[0])
        else:
            self._iface_combo.set("No active interfaces found")
            self._iface_info_var.set("")

    def _on_iface_selected(self, _event=None):
        name = self._iface_var.get()
        self._update_iface_info(name)

    def _update_iface_info(self, name: str):
        iface = next((i for i in self._iface_data if i["name"] == name), None)
        if not iface:
            self._iface_info_var.set("")
            return
        ipv4 = _get_ipv4(iface) or "No IPv4"
        gws  = ", ".join(iface.get("gateways", [])) or "N/A"
        self._iface_info_var.set(
            f"  Adapter: {iface['type']}   IP: {ipv4}   Gateway: {gws}"
        )

    # ------------------------------------------------------------------
    # Queue management
    # ------------------------------------------------------------------

    def _browse_app(self):
        path = filedialog.askopenfilename(
            title="Select Application",
            filetypes=[("Executable", "*.exe"), ("All files", "*.*")],
        )
        if path:
            self._app_var.set(path)

    def _add_to_queue(self):
        app   = self._app_var.get().strip()
        iface = self._iface_var.get().strip()

        if not app:
            messagebox.showerror("Error", "Please enter an application path or name.")
            return
        if not iface or iface == "No active interfaces found":
            messagebox.showerror("Error", "Please select a valid network interface.")
            return

        # Basic resolution check
        resolved = app if os.path.isabs(app) and os.path.exists(app) else shutil.which(app)
        if resolved is None:
            messagebox.showerror(
                "App Not Found",
                f"Could not find '{app}'.\n\n"
                "Enter either:\n"
                "  • A full path:    C:\\Program Files\\Firefox\\firefox.exe\n"
                "  • A command name: firefox\n\n"
                "Or use the Browse… button.",
            )
            return

        self._pending.append((resolved, iface))
        self._pending_lb.insert(tk.END, f"{os.path.basename(resolved)}  →  {iface}")
        self._app_var.set("")

    def _remove_selected(self):
        sel = self._pending_lb.curselection()
        if not sel:
            return
        idx = sel[0]
        self._pending_lb.delete(idx)
        self._pending.pop(idx)

    def _clear_queue(self):
        self._pending.clear()
        self._pending_lb.delete(0, tk.END)

    # ------------------------------------------------------------------
    # Already-running dialog
    # ------------------------------------------------------------------

    def _prompt_close_running(self, app_path: str) -> str:
        """
        If the app has running instances, ask what to do.
        Returns: 'close_reopen' | 'new_profile' | 'cancel'
        """
        pids = plat.find_running_instances(app_path)
        if not pids:
            return "close_reopen"

        app_name = os.path.basename(app_path)
        win = tk.Toplevel(self.root)
        win.title("App Already Running")
        win.geometry("440x210")
        win.configure(bg=BG)
        win.grab_set()

        choice = tk.StringVar(value="cancel")

        msg = (f"'{app_name}' is already running ({len(pids)} instance(s)).\n\n"
               "What would you like to do?")
        tk.Label(win, text=msg, bg=BG, fg=FG, font=("Segoe UI", 10),
                 wraplength=400, justify=tk.LEFT).pack(pady=(18, 10), padx=20)

        bf = ttk.Frame(win, style="Dark.TFrame")
        bf.pack(pady=6)

        def pick(val):
            choice.set(val)
            win.destroy()

        ttk.Button(bf, text="⏹  Close & reopen on selected network",
                   width=40, style="Dark.TButton",
                   command=lambda: pick("close_reopen")).pack(pady=3)
        ttk.Button(bf, text="➕  Open new instance (separate profile)",
                   width=40, style="Dark.TButton",
                   command=lambda: pick("new_profile")).pack(pady=3)
        ttk.Button(bf, text="✕  Cancel",
                   width=40, style="Dark.TButton",
                   command=lambda: pick("cancel")).pack(pady=3)

        self.root.wait_window(win)
        return choice.get()

    # ------------------------------------------------------------------
    # Assign
    # ------------------------------------------------------------------

    def _assign_all(self):
        if not self._pending:
            messagebox.showinfo("Info", "No applications in the queue.")
            return

        items = list(self._pending)
        self._clear_queue()

        for app_path, iface_name in items:
            self._assign_one(app_path, iface_name)

    def _assign_one(self, app_path: str, iface_name: str):
        app_name = os.path.basename(app_path).lower()

        # Find interface dict
        iface = next((i for i in self._iface_data if i["name"] == iface_name), None)
        if iface is None:
            messagebox.showerror("Error", f"Interface '{iface_name}' not found. Please Refresh.")
            return

        ipv4 = _get_ipv4(iface)
        if not ipv4:
            messagebox.showerror(
                "No IPv4",
                f"'{iface_name}' has no IPv4 address.\n"
                "Cannot bind proxy to this adapter.",
            )
            return

        # Handle already-running instances
        use_new_profile = False
        pids = plat.find_running_instances(app_path)
        if pids:
            action = self._prompt_close_running(app_path)
            if action == "cancel":
                return
            elif action == "close_reopen":
                plat.kill_running_instances(app_path)
            elif action == "new_profile":
                use_new_profile = True

        # Launch in thread so GUI stays responsive
        self._set_status(f"Starting proxy for {iface_name} ({ipv4})…")

        def _do_launch():
            try:
                proc = launch_app(app_path, iface_name, ipv4, use_new_profile=use_new_profile)
                proxy_port = proxy_registry.running_proxies().get(ipv4, "?")
                display = (
                    f"{os.path.basename(app_path)}  →  {iface_name} "
                    f"[proxy :{proxy_port}] [PID {proc.pid}]"
                )
                self._active.append({
                    "app": app_path, "iface": iface_name,
                    "bind_ip": ipv4, "port": proxy_port, "pid": proc.pid,
                })
                self.root.after(0, lambda: self._active_lb.insert(tk.END, display))
                self.root.after(0, lambda: self._set_status(
                    f"✔ '{os.path.basename(app_path)}' running via {iface_name}  "
                    f"| Proxy socks5://127.0.0.1:{proxy_port}"
                ))
                log.info(f"Launched {app_path} via {iface_name} ({ipv4}), proxy :{proxy_port}")
            except Exception as e:
                log.error(f"Launch failed: {e}", exc_info=True)
                self.root.after(0, lambda: messagebox.showerror(
                    "Launch Failed", f"Could not launch '{os.path.basename(app_path)}':\n\n{e}"
                ))
                self.root.after(0, lambda: self._set_status("Error during launch."))

        threading.Thread(target=_do_launch, daemon=True).start()

    # ------------------------------------------------------------------
    # Stop / Reset
    # ------------------------------------------------------------------

    def _stop_all(self):
        if not messagebox.askyesno(
            "Stop All Proxies",
            "Stop all InterMux proxy servers?\n\n"
            "Running apps will continue but lose their interface binding\n"
            "(traffic will use the system default interface instead).",
        ):
            return
        proxy_registry.stop_all()
        self._active.clear()
        self._active_lb.delete(0, tk.END)
        self._set_status("All proxies stopped.")

    # ------------------------------------------------------------------
    # Status bar
    # ------------------------------------------------------------------

    def _set_status(self, msg: str):
        self._status_var.set(f"  {msg}")

    # ------------------------------------------------------------------
    # Dependency check (async so it doesn't block startup)
    # ------------------------------------------------------------------

    def _check_deps_async(self):
        def _check():
            missing = plat.get_missing_dependencies()
            if missing:
                hints = "  |  ".join(f"{d['tool']}: {d['install_hint']}" for d in missing)
                self.root.after(0, lambda: self._show_dep_banner(hints))

        threading.Thread(target=_check, daemon=True).start()

    def _show_dep_banner(self, hints: str):
        self._dep_banner_var.set(f"⚠  Missing dependencies: {hints}")
        self._dep_banner.pack(fill=tk.X, pady=(0, 10), before=self._main.winfo_children()[0])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if sys.platform != "win32":
        print("[X] This GUI is for Windows only.")
        print("    On Linux, run: python3 gui/app.py")
        sys.exit(1)

    root = tk.Tk()

    # Windows taskbar icon & DPI awareness
    try:
        import ctypes as _ct
        _ct.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    try:
        root.iconbitmap(default="")  # suppress default Tk feather icon
    except Exception:
        pass

    app = InterMuxApp(root)
    root.mainloop()

    # Cleanup on window close
    proxy_registry.stop_all()


if __name__ == "__main__":
    main()
