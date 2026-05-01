#!/usr/bin/env python3
"""
InterMux Platform Utilities
Cross-distro support: display detection, DNS, privilege separation, running app detection.
"""

import os
import re
import pwd
import subprocess
import platform
import logging
from pathlib import Path

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Distro Detection
# ---------------------------------------------------------------------------

def get_distro_info() -> dict:
    """
    Returns distro info parsed from /etc/os-release.
    Returns: {id, name, version_id, package_manager}
    """
    info = {"id": "unknown", "name": "Unknown Linux", "version_id": "", "package_manager": "unknown"}
    try:
        # Python 3.10+ stdlib
        release = platform.freedesktop_os_release()
        info["id"] = release.get("ID", "unknown").lower()
        info["name"] = release.get("NAME", "Unknown Linux")
        info["version_id"] = release.get("VERSION_ID", "")
    except AttributeError:
        # Fallback: parse /etc/os-release manually
        for path in ("/etc/os-release", "/usr/lib/os-release"):
            if os.path.exists(path):
                with open(path) as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("ID="):
                            info["id"] = line.split("=", 1)[1].strip('"').lower()
                        elif line.startswith("NAME="):
                            info["name"] = line.split("=", 1)[1].strip('"')
                        elif line.startswith("VERSION_ID="):
                            info["version_id"] = line.split("=", 1)[1].strip('"')
                break
    except OSError:
        pass

    # Detect package manager
    pm_map = {
        "apt": ["ubuntu", "debian", "linuxmint", "pop", "elementary", "kali"],
        "pacman": ["arch", "manjaro", "endeavouros", "garuda"],
        "dnf": ["fedora", "rhel", "centos", "rocky", "almalinux"],
        "zypper": ["opensuse", "sles"],
        "apk": ["alpine"],
    }
    for pm, ids in pm_map.items():
        if any(d in info["id"] for d in ids):
            info["package_manager"] = pm
            break
    else:
        # Fallback: check which binary exists
        for pm in ("apt", "pacman", "dnf", "zypper", "apk"):
            if _which(pm):
                info["package_manager"] = pm
                break

    return info


# ---------------------------------------------------------------------------
# Invoking User (real user under sudo / pkexec)
# ---------------------------------------------------------------------------

def get_invoking_user() -> dict:
    """
    Returns info about the real (non-root) user who invoked the tool.
    Works correctly under sudo, pkexec, and direct root login.
    Falls back to current user if no escalation is detected.
    Returns: {username, uid, gid, home, xdg_runtime_dir}
    """
    username = None

    # pkexec sets PKEXEC_UID
    pkexec_uid = os.environ.get("PKEXEC_UID")
    if pkexec_uid:
        try:
            pw = pwd.getpwuid(int(pkexec_uid))
            username = pw.pw_name
        except (KeyError, ValueError):
            pass

    # sudo sets SUDO_USER
    if not username:
        username = os.environ.get("SUDO_USER")

    # Fall back to current user
    if not username:
        try:
            username = pwd.getpwuid(os.getuid()).pw_name
        except KeyError:
            username = "nobody"

    try:
        pw = pwd.getpwnam(username)
        uid = pw.pw_uid
        gid = pw.pw_gid
        home = pw.pw_dir
    except KeyError:
        uid = os.getuid()
        gid = os.getgid()
        home = os.path.expanduser("~")

    return {
        "username": username,
        "uid": uid,
        "gid": gid,
        "home": home,
        "xdg_runtime_dir": f"/run/user/{uid}",
    }


# ---------------------------------------------------------------------------
# Display / Session Detection
# ---------------------------------------------------------------------------

def get_display_env() -> dict:
    """
    Detects the display environment for the invoking user.
    Handles X11, Wayland, and mixed sessions.
    Returns a dict of env vars to pass to launched apps.
    """
    user = get_invoking_user()
    env = {}

    # Try to inherit from current environment first
    display = os.environ.get("DISPLAY")
    wayland_display = os.environ.get("WAYLAND_DISPLAY")
    xdg_session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()

    # If we're under sudo/pkexec, environment may not have DISPLAY — probe user's session
    if not display and not wayland_display:
        display, wayland_display, xdg_session_type = _probe_user_session(user["uid"])

    # Determine session type
    if not xdg_session_type:
        if wayland_display and not display:
            xdg_session_type = "wayland"
        elif display:
            xdg_session_type = "x11"

    env["XDG_SESSION_TYPE"] = xdg_session_type
    env["XDG_RUNTIME_DIR"] = user["xdg_runtime_dir"]

    if display:
        env["DISPLAY"] = display

    if wayland_display:
        # Make sure it's a full path if it's relative
        if not wayland_display.startswith("/"):
            wayland_display = f"{user['xdg_runtime_dir']}/{wayland_display}"
        env["WAYLAND_DISPLAY"] = wayland_display

    # XAUTHORITY (X11 auth cookie)
    xauthority = os.environ.get("XAUTHORITY")
    if not xauthority:
        # Try SUDO_USER's home
        candidate = os.path.join(user["home"], ".Xauthority")
        if os.path.exists(candidate):
            xauthority = candidate
        # Also try XDG_RUNTIME_DIR
        if not xauthority:
            candidate2 = f"{user['xdg_runtime_dir']}/Xauthority"
            if os.path.exists(candidate2):
                xauthority = candidate2
    if xauthority:
        env["XAUTHORITY"] = xauthority

    # Audio: PulseAudio / PipeWire
    pulse_server = os.environ.get("PULSE_SERVER")
    if not pulse_server:
        pulse_sock = f"{user['xdg_runtime_dir']}/pulse/native"
        if os.path.exists(pulse_sock):
            pulse_server = f"unix:{pulse_sock}"
    if pulse_server:
        env["PULSE_SERVER"] = pulse_server

    # PipeWire
    pw_remote = os.environ.get("PIPEWIRE_REMOTE")
    if not pw_remote:
        pw_sock = f"{user['xdg_runtime_dir']}/pipewire-0"
        if os.path.exists(pw_sock):
            pw_remote = pw_sock
    if pw_remote:
        env["PIPEWIRE_REMOTE"] = pw_remote

    # D-Bus session bus
    dbus = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    if not dbus:
        dbus = _probe_dbus(user["uid"])
    if dbus:
        env["DBUS_SESSION_BUS_ADDRESS"] = dbus

    env["HOME"] = user["home"]
    env["USER"] = user["username"]

    return env


def is_wayland_session() -> bool:
    """Returns True if the current session is Wayland."""
    env = get_display_env()
    session_type = env.get("XDG_SESSION_TYPE", "").lower()
    return session_type == "wayland" or ("WAYLAND_DISPLAY" in env and "DISPLAY" not in env)


def setup_xhost_for_root():
    """
    Grants root access to the X11 display (needed when we escalate via pkexec/sudo).
    Silently skips on Wayland.
    """
    if is_wayland_session():
        log.debug("Wayland session detected — skipping xhost setup")
        return
    display = get_display_env().get("DISPLAY", ":0")
    user = get_invoking_user()
    try:
        subprocess.run(
            ["xhost", f"+SI:localuser:{user['username']}", f"+SI:localuser:root"],
            env={**os.environ, "DISPLAY": display},
            capture_output=True,
            timeout=3,
        )
        subprocess.run(
            ["xhost", "+local:"],
            env={**os.environ, "DISPLAY": display},
            capture_output=True,
            timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        log.debug("xhost not available or timed out — skipping")


# ---------------------------------------------------------------------------
# DNS Resolution
# ---------------------------------------------------------------------------

def get_real_dns_servers() -> list:
    """
    Returns actual upstream DNS servers, bypassing systemd-resolved's 127.0.0.53 stub.
    Falls back to reading /etc/resolv.conf directly.
    """
    servers = []

    # Try resolvectl first (systemd-resolved)
    if _which("resolvectl"):
        try:
            out = subprocess.check_output(
                ["resolvectl", "dns"], text=True, stderr=subprocess.DEVNULL, timeout=3
            )
            for line in out.splitlines():
                # Lines like: "Global: 1.1.1.1 8.8.8.8" or "Link 2 (wlan0): 192.168.1.1"
                parts = line.split(":")
                if len(parts) >= 2:
                    for token in parts[1].split():
                        if _is_ip(token) and token != "127.0.0.53":
                            servers.append(token)
        except (subprocess.SubprocessError, FileNotFoundError):
            pass

    # Fallback: parse resolv.conf, skip stub
    if not servers:
        resolv = _resolve_resolv_conf_path()
        try:
            with open(resolv) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("nameserver"):
                        ip = line.split()[1] if len(line.split()) > 1 else ""
                        if _is_ip(ip) and ip != "127.0.0.53":
                            servers.append(ip)
        except OSError:
            pass

    # Ultimate fallback
    if not servers:
        servers = ["1.1.1.1", "8.8.8.8"]

    return list(dict.fromkeys(servers))  # deduplicate, preserve order


def write_namespace_resolv_conf(ns_etc_path: str):
    """
    Writes a proper resolv.conf into the namespace's /etc directory,
    using real upstream DNS servers (not 127.0.0.53).
    """
    os.makedirs(ns_etc_path, exist_ok=True)
    servers = get_real_dns_servers()
    resolv_path = os.path.join(ns_etc_path, "resolv.conf")
    with open(resolv_path, "w") as f:
        f.write("# Generated by InterMux\n")
        for s in servers:
            f.write(f"nameserver {s}\n")
    log.debug(f"Wrote resolv.conf to {resolv_path} with servers: {servers}")


# ---------------------------------------------------------------------------
# Running App Detection
# ---------------------------------------------------------------------------

def find_running_instances(app_path: str) -> list:
    """
    Finds PIDs of currently running instances of the given application.
    Matches against the executable path or name.
    Returns list of PIDs (integers).
    """
    app_name = os.path.basename(app_path)
    pids = []
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", app_name], text=True, stderr=subprocess.DEVNULL
        ).strip()
        if out:
            pids = [int(p) for p in out.splitlines() if p.strip().isdigit()]
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    # Exclude our own PID
    pids = [p for p in pids if p != os.getpid()]
    return pids


def kill_running_instances(app_path: str) -> bool:
    """
    Terminates all running instances of the app gracefully (SIGTERM), then SIGKILL.
    Returns True if all instances were killed.
    """
    import signal
    import time
    pids = find_running_instances(app_path)
    if not pids:
        return True
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    time.sleep(1.5)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return True


# ---------------------------------------------------------------------------
# Dependency Checking
# ---------------------------------------------------------------------------

def check_dependencies() -> list:
    """
    Checks that required system tools are present.
    Returns list of dicts: [{tool, missing, install_hint}]
    """
    distro = get_distro_info()
    pm = distro["package_manager"]

    install_cmds = {
        "apt": "sudo apt install -y {}",
        "pacman": "sudo pacman -S {}",
        "dnf": "sudo dnf install -y {}",
        "zypper": "sudo zypper install {}",
        "apk": "sudo apk add {}",
        "unknown": "install {} using your package manager",
    }
    # tool -> package name per PM
    packages = {
        "ip": {"apt": "iproute2", "pacman": "iproute2", "dnf": "iproute", "default": "iproute2"},
        "iptables": {"apt": "iptables", "pacman": "iptables", "dnf": "iptables", "default": "iptables"},
        "sysctl": {"apt": "procps", "pacman": "procps-ng", "dnf": "procps-ng", "default": "procps"},
        "pkexec": {"apt": "policykit-1", "pacman": "polkit", "dnf": "polkit", "default": "polkit"},
        "xhost": {"apt": "x11-xserver-utils", "pacman": "xorg-xhost", "dnf": "xorg-x11-server-utils", "default": "xhost"},
    }

    results = []
    for tool, pkg_map in packages.items():
        if not _which(tool):
            pkg = pkg_map.get(pm, pkg_map.get("default", tool))
            hint = install_cmds.get(pm, install_cmds["unknown"]).format(pkg)
            results.append({"tool": tool, "missing": True, "install_hint": hint})
        else:
            results.append({"tool": tool, "missing": False, "install_hint": ""})
    return results


def get_missing_dependencies() -> list:
    """Returns only the missing dependencies."""
    return [d for d in check_dependencies() if d["missing"]]


# ---------------------------------------------------------------------------
# App-specific Helpers
# ---------------------------------------------------------------------------

def build_app_flags(app_path: str, profile_dir: str = None) -> list:
    """
    Returns extra flags needed for specific apps to work in namespaces
    with the user's real profile, avoiding lock conflicts.

    For Firefox: uses --no-remote and a persistent per-iface profile
    (or prompts to close the running instance and use the default profile).
    """
    app_name = os.path.basename(app_path).lower()
    flags = []

    if "firefox" in app_name:
        flags.append("--no-remote")
        if profile_dir:
            flags += ["--profile", profile_dir]

    return flags


def get_or_create_firefox_profile(iface: str, user_home: str) -> str:
    """
    Returns path to a persistent Firefox profile for the given interface.
    Creates it if it doesn't exist.
    """
    profile_base = os.path.join(user_home, ".mozilla", "firefox", f"intermux-{iface}")
    os.makedirs(profile_base, exist_ok=True)
    
    # If running as root, make sure the real user owns the profile directory
    if os.geteuid() == 0:
        user = get_invoking_user()
        try:
            # Check intermediate directories too to avoid permission issues if they were created by root
            for p in [os.path.join(user_home, ".mozilla"),
                      os.path.join(user_home, ".mozilla", "firefox"),
                      profile_base]:
                if os.path.exists(p):
                    os.chown(p, user["uid"], user["gid"])
        except OSError:
            pass

    return profile_base


# ---------------------------------------------------------------------------
# Internal Helpers
# ---------------------------------------------------------------------------

def _which(cmd: str) -> str:
    """Returns full path to cmd if available, else empty string."""
    try:
        result = subprocess.run(
            ["which", cmd], capture_output=True, text=True, timeout=2
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _is_ip(token: str) -> bool:
    return bool(re.match(r"^\d{1,3}(\.\d{1,3}){3}$", token) or
                re.match(r"^[0-9a-fA-F:]{3,39}$", token))


def _resolve_resolv_conf_path() -> str:
    """Returns the real path of /etc/resolv.conf (follows symlinks)."""
    p = Path("/etc/resolv.conf")
    try:
        return str(p.resolve())
    except Exception:
        return "/etc/resolv.conf"


def _probe_user_session(uid: int):
    """
    Attempt to find DISPLAY/WAYLAND_DISPLAY for a given UID by inspecting
    /proc/<pid>/environ for processes owned by that user.
    Returns (display, wayland_display, session_type).
    """
    display = wayland_display = session_type = None
    try:
        for pid_dir in Path("/proc").iterdir():
            if not pid_dir.name.isdigit():
                continue
            try:
                if pid_dir.stat().st_uid != uid:
                    continue
                environ_file = pid_dir / "environ"
                if not environ_file.exists():
                    continue
                data = environ_file.read_bytes().decode("utf-8", errors="replace")
                env_vars = dict(
                    item.split("=", 1) for item in data.split("\x00")
                    if "=" in item
                )
                if not display and "DISPLAY" in env_vars:
                    display = env_vars["DISPLAY"]
                if not wayland_display and "WAYLAND_DISPLAY" in env_vars:
                    wayland_display = env_vars["WAYLAND_DISPLAY"]
                if not session_type and "XDG_SESSION_TYPE" in env_vars:
                    session_type = env_vars["XDG_SESSION_TYPE"]
                if display or wayland_display:
                    break
            except (PermissionError, ValueError, OSError):
                continue
    except Exception:
        pass
    return display, wayland_display, session_type


def _probe_dbus(uid: int) -> str:
    """Probe D-Bus session bus address for the given UID from /proc."""
    try:
        for pid_dir in Path("/proc").iterdir():
            if not pid_dir.name.isdigit():
                continue
            try:
                if pid_dir.stat().st_uid != uid:
                    continue
                environ_file = pid_dir / "environ"
                if not environ_file.exists():
                    continue
                data = environ_file.read_bytes().decode("utf-8", errors="replace")
                env_vars = dict(
                    item.split("=", 1) for item in data.split("\x00")
                    if "=" in item
                )
                if "DBUS_SESSION_BUS_ADDRESS" in env_vars:
                    return env_vars["DBUS_SESSION_BUS_ADDRESS"]
            except (PermissionError, ValueError, OSError):
                continue
    except Exception:
        pass
    return ""


if __name__ == "__main__":
    import json
    print("=== Distro ===")
    print(json.dumps(get_distro_info(), indent=2))
    print("\n=== Invoking User ===")
    print(json.dumps(get_invoking_user(), indent=2))
    print("\n=== Display Env ===")
    print(json.dumps(get_display_env(), indent=2))
    print("\n=== DNS Servers ===")
    print(get_real_dns_servers())
    print("\n=== Missing Dependencies ===")
    missing = get_missing_dependencies()
    if missing:
        for m in missing:
            print(f"  ✗ {m['tool']} — install with: {m['install_hint']}")
    else:
        print("  All dependencies satisfied!")
