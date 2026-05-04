#!/usr/bin/env python3
"""
InterMux Windows — Platform Utilities
Windows-specific helpers: UAC elevation, user detection, DNS, dependency checks,
process detection, and running-instance management.
"""

import os
import sys
import re
import ctypes
import subprocess
import logging
import socket
import shutil
import winreg
from pathlib import Path
from typing import List, Dict, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Privilege / UAC
# ---------------------------------------------------------------------------

def is_admin() -> bool:
    """Return True if the current process has Administrator privileges."""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except AttributeError:
        return False


def relaunch_as_admin():
    """
    Re-launch the current script with Administrator privileges via UAC.
    This exits the current (unprivileged) process.
    """
    script = sys.argv[0]
    params = " ".join(f'"{a}"' for a in sys.argv[1:])
    log.info("Requesting UAC elevation...")
    ret = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, f'"{script}" {params}', None, 1
    )
    if ret <= 32:
        log.error(f"UAC elevation failed (ShellExecute returned {ret})")
        sys.exit(1)
    sys.exit(0)


def ensure_admin():
    """
    If not running as admin, re-launch as admin via UAC and exit.
    Call this at the start of CLI/GUI entrypoints.
    """
    if not is_admin():
        relaunch_as_admin()


# ---------------------------------------------------------------------------
# Current User
# ---------------------------------------------------------------------------

def get_current_user() -> Dict:
    """
    Returns info about the current Windows user.
    Returns: {username, home, appdata, localappdata}
    """
    username = os.environ.get("USERNAME", os.environ.get("USER", "User"))
    home     = os.path.expanduser("~")
    appdata  = os.environ.get("APPDATA",      os.path.join(home, "AppData", "Roaming"))
    localapp = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
    return {
        "username":    username,
        "home":        home,
        "appdata":     appdata,
        "localappdata": localapp,
    }


# ---------------------------------------------------------------------------
# DNS Detection
# ---------------------------------------------------------------------------

def get_system_dns_servers() -> List[str]:
    """
    Returns upstream DNS servers from ipconfig /all.
    Filters out loopback (127.x.x.x).
    """
    servers: List[str] = []
    try:
        out = subprocess.check_output(
            ["ipconfig", "/all"],
            text=True, stderr=subprocess.DEVNULL,
            encoding="utf-8", errors="replace",
        )
        in_dns_block = False
        for line in out.splitlines():
            stripped = line.strip()
            if "DNS Servers" in stripped:
                in_dns_block = True
            if in_dns_block:
                ips = re.findall(r'\b\d{1,3}(?:\.\d{1,3}){3}\b', stripped)
                for ip in ips:
                    if not ip.startswith("127.") and ip not in servers:
                        servers.append(ip)
                # Stop at the next non-IP line after DNS block started
                if in_dns_block and stripped and not ips and "DNS Servers" not in stripped:
                    in_dns_block = False
    except Exception as e:
        log.warning(f"DNS detection failed: {e}")
    return servers or ["1.1.1.1", "8.8.8.8"]


# ---------------------------------------------------------------------------
# Process / Running-instance detection
# ---------------------------------------------------------------------------

def find_running_instances(app_path: str) -> List[int]:
    """
    Finds PIDs of currently running instances of the given application.
    Uses `tasklist` on Windows.
    Returns list of PIDs (integers), excluding our own PID.
    """
    app_name = os.path.basename(app_path).lower()
    pids: List[int] = []
    try:
        out = subprocess.check_output(
            ["tasklist", "/FO", "CSV", "/NH"],
            text=True, stderr=subprocess.DEVNULL,
            encoding="utf-8", errors="replace",
        )
        for line in out.splitlines():
            parts = [p.strip('"') for p in line.split('","')]
            if parts and parts[0].lower() == app_name:
                try:
                    pids.append(int(parts[1]))
                except (ValueError, IndexError):
                    pass
    except Exception as e:
        log.warning(f"tasklist failed: {e}")
    pids = [p for p in pids if p != os.getpid()]
    return pids


def kill_running_instances(app_path: str) -> bool:
    """
    Terminates all running instances of the given app using taskkill.
    Returns True if successful (or no instances found).
    """
    app_name = os.path.basename(app_path)
    try:
        subprocess.run(
            ["taskkill", "/IM", app_name, "/F"],
            capture_output=True, text=True,
        )
        return True
    except Exception as e:
        log.warning(f"taskkill failed for {app_name}: {e}")
        return False


# ---------------------------------------------------------------------------
# App-specific helpers
# ---------------------------------------------------------------------------

def build_firefox_proxy_prefs(proxy_host: str, proxy_port: int) -> Dict[str, str]:
    """
    Returns Firefox user.js preferences to configure the SOCKS5 proxy.
    These can be written to a temporary profile to force Firefox to use InterMux.
    """
    return {
        "network.proxy.type":             "1",
        "network.proxy.socks":            proxy_host,
        "network.proxy.socks_port":       str(proxy_port),
        "network.proxy.socks_version":    "5",
        "network.proxy.socks_remote_dns": "true",
        "network.proxy.no_proxies_on":    "",
    }


def get_or_create_firefox_profile(iface_name: str) -> str:
    """
    Returns path to a persistent Firefox profile for the given interface.
    Creates it if it doesn't exist.
    """
    user = get_current_user()
    profile_base = os.path.join(
        user["appdata"], "Mozilla", "Firefox", "Profiles", f"intermux-{iface_name}"
    )
    os.makedirs(profile_base, exist_ok=True)
    return profile_base


def write_firefox_proxy_userjs(profile_dir: str, proxy_host: str, proxy_port: int):
    """
    Writes user.js into a Firefox profile directory to set the SOCKS5 proxy.
    This overrides any existing proxy settings for that profile.
    """
    prefs = build_firefox_proxy_prefs(proxy_host, proxy_port)
    user_js = os.path.join(profile_dir, "user.js")
    with open(user_js, "w") as f:
        f.write("// InterMux — automatically generated proxy config\n")
        for key, val in prefs.items():
            f.write(f'user_pref("{key}", {val});\n')
    log.debug(f"Wrote Firefox proxy prefs to {user_js}")


def clear_firefox_locks(profile_dir: str):
    """Remove stale Firefox lock files so a new instance can start cleanly."""
    for lock in ("lock", "parent.lock", ".parentlock"):
        path = os.path.join(profile_dir, lock)
        try:
            if os.path.exists(path) or os.path.islink(path):
                os.remove(path)
                log.debug(f"Removed stale Firefox lock: {path}")
        except OSError as e:
            log.warning(f"Could not remove lock {path}: {e}")


def find_firefox_default_profile() -> str:
    """
    Finds the default Firefox profile directory on Windows.
    Returns path string or empty string.
    """
    import configparser
    user = get_current_user()
    profiles_ini = os.path.join(user["appdata"], "Mozilla", "Firefox", "profiles.ini")
    if not os.path.exists(profiles_ini):
        return ""
    cfg = configparser.ConfigParser()
    cfg.read(profiles_ini)
    for section in cfg.sections():
        if cfg.get(section, "Default", fallback="0") == "1":
            rel  = cfg.get(section, "IsRelative", fallback="1")
            path = cfg.get(section, "Path", fallback="")
            if not path:
                continue
            if rel == "1":
                return os.path.join(user["appdata"], "Mozilla", "Firefox", path)
            return path
    for section in cfg.sections():
        path = cfg.get(section, "Path", fallback="")
        if path:
            rel = cfg.get(section, "IsRelative", fallback="1")
            if rel == "1":
                return os.path.join(user["appdata"], "Mozilla", "Firefox", path)
            return path
    return ""


# ---------------------------------------------------------------------------
# Dependency checking
# ---------------------------------------------------------------------------

_REQUIRED_PYTHON_PACKAGES = [
    ("psutil",  "pip install psutil"),
]


def check_dependencies() -> List[Dict]:
    """
    Checks that required Python packages are installed.
    Returns list of {tool, missing, install_hint}.
    """
    results = []
    for pkg, hint in _REQUIRED_PYTHON_PACKAGES:
        try:
            __import__(pkg)
            results.append({"tool": pkg, "missing": False, "install_hint": ""})
        except ImportError:
            results.append({"tool": pkg, "missing": True, "install_hint": hint})
    return results


def get_missing_dependencies() -> List[Dict]:
    return [d for d in check_dependencies() if d["missing"]]


# ---------------------------------------------------------------------------
# Proxy environment variables
# ---------------------------------------------------------------------------

def build_proxy_env(proxy_port: int) -> Dict[str, str]:
    """
    Returns a dict of environment variables to set on launched apps
    so they route traffic through the local SOCKS5 proxy.
    """
    proxy_url = f"socks5://127.0.0.1:{proxy_port}"
    return {
        "HTTP_PROXY":   proxy_url,
        "HTTPS_PROXY":  proxy_url,
        "ALL_PROXY":    proxy_url,
        "all_proxy":    proxy_url,
        "http_proxy":   proxy_url,
        "https_proxy":  proxy_url,
    }

# ---------------------------------------------------------------------------
# App Path Fuzzy Finder
# ---------------------------------------------------------------------------

def find_app_path_fuzzy(app_name: str) -> Optional[str]:
    """
    Quickly resolves an application name to its full path.
    Checks exact matches, PATH (shutil.which), and Windows Registry App Paths.
    """
    if os.path.isabs(app_name) and os.path.exists(app_name):
        return app_name

    # Try shutil.which first
    resolved = shutil.which(app_name)
    if resolved:
        return resolved
    for ext in (".exe", ".cmd", ".bat"):
        resolved = shutil.which(app_name + ext)
        if resolved:
            return resolved

    # Try Windows Registry App Paths (Fuzzy match)
    app_name_lower = app_name.lower()
    if not app_name_lower.endswith(".exe"):
        app_name_lower += ".exe"

    for hkey in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hkey, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths") as key:
                num_subkeys = winreg.QueryInfoKey(key)[0]
                for i in range(num_subkeys):
                    try:
                        subkey_name = winreg.EnumKey(key, i)
                        if app_name_lower in subkey_name.lower():
                            with winreg.OpenKey(key, subkey_name) as subkey:
                                val, _ = winreg.QueryValueEx(subkey, "")
                                if val and os.path.exists(val):
                                    return val
                    except Exception:
                        pass
        except Exception:
            pass
            
    return None


if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.DEBUG)
    print("=== Admin check ===", is_admin())
    print("=== Current user ===")
    print(json.dumps(get_current_user(), indent=2))
    print("=== DNS servers ===", get_system_dns_servers())
    print("=== Missing deps ===", get_missing_dependencies())
