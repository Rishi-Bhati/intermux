"""Application handlers and path resolution for bound applications."""
from __future__ import annotations

import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

from intermux.errors import ConflictError, NotFoundError
from intermux.system.session import UserInfo

log = logging.getLogger("intermux.apps")

CHROMIUM_NAMES = ("chromium", "chrome", "google-chrome", "brave", "vivaldi", "msedge", "microsoft-edge", "opera")


@dataclass
class LaunchContext:
    user: UserInfo
    iface: str
    profile_mode: str            # "default" | "separate"


class AppHandler:
    name: str = "generic"
    supported: bool = True
    unsupported_reason: str = ""

    def comm_names(self, app_path: str) -> List[str]:
        """Process names (comm) used to recognise running instances of the app."""
        return [os.path.basename(app_path)[:15]]

    def prepare(self, ctx: LaunchContext, app_path: str) -> None:
        """Hook run before launch. Default does nothing."""
        return None

    def extra_args(self, ctx: LaunchContext) -> List[str]:
        """Extra command-line arguments appended at launch."""
        return []


# Binary-name prefix -> (family, process names of that browser only). A launcher such as
# google-chrome-stable execs a binary with another name, so the family's real process names are listed.
# Each family keeps to its own names: "close and reopen" for Brave must never terminate a running Chrome.
CHROMIUM_FAMILIES = (
    ("brave", "brave", ("brave", "brave-browser")),
    ("google-chrome", "chrome", ("chrome",)),
    ("chrome", "chrome", ("chrome",)),
    ("chromium", "chromium", ("chromium", "chromium-browser")),
    ("vivaldi", "vivaldi", ("vivaldi", "vivaldi-bin")),
    ("microsoft-edge", "msedge", ("msedge",)),
    ("msedge", "msedge", ("msedge",)),
    ("opera", "opera", ("opera",)),
)


def chromium_family(app_path: str) -> Tuple[str, Tuple[str, ...]]:
    """(family, process names) for a Chromium-family binary; the family is "chromium" if unknown. Pure."""
    base = os.path.basename(app_path).lower()
    for prefix, family, names in CHROMIUM_FAMILIES:
        if base.startswith(prefix):
            return family, names
    return "chromium", ()


class ChromiumHandler(AppHandler):
    """Chromium-family browsers are single-instance: a second launch hands off to the running one,
    which would keep the window outside the namespace. A separate profile therefore gets its own
    --user-data-dir, so the new process is a real second instance that stays inside the namespace."""

    name = "chromium"
    supported = True
    unsupported_reason = ""

    def __init__(self, app_path: str = "") -> None:
        self.family = chromium_family(app_path)[0]

    def comm_names(self, app_path: str) -> List[str]:
        names = {os.path.basename(app_path)[:15]}
        names.update(n[:15] for n in chromium_family(app_path)[1])
        return sorted(names)

    def profile_dir(self, ctx: LaunchContext) -> Path:
        # One directory per browser and interface: two browsers must never share a user-data-dir, or a
        # launch of one would hand off to the other, and their profile formats differ.
        iface = re.sub(r"[^A-Za-z0-9_.-]", "_", ctx.iface)
        return (Path(ctx.user.home) / ".local" / "state" / "intermux" / "profiles"
                / f"{self.family}-{iface}")

    def prepare(self, ctx: LaunchContext, app_path: str) -> None:
        if ctx.profile_mode == "separate":
            from intermux.system import safefs

            try:
                safefs.ensure_user_dir(self.profile_dir(ctx), Path(ctx.user.home), ctx.user.uid, ctx.user.gid)
            except OSError as exc:
                raise ConflictError(f"cannot create the separate browser profile {self.profile_dir(ctx)}: "
                                    f"{exc.strerror or exc}", code="profile_unwritable") from exc

    def extra_args(self, ctx: LaunchContext) -> List[str]:
        if ctx.profile_mode == "separate":
            return [f"--user-data-dir={self.profile_dir(ctx)}"]
        return []


def resolve_app_path(app: str) -> str:
    """Return the absolute path of an application given as a name or path."""
    path = os.path.expanduser(app)
    if os.path.isabs(path) or "/" in path:
        if not os.path.exists(path):
            raise NotFoundError(f"Application not found: {app}")
        return os.path.abspath(path)
    found = shutil.which(app)
    if not found:
        raise NotFoundError(f"Application not found in PATH: {app}")
    return os.path.abspath(found)


def get_handler(app_path: str) -> AppHandler:
    """Pick the handler for an application based on its binary name."""
    # Imported here: firefox.py imports this module for AppHandler.
    from intermux.apps.firefox import FirefoxHandler

    base = os.path.basename(app_path).lower()
    if "firefox" in base:
        return FirefoxHandler()
    if base.startswith(CHROMIUM_NAMES):
        return ChromiumHandler(app_path)
    return AppHandler()
