"""Top-level dispatch: GUI with no arguments, privileged helper for "helper", CLI otherwise."""

from __future__ import annotations

import os
import sys
from typing import List, Optional


def main(argv: Optional[List[str]] = None) -> int:
    """Dispatch argv to the GUI, the helper, or the CLI and return the exit code."""
    if argv is None:
        argv = sys.argv[1:]
    # polkit authorises the whole binary, so root may only ever run the helper or the watcher.
    if os.geteuid() == 0 and argv[:1] not in (["helper"], ["watch"]):
        if argv:
            sys.stderr.write("[X] run InterMux commands as your own user; root is used only by the helper\n")
        else:
            sys.stderr.write("[X] the GUI cannot run as root; run InterMux as your own user\n")
        return 1
    # The watcher is a long-running root service for systemd; pkexec must only ever start the helper.
    if argv[:1] == ["watch"] and "PKEXEC_UID" in os.environ:
        sys.stderr.write("[X] the watcher runs as a system service (intermux-watch.service), not through pkexec\n")
        return 1
    # Imports are lazy so that `intermux helper` never imports tkinter.
    if not argv:
        from intermux.ui.app import main as gui_main

        return gui_main()
    if argv[0] == "helper":
        from intermux.helper.server import main as helper_main

        return helper_main()
    if argv[0] == "watch":
        from intermux.watch import main as watch_main

        return watch_main()
    from intermux.cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())
