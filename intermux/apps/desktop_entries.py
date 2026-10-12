"""Discovery of launchable applications from XDG .desktop files."""
from __future__ import annotations

import logging
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set

from intermux.system.commands import which

log = logging.getLogger("intermux.apps.desktop_entries")

FIELD_CODES = frozenset({"%f", "%F", "%u", "%U", "%i", "%c", "%k",
                         "%d", "%D", "%n", "%N", "%v", "%m"})
FLATPAK_REASON = "Flatpak apps run in their own sandbox (not supported yet)"
MISSING_BINARY_REASON = "Application binary not found"


@dataclass
class AppEntry:
    name: str
    comment: str
    icon: str
    exec_argv: List[str]          # cleaned argv (field codes removed)
    desktop_file: str
    binary: Optional[str]         # resolved absolute path of argv[0] or None
    bindable: bool
    reason: str                   # "" when bindable, else why not


def desktop_dirs() -> List[Path]:
    """Directories searched for .desktop files, highest priority first."""
    home = Path(os.path.expanduser("~"))
    data_home = os.environ.get("XDG_DATA_HOME") or str(home / ".local" / "share")
    data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    candidates = [Path(data_home) / "applications"]
    candidates += [Path(d) / "applications" for d in data_dirs.split(":") if d]
    candidates += [
        Path("/var/lib/flatpak/exports/share/applications"),
        home / ".local" / "share" / "flatpak" / "exports" / "share" / "applications",
        Path("/var/lib/snapd/desktop/applications"),
    ]
    unique: List[Path] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def clean_exec(exec_value: str) -> List[str]:
    """Split a desktop Exec value into argv, dropping field codes. Pure."""
    if not exec_value or not exec_value.strip():
        return []
    try:
        tokens = shlex.split(exec_value)
    except ValueError:
        return []
    argv: List[str] = []
    for token in tokens:
        if token in FIELD_CODES:
            continue
        argv.append(token.replace("%%", "%"))
    return argv


def parse_desktop_file(path: Path) -> Optional[AppEntry]:
    """Parse the [Desktop Entry] group of a .desktop file; None if it is not a launchable application."""
    keys = _read_main_group(Path(path))
    if keys is None:
        return None
    if keys.get("Type") != "Application":
        return None
    if _is_true(keys.get("Hidden")) or _is_true(keys.get("NoDisplay")):
        return None
    name = keys.get("Name", "")
    exec_value = keys.get("Exec", "")
    if not name or not exec_value:
        return None

    argv = clean_exec(exec_value)
    binary = _resolve_binary(argv[0]) if argv else None
    if argv and os.path.basename(argv[0]) == "flatpak":
        bindable, reason = False, FLATPAK_REASON
    elif binary is None:
        bindable, reason = False, MISSING_BINARY_REASON
    else:
        bindable, reason = True, ""

    return AppEntry(
        name=name,
        comment=keys.get("Comment", ""),
        icon=keys.get("Icon", ""),
        exec_argv=argv,
        desktop_file=str(path),
        binary=binary,
        bindable=bindable,
        reason=reason,
    )


def scan_desktop_entries(dirs: Optional[Sequence[Path]] = None) -> List[AppEntry]:
    """Scan .desktop files; the first directory holding a given basename wins."""
    search = desktop_dirs() if dirs is None else [Path(d) for d in dirs]
    seen: Set[str] = set()
    entries: List[AppEntry] = []
    for directory in search:
        if not directory.is_dir():
            continue
        try:
            files = sorted(directory.glob("*.desktop"))
        except OSError as exc:
            log.debug("cannot list %s: %s", directory, exc)
            continue
        for desktop_file in files:
            if desktop_file.name in seen:
                continue
            seen.add(desktop_file.name)
            entry = parse_desktop_file(desktop_file)
            if entry is not None:
                entries.append(entry)
    entries.sort(key=lambda entry: entry.name.casefold())
    return entries


def search_entries(entries: Sequence[AppEntry], query: str) -> List[AppEntry]:
    """Case-insensitive substring match over name, comment and binary."""
    needle = query.strip().casefold()
    if not needle:
        return list(entries)
    matches: List[AppEntry] = []
    for entry in entries:
        haystacks = [entry.name, entry.comment, entry.binary or ""]
        if any(needle in text.casefold() for text in haystacks):
            matches.append(entry)
    return matches


def _read_main_group(path: Path) -> Optional[Dict[str, str]]:
    """Return the keys of the [Desktop Entry] group, or None if the file cannot be read."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.debug("cannot read %s: %s", path, exc)
        return None
    keys: Dict[str, str] = {}
    in_main = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            in_main = line == "[Desktop Entry]"
            continue
        if not in_main or "=" not in line:
            continue
        key, value = line.split("=", 1)
        keys.setdefault(key.strip(), value.strip())
    return keys


def _resolve_binary(command: str) -> Optional[str]:
    found = which(command)
    return os.path.abspath(found) if found else None


def _is_true(value: Optional[str]) -> bool:
    return value is not None and value.strip().lower() == "true"
