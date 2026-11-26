"""Unified Windows CLI shim delegating to intermux.__main__."""

from __future__ import annotations

import os
import sys
from typing import List, Optional

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from intermux.__main__ import main as _intermux_main


def main(argv: Optional[List[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        argv = ["--help"]
    return _intermux_main(list(argv))


if __name__ == "__main__":
    raise SystemExit(main())
