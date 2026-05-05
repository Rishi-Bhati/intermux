#!/usr/bin/env python3
"""
InterMux Platform Utilities
Cross-distro support: display detection, DNS, privilege separation, running app detection.
Fully ported for macOS (Darwin) and Linux compatibility.
"""

import os
import re
import pwd
import subprocess
import platform
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Global OS Check
IS_MAC = platform.system() == "Darwin"

# ---------------------------------------------------------------------------
# Distro Detection
# ---------------------------------------------------------------------------

def get_distro_info() -> dict:
    """
    Returns distro info. Handles macOS (Darwin) and various Linux distros.
    """
    if IS_MAC:
        return {
            "id": "macos",
            "name": f"macOS {platform.mac_ver()[0]}",
            "version_id": platform.mac_ver()[0],
            "package_manager": "brew" if _which("brew") else "unknown"
        }

    info = {"id": "unknown", "name": "Unknown Linux", "version_id": "", "package_manager": "unknown"}
    try:
        # Python 3.10+ stdlib
        release = platform.freedesktop_os_release()
        info["id"] = release.get("ID", "unknown").lower()
        info["name"] = release.get("NAME", "Unknown Linux")
        info["version_id"] = release.get("VERSION_ID", "")
    except (AttributeError, OSError):
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

    # macOS doesn't use /run/user/ hierarchy
    runtime_dir = f"/tmp/intermux-{uid}" if IS_MAC else f"/run/user/{uid}"

    return {
        "username": username,
        "uid": uid,
        "gid": gid,
        "home": home,
        "xdg_runtime_dir": runtime_dir,
    }

# ---------------------------------------------------------------------------
# Display / Session Detection
# ---------------------------------------------------------------------------

def get_display_env() -> dict:
    """
    Detects the display environment. Adapted for Aqua (macOS) and X11/Wayland (Linux).
    """
    user = get_invoking_user()
    env = {}

    if IS_MAC:
        # macOS native display management
        env["DISPLAY"] = os.environ.get("DISPLAY", ":0")
        env["HOME"] = user["home"]
        env["USER"] = user["username"]
        return env

    # Linux-specific probing
    display = os.environ.get("DISPLAY")
    wayland_display = os.environ.get("WAYLAND_DISPLAY")
    xdg_session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()

    if not display and not wayland_display:
        display, wayland_display, xdg_session_type = _probe_user_session(user["uid"])

    if not xdg_session_type:
        if wayland_display and not display:
            xdg_session_type = "wayland"
        elif display:
            xdg_session_type = "x11"

    env["XDG_SESSION_TYPE"] = xdg_session_type
    env["XDG_RUNTIME_DIR"] = user["xdg_runtime_dir"]

    if display: env["DISPLAY"] = display
    if wayland_display:
        if not wayland_display.startswith("/"):
            wayland_display = f"{user['xdg_runtime_dir']}/{wayland_display}"
        env["WAYLAND_DISPLAY"] = wayland_display

    # XAUTHORITY (X11 auth cookie)
    xauthority = os.environ.get("XAUTHORITY")
    if not xauthority:
        candidate = os.path.join(user["home"], ".Xauthority")
        if os.path.exists(candidate): xauthority = candidate
    if xauthority: env["XAUTHORITY"] = xauthority

    env["HOME"] = user["home"]
    env["USER"] = user["username"]

    return env

def is_wayland_session() -> bool:
    if IS_MAC: return False
    env = get_display_env()
    session_type = env.get("XDG_SESSION_TYPE", "").lower()
    return session_type == "wayland"

def setup_xhost_for_root():
    if IS_MAC or is_wayland_session():
        return
    display = get_display_env().get("DISPLAY", ":0")
    user = get_invoking_user()
    try:
        subprocess.run(["xhost", f"+SI:localuser:{user['username']}"], capture_output=True, timeout=3)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# DNS Resolution
# ---------------------------------------------------------------------------

def get_real_dns_servers() -> list:
    """
    Returns upstream DNS servers. Uses scutil on Mac, resolvectl/resolv.conf on Linux.
    """
    servers = []

    if IS_MAC:
        try:
            out = subprocess.check_output(["scutil", "--dns"], text=True)
            for line in out.splitlines():
                if "nameserver" in line:
                    ip = line.split(":")[-1].strip()
                    if _is_ip(ip) and ip != "127.0.0.1":
                        servers.append(ip)
        except Exception: pass

    if not servers and _which("resolvectl"):
        try:
            out = subprocess.check_output(["resolvectl", "dns"], text=True, timeout=3)
            for line in out.splitlines():
                parts = line.split(":")
                if len(parts) >= 2:
                    for token in parts[1].split():
                        if _is_ip(token) and token != "127.0.0.53":
                            servers.append(token)
        except Exception: pass

    if not servers:
        try:
            with open("/etc/resolv.conf") as f:
                for line in f:
                    if line.startswith("nameserver"):
                        ip = line.split()[1]
                        if _is_ip(ip) and ip != "127.0.0.53": servers.append(ip)
        except OSError: pass

    return list(dict.fromkeys(servers)) or ["1.1.1.1", "8.8.8.8"]

# ---------------------------------------------------------------------------
# Dependency Checking
# ---------------------------------------------------------------------------

def check_dependencies() -> list:
    if IS_MAC:
        # Essential macOS networking tools
        tools = ["networksetup", "route", "scutil", "pfctl"]
        return [{"tool": t, "missing": not _which(t), "install_hint": ""} for t in tools]

    distro = get_distro_info()
    pm = distro["package_manager"]
    packages = {
        "ip": {"apt": "iproute2", "default": "iproute2"},
        "iptables": {"default": "iptables"},
        "sysctl": {"default": "procps"},
        "pkexec": {"default": "polkit"},
    }

    results = []
    for tool, pkg_map in packages.items():
        if not _which(tool):
            results.append({"tool": tool, "missing": True, "install_hint": f"Install {tool}"})
        else:
            results.append({"tool": tool, "missing": False, "install_hint": ""})
    return results

# ---------------------------------------------------------------------------
# App-specific Helpers
# ---------------------------------------------------------------------------

def get_or_create_firefox_profile(iface: str, user_home: str) -> str:
    profile_base = os.path.join(user_home, "Library/Application Support/Firefox/Profiles" if IS_MAC else ".mozilla/firefox", f"intermux-{iface}")
    os.makedirs(profile_base, exist_ok=True)
    
    if os.geteuid() == 0:
        user = get_invoking_user()
        try:
            os.chown(profile_base, user["uid"], user["gid"])
        except OSError: pass

    return profile_base

# ---------------------------------------------------------------------------
# Internal Helpers
# ---------------------------------------------------------------------------

def _which(cmd: str) -> str:
    try:
        return subprocess.check_output(["which", cmd], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception: return ""

def _is_ip(token: str) -> bool:
    return bool(re.match(r"^\d{1,3}(\.\d{1,3}){3}$", token))

def _probe_user_session(uid: int):
    if IS_MAC: return os.environ.get("DISPLAY"), None, "aqua"
    # Linux-only /proc probing logic
    try:
        for pid_dir in Path("/proc").iterdir():
            if not pid_dir.name.isdigit() or pid_dir.stat().st_uid != uid: continue
            env_file = pid_dir / "environ"
            if not env_file.exists(): continue
            data = env_file.read_bytes().decode("utf-8", errors="replace")
            env = dict(i.split("=", 1) for i in data.split("\x00") if "=" in i)
            return env.get("DISPLAY"), env.get("WAYLAND_DISPLAY"), env.get("XDG_SESSION_TYPE")
    except Exception: pass
    return None, None, None

if __name__ == "__main__":
    import json
    print(f"--- Running on {platform.system()} ---")
    print(json.dumps(get_distro_info(), indent=2))
    print(f"DNS: {get_real_dns_servers()}")