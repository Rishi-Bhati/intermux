"""Issue and feature reports for the Reportary tracker.

A report can be sent in two ways:
  * opened as a web form in the browser (no credentials needed), or
  * posted to the Reportary API when a token is set in INTERMUX_REPORTARY_TOKEN.

Credentials are never bundled with InterMux. The token is read from the
environment only, so it never ships in the binary or the repository.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from intermux.config import VERSION
from intermux.errors import InterMuxError, ValidationError
from intermux.system import commands

log = logging.getLogger("intermux.report")

REPORT_PAGE_URL = "https://reportary.onrender.com/p/nWQbq2dDTlsy89u6XBWG2UV0pz9-vQuR_WnRuU7U7bWUMVi5rqRlwDxhs8j_w6r9/"
API_URL = "https://reportary.onrender.com/api/v1/reports/"
TOKEN_ENV = "INTERMUX_REPORTARY_TOKEN"
REPORT_TYPES = ("bug", "feature", "vulnerability")
IMPACTS = ("low", "medium", "high", "critical")
FREQUENCIES = ("once", "daily", "weekly", "monthly")
TITLE_MAX = 200
DESCRIPTION_MAX = 10000
STEPS_MAX = 5000


def build_payload(*, title: Optional[str], description: Optional[str], report_type: str = "bug",
                  impact: Optional[str] = None, frequency: Optional[str] = None,
                  steps: Optional[str] = None) -> Dict[str, str]:
    """Validate the form fields and return the API payload. Raises ValidationError."""
    title = (title or "").strip()
    description = (description or "").strip()
    if not title:
        raise ValidationError("a title is required")
    if len(title) > TITLE_MAX:
        raise ValidationError(f"title must be at most {TITLE_MAX} characters")
    if not description:
        raise ValidationError("a description is required")
    if len(description) > DESCRIPTION_MAX:
        raise ValidationError(f"description must be at most {DESCRIPTION_MAX} characters")
    if report_type not in REPORT_TYPES:
        raise ValidationError(f"report type must be one of: {', '.join(REPORT_TYPES)}")
    if impact is not None and impact not in IMPACTS:
        raise ValidationError(f"impact must be one of: {', '.join(IMPACTS)}")
    if frequency is not None and frequency not in FREQUENCIES:
        raise ValidationError(f"frequency must be one of: {', '.join(FREQUENCIES)}")
    steps = (steps or "").strip()
    if len(steps) > STEPS_MAX:
        raise ValidationError(f"steps must be at most {STEPS_MAX} characters")

    payload: Dict[str, str] = {"title": title, "description": description, "report_type": report_type}
    if impact:
        payload["impact"] = impact
    if frequency:
        payload["frequency"] = frequency
    if steps:
        payload["steps"] = steps
    return payload


def system_summary() -> str:
    """Short, non-personal description of the InterMux version and the system it runs on."""
    lines = [f"InterMux {VERSION}", f"Python {platform.python_version()}", f"Kernel {platform.release()}"]
    try:
        from intermux.system.deps import distro_info

        info = distro_info()
        lines.append(f"Distribution {info.name} {info.version_id}".strip())
    except Exception:  # the summary is optional; never block a report on it
        log.debug("could not read distribution info", exc_info=True)
    return "\n".join(lines)


def with_environment(payload: Dict[str, str]) -> Dict[str, str]:
    """Return a copy of payload with the system summary appended to the description."""
    out = dict(payload)
    out["description"] = f"{payload['description']}\n\n---\nEnvironment:\n{system_summary()}"
    return out


def configured_token() -> Optional[str]:
    """The API token from the environment, or None when submitting from the app is not set up."""
    value = os.environ.get(TOKEN_ENV, "").strip()
    return value or None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect, so the Authorization header is never sent to another URL."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def submit(payload: Dict[str, str], token: Optional[str] = None, timeout: float = 30.0) -> Dict[str, Any]:
    """POST a report to the Reportary API and return the created report. Raises InterMuxError."""
    token = token or configured_token()
    if not token:
        raise InterMuxError(
            f"no Reportary token is set; export {TOKEN_ENV} or open the web form instead",
            code="not_configured")
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": f"intermux/{VERSION}",
        })
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise InterMuxError(f"Reportary rejected the report (HTTP {exc.code}): {detail}",
                            code="report_rejected") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise InterMuxError(f"could not reach Reportary: {exc}", code="network_error") from exc
    try:
        data = json.loads(body.decode("utf-8") or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def open_report_page() -> bool:
    """Open the web report form in the default browser. Returns False if no opener is available."""
    return commands.run(["xdg-open", REPORT_PAGE_URL], timeout=10.0).ok
