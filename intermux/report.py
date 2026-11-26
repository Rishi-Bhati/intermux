"""The web portal where bug reports and feature requests are filed."""

from __future__ import annotations

import sys

REPORT_PAGE_URL = "https://reportary.onrender.com/p/nWQbq2dDTlsy89u6XBWG2UV0pz9-vQuR_WnRuU7U7bWUMVi5rqRlwDxhs8j_w6r9/"


def open_report_page() -> bool:
    """Open the web report form in the default browser. Returns False if no opener is available."""
    if sys.platform == "win32":
        from intermux import platform as _platform

        return bool(_platform.get_platform().diagnostics.open_with_desktop(REPORT_PAGE_URL).ok)
    from intermux.system import commands

    return commands.open_with_desktop(REPORT_PAGE_URL).ok
