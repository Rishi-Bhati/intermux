"""Windows application discovery, path resolution, browser profile separation, and process lookup."""

from __future__ import annotations

import configparser
import ctypes
import logging
import ntpath
import os
import re
import shutil
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from intermux.errors import ConflictError, NotFoundError
from intermux.platform import get_paths

log = logging.getLogger("intermux.platform.win_apps")

_IFACE_BAD_CHARS = re.compile(r"[^A-Za-z0-9_.-]")
_CHROMIUM_FAMILIES: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("brave", "brave", ("brave.exe",)),
    ("chrome", "chrome", ("chrome.exe",)),
    ("chromium", "chromium", ("chromium.exe",)),
    ("vivaldi", "vivaldi", ("vivaldi.exe",)),
    ("msedge", "msedge", ("msedge.exe",)),
    ("opera", "opera", ("opera.exe",)),
)
_FIREFOX_LOCKS = ("lock", ".parentlock", "parent.lock")


@dataclass
class WinAppEntry:
    name: str
    comment: str
    icon: str
    exec_argv: List[str]
    desktop_file: str
    binary: Optional[str]
    bindable: bool
    reason: str


def chromium_family(app_path: str) -> Tuple[str, Tuple[str, ...]]:
    stem = ntpath.splitext(ntpath.basename(app_path).lower())[0]
    for prefix, family, names in _CHROMIUM_FAMILIES:
        if stem == prefix or stem.startswith(prefix + "-"):
            return family, names
    return "chromium", ()


class WinAppHandler:
    name = "generic"

    def comm_names(self, app_path: str) -> List[str]:
        return [ntpath.basename(app_path).lower()]

    def prepare(self, iface: str, profile_mode: str, app_path: str) -> None:
        return None

    def extra_args(self, iface: str, profile_mode: str) -> List[str]:
        return []


class WinChromiumHandler(WinAppHandler):
    name = "chromium"

    def __init__(self, app_path: str = "") -> None:
        self.family, self._names = chromium_family(app_path)

    def comm_names(self, app_path: str) -> List[str]:
        names = {ntpath.basename(app_path).lower()}
        names.update(self._names)
        return sorted(names)

    def profile_dir(self, iface: str) -> Path:
        safe = _IFACE_BAD_CHARS.sub("_", iface)
        return get_paths("windows").profile_dir / f"{self.family}-{safe}"

    def prepare(self, iface: str, profile_mode: str, app_path: str) -> None:
        if profile_mode == "separate":
            try:
                self.profile_dir(iface).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ConflictError(
                    f"cannot create separate browser profile {self.profile_dir(iface)}: {exc}",
                    code="profile_unwritable",
                ) from exc

    def extra_args(self, iface: str, profile_mode: str) -> List[str]:
        if profile_mode == "separate":
            return [f"--user-data-dir={self.profile_dir(iface)}"]
        return []


class WinFirefoxHandler(WinAppHandler):
    name = "firefox"

    def comm_names(self, app_path: str) -> List[str]:
        return sorted({"firefox.exe", ntpath.basename(app_path).lower()})

    def separate_profile_dir(self, iface: str) -> Path:
        safe = _IFACE_BAD_CHARS.sub("_", iface)
        return get_paths("windows").profile_dir / f"firefox-{safe}"

    def default_profile_dir(self) -> Optional[Path]:
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        root = Path(appdata) / "Mozilla" / "Firefox"
        ini_path = root / "profiles.ini"
        if not ini_path.is_file():
            return None
        cp = configparser.RawConfigParser()
        try:
            cp.read(ini_path, encoding="utf-8", errors="replace")
        except Exception:
            return None
        for sec in cp.sections():
            if sec.startswith("Install") and cp.has_option(sec, "Default"):
                rel = cp.get(sec, "Default").strip()
                if rel:
                    p = root / rel
                    return p if p.is_dir() else None
        for sec in cp.sections():
            if sec.startswith("Profile") and cp.get(sec, "Default", fallback="").strip() == "1":
                val = cp.get(sec, "Path", fallback="").strip()
                is_rel = cp.get(sec, "IsRelative", fallback="1").strip() == "1"
                if val:
                    p = (root / val) if is_rel else Path(val)
                    return p if p.is_dir() else None
        return None

    def _profile_in_use(self, pdir: Path, app_path: str) -> bool:
        if not find_running_pids(app_path, self.comm_names(app_path)):
            return False
        if sys.platform != "win32":
            return False
        try:
            proc = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='firefox.exe'\" "
                    "| Select-Object -ExpandProperty CommandLine",
                ],
                capture_output=True,
                text=True,
                timeout=6,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode != 0:
                return False
            needle = ntpath.normcase(str(pdir))
            return any(needle in ntpath.normcase(line) for line in proc.stdout.splitlines())
        except Exception:
            return False

    def prepare(self, iface: str, profile_mode: str, app_path: str) -> None:
        if profile_mode == "separate":
            pdir = self.separate_profile_dir(iface)
            pdir.mkdir(parents=True, exist_ok=True)
            if self._profile_in_use(pdir, app_path):
                return
            for lock_name in _FIREFOX_LOCKS:
                lp = pdir / lock_name
                if lp.exists():
                    try:
                        lp.unlink()
                    except OSError:
                        pass
            return
        pdir = self.default_profile_dir()
        if pdir and not find_running_pids(app_path, self.comm_names(app_path)):
            for lock_name in _FIREFOX_LOCKS:
                lp = pdir / lock_name
                if lp.exists():
                    try:
                        lp.unlink()
                    except OSError:
                        pass

    def extra_args(self, iface: str, profile_mode: str) -> List[str]:
        if profile_mode == "separate":
            return ["-new-instance", "--no-remote", "--profile", str(self.separate_profile_dir(iface))]
        return ["--no-remote"]


def get_handler(app_path: str) -> WinAppHandler:
    base = ntpath.basename(app_path).lower()
    if "firefox" in base:
        return WinFirefoxHandler()
    stem = ntpath.splitext(base)[0]
    for prefix, _, _ in _CHROMIUM_FAMILIES:
        if stem == prefix or stem.startswith(prefix + "-"):
            return WinChromiumHandler(app_path)
    return WinAppHandler()


def _resolve_registry_app_path(name: str) -> Optional[str]:
    if sys.platform != "win32":
        return None
    import winreg  # type: ignore[import-not-found]

    candidates = [name] if name.lower().endswith(".exe") else [name + ".exe", name]
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for cand in candidates:
            subkey = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{cand}"
            try:
                with winreg.OpenKey(root, subkey) as key:
                    val, _ = winreg.QueryValueEx(key, "")
                    if isinstance(val, str):
                        expanded = os.path.expandvars(val.strip().strip('"'))
                        if os.path.isfile(expanded):
                            return os.path.abspath(expanded)
            except OSError:
                continue
    return None


def resolve_app_path(app: str) -> str:
    """Resolve an executable path, PATH command, Registry App Path, or Start Menu app name."""
    raw = os.path.expandvars(os.path.expanduser(app.strip().strip('"')))
    if ntpath.isabs(raw) or "\\" in raw or "/" in raw:
        if not os.path.isfile(raw):
            raise NotFoundError(f"Application not found: {app}")
        return os.path.abspath(raw)
    found = shutil.which(raw)
    if found and os.path.isfile(found):
        return os.path.abspath(found)
    reg = _resolve_registry_app_path(raw)
    if reg:
        return reg
    wanted = raw.casefold()
    matches = [e.binary for e in scan_apps() if e.bindable and e.binary and e.name.casefold() == wanted]
    if len(matches) == 1 and matches[0]:
        return matches[0]
    raise NotFoundError(f"Application not found in PATH, Registry App Paths, or Start Menu: {app}")


def parse_lnk_target(lnk_path: Path) -> Optional[str]:
    """Best-effort pure-Python MS-SHLLINK target local-path parser (no COM/PowerShell needed)."""
    try:
        data = lnk_path.read_bytes()
    except OSError:
        return None
    if len(data) < 76 or data[:4] != b"\x4c\x00\x00\x00":
        return None
    flags = struct.unpack_from("<I", data, 20)[0]
    offset = 76
    if flags & 0x01:  # HasLinkTargetIDList
        if offset + 2 > len(data):
            return None
        id_len = struct.unpack_from("<H", data, offset)[0]
        offset += 2 + id_len
    if flags & 0x02:  # HasLinkInfo
        if offset + 28 > len(data):
            return None
        link_info_size, link_info_hdr, link_info_flags, _, _, local_base_off = struct.unpack_from("<IIIIII", data, offset)
        if (link_info_flags & 0x01) and local_base_off and offset + local_base_off < len(data):
            end = data.find(b"\x00", offset + local_base_off)
            if end != -1:
                try:
                    cand = data[offset + local_base_off : end].decode("mbcs" if sys.platform == "win32" else "utf-8", errors="ignore")
                    if cand.lower().endswith(".exe") and (sys.platform != "win32" or os.path.isfile(cand)):
                        return cand
                except Exception:
                    pass
    return None


def scan_apps(start_menu_dirs: Optional[Sequence[Path]] = None) -> List[WinAppEntry]:
    """Discover launchable Windows applications from Start Menu .lnk shortcuts and Registry App Paths."""
    entries: List[WinAppEntry] = []
    seen: set[str] = set()

    dirs: List[Path] = list(start_menu_dirs) if start_menu_dirs is not None else []
    if start_menu_dirs is None:
        prog_data = os.environ.get("ProgramData") or os.environ.get("PROGRAMDATA")
        appdata = os.environ.get("APPDATA")
        if prog_data:
            dirs.append(Path(prog_data) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
        if appdata:
            dirs.append(Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")

    for root in dirs:
        if not root.is_dir():
            continue
        for lnk in sorted(root.rglob("*.lnk")):
            name = lnk.stem
            if "uninstall" in name.lower() or name.casefold() in seen:
                continue
            target = parse_lnk_target(lnk)
            if not target or not target.lower().endswith(".exe"):
                continue
            seen.add(name.casefold())
            entries.append(
                WinAppEntry(
                    name=name,
                    comment=target,
                    icon=target,
                    exec_argv=[target],
                    desktop_file=str(lnk),
                    binary=target,
                    bindable=True,
                    reason="",
                )
            )

    if sys.platform == "win32":
        import winreg  # type: ignore[import-not-found]

        for root_hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(root_hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths") as key:
                    idx = 0
                    while True:
                        try:
                            sub = winreg.EnumKey(key, idx)
                            idx += 1
                        except OSError:
                            break
                        display = sub[:-4] if sub.lower().endswith(".exe") else sub
                        if display.casefold() in seen:
                            continue
                        with winreg.OpenKey(key, sub) as subkey:
                            try:
                                val, _ = winreg.QueryValueEx(subkey, "")
                            except OSError:
                                continue
                        if isinstance(val, str):
                            exe = os.path.expandvars(val.strip().strip('"'))
                            if os.path.isfile(exe):
                                seen.add(display.casefold())
                                entries.append(
                                    WinAppEntry(
                                        name=display,
                                        comment=exe,
                                        icon=exe,
                                        exec_argv=[exe],
                                        desktop_file="",
                                        binary=os.path.abspath(exe),
                                        bindable=True,
                                        reason="",
                                    )
                                )
            except OSError:
                continue

    entries.sort(key=lambda e: e.name.casefold())
    return entries


def search_entries(entries: Sequence[WinAppEntry], query: str) -> List[WinAppEntry]:
    needle = query.strip().casefold()
    if not needle:
        return list(entries)
    return [
        e for e in entries
        if needle in e.name.casefold() or needle in (e.binary or "").casefold() or needle in e.comment.casefold()
    ]


def find_running_pids(app_path: str, comm_names: Sequence[str]) -> List[int]:
    """Find running PIDs on Windows matching app_path or comm_names owned by current user."""
    if sys.platform != "win32":
        return []
    wanted = {ntpath.basename(app_path).lower()} | {c.lower() for c in comm_names}
    try:
        proc = subprocess.run(
            ["tasklist.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode != 0:
            return []
        import csv

        pids: List[int] = []
        for row in csv.reader(proc.stdout.splitlines()):
            if len(row) >= 2 and row[0].lower() in wanted:
                try:
                    pids.append(int(row[1]))
                except ValueError:
                    pass
        return pids
    except Exception:
        return []


def terminate_pids(pids: Sequence[int], *, force: bool = False) -> Tuple[List[int], List[int]]:
    """Terminate owned Windows PIDs cleanly, returning (stopped, still_running)."""
    if sys.platform != "win32":
        return [], list(pids)
    import ctypes.wintypes as wt

    stopped: List[int] = []
    still: List[int] = []
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel32.OpenProcess.restype = wt.HANDLE
    kernel32.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
    kernel32.TerminateProcess.restype = wt.BOOL
    kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    kernel32.WaitForSingleObject.restype = wt.DWORD
    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    kernel32.CloseHandle.restype = wt.BOOL
    PROCESS_TERMINATE = 0x0001
    SYNCHRONIZE = 0x00100000
    for pid in pids:
        h = kernel32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, int(pid))
        if not h:
            continue
        try:
            if force:
                ok = bool(kernel32.TerminateProcess(h, 1))
            else:
                subprocess.run(
                    ["taskkill.exe", "/PID", str(pid), "/T"],
                    capture_output=True,
                    timeout=5,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                wait_res = kernel32.WaitForSingleObject(h, 2000)
                if wait_res != 0:
                    ok = bool(kernel32.TerminateProcess(h, 1))
                else:
                    ok = True
            if ok:
                stopped.append(int(pid))
            else:
                still.append(int(pid))
        finally:
            kernel32.CloseHandle(h)
    return stopped, still
