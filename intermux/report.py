"""The web portal where bug reports and feature requests are filed."""

from __future__ import annotations

from intermux.system import commands

REPORT_PAGE_URL = "https://reportary.onrender.com/p/nWQbq2dDTlsy89u6XBWG2UV0pz9-vQuR_WnRuU7U7bWUMVi5rqRlwDxhs8j_w6r9/"


def open_report_page() -> bool:
    """Open the web report form in the default browser. Returns False if no opener is available."""
    return commands.open_with_desktop(REPORT_PAGE_URL).ok
