"""Run external system commands as argv lists. Never uses a shell."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

from intermux.errors import SystemCommandError

log = logging.getLogger("intermux.system.commands")

FALLBACK_DIRS = ("/usr/local/sbin", "/usr/sbin", "/sbin")
NOT_FOUND_RETURNCODE = 127
TIMEOUT_RETURNCODE = 124


def ip_bin() -> str:
    """Path of the ip tool, looked up when called so tests and PATH changes are honoured."""
    return which("ip") or "/usr/sbin/ip"


@dataclass
class CmdResult:
    argv: List[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def which(name: str) -> Optional[str]:
    """Return the path of an executable found on PATH or in FALLBACK_DIRS, or None."""
    found = shutil.which(name)
    if found:
        return found
    for directory in FALLBACK_DIRS:
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _is_tolerated(result: CmdResult, tolerate: Sequence[str]) -> bool:
    return any(item and item in result.stderr for item in tolerate)


def run(argv: Sequence[str], *, timeout: float = 30.0, input_text: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None, tolerate: Sequence[str] = ()) -> CmdResult:
    """Run argv and return its result. Nonzero exit, timeout and missing binary are not raised."""
    if not isinstance(argv, (list, tuple)) or not all(isinstance(a, str) for a in argv):
        raise TypeError("argv must be a list or tuple of str")
    if not argv:
        raise ValueError("argv must not be empty")

    args = list(argv)
    log.debug("run: %s", args)
    stdin_kwargs = {"input": input_text} if input_text is not None else {"stdin": subprocess.DEVNULL}
    run_env = dict(env) if env is not None else None

    try:
        proc = subprocess.run(args, capture_output=True, text=True, errors="replace",
                              timeout=timeout, env=run_env, **stdin_kwargs)
        result = CmdResult(args, proc.returncode, proc.stdout or "", proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        partial = _text(exc.stderr)
        result = CmdResult(args, TIMEOUT_RETURNCODE, _text(exc.stdout),
                           partial + f"\ntimed out after {timeout}s")
    except FileNotFoundError:
        result = CmdResult(args, NOT_FOUND_RETURNCODE, "", f"command not found: {args[0]}")
    except OSError as exc:
        result = CmdResult(args, 126, "", f"cannot execute {args[0]}: {exc}")

    if not result.ok:
        if _is_tolerated(result, tolerate):
            log.debug("tolerated failure (%d): %s", result.returncode, args)
        else:
            log.warning("command failed (%d): %s: %s", result.returncode, args,
                        result.stderr.strip())
    return result


def desktop_env(environ: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Return environ (default os.environ) as it was before a PyInstaller bundle changed it.

    A frozen build points LD_LIBRARY_PATH (and Tcl/Tk paths) at its own bundled libraries.
    Desktop openers and browsers started with that environment load the bundle's older
    libraries and fail, so the original value is restored and bundle-only keys are dropped.
    """
    env = dict(os.environ if environ is None else environ)
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if not getattr(sys, "frozen", False) and not bundle_dir:
        return env
    original = env.pop("LD_LIBRARY_PATH_ORIG", None)
    if original:
        env["LD_LIBRARY_PATH"] = original
    else:
        env.pop("LD_LIBRARY_PATH", None)
    for key in list(env):
        if key.startswith("_PYI") or (bundle_dir and bundle_dir in env[key]):
            del env[key]
    return env


DESKTOP_OPENERS = (("xdg-open",), ("gio", "open"))


def open_with_desktop(target: str, *, timeout: float = 5.0) -> CmdResult:
    """Open a URL, file or folder with the desktop's default handler, without blocking on it.

    The opener runs detached with no pipes: a browser it starts can outlive it and would
    otherwise hold captured pipes open until the timeout. An opener still running after
    `timeout` is taken as having handed the target over. Falls back to `gio open`.
    """
    if not isinstance(target, str) or not target:
        raise ValueError("target must be a non-empty str")
    env = desktop_env()
    result = CmdResult([DESKTOP_OPENERS[0][0], target], NOT_FOUND_RETURNCODE, "",
                       "no desktop opener found (install xdg-utils)")
    for opener in DESKTOP_OPENERS:
        path = which(opener[0])
        if path is None:
            continue
        args = [path, *opener[1:], target]
        log.debug("open: %s", args)
        try:
            proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, env=env, start_new_session=True)
        except OSError as exc:
            result = CmdResult(args, 126, "", f"cannot execute {args[0]}: {exc}")
            continue
        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return CmdResult(args, 0, "", "")
        if returncode == 0:
            return CmdResult(args, 0, "", "")
        result = CmdResult(args, returncode, "", f"{opener[0]} exited with status {returncode}")
        log.warning("opener failed (%d): %s", returncode, args)
    return result


def run_checked(argv: Sequence[str], **kwargs: Any) -> CmdResult:
    """Like run(), but raises SystemCommandError on failure unless stderr matches `tolerate`."""
    result = run(argv, **kwargs)
    if result.ok or _is_tolerated(result, kwargs.get("tolerate", ())):
        return result
    detail = result.stderr.strip() or "no output"
    raise SystemCommandError(
        f"command failed ({result.returncode}): {' '.join(result.argv)}: {detail}",
        result=result,
    )


def json_cmd(argv: Sequence[str], **kwargs: Any) -> Any:
    """Run argv and parse stdout as JSON; returns [] on failure or invalid JSON."""
    result = run(argv, **kwargs)
    if not result.ok:
        return []
    try:
        return json.loads(result.stdout)
    except ValueError:
        log.warning("invalid JSON from %s", result.argv[0])
        return []
