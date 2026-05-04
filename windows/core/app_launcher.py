#!/usr/bin/env python3
"""
InterMux Windows — App Launcher
Starts applications with SOCKS5 proxy environment variables injected so that
all their TCP traffic exits through the chosen network adapter.

For Firefox specifically, we also write proxy preferences directly into the
profile (user.js) for rock-solid proxy enforcement regardless of system settings.
"""

import os
import sys
import logging
import subprocess
import shutil
from typing import Optional

from windows.core.proxy_engine import proxy_registry
from windows.core import platform_utils as plat

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# App type detection helpers
# ---------------------------------------------------------------------------

def _is_firefox(app_path: str) -> bool:
    return "firefox" in os.path.basename(app_path).lower()


def _is_chromium(app_path: str) -> bool:
    name = os.path.basename(app_path).lower()
    return any(k in name for k in ("chrome", "chromium", "msedge", "edge"))


def _resolve_app(app: str) -> Optional[str]:
    """
    Resolve a short command name or path to a full executable path.
    Returns None if not found.
    """
    return plat.find_app_path_fuzzy(app)


# ---------------------------------------------------------------------------
# Core launcher
# ---------------------------------------------------------------------------

def launch_app(app_path: str, iface_name: str, bind_ip: str, use_new_profile: bool = False) -> subprocess.Popen:
    """
    Launch *app_path* with its traffic bound to *bind_ip* via an InterMux SOCKS5 proxy.

    Steps:
      1. Ensure a SOCKS5 proxy is running for bind_ip (starts one if not).
      2. Build proxy environment variables.
      3. For Firefox: also write user.js proxy prefs into the profile.
      4. Spawn the app with Popen, inheriting the current env + proxy vars.

    Returns the Popen object.
    """
    resolved = _resolve_app(app_path)
    if resolved is None:
        raise FileNotFoundError(f"Could not find application: '{app_path}'")

    app_path = resolved
    app_name = os.path.basename(app_path).lower()

    # --- Start / get proxy ---
    proxy_port = proxy_registry.get_or_start(bind_ip)
    log.info(f"[launcher] Proxy for {bind_ip} on port {proxy_port}")

    # --- Build environment ---
    env = os.environ.copy()
    env.update(plat.build_proxy_env(proxy_port))

    # --- Extra flags and profile handling ---
    extra_flags = []

    if _is_firefox(app_path):
        if use_new_profile:
            profile_dir = plat.get_or_create_firefox_profile(iface_name)
            plat.clear_firefox_locks(profile_dir)
            plat.write_firefox_proxy_userjs(profile_dir, "127.0.0.1", proxy_port)
            extra_flags += ["-new-instance", "--no-remote", "--profile", profile_dir]
            log.info(f"[launcher] Firefox → new profile: {profile_dir}")
        else:
            default_profile = plat.find_firefox_default_profile()
            if default_profile:
                plat.clear_firefox_locks(default_profile)
                plat.write_firefox_proxy_userjs(default_profile, "127.0.0.1", proxy_port)
                log.info(f"[launcher] Firefox → default profile: {default_profile}")
            extra_flags += ["-new-instance", "--no-remote"]

    elif _is_chromium(app_path):
        # Chromium-based browsers support --proxy-server flag directly
        extra_flags += [
            f"--proxy-server=socks5://127.0.0.1:{proxy_port}",
            "--proxy-bypass-list=<-loopback>",
        ]
        log.info(f"[launcher] Chromium → proxy flag mode")

    # --- Spawn ---
    cmd = [app_path] + extra_flags
    log.info(f"[launcher] Spawning: {' '.join(cmd)}")
    log.info(f"[launcher] Proxy env: ALL_PROXY=socks5://127.0.0.1:{proxy_port}")

    creationflags = subprocess.CREATE_NEW_CONSOLE if os.name == 'nt' else 0
    proc = subprocess.Popen(
        cmd,
        env=env,
        creationflags=creationflags,
        close_fds=True,
    )
    log.info(f"[launcher] PID {proc.pid} → '{os.path.basename(app_path)}' via {bind_ip}")
    return proc


# ---------------------------------------------------------------------------
# Session tracker (lightweight — tracks what's bound to what)
# ---------------------------------------------------------------------------

class SessionTracker:
    """Tracks which apps are currently bound to which interface."""

    def __init__(self):
        self._sessions: list[dict] = []   # [{app, iface, bind_ip, proxy_port, pid}]

    def add(self, app: str, iface: str, bind_ip: str, proxy_port: int, pid: int):
        self._sessions.append({
            "app":        os.path.basename(app),
            "iface":      iface,
            "bind_ip":    bind_ip,
            "proxy_port": proxy_port,
            "pid":        pid,
        })

    def remove(self, pid: int):
        self._sessions = [s for s in self._sessions if s["pid"] != pid]

    def all(self) -> list:
        return list(self._sessions)

    def clear(self):
        self._sessions.clear()


session_tracker = SessionTracker()


if __name__ == "__main__":
    import time
    logging.basicConfig(level=logging.DEBUG)

    # Quick test — launch notepad via a proxy bound to the default interface
    import socket as _s
    bind_ip = "0.0.0.0"
    with _s.socket(_s.AF_INET, _s.SOCK_DGRAM) as sock:
        try:
            sock.connect(("8.8.8.8", 80))
            bind_ip = sock.getsockname()[0]
        except Exception:
            pass

    print(f"Launching notepad via proxy on {bind_ip}...")
    proc = launch_app("notepad.exe", "test", bind_ip)
    print(f"PID: {proc.pid}")
    time.sleep(3)
    proc.terminate()
    proxy_registry.stop_all()
