"""Platform-aware filesystem path resolution for InterMux."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Mapping, Optional

from intermux.platform.base import normalize_platform


@dataclass(frozen=True)
class PlatformPaths:
    """Resolved OS directories and files used by InterMux."""

    state_dir: PurePath
    state_file: PurePath
    run_dir: PurePath
    helper_lock_path: PurePath
    app_log_dir: PurePath
    history_path: PurePath
    report_dir: PurePath
    profile_dir: PurePath
    program_files_dir: Optional[PurePath] = None
    ipc_token_path: Optional[PurePath] = None


def resolve_paths(
    *,
    platform: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    home: Optional[str] = None,
) -> PlatformPaths:
    """Compute OS-appropriate InterMux paths.

    On Linux, returns exact legacy v2.2.1 paths (/var/lib/intermux, /run/intermux,
    ~/.local/state/intermux/*, ~/intermux-reports). On Windows, returns ProgramData,
    ProgramFiles, LOCALAPPDATA, and USERPROFILE locations.
    """
    plat = normalize_platform(platform)
    env = os.environ if environ is None else environ

    if plat == "windows":
        user_home = home or env.get("USERPROFILE") or os.path.expanduser("~")
        local_app_data = env.get("LOCALAPPDATA") or str(PureWindowsPath(user_home) / "AppData" / "Local")
        program_data = env.get("ProgramData") or env.get("PROGRAMDATA") or r"C:\ProgramData"
        program_files = env.get("ProgramFiles") or env.get("PROGRAMFILES") or r"C:\Program Files"

        # Use Path when running on Windows, or PureWindowsPath-formatted Path for cross-platform tests.
        pcls = Path if normalize_platform() == "windows" else PureWindowsPath
        state_dir = Path(str(pcls(program_data) / "InterMux"))
        local_dir = Path(str(pcls(local_app_data) / "InterMux"))
        run_dir = Path(str(pcls(str(local_dir)) / "run"))
        home_dir = Path(str(pcls(user_home)))
        pf_dir = Path(str(pcls(program_files) / "InterMux"))
        return PlatformPaths(
            state_dir=state_dir,
            state_file=Path(str(pcls(str(state_dir)) / "state.json")),
            run_dir=run_dir,
            helper_lock_path=Path(str(pcls(str(run_dir)) / "helper.lock")),
            app_log_dir=Path(str(pcls(str(local_dir)) / "logs")),
            history_path=Path(str(pcls(str(local_dir)) / "history.json")),
            report_dir=Path(str(pcls(str(home_dir)) / "intermux-reports")),
            profile_dir=Path(str(pcls(str(local_dir)) / "profiles")),
            program_files_dir=pf_dir,
            ipc_token_path=Path(str(pcls(str(local_dir)) / "ipc.token")),
        )

    user_home = home or os.path.expanduser("~")
    if normalize_platform() == "windows":
        # Cross-testing Linux paths from a Windows host: preserve POSIX separators.
        base = PurePosixPath(user_home)
        return PlatformPaths(
            state_dir=PurePosixPath("/var/lib/intermux"),
            state_file=PurePosixPath("/var/lib/intermux/state.json"),
            run_dir=PurePosixPath("/run/intermux"),
            helper_lock_path=PurePosixPath("/run/intermux/helper.lock"),
            app_log_dir=base / ".local/state/intermux/logs",
            history_path=base / ".local/state/intermux/history.json",
            report_dir=base / "intermux-reports",
            profile_dir=base / ".local/state/intermux/profiles",
        )

    home_path = Path(user_home)
    state_dir = Path("/var/lib/intermux")
    run_dir = Path("/run/intermux")
    local_state = home_path / ".local" / "state" / "intermux"
    return PlatformPaths(
        state_dir=state_dir,
        state_file=state_dir / "state.json",
        run_dir=run_dir,
        helper_lock_path=run_dir / "helper.lock",
        app_log_dir=local_state / "logs",
        history_path=local_state / "history.json",
        report_dir=home_path / "intermux-reports",
        profile_dir=local_state / "profiles",
    )
