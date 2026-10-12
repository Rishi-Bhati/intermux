"""Application handlers and path resolution for bound applications."""
from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from typing import List

from intermux.errors import NotFoundError
from intermux.system.session import UserInfo

log = logging.getLogger("intermux.apps")

CHROMIUM_NAMES = ("chromium", "chrome", "brave", "vivaldi", "msedge", "opera")
CHROMIUM_UNSUPPORTED_REASON = (
    "Chromium-based browsers fail inside network namespaces (sandbox). "
    "Use --allow-unsupported to try anyway."
)


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


class ChromiumHandler(AppHandler):
    name = "chromium"
    supported = False
    unsupported_reason = CHROMIUM_UNSUPPORTED_REASON


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
        return ChromiumHandler()
    return AppHandler()
