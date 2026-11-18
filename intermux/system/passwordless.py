"""Opt-in polkit rule for passwordless helper execution via the intermux group."""

from __future__ import annotations

import grp
import json
import logging
import os
import pwd
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from intermux import config
from intermux.errors import MissingToolError, PermissionDeniedError, ValidationError
from intermux.system import commands

log = logging.getLogger("intermux.system.passwordless")

GROUP = "intermux"
RULES_PATH = Path("/etc/polkit-1/rules.d/49-intermux-passwordless.rules")
PKLA_PATH = Path("/etc/polkit-1/localauthority/50-local.d/49-intermux-passwordless.pkla")
MARKER_PATH = config.STATE_DIR / "passwordless.json"

# The window turns the mode on or off by running this root-owned script through pkexec. Its polkit action,
# org.intermux.passwordless, always asks for an administrator password: the intermux-group rule below only
# covers org.intermux.run. pkexec clears the environment, so only the script can set POLKIT_ENV.
TOGGLE_PATH = config.INSTALL_BIN.parent / "intermux-passwordless"
POLKIT_ACTION = "org.intermux.passwordless"
POLKIT_ENV = "INTERMUX_PASSWORDLESS_ACTION"
_PKEXEC_DENIED_CODES = (126, 127)

RULES_TEXT = (
    '// Installed by "intermux passwordless on". Remove with: sudo intermux passwordless off --all\n'
    '// Members of the intermux group run the InterMux helper without a password, from a local, active session only.\n'
    'polkit.addRule(function (action, subject) {\n'
    '    if (action.id == "org.intermux.run" && subject.isInGroup("intermux") &&\n'
    '            subject.local && subject.active) {\n'
    '        return polkit.Result.YES;\n'
    '    }\n'
    '});\n'
)

PKLA_TEXT = (
    '# Installed by "intermux passwordless on". Remove with: sudo intermux passwordless off --all\n'
    '[InterMux passwordless helper]\n'
    'Identity=unix-group:intermux\n'
    'Action=org.intermux.run\n'
    'ResultActive=yes\n'
)


def parse_pkaction_version(text: str) -> Optional[Tuple[int, ...]]:
    """Parse version numbers from pkaction --version output. Pure."""
    cleaned = text.strip()
    if "version" in cleaned.lower():
        m = re.search(r"\bversion\s+(\d+(?:\.\d+)*)", cleaned, re.IGNORECASE)
        if m:
            try:
                return tuple(int(x) for x in m.group(1).split("."))
            except ValueError:
                return None
    elif re.fullmatch(r"\d+(?:\.\d+)*", cleaned):
        try:
            return tuple(int(x) for x in cleaned.split("."))
        except ValueError:
            return None
    return None


def detect_backend() -> str:
    """Detect whether to use polkit JavaScript rules or pkla files."""
    res = commands.run(["pkaction", "--version"], tolerate=("command not found",))
    if res.ok:
        ver = parse_pkaction_version(res.stdout or res.stderr)
        if ver is not None:
            return "pkla" if ver < (0, 106) else "rules"

    # Directory layout fallback: pkla when localauthority exists and no rules.d dir exists
    if (Path("/etc/polkit-1/localauthority").exists()
            and not Path("/etc/polkit-1/rules.d").exists()
            and not Path("/usr/share/polkit-1/rules.d").exists()):
        return "pkla"
    return "rules"


def read_marker() -> Optional[Dict[str, Any]]:
    """Read the passwordless marker file. Returns None on missing, invalid JSON or wrong shape."""
    try:
        if not MARKER_PATH.is_file():
            return None
        with open(MARKER_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        if not isinstance(data.get("v"), int):
            return None
        if data.get("backend") not in ("rules", "pkla"):
            return None
        if not isinstance(data.get("rule_path"), str):
            return None
        if not isinstance(data.get("group_created"), bool):
            return None
        return data
    except Exception:
        return None


def status(user: Optional[str] = None) -> Dict[str, Any]:
    """Return status dict for passwordless helper execution. Never raises."""
    try:
        if user is None:
            if os.getuid() == 0 and os.environ.get("SUDO_USER") and os.environ["SUDO_USER"] != "root":
                user = os.environ["SUDO_USER"]
            else:
                try:
                    user = pwd.getpwuid(os.getuid()).pw_name
                except Exception:
                    user = "root" if os.getuid() == 0 else (os.environ.get("USER") or "unknown")

        marker = read_marker()
        rule_installed = marker is not None
        backend = marker.get("backend") if marker else None
        rule_path = marker.get("rule_path") if marker else None

        group_exists = False
        members: List[str] = []
        user_in_group = False

        try:
            gr = grp.getgrnam(GROUP)
            group_exists = True
            members = sorted(list(gr.gr_mem))
            if user in members:
                user_in_group = True
            else:
                try:
                    pw = pwd.getpwnam(user)
                    user_gids = os.getgrouplist(user, pw.pw_gid)
                    if gr.gr_gid in user_gids:
                        user_in_group = True
                except Exception:
                    pass
        except KeyError:
            pass

        enabled = bool(rule_installed and user_in_group)

        return {
            "enabled": enabled,
            "rule_installed": rule_installed,
            "backend": backend,
            "rule_path": rule_path,
            "group_exists": group_exists,
            "members": members,
            "user": user,
            "user_in_group": user_in_group,
        }
    except Exception:
        return {
            "enabled": False,
            "rule_installed": False,
            "backend": None,
            "rule_path": None,
            "group_exists": False,
            "members": [],
            "user": user or "unknown",
            "user_in_group": False,
        }


def via_toggle_action() -> bool:
    """True when pkexec started us through the org.intermux.passwordless script, which always asks for a password."""
    return "PKEXEC_UID" in os.environ and os.environ.get(POLKIT_ENV) == "1"


def require_sudo_root() -> None:
    """Raise PermissionDeniedError unless running as root through sudo or the password-checked pkexec action.

    Plain pkexec is refused: with passwordless on, any program can run the helper's pkexec action without a password.
    """
    if "PKEXEC_UID" in os.environ and not via_toggle_action():
        raise PermissionDeniedError("passwordless can only be changed with sudo, not through pkexec")
    if os.geteuid() != 0:
        raise PermissionDeniedError("run: sudo intermux passwordless on")


def resolve_target_user(explicit: Optional[str]) -> str:
    """Resolve and validate the target user. Raises ValidationError if invalid."""
    if via_toggle_action():
        # Through the window's toggle, only the user who asked can be changed.
        try:
            username = pwd.getpwuid(int(os.environ["PKEXEC_UID"])).pw_name
        except (KeyError, ValueError):
            raise ValidationError("could not tell which user asked for the change") from None
        if explicit is not None and explicit.strip() != username:
            raise ValidationError("the window can only change passwordless for your own user")
    elif explicit is not None:
        username = explicit.strip()
        if not username:
            raise ValidationError("specify a user with --user")
    else:
        sudo_user = os.environ.get("SUDO_USER", "").strip()
        if not sudo_user or sudo_user == "root":
            raise ValidationError("specify a user with --user")
        username = sudo_user

    if username == "root":
        raise ValidationError("passwordless cannot be enabled for root")

    try:
        pw = pwd.getpwnam(username)
    except KeyError:
        raise ValidationError(f"user '{username}' does not exist") from None

    if pw.pw_uid == 0:
        raise ValidationError("passwordless cannot be enabled for root (uid 0)")

    return username


def _write_atomic(path: Path, content: str) -> None:
    """Write content to path atomically (temp file in same dir, chmod 0o644, os.replace)."""
    parent = path.parent
    if not parent.exists():
        parent.mkdir(parents=True, mode=0o755)
    fd, tmp_path = tempfile.mkstemp(dir=parent, prefix=f".{path.name}.")
    try:
        with open(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def enable(user: str) -> List[str]:
    """Enable passwordless helper execution for user."""
    # (1) ensure group exists (groupadd --system intermux; remember if we created it)
    group_created = False
    earlier = read_marker()
    if earlier and earlier.get("group_created"):
        group_created = True

    try:
        grp.getgrnam(GROUP)
    except KeyError:
        commands.run_checked(["groupadd", "--system", GROUP])
        group_created = True

    # (2) add user (gpasswd -a USER intermux; if gpasswd is missing, usermod -aG intermux USER)
    if shutil.which("gpasswd"):
        commands.run_checked(["gpasswd", "-a", user, GROUP])
    elif shutil.which("usermod"):
        commands.run_checked(["usermod", "-aG", GROUP, user])
    else:
        raise MissingToolError("neither gpasswd nor usermod was found")

    # (3) detect backend and write matching rule file
    backend = detect_backend()
    if backend == "rules":
        rule_path = RULES_PATH
        rule_content = RULES_TEXT
        other_path = PKLA_PATH
    else:
        rule_path = PKLA_PATH
        rule_content = PKLA_TEXT
        other_path = RULES_PATH

    _write_atomic(rule_path, rule_content)
    try:
        if other_path.exists():
            other_path.unlink()
    except OSError:
        pass

    # (4) write marker
    marker_data = {
        "v": 1,
        "backend": backend,
        "rule_path": str(rule_path),
        "group_created": group_created,
    }
    _write_atomic(MARKER_PATH, json.dumps(marker_data, indent=2) + "\n")

    # (5) return human lines describing what was done
    return [
        f"Added {user} to the {GROUP} group.",
        f"Installed polkit rule: {rule_path}",
        "Takes effect now; polkit checks group membership on every request. Turn off: sudo intermux passwordless off",
    ]


def disable(user: Optional[str] = None, all_users: bool = False) -> List[str]:
    """Disable passwordless helper execution for user or all users."""
    if not all_users:
        if user is None:
            user = resolve_target_user(None)

        try:
            gr = grp.getgrnam(GROUP)
            group_exists = True
        except KeyError:
            group_exists = False

        if group_exists:
            if shutil.which("gpasswd"):
                # Not being a member is fine: "off" is idempotent.
                commands.run_checked(["gpasswd", "-d", user, GROUP], tolerate=("is not a member",))
            elif shutil.which("usermod"):
                commands.run_checked(["usermod", "-rG", GROUP, user])
            else:
                raise MissingToolError("neither gpasswd nor usermod was found")

            try:
                gr = grp.getgrnam(GROUP)
                remaining = list(gr.gr_mem)
            except KeyError:
                remaining = []
        else:
            remaining = []

        if remaining:
            return [f"Removed {user} from the {GROUP} group."]

        # No members remaining: fall through to --all
        lines = [f"Removed {user} from the {GROUP} group."]
        all_lines = _disable_all()
        lines.extend(all_lines)
        return lines

    return _disable_all()


def _disable_all() -> List[str]:
    marker = read_marker()
    was_installed = (
        marker is not None
        or RULES_PATH.exists()
        or PKLA_PATH.exists()
    )

    group_created = bool(marker and marker.get("group_created"))

    for p in (RULES_PATH, PKLA_PATH):
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass

    if group_created:
        try:
            grp.getgrnam(GROUP)
            commands.run_checked(["groupdel", GROUP])
        except KeyError:
            pass

    try:
        if MARKER_PATH.exists():
            MARKER_PATH.unlink()
    except OSError:
        pass

    if not was_installed:
        return ["Passwordless was not on"]

    lines = ["Removed polkit rule and disabled passwordless."]
    if group_created:
        lines.append(f"Deleted group {GROUP}.")
    return lines


def toggle_command(on: bool) -> List[str]:
    """argv that turns the mode on or off for the calling user, asking for an administrator password."""
    pkexec = shutil.which("pkexec")
    if pkexec is None:
        raise MissingToolError("pkexec not found; install polkit")
    if not TOGGLE_PATH.is_file():
        raise MissingToolError(f"{TOGGLE_PATH} is missing; run: sudo intermux passwordless {'on' if on else 'off'}")
    return [pkexec, str(TOGGLE_PATH), "on" if on else "off"]


def set_from_gui(on: bool, *, timeout: float = 300.0) -> List[str]:
    """Turn the mode on or off for the calling user via pkexec. Returns the output lines; raises on failure."""
    argv = toggle_command(on)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise PermissionDeniedError("the password prompt was not answered in time") from None
    except OSError as exc:
        raise MissingToolError(f"could not run pkexec: {exc}") from exc
    if proc.returncode in _PKEXEC_DENIED_CODES:
        raise PermissionDeniedError("authorization was cancelled or denied")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        message = detail[-1].strip() if detail else f"exit status {proc.returncode}"
        if message.startswith("[X]"):
            message = message[3:].strip()
        raise ValidationError(message)
    return [line for line in proc.stdout.splitlines() if line.strip()]
