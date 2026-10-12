"""Top-level dispatch: GUI with no arguments, privileged helper for "helper", CLI otherwise."""

from __future__ import annotations

import os
import sys
from typing import List, Optional


def main(argv: Optional[List[str]] = None) -> int:
    """Dispatch argv to the GUI, the helper, or the CLI and return the exit code."""
    if argv is None:
        argv = sys.argv[1:]
    # polkit authorises the whole binary, so root may only ever run the helper.
    if os.geteuid() == 0 and argv[:1] != ["helper"]:
        if argv:
            sys.stderr.write("[X] run InterMux commands as your own user; root is used only by the helper\n")
        else:
            sys.stderr.write("[X] the GUI cannot run as root; run InterMux as your own user\n")
        return 1
    # Imports are lazy so that `intermux helper` never imports tkinter.
    if not argv:
        from intermux.ui.app import main as gui_main

        return gui_main()
    if argv[0] == "helper":
        from intermux.helper.server import main as helper_main

        return helper_main()
    from intermux.cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())
