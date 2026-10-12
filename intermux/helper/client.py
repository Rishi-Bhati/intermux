"""Unprivileged client for the helper: builds the pkexec command and exchanges one JSON request."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from intermux import config
from intermux.errors import HelperError, MissingToolError
from intermux.helper.protocol import PROTOCOL_VERSION

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PKEXEC_DENIED_CODES = (126, 127)


def helper_command() -> List[str]:
    """Return the argv that runs the helper, wrapped in pkexec when not root."""
    euid = os.geteuid()
    if getattr(sys, "frozen", False):
        exe = str(config.INSTALL_BIN) if config.INSTALL_BIN.exists() else sys.executable
        argv = [exe, "helper"]
    else:
        argv = [sys.executable, "-m", "intermux", "helper"]
        if euid != 0:
            argv = ["env", f"PYTHONPATH={_REPO_ROOT}"] + argv
    if euid != 0:
        pkexec = shutil.which("pkexec")
        if pkexec is None:
            raise MissingToolError("pkexec not found; install polkit to run privileged actions")
        argv = [pkexec] + argv
    return argv


def call(action: str, params: Optional[Dict[str, Any]] = None, *, timeout: float = 300.0) -> Any:
    """Send one request to the helper and return its data, or raise HelperError."""
    request = {"v": PROTOCOL_VERSION, "action": action, "params": dict(params or {})}
    argv = helper_command()
    try:
        proc = subprocess.run(
            argv,
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise HelperError(f"helper did not respond within {timeout:.0f} seconds", code="timeout") from exc
    except OSError as exc:
        raise HelperError(f"could not start helper: {exc}", code="helper_error") from exc

    if os.geteuid() != 0 and proc.returncode in _PKEXEC_DENIED_CODES:
        raise HelperError("authorization was cancelled or denied", code="permission_denied")

    try:
        response = json.loads(proc.stdout)
    except ValueError as exc:
        raise HelperError("helper returned invalid output", code="helper_error") from exc
    if not isinstance(response, dict):
        raise HelperError("helper returned invalid output", code="helper_error")

    if response.get("ok") is True:
        return response.get("data")

    error = response.get("error")
    if not isinstance(error, dict):
        error = {}
    raise HelperError(
        str(error.get("message") or "helper request failed"),
        code=str(error.get("code") or "helper_error"),
        data=error.get("data"),
    )


def bind(
    iface: str,
    app: str,
    *,
    running_policy: str = "ask",
    allow_unsupported: bool = False,
    env: Optional[Dict[str, str]] = None,
) -> Any:
    """Bind an application to an interface through the helper."""
    return call(
        "bind",
        {
            "iface": iface,
            "app": app,
            "running_policy": running_policy,
            "allow_unsupported": allow_unsupported,
            "env": dict(env or {}),
        },
    )


def batch(ops: Sequence[Dict[str, Any]], *, timeout: float = 600.0) -> list:
    """Run many bind/adopt operations in one helper call (one authorization prompt).

    Returns one result per op, in order: {"ok": True, "data": ...} or {"ok": False, "error": {...}}.
    """
    data = call("batch", {"ops": [dict(op) for op in ops]}, timeout=timeout)
    if not isinstance(data, list):
        raise HelperError("helper returned invalid output", code="helper_error")
    return data
