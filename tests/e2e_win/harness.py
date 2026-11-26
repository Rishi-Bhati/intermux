"""Environment isolation, capability probing, and strict M5 gating for E2E tests."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
GO_ENGINE_DIR = REPO_ROOT / "windows" / "engine"


def strict_mode() -> bool:
    return os.environ.get("INTERMUX_E2E_STRICT", "") == "1"


def require_capability(test_case: unittest.TestCase, name: str, available: bool, reason: str = "") -> None:
    if available:
        return
    msg = f"missing capability {name}: {reason or 'not implemented yet'}"
    if strict_mode():
        test_case.fail(msg)
    test_case.skipTest(msg)


def has_windows_backend() -> bool:
    try:
        from intermux.platform import windows as _win  # noqa: F401
        return True
    except ImportError:
        return False


def has_go_toolchain() -> bool:
    return shutil.which("go") is not None and (GO_ENGINE_DIR / "go.mod").exists()


def make_isolated_env(test_case: unittest.TestCase) -> Path:
    tmp = tempfile.TemporaryDirectory(prefix="intermux_e2e_")
    test_case.addCleanup(tmp.cleanup)
    root = Path(tmp.name)
    home = root / "home"
    local_app_data = root / "LocalAppData"
    program_data = root / "ProgramData"
    program_files = root / "ProgramFiles"
    for p in (home, local_app_data, program_data, program_files):
        p.mkdir(parents=True, exist_ok=True)

    saved = {k: os.environ.get(k) for k in (
        "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "ProgramData", "PROGRAMDATA",
        "ProgramFiles", "PROGRAMFILES",
    )}

    def _restore() -> None:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    test_case.addCleanup(_restore)
    os.environ["HOME"] = str(home)
    os.environ["USERPROFILE"] = str(home)
    os.environ["LOCALAPPDATA"] = str(local_app_data)
    os.environ["APPDATA"] = str(root / "Roaming")
    os.environ["ProgramData"] = str(program_data)
    os.environ["PROGRAMDATA"] = str(program_data)
    os.environ["ProgramFiles"] = str(program_files)
    os.environ["PROGRAMFILES"] = str(program_files)
    return root
