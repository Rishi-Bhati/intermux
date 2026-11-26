"""Unified Windows GUI entrypoint delegating to intermux.__main__ (runs unprivileged)."""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from intermux.__main__ import main as _intermux_main


def main() -> int:
    # Support passing CLI arguments to InterMux.exe directly as well as GUI launch with no args.
    return _intermux_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
