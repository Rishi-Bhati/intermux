"""Privileged helper: validates one JSON request on stdin and runs the matching engine action as root."""

from __future__ import annotations

import json
import logging
import os
import posixpath
import re
import sys
from typing import Any, Dict, Mapping, Optional, Sequence

from intermux import engine, state
from intermux.errors import InterMuxError, PermissionDeniedError, ValidationError
from intermux.helper.protocol import error_response, ok_response, validate_request
from intermux.system import session

log = logging.getLogger("intermux.helper")

_DISPLAY_RE = re.compile(r":[0-9]+(\.[0-9]+)?")
_PLAIN_SOCKET_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_MAX_REQUEST_CHARS = 1024 * 1024


def _is_under(path: str, roots: Sequence[str]) -> bool:
    """True if path is absolute, already normalized, and equal to or below one of roots."""
    if not path.startswith("/") or posixpath.normpath(path) != path:
        return False
    for root in roots:
        base = root.rstrip("/")
        if path == base or path.startswith(base + "/"):
            return True
    return False


def _valid_wayland(name: str, user: "session.UserInfo") -> bool:
    if "/" not in name:
        return _PLAIN_SOCKET_RE.fullmatch(name) is not None
    return _is_under(name, (user.runtime_dir,))


def _sanitize_bind_env(env: Mapping[str, str], user: "session.UserInfo") -> Dict[str, str]:
    """Check display and session variables against the requesting user; drop a mismatched XDG_RUNTIME_DIR."""
    clean = dict(env)
    if "WAYLAND_DISPLAY" in clean and not _valid_wayland(clean["WAYLAND_DISPLAY"], user):
        raise ValidationError("WAYLAND_DISPLAY does not belong to the requesting user")
    if "XDG_RUNTIME_DIR" in clean and clean["XDG_RUNTIME_DIR"] != user.runtime_dir:
        del clean["XDG_RUNTIME_DIR"]
    if "XAUTHORITY" in clean and not _is_under(clean["XAUTHORITY"], (user.home, user.runtime_dir)):
        raise ValidationError("XAUTHORITY must be under the requesting user's home or runtime directory")
    if "DISPLAY" in clean and not _DISPLAY_RE.fullmatch(clean["DISPLAY"]):
        raise ValidationError("DISPLAY has an invalid format")
    return clean


def _resolve_user(environ: Mapping[str, str]) -> "session.UserInfo":
    """Identify the unprivileged user that pkexec or sudo ran the helper for."""
    raw = environ.get("PKEXEC_UID") or environ.get("SUDO_UID")
    if not raw:
        raise PermissionDeniedError("could not determine the requesting user")
    try:
        uid = int(raw)
    except ValueError as exc:
        raise PermissionDeniedError("could not determine the requesting user") from exc
    if uid == 0:
        raise PermissionDeniedError("requests from root are not accepted")
    return session.user_for_uid(uid)


_USER_ACTIONS = ("bind", "stop_app", "kill_app", "adopt", "batch")


def _require_user(user: Optional["session.UserInfo"]) -> "session.UserInfo":
    if user is None:
        raise PermissionDeniedError("could not determine the requesting user")
    return user


def _run_batch(ops: Sequence[Dict[str, Any]], user: "session.UserInfo") -> list:
    """Run each op in order; one failing op never stops the rest."""
    results = []
    for op in ops:
        kind = op["op"]
        log.info("batch %s %s via %s for uid %s", kind, op["app"], op["iface"], user.uid)
        try:
            if kind == "bind":
                data = engine.bind(
                    op["app"],
                    op["iface"],
                    user=user,
                    env=_sanitize_bind_env(op["env"], user),
                    running_policy=op["running_policy"],
                )
            else:
                data = engine.adopt(op["iface"], op["app"], user=user)
            results.append({"ok": True, "data": data})
        except InterMuxError as exc:
            log.warning("batch %s %s failed: %s", kind, op["app"], exc.message)
            results.append({"ok": False, "error": {"code": exc.code, "message": exc.message, "data": exc.data}})
        except Exception:  # noqa: BLE001 - one op's unexpected failure must not lose the other results
            log.exception("batch %s %s failed unexpectedly", kind, op["app"])
            results.append({"ok": False, "error": {"code": "internal",
                                                   "message": "internal error; see the helper log", "data": None}})
    return results


def _dispatch(action: str, params: Dict[str, Any], user: Optional["session.UserInfo"] = None) -> Any:
    if action == "status":
        return engine.status()
    with state.state_lock():
        if action in _USER_ACTIONS:
            user = _require_user(user)
        if action == "batch":
            return _run_batch(params["ops"], user)
        if action == "bind":
            env = _sanitize_bind_env(params["env"], user)
            return engine.bind(
                params["app"],
                params["iface"],
                user=user,
                env=env,
                running_policy=params["running_policy"],
            )
        if action == "stop_app":
            return engine.stop_app(params["iface"], params["app"], uid=user.uid)
        if action == "kill_app":
            return engine.kill_app(params["app"], force=params["force"], uid=user.uid)
        if action == "teardown_idle":
            return engine.teardown_idle()
        if action == "reset":
            # Without a requesting user, kill_apps is skipped rather than killing other users' processes.
            kill = bool(params["kill_apps"]) and user is not None
            return engine.reset(kill_apps=kill, uid=user.uid if user is not None else None)
        if action == "verify":
            return engine.verify(params["iface"])
        if action == "adopt":
            return engine.adopt(params["iface"], params["app"], user=user)
        if action == "release_adoption":
            return engine.release_adoption(params["id"], user=user)
    raise ValidationError("unknown action")


def handle(raw: str, *, euid: Optional[int] = None, environ: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Process one raw JSON request and return a response envelope. Never raises."""
    if euid is None:
        euid = os.geteuid()
    if euid != 0:
        return error_response("permission_denied", "helper must run as root")
    env = os.environ if environ is None else environ
    action = "?"
    try:
        try:
            if len(raw) > _MAX_REQUEST_CHARS:
                raise ValidationError("request is larger than 1 MiB")
            obj = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ValidationError("request is not valid JSON") from exc
        action, params = validate_request(obj)
        user: Optional["session.UserInfo"] = None
        if action in _USER_ACTIONS:
            user = _resolve_user(env)
        elif action in ("reset", "release_adoption"):
            try:
                user = _resolve_user(env)
            except InterMuxError:
                user = None
        log.info("action %s requested by uid %s", action, user.uid if user else "none")
        return ok_response(_dispatch(action, params, user))
    except InterMuxError as exc:
        log.warning("action %s failed: %s", action, exc.message)
        return error_response(exc.code, exc.message, exc.data)
    except Exception as exc:
        log.exception("action %s raised an unexpected error", action)
        return error_response("internal", "internal error; see the helper log")


def main() -> int:
    """Read one request from stdin, write the JSON response to stdout, return the exit status."""
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        raw = sys.stdin.read(_MAX_REQUEST_CHARS + 1)
    except (OSError, UnicodeDecodeError):
        response = error_response("invalid_request", "could not read request from stdin")
    else:
        response = handle(raw)
    sys.stdout.write(json.dumps(response, default=str) + "\n")
    sys.stdout.flush()
    return 0 if response.get("ok") else 1
