"""Cross-platform child process fixture for binding/adoption/teardown tests."""

from __future__ import annotations

import subprocess
import sys
import time


def spawn_sleep_child(seconds: float = 30.0) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({seconds})"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
