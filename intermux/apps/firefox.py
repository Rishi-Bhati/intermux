"""Firefox profile handling: the default profile and per-interface separate profiles."""
from __future__ import annotations

import logging
import os
import re
import stat
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from intermux.apps import AppHandler, LaunchContext
from intermux.system import procs, safefs
from intermux.system.session import UserInfo

log = logging.getLogger("intermux.apps.firefox")

FIREFOX_PROFILE_PREFIX = "intermux-"
LOCK_NAMES = ("lock", ".parentlock")
_IFACE_BAD_CHARS = re.compile(r"[^A-Za-z0-9_.-]")


class FirefoxHandler(AppHandler):
    name = "firefox"

    def comm_names(self, app_path: str) -> List[str]:
        return ["firefox", "firefox-bin"]

    def prepare(self, ctx: LaunchContext, app_path: str) -> None:
        home = ctx.user.home
        if ctx.profile_mode == "separate":
            pdir = separate_profile_dir(home, ctx.iface)
            _ensure_profile_dir(pdir, ctx.user)
            if _profile_in_use(pdir):
                log.info("separate profile %s is still in use; leaving its locks alone", pdir)
                return
            remove_stale_locks(pdir, home)
            return
        pdir = default_profile_dir(home)
        if pdir is None:
            log.info("no default Firefox profile found under %s", home)
            return
        if procs.find_app_procs(app_path, self.comm_names(app_path), uid=ctx.user.uid):
            log.info("Firefox is running; leaving the default profile locks alone")
            return
        remove_stale_locks(pdir, home)

    def extra_args(self, ctx: LaunchContext) -> List[str]:
        if ctx.profile_mode == "separate":
            profile = separate_profile_dir(ctx.user.home, ctx.iface)
            return ["--no-remote", "--profile", str(profile)]
        return ["--no-remote"]


def _profile_in_use(profile_dir: Path) -> bool:
    """True if a running Firefox was started with this profile directory on its command line."""
    needle = str(profile_dir).encode()
    for proc in procs.list_procs():
        if proc.comm not in ("firefox", "firefox-bin"):
            continue
        try:
            cmdline = Path(f"/proc/{proc.pid}/cmdline").read_bytes()
        except OSError:
            continue
        if needle in cmdline:
            return True
    return False


def profile_roots(home: str) -> List[Path]:
    """Directories that may hold profiles.ini (native, Snap and Flatpak installs)."""
    base = Path(home)
    return [
        base / ".mozilla" / "firefox",
        base / "snap" / "firefox" / "common" / ".mozilla" / "firefox",
        base / ".var" / "app" / "org.mozilla.firefox" / ".mozilla" / "firefox",
    ]


def default_profile_dir(home: str) -> Optional[Path]:
    """Return the default profile directory, or None if none exists on disk."""
    for root in profile_roots(home):
        ini = root / "profiles.ini"
        if not ini.is_file():
            continue
        try:
            text = ini.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("cannot read %s: %s", ini, exc)
            return None
        profile = parse_default_profile(text, root)
        if profile is None or not profile.is_dir():
            return None
        return profile
    return None


def parse_default_profile(ini_text: str, root: Path) -> Optional[Path]:
    """Pick the default profile from profiles.ini text. Pure: no filesystem access."""
    sections = _ini_sections(ini_text)
    installs = [keys for name, keys in sections
                if name.startswith("Install") and keys.get("Default", "").strip()]
    profiles = [keys for name, keys in sections
                if name.startswith("Profile") and keys.get("Path", "").strip()]

    if installs:
        chosen = installs[0]
        value = chosen["Default"]
    else:
        tagged = [keys for keys in profiles if keys.get("Default", "").strip() == "1"]
        candidates = tagged or profiles
        if not candidates:
            return None
        chosen = candidates[0]
        value = chosen["Path"]

    value = value.strip()
    relative = chosen.get("IsRelative", "1").strip() == "1"
    return Path(root) / value if relative else Path(value)


# Limitation: a Snap- or Flatpak-confined Firefox cannot read this path under
# ~/.mozilla/firefox, so the separate profile does not work for those builds.
# Default-profile discovery (default_profile_dir) does cover the Snap and Flatpak locations.
def separate_profile_dir(home: str, iface: str) -> Path:
    """Profile directory used for a per-interface separate Firefox profile."""
    safe = _IFACE_BAD_CHARS.sub("_", iface)
    return Path(home) / ".mozilla" / "firefox" / (FIREFOX_PROFILE_PREFIX + safe)


def remove_stale_locks(profile_dir: Path, home: str) -> List[str]:
    """Delete Firefox lock files in a profile directory under home; return the names removed.

    The profile directory is resolved first, so a profiles.ini that points outside home
    is refused. Only lock names and non-directory entries are removed.
    """
    if not safefs.is_within(os.fspath(profile_dir), home):
        log.warning("refusing to remove Firefox locks outside the home directory: %s", profile_dir)
        return []
    real_dir = os.path.realpath(profile_dir)
    removed: List[str] = []
    try:
        dir_fd = os.open(real_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return removed
    try:
        for name in LOCK_NAMES:
            try:
                mode = os.lstat(name, dir_fd=dir_fd).st_mode
            except OSError:
                continue
            if not (stat.S_ISLNK(mode) or stat.S_ISREG(mode)):
                continue
            try:
                os.unlink(name, dir_fd=dir_fd)
            except OSError as exc:
                log.warning("could not remove %s in %s: %s", name, real_dir, exc)
                continue
            removed.append(name)
            log.info("removed stale Firefox lock %s", os.path.join(real_dir, name))
    finally:
        os.close(dir_fd)
    return removed


def _ini_sections(text: str) -> List[Tuple[str, Dict[str, str]]]:
    """Split INI text into (section name, keys) pairs; the first occurrence of a key wins."""
    sections: List[Tuple[str, Dict[str, str]]] = []
    current: Optional[Dict[str, str]] = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith((";", "#")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = {}
            sections.append((line[1:-1], current))
            continue
        if current is None or "=" not in line:
            continue
        key, value = line.split("=", 1)
        current.setdefault(key.strip(), value.strip())
    return sections


def _ensure_profile_dir(path: Path, user: UserInfo) -> None:
    """Create the profile directory under the user's home, owned by the user, mode 0700.

    Refuses symlinks and paths outside the home (ValidationError). Other OS errors are logged.
    """
    try:
        safefs.ensure_user_dir(Path(path), Path(user.home), user.uid, user.gid, 0o700)
    except OSError as exc:
        log.warning("could not prepare profile directory %s: %s", path, exc)
