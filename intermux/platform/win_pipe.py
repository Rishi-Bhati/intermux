"""Hardened Windows Named Pipe client, server-identity verifier, and UAC/Scheduled-Task elevation."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import logging
import ntpath
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from intermux.errors import HelperError, MissingToolError, PermissionDeniedError
from intermux.platform import get_paths

log = logging.getLogger("intermux.platform.win_pipe")

_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_WIN32_APIS: Optional[tuple[Any, Any, Any]] = None


def _win32_apis() -> tuple[Any, Any, Any]:
    """Return (kernel32, advapi32, shell32) with explicit 64-bit-safe argtypes/restype."""
    global _WIN32_APIS
    if _WIN32_APIS is not None:
        return _WIN32_APIS
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)  # type: ignore[attr-defined]

    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wt.HANDLE

    kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel32.OpenProcess.restype = wt.HANDLE

    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    kernel32.CloseHandle.restype = wt.BOOL

    kernel32.LocalFree.argtypes = [wt.LPVOID]
    kernel32.LocalFree.restype = wt.LPVOID

    kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wt.BOOL

    kernel32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.LPVOID, wt.DWORD, wt.DWORD, wt.HANDLE]
    kernel32.CreateFileW.restype = wt.HANDLE

    kernel32.GetNamedPipeServerProcessId.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    kernel32.GetNamedPipeServerProcessId.restype = wt.BOOL

    kernel32.WriteFile.argtypes = [wt.HANDLE, wt.LPCVOID, wt.DWORD, ctypes.POINTER(wt.DWORD), wt.LPVOID]
    kernel32.WriteFile.restype = wt.BOOL

    kernel32.ReadFile.argtypes = [wt.HANDLE, wt.LPVOID, wt.DWORD, ctypes.POINTER(wt.DWORD), wt.LPVOID]
    kernel32.ReadFile.restype = wt.BOOL

    advapi32.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
    advapi32.OpenProcessToken.restype = wt.BOOL

    advapi32.GetTokenInformation.argtypes = [wt.HANDLE, wt.DWORD, wt.LPVOID, wt.DWORD, ctypes.POINTER(wt.DWORD)]
    advapi32.GetTokenInformation.restype = wt.BOOL

    advapi32.ConvertSidToStringSidW.argtypes = [wt.LPVOID, ctypes.POINTER(wt.LPVOID)]
    advapi32.ConvertSidToStringSidW.restype = wt.BOOL

    shell32.ShellExecuteW.argtypes = [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, wt.INT]
    shell32.ShellExecuteW.restype = wt.HINSTANCE

    _WIN32_APIS = (kernel32, advapi32, shell32)
    return _WIN32_APIS


def short_sid(sid: str) -> str:
    return hashlib.sha256(sid.strip().upper().encode("utf-8")).hexdigest()[:12]


def pipe_name_for_sid(sid: str) -> str:
    return rf"\\.\pipe\intermux-net-{short_sid(sid)}"


def task_name_for_sid(sid: str) -> str:
    return rf"\InterMux\Helper-{short_sid(sid)}"


def installed_engine_path() -> Path:
    """Canonical admin-only Program Files path for elevated intermux-net.exe."""
    pf = get_paths("windows").program_files_dir
    assert pf is not None
    return pf / "intermux-net.exe"


def find_unprivileged_engine() -> Optional[Path]:
    """Locate intermux-net.exe for unprivileged checks/interfaces/socks (Program Files or bundle/repo)."""
    candidates = [installed_engine_path()]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "intermux-net.exe")
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / "intermux-net.exe")
    candidates.append(Path(__file__).resolve().parents[2] / "windows" / "engine" / "intermux-net.exe")
    for c in candidates:
        if c.is_file():
            return c
    found = shutil.which("intermux-net.exe")
    return Path(found) if found else None


def current_user_sid() -> str:
    """Return the current Windows process user SID string (S-1-5-...)."""
    if sys.platform != "win32":
        return "S-1-5-21-0-0-0-1000"
    kernel32, advapi32, _ = _win32_apis()
    TOKEN_QUERY = 0x0008
    TokenUser = 1

    tok = wt.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(tok)):
        err = ctypes.get_last_error()
        raise PermissionDeniedError(f"cannot open current process token (winerr={err})")
    try:
        needed = wt.DWORD(0)
        advapi32.GetTokenInformation(tok, TokenUser, None, 0, ctypes.byref(needed))
        buf = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(tok, TokenUser, buf, needed, ctypes.byref(needed)):
            raise PermissionDeniedError("cannot query TokenUser")
        sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        str_ptr = wt.LPVOID()
        if not advapi32.ConvertSidToStringSidW(ctypes.c_void_p(sid_ptr), ctypes.byref(str_ptr)):
            raise PermissionDeniedError("cannot format user SID")
        try:
            return ctypes.wstring_at(str_ptr.value)
        finally:
            kernel32.LocalFree(str_ptr)
    finally:
        kernel32.CloseHandle(tok)


def normalize_win_path(path: str) -> str:
    expanded = ntpath.normcase(ntpath.normpath(path.strip()))
    if expanded.startswith("\\\\?\\"):
        expanded = expanded[4:]
    return expanded


def verify_pipe_server_identity(
    server_pid: int,
    server_exe_path: str,
    server_elevated: bool,
    expected_exe_path: str,
) -> None:
    """Verify the Named Pipe server PID owns the expected Program Files image and is elevated.

    Extracted as a pure testable policy function used immediately after CreateFileW + GetNamedPipeServerProcessId.
    """
    if server_pid <= 0:
        raise PermissionDeniedError("refusing pipe server with invalid process ID")
    if not server_elevated:
        raise PermissionDeniedError("refusing non-elevated pipe server (possible pipe squatting)")
    if normalize_win_path(server_exe_path) != normalize_win_path(expected_exe_path):
        raise PermissionDeniedError(
            f"refusing untrusted pipe server image {server_exe_path!r}; expected {expected_exe_path!r}"
        )


def _inspect_pid_identity(pid: int) -> tuple[str, bool]:
    kernel32, advapi32, _ = _win32_apis()
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    TOKEN_QUERY = 0x0008
    TokenElevation = 20

    hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not hproc:
        raise PermissionDeniedError(f"cannot inspect pipe server process {pid}")
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(len(buf))
        if not kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size)):
            raise PermissionDeniedError("cannot query pipe server executable path")
        exe_path = buf.value

        tok = wt.HANDLE()
        if not advapi32.OpenProcessToken(hproc, TOKEN_QUERY, ctypes.byref(tok)):
            raise PermissionDeniedError("cannot open pipe server token")
        try:
            elev = wt.DWORD(0)
            ret_len = wt.DWORD(0)
            if not advapi32.GetTokenInformation(
                tok, TokenElevation, ctypes.byref(elev), ctypes.sizeof(elev), ctypes.byref(ret_len)
            ):
                raise PermissionDeniedError("cannot verify pipe server elevation")
            return exe_path, bool(elev.value)
        finally:
            kernel32.CloseHandle(tok)
    finally:
        kernel32.CloseHandle(hproc)


def _connect_and_verify_pipe(pipe_path: str, expected_exe: Path) -> Any:
    if sys.platform != "win32":
        raise PermissionDeniedError("Windows Named Pipe client requires win32")
    kernel32, _, _ = _win32_apis()
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    OPEN_EXISTING = 3
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    h = kernel32.CreateFileW(
        pipe_path,
        GENERIC_READ | GENERIC_WRITE,
        0,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    if h is None or h == INVALID_HANDLE_VALUE or h == 0:
        raise FileNotFoundError(f"pipe not ready: {pipe_path}")
    try:
        server_pid = wt.DWORD(0)
        if not kernel32.GetNamedPipeServerProcessId(h, ctypes.byref(server_pid)):
            raise PermissionDeniedError("GetNamedPipeServerProcessId failed")
        srv_exe, srv_elevated = _inspect_pid_identity(int(server_pid.value))
        verify_pipe_server_identity(int(server_pid.value), srv_exe, srv_elevated, str(expected_exe))
        return h
    except BaseException:
        kernel32.CloseHandle(h)
        raise


def _pipe_exchange(handle: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
    kernel32, _, _ = _win32_apis()
    try:
        raw = (json.dumps(payload) + "\n").encode("utf-8")
        written = wt.DWORD(0)
        if not kernel32.WriteFile(handle, raw, len(raw), ctypes.byref(written), None):
            raise HelperError("failed writing to intermux-net pipe", code="helper_error")
        buf = bytearray()
        chunk = ctypes.create_string_buffer(65536)
        read_n = wt.DWORD(0)
        while len(buf) < _MAX_RESPONSE_BYTES:
            if not kernel32.ReadFile(handle, chunk, len(chunk), ctypes.byref(read_n), None):
                break
            if read_n.value == 0:
                break
            buf.extend(chunk.raw[: read_n.value])
            if b"\n" in buf:
                break
        line = bytes(buf).split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
        if not line:
            raise HelperError("empty response from intermux-net pipe", code="helper_error")
        resp = json.loads(line)
        if not isinstance(resp, dict):
            raise HelperError("invalid JSON response from intermux-net pipe", code="helper_error")
        return resp
    finally:
        kernel32.CloseHandle(handle)


def passwordless_status() -> Dict[str, Any]:
    if sys.platform != "win32":
        return {"enabled": False, "user": "", "backend": "schtasks"}
    sid = current_user_sid()
    tname = task_name_for_sid(sid)
    proc = subprocess.run(
        ["schtasks.exe", "/Query", "/TN", tname],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return {
        "enabled": proc.returncode == 0,
        "user": os.environ.get("USERNAME", ""),
        "backend": "schtasks",
        "task_name": tname,
        "exe": str(installed_engine_path()),
    }


def _launch_elevated_engine(exe: Path, args_str: str) -> None:
    if sys.platform != "win32":
        raise PermissionDeniedError("UAC elevation is only supported on Windows")
    _, _, shell32 = _win32_apis()
    ret = shell32.ShellExecuteW(None, "runas", str(exe), args_str, str(exe.parent), 0)
    if int(ret or 0) <= 32:
        raise PermissionDeniedError("UAC elevation was cancelled or failed", code="permission_denied")


def ensure_daemon(timeout: float = 12.0) -> tuple[str, Path]:
    """Ensure the admin-only intermux-net.exe daemon is running and return (pipe_path, expected_exe)."""
    expected_exe = installed_engine_path()
    if not expected_exe.is_file():
        raise MissingToolError(
            f"Privileged engine not installed at {expected_exe}. Install InterMux to Program Files first.",
            code="missing_tool",
        )
    kernel32, _, _ = _win32_apis()
    sid = current_user_sid()
    pipe_path = pipe_name_for_sid(sid)
    try:
        h = _connect_and_verify_pipe(pipe_path, expected_exe)
        kernel32.CloseHandle(h)
        return pipe_path, expected_exe
    except FileNotFoundError:
        pass

    # Prefer passwordless Scheduled Task if configured; otherwise trigger one UAC runas prompt.
    pw_stat = passwordless_status()
    if pw_stat.get("enabled"):
        subprocess.run(
            ["schtasks.exe", "/Run", "/TN", str(pw_stat["task_name"])],
            capture_output=True,
            timeout=5,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    else:
        _launch_elevated_engine(expected_exe, f"daemon --allowed-sid {sid}")

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            h = _connect_and_verify_pipe(pipe_path, expected_exe)
            kernel32.CloseHandle(h)
            return pipe_path, expected_exe
        except FileNotFoundError:
            time.sleep(0.15)
    raise HelperError("intermux-net.exe daemon did not become ready in time", code="timeout")


def call_engine(action: str, params: Optional[Dict[str, Any]] = None) -> Any:
    """Send one typed action to the verified elevated intermux-net.exe Named Pipe."""
    pipe_path, expected_exe = ensure_daemon()
    handle = _connect_and_verify_pipe(pipe_path, expected_exe)
    resp = _pipe_exchange(handle, {"v": 1, "action": action, "params": dict(params or {})})
    if resp.get("ok") is True:
        return resp.get("data")
    err = resp.get("error") or {}
    raise HelperError(
        str(err.get("message") or "intermux-net request failed"),
        code=str(err.get("code") or "helper_error"),
        data=err.get("data"),
    )


def try_call_engine_if_running(action: str, params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
    """Call intermux-net.exe only if already running (no UAC prompt for read-only status/counters)."""
    if sys.platform != "win32":
        return None
    expected_exe = installed_engine_path()
    if not expected_exe.is_file():
        return None
    pipe_path = pipe_name_for_sid(current_user_sid())
    try:
        handle = _connect_and_verify_pipe(pipe_path, expected_exe)
    except FileNotFoundError:
        return None
    resp = _pipe_exchange(handle, {"v": 1, "action": action, "params": dict(params or {})})
    return resp.get("data") if resp.get("ok") is True else None


def set_passwordless(enable: bool) -> Dict[str, Any]:
    expected_exe = installed_engine_path()
    if not expected_exe.is_file():
        raise MissingToolError(f"Privileged engine not installed at {expected_exe}", code="missing_tool")
    sid = current_user_sid()
    # If daemon is already running and verified, send typed action; else run one-shot UAC install-task/remove-task.
    action = "passwordless_enable" if enable else "passwordless_disable"
    running = try_call_engine_if_running("ping")
    if running is not None:
        return call_engine(action) or {"enabled": enable}
    subcmd = "install-task" if enable else "remove-task"
    _launch_elevated_engine(expected_exe, f"{subcmd} --allowed-sid {sid}")
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        st = passwordless_status()
        if bool(st.get("enabled")) == enable:
            return st
        time.sleep(0.2)
    return passwordless_status()
