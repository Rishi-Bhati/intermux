"""Desktop session discovery for the invoking user, and the environment passed to GUI apps."""

from __future__ import annotations

import logging
import os
import pwd
from dataclasses import dataclass
from typing import Dict, Iterable, Optional

from intermux import config
from intermux.errors import NotFoundError
from intermux.system import procs

log = logging.getLogger("intermux.system.session")

_PROBE_KEYS = ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "XDG_SESSION_TYPE")


@dataclass
class UserInfo:
    uid: int
    gid: int
    username: str
    home: str

    @property
    def runtime_dir(self) -> str:
        return f"/run/user/{self.uid}"


@dataclass
class Session:
    user: UserInfo
    session_type: str                      # "x11" | "wayland" | "unknown"
    display: Optional[str] = None          # e.g. ":0"
    wayland_display: Optional[str] = None  # socket name, e.g. "wayland-0"
    xauthority: Optional[str] = None
    dbus_address: Optional[str] = None
    pulse_server: Optional[str] = None
    pipewire_remote: Optional[str] = None


def user_for_uid(uid: int) -> UserInfo:
    """Look up a user by uid; raises NotFoundError if there is no such user."""
    try:
        entry = pwd.getpwuid(uid)
    except KeyError:
        raise NotFoundError(f"no user with uid {uid}") from None
    return UserInfo(uid=entry.pw_uid, gid=entry.pw_gid, username=entry.pw_name, home=entry.pw_dir)


def _uid_from_env(name: str) -> Optional[int]:
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        log.warning("ignoring non-numeric %s=%r", name, raw)
        return None


def invoking_user() -> UserInfo:
    """Return the user who started us: PKEXEC_UID, then SUDO_UID, then the real uid."""
    uid = _uid_from_env("PKEXEC_UID")
    if uid is None:
        uid = _uid_from_env("SUDO_UID")
    if uid is None:
        uid = os.getuid()
    return user_for_uid(uid)


def _probe_user_env(uid: int, keys: Iterable[str]) -> Dict[str, str]:
    """Take the first value of each key found in the environment of a process owned by uid."""
    wanted = tuple(keys)
    found: Dict[str, str] = {}
    for proc in procs.list_procs():
        if proc.uid != uid:
            continue
        env = procs.proc_environ(proc.pid)
        for key in wanted:
            if key not in found and env.get(key):
                found[key] = env[key]
        if len(found) == len(wanted):
            break
    return found


def _existing(path: str) -> Optional[str]:
    return path if os.path.exists(path) else None


def detect_session(user: Optional[UserInfo] = None, probe: bool = True) -> Session:
    """Describe the desktop session of `user` (default: invoking user)."""
    if user is None:
        user = invoking_user()
    env = os.environ
    display = env.get("DISPLAY") or None
    wayland = env.get("WAYLAND_DISPLAY") or None
    dbus = env.get("DBUS_SESSION_BUS_ADDRESS") or None
    xdg_type = env.get("XDG_SESSION_TYPE") or None

    if display is None and wayland is None and probe:
        found = _probe_user_env(user.uid, _PROBE_KEYS)
        display = found.get("DISPLAY")
        wayland = found.get("WAYLAND_DISPLAY")
        dbus = dbus or found.get("DBUS_SESSION_BUS_ADDRESS")
        xdg_type = xdg_type or found.get("XDG_SESSION_TYPE")

    runtime = user.runtime_dir
    xauthority = next(
        (c for c in (env.get("XAUTHORITY"),
                     os.path.join(user.home, ".Xauthority"),
                     os.path.join(runtime, "Xauthority"))
         if c and os.path.exists(c)),
        None,
    )
    bus_socket = os.path.join(runtime, "bus")
    dbus_address = dbus or (f"unix:path={bus_socket}" if os.path.exists(bus_socket) else None)
    pulse_server = env.get("PULSE_SERVER") or _existing(os.path.join(runtime, "pulse", "native"))
    pipewire_remote = env.get("PIPEWIRE_REMOTE") or _existing(os.path.join(runtime, "pipewire-0"))

    if xdg_type in ("x11", "wayland"):
        session_type = xdg_type
    elif wayland:
        session_type = "wayland"
    elif display:
        session_type = "x11"
    else:
        session_type = "unknown"

    return Session(
        user=user,
        session_type=session_type,
        display=display,
        wayland_display=wayland,
        xauthority=xauthority,
        dbus_address=dbus_address,
        pulse_server=pulse_server,
        pipewire_remote=pipewire_remote,
    )


def _wayland_path(name: Optional[str], runtime: str) -> Optional[str]:
    if not name:
        return None
    return name if os.path.isabs(name) else os.path.join(runtime, name)


def build_app_env(session: Session) -> Dict[str, str]:
    """Return the environment for GUI apps: allowlisted keys with values, plus XDG_RUNTIME_DIR and XDG_SESSION_TYPE."""
    runtime = session.user.runtime_dir
    session_values = {
        "DISPLAY": session.display,
        "WAYLAND_DISPLAY": _wayland_path(session.wayland_display, runtime),
        "XDG_RUNTIME_DIR": runtime,
        "XDG_SESSION_TYPE": session.session_type,
        "XAUTHORITY": session.xauthority,
        "DBUS_SESSION_BUS_ADDRESS": session.dbus_address,
        "PULSE_SERVER": session.pulse_server,
        "PIPEWIRE_REMOTE": session.pipewire_remote,
    }
    app_env: Dict[str, str] = {}
    for key in sorted(config.ENV_ALLOWLIST):
        # Keys not carried by Session (locale, theme, desktop) come from the calling process.
        value = session_values[key] if key in session_values else os.environ.get(key)
        if value:
            app_env[key] = value
    app_env["XDG_RUNTIME_DIR"] = runtime
    app_env["XDG_SESSION_TYPE"] = session.session_type
    return app_env
