#!/usr/bin/env python3
import shutil as _shutil  # used for which() — imported early so add_path can use it
"""
InterMux GUI — Network Interface Binding
Launches the app as the original (non-root) user inside a network namespace,
preserving user data and profiles.
"""

import sys
import os
import re
import subprocess
import tkinter as tk
import hashlib
import logging
from tkinter import ttk, messagebox

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import core.interface as interface
import core.platform_utils as plat
from core.router import check_existing_routing_tables, clear_custom_routing_tables

# ---------------------------------------------------------------------------
# Logging Setup  (console: INFO+  |  file: DEBUG+ for full troubleshooting)
# ---------------------------------------------------------------------------

def _setup_logging() -> logging.Logger:
    log_dir = os.path.join(os.path.expanduser("~"), ".local", "share", "intermux")
    try:
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, "intermux.log")
    except OSError:
        log_file = "/tmp/intermux.log"

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    logger = logging.getLogger("intermux")
    logger.setLevel(logging.DEBUG)

    if not logger.handlers:   # avoid duplicate handlers on re-import
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)
        ch.setFormatter(fmt)
        logger.addHandler(ch)

        try:
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        except OSError as exc:
            logger.warning(f"Could not open log file {log_file!r}: {exc}")

    return logger

log = _setup_logging()
log.info(f"InterMux starting — log: ~/.local/share/intermux/intermux.log")

# ---------------------------------------------------------------------------
# Tool-path Detection  (cross-distro: Arch, Ubuntu, Fedora, openSUSE, Alpine…)
# ---------------------------------------------------------------------------

def _find_tool(*candidates: str, fallback: str = "") -> str:
    """Return the first candidate found in PATH, or fallback."""
    for c in candidates:
        p = _shutil.which(c)
        if p:
            return p
    return fallback

_IP_CMD     = _find_tool("ip",       fallback="/usr/sbin/ip")
_IPT_CMD    = _find_tool("iptables", "iptables-legacy", "iptables-nft",
                          fallback="/usr/sbin/iptables")
_SYSCTL_CMD = _find_tool("sysctl",   fallback="/usr/sbin/sysctl")

log.debug(f"Tool paths: ip={_IP_CMD!r}  iptables={_IPT_CMD!r}  sysctl={_SYSCTL_CMD!r}")

# ---------------------------------------------------------------------------
# Privilege Escalation
# ---------------------------------------------------------------------------
# Capture the real user BEFORE escalating so we always know who invoked us.
_invoking_user = plat.get_invoking_user()

if os.geteuid() != 0:
    script_path = os.path.abspath(__file__)
    # Preserve env vars needed to detect the user's session after escalation
    env_to_preserve = []
    for var in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_SESSION_TYPE",
                "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
                "XAUTHORITY", "PULSE_SERVER", "PIPEWIRE_REMOTE"):
        if os.environ.get(var):
            env_to_preserve.append(f"{var}={os.environ[var]}")

    # Try pkexec first, fall back to sudo
    if plat._which("pkexec"):
        try:
            os.execvp("pkexec", ["pkexec", "env"] + env_to_preserve +
                      ["python3", script_path])
        except Exception as e:
            print(f"[!] pkexec failed: {e} — trying sudo")

    try:
        os.execvp("sudo", ["sudo", "-E", "python3", script_path])
    except Exception as e:
        print(f"[X] Failed to elevate privileges: {e}")
        sys.exit(1)

# Now running as root — set up X11 access for root and the real user
plat.setup_xhost_for_root()

# Check dependencies and warn
_missing_deps = plat.get_missing_dependencies()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Errors that are safe to ignore (teardown of non-existent objects, etc.)
_HARMLESS_ERRORS = [
    "File exists",
    "Cannot create namespace file",
    "already exists",
    "exists but is not a directory",
    "Cannot find device",
    "No such file or directory",
    "FIB table does not exist",      # ip route flush on a table that hasn't been created yet
    "Flush terminated",              # accompanies the above on some kernels
    "RTNETLINK answers: No such process",
    "RTNETLINK answers: No such file or directory",
    "No rule found",
    "does not exist",
]


def run_cmd(cmd: str, suppress_harmless: bool = True) -> str:
    """Run a shell command as root, return stdout.  All output is logged."""
    log.debug(f"RUN: {cmd}")
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        stderr = result.stderr.strip()
        if result.returncode != 0:
            if suppress_harmless and any(h in stderr for h in _HARMLESS_ERRORS):
                log.debug(f"  (harmless) {stderr}")
            else:
                msg = f"CMD FAILED: {cmd}\n    -> {stderr}"
                log.warning(msg)
                print(f"[!] {cmd}\n    -> {stderr}")
        else:
            if result.stdout.strip():
                log.debug(f"  stdout: {result.stdout.strip()[:200]}")
        return result.stdout.strip()
    except Exception as exc:
        log.error(f"Exception running '{cmd}': {exc}", exc_info=True)
        print(f"[X] Exception running '{cmd}': {exc}")
        return ""


_VIRTUAL_PREFIXES = ("veth", "br-", "docker", "tun", "tap", "virbr", "vmnet", "vboxnet")

def _is_real_interface(name: str) -> bool:
    """Return True only for physical/real interfaces (Wi-Fi, Ethernet, USB, etc.)."""
    return not name.startswith(_VIRTUAL_PREFIXES)


def refresh():
    global interface_names
    ifaces = interface.get_active_interfaces()
    interface_names = [
        i["name"] for i in ifaces
        if i["flag"] == "UP" and i["ip_addresses"] and _is_real_interface(i["name"])
    ]
    interface_combo["values"] = interface_names
    if not interface_names:
        interface_combo.set("No active interfaces found")
    else:
        interface_combo.set(interface_names[0])


def add_path():
    app = path_entry.get().strip()
    iface = interface_combo.get()
    if not app or not iface or iface == "No active interfaces found":
        messagebox.showerror("Error", "Please enter a valid application path and select an interface.")
        return

    # Accept a short name (e.g. "firefox") OR a full path
    resolved = app if os.path.isabs(app) else _shutil.which(app)
    if resolved is None or not os.path.exists(resolved):
        # Last-ditch: maybe it's a relative path that exists
        if os.path.exists(app):
            resolved = os.path.abspath(app)
        else:
            messagebox.showerror(
                "Application Not Found",
                f"Could not find '{app}'.\n\n"
                "Enter either:\n"
                "  • A full path: /usr/lib/firefox/firefox\n"
                "  • A command name in PATH: firefox"
            )
            return

    selected_paths.insert(tk.END, f"{resolved} -> {iface}")
    path_entry.delete(0, tk.END)



def clear_all():
    if not selected_paths.size() and not created_paths.size():
        messagebox.showinfo("Info", "No paths to clear.")
        return
    if messagebox.askyesno("Confirm", "Clear all paths and routing tables?"):
        clear_custom_routing_tables()
        selected_paths.delete(0, tk.END)
        created_paths.delete(0, tk.END)
        path_entry.delete(0, tk.END)
        messagebox.showinfo("Success", "All paths cleared.")


def setup_routing():
    """Create policy routing tables for all active interfaces if not done yet."""
    if check_existing_routing_tables():
        return
    script_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../core/router.py"))
    result = subprocess.run(["python3", script_path], capture_output=True, text=True)
    if result.returncode != 0:
        messagebox.showerror("Error", f"Failed to create routing tables:\n{result.stderr}")


def _prompt_close_running(app_path: str) -> str:
    """
    If the app has running instances, ask the user what to do.
    Returns: 'close_reopen' | 'new_profile' | 'cancel'
    """
    pids = plat.find_running_instances(app_path)
    if not pids:
        return "close_reopen"  # No conflict, proceed normally

    app_name = os.path.basename(app_path)
    win = tk.Toplevel(root)
    win.title("App Already Running")
    win.geometry("420x200")
    win.configure(bg=bg_color)
    win.grab_set()

    choice = tk.StringVar(value="cancel")

    msg = (f"'{app_name}' is already running ({len(pids)} instance(s)).\n\n"
           "What would you like to do?")
    ttk.Label(win, text=msg, style="Dark.TLabel", wraplength=380,
              justify=tk.LEFT).pack(pady=(18, 10), padx=20)

    btn_frame = ttk.Frame(win, style="Dark.TFrame")
    btn_frame.pack(pady=6)

    def pick(val):
        choice.set(val)
        win.destroy()

    ttk.Button(btn_frame, text="⏹ Close & reopen on selected network",
               width=36, style="Dark.TButton",
               command=lambda: pick("close_reopen")).pack(pady=4)
    ttk.Button(btn_frame, text="➕ Open new instance (separate profile)",
               width=36, style="Dark.TButton",
               command=lambda: pick("new_profile")).pack(pady=4)
    ttk.Button(btn_frame, text="✕ Cancel",
               width=36, style="Dark.TButton",
               command=lambda: pick("cancel")).pack(pady=4)

    root.wait_window(win)
    return choice.get()


def _clear_firefox_locks(profile_dir: str):
    """Remove stale Firefox lock files so a new instance can start cleanly."""
    import glob
    for lock in ["lock", ".parentlock"]:
        path = os.path.join(profile_dir, lock)
        try:
            if os.path.exists(path) or os.path.islink(path):
                os.remove(path)
                print(f"[+] Removed stale Firefox lock: {path}")
        except OSError as e:
            print(f"[!] Could not remove lock {path}: {e}")


def _build_launch_cmd(app: str, ns: str, iface: str, use_new_profile: bool = False) -> str:
    """
    Builds the full launch command that:
    - Executes inside the network namespace
    - Runs as the original (non-root) user
    - Passes all required display/audio/dbus environment variables
    - Gives each namespace its own TMPDIR so app IPC sockets don't collide
    """
    user = _invoking_user
    env = plat.get_display_env()

    # Each namespace gets its own TMPDIR so apps (especially Firefox) don't
    # discover each other through shared /tmp IPC sockets.
    ns_tmp = f"/tmp/intermux_{ns}"
    os.makedirs(ns_tmp, exist_ok=True)
    # Make sure the real user owns it (needed for apps that check ownership)
    try:
        os.chown(ns_tmp, user["uid"], user["gid"])
    except OSError:
        pass
    env["TMPDIR"] = ns_tmp
    env["TMP"] = ns_tmp
    env["TEMP"] = ns_tmp

    # Build env string
    env_pairs = " ".join(f'{k}="{v}"' for k, v in env.items())

    # App-specific flags
    extra_flags = ""
    app_name = os.path.basename(app).lower()

    if "firefox" in app_name:
        if use_new_profile:
            # Another Firefox is already running — use an isolated per-interface
            # profile so the two instances don't share state or lock files.
            profile_dir = plat.get_or_create_firefox_profile(iface, user["home"])
            _clear_firefox_locks(profile_dir)
            extra_flags = f"-new-instance --no-remote --profile {profile_dir}"
        else:
            # No conflict (or user chose close-and-reopen): use the user's normal
            # default profile.  Clear its lock first so Firefox starts cleanly
            # even if a stale lock was left behind.
            default_profile = _find_firefox_default_profile(user["home"])
            if default_profile:
                _clear_firefox_locks(default_profile)
            # -new-instance + --no-remote prevents X11 remote-control conflicts
            # inside the namespace without forcing a brand-new empty profile.
            extra_flags = "-new-instance --no-remote"

    # Detect session type for display socket access inside namespace
    session_type = env.get("XDG_SESSION_TYPE", "x11").lower()
    runtime_dir = env.get("XDG_RUNTIME_DIR", f"/run/user/{user['uid']}")

    if session_type == "wayland":
        wayland_sock = env.get("WAYLAND_DISPLAY", "")
        if wayland_sock and not wayland_sock.startswith("/"):
            wayland_sock = f"{runtime_dir}/{wayland_sock}"
        xwayland_display = env.get("DISPLAY", "")
        if xwayland_display:
            env_pairs += f' DISPLAY="{xwayland_display}"'

    cmd = (
        f"{_IP_CMD} netns exec {ns} "
        f"sudo -u {user['username']} "
        f"env {env_pairs} "
        f"{app} {extra_flags}"
    ).strip()

    return cmd


def _find_firefox_default_profile(home: str) -> str:
    """
    Finds the path of the user's default Firefox profile directory.
    Returns the path string, or empty string if not found.
    """
    import configparser
    profiles_ini = os.path.join(home, ".mozilla", "firefox", "profiles.ini")
    if not os.path.exists(profiles_ini):
        return ""
    cfg = configparser.ConfigParser()
    cfg.read(profiles_ini)
    for section in cfg.sections():
        if cfg.get(section, "Default", fallback="0") == "1":
            rel = cfg.get(section, "IsRelative", fallback="1")
            path = cfg.get(section, "Path", fallback="")
            if not path:
                continue
            if rel == "1":
                return os.path.join(home, ".mozilla", "firefox", path)
            return path
    # Fallback: return first profile that has a Path
    for section in cfg.sections():
        path = cfg.get(section, "Path", fallback="")
        if path:
            rel = cfg.get(section, "IsRelative", fallback="1")
            if rel == "1":
                return os.path.join(home, ".mozilla", "firefox", path)
            return path
    return ""


def assign():
    """Set up network namespaces and launch each app."""
    items = selected_paths.get(0, tk.END)
    if not items:
        messagebox.showinfo("Info", "No applications to assign.")
        return

    app_list = []
    for item in items:
        parts = item.split(" -> ", 1)
        if len(parts) == 2:
            app_list.append((parts[0].strip(), parts[1].strip()))

    for i in selected_paths.get(0, tk.END):
        created_paths.insert(tk.END, i)
    selected_paths.delete(0, tk.END)

    setup_routing()

    for app, iface in app_list:
        app_name = os.path.basename(app).lower()

        if "chromium" in app_name:
            messagebox.showwarning(
                "Chromium Not Supported",
                "Chromium cannot run in network namespaces due to its sandboxing.\n"
                "Use Firefox or another application instead."
            )
            continue

        # --- Handle already-running instances ---
        use_new_profile = False
        pids = plat.find_running_instances(app)
        if pids:
            action = _prompt_close_running(app)
            if action == "cancel":
                continue
            elif action == "close_reopen":
                plat.kill_running_instances(app)
                use_new_profile = False
            elif action == "new_profile":
                use_new_profile = True

        # --- Namespace setup (runs as root) ---
        iface_hash  = hashlib.md5(iface.encode()).hexdigest()[:8]
        ns    = f"ns_{iface_hash}"
        veth0 = f"veth0_{iface_hash}"
        veth1 = f"veth1_{iface_hash}"
        # Derive subnet_byte deterministically from the md5 hash so it is
        # stable across restarts.  Python's built-in hash() is randomised by
        # PYTHONHASHSEED and would produce a different value every run.
        subnet_byte = int(iface_hash, 16) % 253 + 1   # 1-253, always the same for a given iface

        log.info(f"Setting up namespace '{ns}' for '{iface}' (subnet 10.0.{subnet_byte}.0/24)")

        # Tear down any leftover state from a previous run
        run_cmd(f"{_IP_CMD} netns del {ns}")
        run_cmd(f"{_IP_CMD} link del {veth0}")
        run_cmd(f"{_IP_CMD} link del {veth1}")

        # Build namespace and veth pair
        run_cmd(f"{_IP_CMD} netns add {ns}")
        run_cmd(f"{_IP_CMD} link add {veth0} type veth peer name {veth1}")
        run_cmd(f"{_IP_CMD} link set {veth1} netns {ns}")
        run_cmd(f"{_IP_CMD} addr add 10.0.{subnet_byte}.1/24 dev {veth0}")
        run_cmd(f"{_IP_CMD} link set {veth0} up")
        run_cmd(f"{_IP_CMD} netns exec {ns} {_IP_CMD} addr add 10.0.{subnet_byte}.2/24 dev {veth1}")
        run_cmd(f"{_IP_CMD} netns exec {ns} {_IP_CMD} link set {veth1} up")
        run_cmd(f"{_IP_CMD} netns exec {ns} {_IP_CMD} link set lo up")
        run_cmd(f"{_IP_CMD} netns exec {ns} {_IP_CMD} route add default via 10.0.{subnet_byte}.1")

        # NAT so the namespace can reach the internet through the real interface
        run_cmd(f"{_IPT_CMD} -t nat -A POSTROUTING -s 10.0.{subnet_byte}.0/24 -o {iface} -j MASQUERADE")
        run_cmd(f"{_IPT_CMD} -A FORWARD -j ACCEPT")
        run_cmd(f"{_SYSCTL_CMD} -w net.ipv4.ip_forward=1")

        # Policy routing: force traffic from this namespace's subnet to exit via
        # the correct physical interface.  Without this, the host uses its default
        # route (e.g. USB tether) for ALL namespace traffic, so the wlan0
        # MASQUERADE rule never fires → no internet on that namespace.
        ifaces_info = interface.get_active_interfaces()
        iface_info  = next((i for i in ifaces_info if i['name'] == iface), None)
        if iface_info and iface_info.get('gateways'):
            gw  = iface_info['gateways'][0]
            tbl = 200 + subnet_byte    # unique table per namespace (201-453)
            pri = 500 + subnet_byte    # unique priority
            run_cmd(f"{_IP_CMD} route flush table {tbl}")
            run_cmd(f"{_IP_CMD} route add default via {gw} dev {iface} table {tbl}")
            run_cmd(f"{_IP_CMD} rule del from 10.0.{subnet_byte}.0/24 table {tbl} priority {pri}")
            run_cmd(f"{_IP_CMD} rule add from 10.0.{subnet_byte}.0/24 table {tbl} priority {pri}")
            log.info(f"Policy route: 10.0.{subnet_byte}.0/24 → {iface} via {gw} (table {tbl})")
            print(f"[+] Policy route: 10.0.{subnet_byte}.0/24 → {iface} via {gw} (table {tbl})")
        else:
            log.warning(f"No gateway found for '{iface}' — internet inside namespace may not work")
            print(f"[!] No gateway for {iface} — internet inside namespace may not work")

        # Write proper resolv.conf (handles systemd-resolved stubs)
        ns_etc = f"/etc/netns/{ns}"
        plat.write_namespace_resolv_conf(ns_etc)

        # --- App launch (drops to original user) ---
        launch_cmd = _build_launch_cmd(app, ns, iface, use_new_profile=use_new_profile)
        log.info(f"Launching: {launch_cmd}")
        print(f"[+] Launching: {launch_cmd}")
        subprocess.Popen(launch_cmd, shell=True)
        log.info(f"'{os.path.basename(app)}' launched in namespace '{ns}' via '{iface}'")
        print(f"[✓] '{os.path.basename(app)}' launched in namespace '{ns}' via '{iface}'")


def reset_all():
    if not messagebox.askyesno(
        "Confirm Reset",
        "This will remove all veth interfaces, network namespaces, "
        "and routing tables created by InterMux. Proceed?"
    ):
        return
    try:
        clear_custom_routing_tables()

        # Remove veth pairs
        veths = [
            line.split(":")[1].strip().split("@")[0]
            for line in (run_cmd(f"{_IP_CMD} -o link show") or "").split("\n")
            if "veth" in line
        ]
        for veth in veths:
            run_cmd(f"{_IP_CMD} link del {veth}")

        # Remove namespaces
        for iface in interface_names:
            iface_hash = hashlib.md5(iface.encode()).hexdigest()[:8]
            run_cmd(f"{_IP_CMD} netns del ns_{iface_hash}")

        # Remove policy routing rules added by InterMux
        existing_rules = run_cmd(f"{_IP_CMD} rule show") or ""
        for line in existing_rules.splitlines():
            m = re.search(r'^(\d+):\s+from 10\.0\.\d+\.0/24', line)
            if m:
                run_cmd(f"{_IP_CMD} rule del priority {m.group(1)}")

        # Flush the private routing tables (201-453)
        for tbl in range(201, 454):
            run_cmd(f"{_IP_CMD} route flush table {tbl}")

        # Remove iptables MASQUERADE rules added for InterMux subnets
        for entry in (run_cmd(f"{_IPT_CMD} -t nat -S POSTROUTING") or "").splitlines():
            if "-s 10.0." in entry and "-j MASQUERADE" in entry:
                run_cmd(f"{_IPT_CMD} -t nat {entry.replace('-A', '-D', 1)}")

        log.info("Reset complete.")
        selected_paths.delete(0, tk.END)
        created_paths.delete(0, tk.END)
        messagebox.showinfo("Success", "System reset to defaults.")
        refresh()
    except Exception as exc:
        log.error(f"Reset failed: {exc}", exc_info=True)
        messagebox.showerror("Error", f"Reset failed: {exc}")



# ---------------------------------------------------------------------------
# GUI Setup
# ---------------------------------------------------------------------------

root = tk.Tk()
root.title("InterMux — Network Interface Binding")
root.geometry("820x620")
root.resizable(True, True)

bg_color = "#0d1117"
fg_color = "#c9d1d9"
entry_bg = "#161b22"
button_bg = "#21262d"
button_fg = "#58a6ff"
listbox_bg = "#161b22"
listbox_fg = "#c9d1d9"
border_color = "#30363d"
highlight_bg = "#1f6feb"
title_color = "#58a6ff"
warn_color = "#e3b341"

style = ttk.Style()
style.theme_use("default")
for widget, opts in {
    "Dark.TFrame": {"background": bg_color},
    "Dark.TLabel": {"background": bg_color, "foreground": fg_color},
    "Dark.TButton": {"background": button_bg, "foreground": button_fg, "borderwidth": 1},
    "Dark.TLabelframe": {"background": bg_color, "foreground": fg_color},
    "Dark.TLabelframe.Label": {"background": bg_color, "foreground": fg_color},
    "Dark.TEntry": {"fieldbackground": entry_bg, "foreground": fg_color},
    "TCombobox": {"fieldbackground": entry_bg, "background": button_bg,
                  "foreground": fg_color, "arrowcolor": fg_color},
}.items():
    style.configure(widget, **opts)

root.configure(bg=bg_color)

main_frame = ttk.Frame(root, padding="20", style="Dark.TFrame")
main_frame.pack(fill=tk.BOTH, expand=True)

# --- Dependency Warning Banner ---
if _missing_deps:
    distro = plat.get_distro_info()
    hints = "\n".join(f"  • {d['tool']}: {d['install_hint']}" for d in _missing_deps)
    warn_label = tk.Label(
        main_frame,
        text=f"⚠  Missing dependencies — some features may not work:\n{hints}",
        bg="#2d1d00", fg=warn_color, justify=tk.LEFT,
        font=("monospace", 9), padx=10, pady=6, anchor="w"
    )
    warn_label.pack(fill=tk.X, pady=(0, 12))

# --- Title + Refresh ---
title_frame = ttk.Frame(main_frame, style="Dark.TFrame")
title_frame.pack(fill=tk.X, pady=(0, 16))

session_label = ""
env = plat.get_display_env()
session_type = env.get("XDG_SESSION_TYPE", "x11")
session_label = f"  [{session_type.upper()}]"

ttk.Label(
    title_frame,
    text=f"🌐 InterMux{session_label}",
    font=("monospace", 15, "bold"),
    style="Dark.TLabel",
    foreground=title_color
).pack(side=tk.LEFT)

user_label = tk.Label(
    title_frame,
    text=f"Running as root · Launching apps as: {_invoking_user['username']}",
    bg=bg_color, fg="#57ab5a", font=("monospace", 9)
)
user_label.pack(side=tk.LEFT, padx=12)

ttk.Button(title_frame, text="⟳ Refresh", width=12,
           command=refresh, style="Dark.TButton").pack(side=tk.RIGHT)

# --- Interface Selection ---
iface_frame = ttk.Frame(main_frame, style="Dark.TFrame")
iface_frame.pack(fill=tk.X, pady=(0, 8))
ttk.Label(iface_frame, text="$ Interface", width=18, style="Dark.TLabel",
          font=("monospace", 10)).pack(side=tk.LEFT)

ifaces = interface.get_active_interfaces()
interface_names = [
    i["name"] for i in ifaces
    if i["flag"] == "UP" and i["ip_addresses"] and _is_real_interface(i["name"])
]
interface_combo = ttk.Combobox(iface_frame, values=interface_names)
interface_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
if interface_names:
    interface_combo.set(interface_names[0])

# --- App Path ---
path_frame = ttk.Frame(main_frame, style="Dark.TFrame")
path_frame.pack(fill=tk.X, pady=(0, 8))
ttk.Label(path_frame, text="$ App / Path", width=18, style="Dark.TLabel",
          font=("monospace", 10)).pack(side=tk.LEFT)
path_entry = ttk.Entry(path_frame, style="Dark.TEntry")
path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

# --- Add Button ---
add_frame = ttk.Frame(main_frame, style="Dark.TFrame")
add_frame.pack(fill=tk.X, pady=(0, 16))
ttk.Button(add_frame, text="+ Add", width=14,
           command=add_path, style="Dark.TButton").pack(anchor=tk.CENTER)

# --- Path Lists ---
lists_frame = ttk.Frame(main_frame, style="Dark.TFrame")
lists_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 16))

for title, attr_name in [("Pending", "selected_paths"), ("Active", "created_paths")]:
    frame = ttk.LabelFrame(lists_frame, text=title, style="Dark.TLabelframe", padding=8)
    frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 8) if title == "Pending" else 0)
    lb = tk.Listbox(
        frame, bg=listbox_bg, fg=listbox_fg, selectmode=tk.SINGLE,
        borderwidth=1, highlightthickness=1, highlightbackground=border_color,
        selectbackground=highlight_bg, selectforeground="#ffffff",
        relief=tk.FLAT, font=("monospace", 10)
    )
    lb.pack(fill=tk.BOTH, expand=True)
    if attr_name == "selected_paths":
        selected_paths = lb
    else:
        created_paths = lb

# --- Bottom Buttons ---
btn_frame = ttk.Frame(main_frame, style="Dark.TFrame")
btn_frame.pack(anchor=tk.CENTER)

for text, cmd, width in [
    ("⚡ Assign", assign, 22),
    ("⌫ Clear All", clear_all, 18),
    ("↺ Reset Everything", reset_all, 20),
]:
    ttk.Button(btn_frame, text=text, width=width,
               command=cmd, style="Dark.TButton").pack(side=tk.LEFT, padx=6)

if __name__ == "__main__":
    root.mainloop()
