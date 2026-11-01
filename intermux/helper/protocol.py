"""Request validation and response shapes for the privileged helper. Pure: no system calls."""

from __future__ import annotations

import copy
import re
from typing import Any, Callable, Dict, Tuple

from intermux import config, dnsconf
from intermux.errors import ValidationError
from intermux.system import shaping

PROTOCOL_VERSION = 1
ACTIONS = ("status", "bind", "stop_app", "kill_app", "teardown_idle", "reset", "verify",
           "adopt", "release_adoption", "batch", "set_dns", "set_limit", "restart_app", "remove_namespace",
           "reconnect", "move_app")
RUNNING_POLICIES = ("ask", "close", "separate")
IFACE_RE = r"^[A-Za-z0-9_.:@-]{1,15}$"

_IFACE_PATTERN = re.compile(IFACE_RE)
_MAX_PATH = 4096
_MAX_ENV_VALUE = 4096
_FORBIDDEN_CHARS = ("\x00", "\n", "\r")
_REQUEST_KEYS = frozenset({"v", "action", "params"})
_REQUIRED = object()
MAX_BATCH_OPS = 50


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _has_control_chars(value: str) -> bool:
    return any(ch in value for ch in _FORBIDDEN_CHARS)


def validate_env(env: Any) -> Dict[str, str]:
    """Return a copy of env if every key is allowlisted and every value is a plain string."""
    if not isinstance(env, dict):
        raise ValidationError("env must be an object")
    clean: Dict[str, str] = {}
    for key, value in env.items():
        if not isinstance(key, str) or key not in config.ENV_ALLOWLIST:
            raise ValidationError(f"environment variable not allowed: {key}")
        if not isinstance(value, str) or len(value) > _MAX_ENV_VALUE or _has_control_chars(value):
            raise ValidationError(f"invalid value for environment variable {key}")
        clean[key] = value
    return clean


def _v_iface(name: str, value: Any) -> str:
    if not isinstance(value, str) or value in (".", "..") or not _IFACE_PATTERN.fullmatch(value):
        raise ValidationError(f"invalid interface name for {name}")
    return value


def _v_app(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("/") or value.endswith("/"):
        raise ValidationError(f"{name} must be an absolute path to a file")
    if len(value) > _MAX_PATH or _has_control_chars(value):
        raise ValidationError(f"invalid {name}: too long or contains control characters")
    return value


_ADOPT_ID_PATTERN = re.compile(r"^[0-9a-f]{8,32}$")


def _v_adopt_id(name: str, value: Any) -> str:
    if not isinstance(value, str) or not _ADOPT_ID_PATTERN.fullmatch(value):
        raise ValidationError(f"invalid {name}")
    return value


def _v_bool(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{name} must be a boolean")
    return value


def _v_policy(name: str, value: Any) -> str:
    if not isinstance(value, str) or value not in RUNNING_POLICIES:
        raise ValidationError(f"{name} must be one of {', '.join(RUNNING_POLICIES)}")
    return value


def _v_env(_name: str, value: Any) -> Dict[str, str]:
    return validate_env(value)


def _v_dns(name: str, value: Any, allow_keep: bool = True) -> Any:
    """"keep" (bind only), "auto", or a list of 1-4 upstream IPv4 resolvers."""
    if isinstance(value, str):
        if value == dnsconf.AUTO or (allow_keep and value == dnsconf.KEEP):
            return value
        choices = '"auto", "keep"' if allow_keep else '"auto"'
        raise ValidationError(f"{name} must be {choices} or a list of servers")
    try:
        return dnsconf.check_servers(value)
    except ValueError as exc:
        raise ValidationError(f"{name}: {exc}") from None


def _v_dns_set(name: str, value: Any) -> Any:
    return _v_dns(name, value, allow_keep=False)


def _v_mbit(name: str, value: Any) -> Any:
    try:
        return shaping.check_mbit(value)
    except ValueError as exc:
        raise ValidationError(f"{name}: {exc}") from None


# action -> {param name: (validator, default)}; _REQUIRED marks a mandatory parameter.
_PARAM_SPECS: Dict[str, Dict[str, Tuple[Callable[[str, Any], Any], Any]]] = {
    "status": {},
    "bind": {
        "iface": (_v_iface, _REQUIRED),
        "app": (_v_app, _REQUIRED),
        "running_policy": (_v_policy, "ask"),
        "env": (_v_env, {}),
        "dns": (_v_dns, dnsconf.KEEP),
    },
    "stop_app": {
        "iface": (_v_iface, _REQUIRED),
        "app": (_v_app, _REQUIRED),
    },
    "kill_app": {
        "app": (_v_app, _REQUIRED),
        "force": (_v_bool, False),
    },
    "teardown_idle": {},
    "reset": {
        "kill_apps": (_v_bool, False),
    },
    "verify": {
        "iface": (_v_iface, _REQUIRED),
    },
    "adopt": {
        "iface": (_v_iface, _REQUIRED),
        "app": (_v_app, _REQUIRED),
    },
    "release_adoption": {
        "id": (_v_adopt_id, _REQUIRED),
    },
    "set_dns": {
        "iface": (_v_iface, _REQUIRED),
        "dns": (_v_dns_set, _REQUIRED),
    },
    "set_limit": {
        "iface": (_v_iface, _REQUIRED),
        "down": (_v_mbit, None),
        "up": (_v_mbit, None),
    },
    "restart_app": {
        "iface": (_v_iface, _REQUIRED),
        "app": (_v_app, _REQUIRED),
        "env": (_v_env, {}),
    },
    "remove_namespace": {
        "iface": (_v_iface, _REQUIRED),
    },
    "reconnect": {},
    "move_app": {
        "iface": (_v_iface, _REQUIRED),
        "app": (_v_app, _REQUIRED),
        "to": (_v_iface, _REQUIRED),
        "env": (_v_env, {}),
    },
}


def _validate_spec(specs: Dict[str, Tuple[Callable[[str, Any], Any], Any]], label: str,
                   params: Dict[str, Any]) -> Dict[str, Any]:
    for key in params:
        if key not in specs:
            raise ValidationError(f"unknown parameter for {label}: {key}")
    clean: Dict[str, Any] = {}
    for name, (validator, default) in specs.items():
        if name in params:
            clean[name] = validator(name, params[name])
        elif default is _REQUIRED:
            raise ValidationError(f"missing parameter for {label}: {name}")
        else:
            clean[name] = copy.deepcopy(default)
    return clean


BATCH_OPS = ("bind", "adopt", "set_limit")


def _v_ops(name: str, value: Any) -> list:
    """Validate the ops of a batch: each op is a bind, adopt or set_limit with the single action's rules."""
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_BATCH_OPS:
        raise ValidationError(f"{name} must be a list of 1 to {MAX_BATCH_OPS} operations")
    clean = []
    for op in value:
        if not isinstance(op, dict):
            raise ValidationError("each operation must be an object")
        kind = op.get("op")
        if not isinstance(kind, str) or kind not in BATCH_OPS:
            raise ValidationError("unknown operation in batch")
        fields = {k: v for k, v in op.items() if k != "op"}
        checked = _validate_spec(_PARAM_SPECS[kind], f"batch {kind}", fields)
        checked["op"] = kind
        clean.append(checked)
    return clean


_PARAM_SPECS["batch"] = {"ops": (_v_ops, _REQUIRED)}


def validate_request(obj: Any) -> Tuple[str, Dict[str, Any]]:
    """Check a decoded request and return (action, sanitized params). Raises ValidationError."""
    if not isinstance(obj, dict):
        raise ValidationError("request must be a JSON object")
    for key in obj:
        if key not in _REQUEST_KEYS:
            raise ValidationError(f"unknown request field: {key}")
    if not _is_int(obj.get("v")) or obj["v"] != PROTOCOL_VERSION:
        raise ValidationError(f"unsupported protocol version; expected {PROTOCOL_VERSION}")
    action = obj.get("action")
    if not isinstance(action, str) or action not in ACTIONS:
        raise ValidationError("unknown action")
    params = obj.get("params", {})
    if not isinstance(params, dict):
        raise ValidationError("params must be an object")

    clean = _validate_spec(_PARAM_SPECS[action], action, params)
    return action, clean


def ok_response(data: Any) -> Dict[str, Any]:
    """Build a success response envelope."""
    return {"ok": True, "data": data}


def error_response(code: str, message: str, data: Any = None) -> Dict[str, Any]:
    """Build an error response envelope."""
    return {"ok": False, "error": {"code": code, "message": message, "data": data}}
