"""Run external system commands as argv lists. Never uses a shell."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Sequence

from intermux.errors import SystemCommandError

log = logging.getLogger("intermux.system.commands")

FALLBACK_DIRS = ("/usr/local/sbin", "/usr/sbin", "/sbin")
NOT_FOUND_RETURNCODE = 127
TIMEOUT_RETURNCODE = 124


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
