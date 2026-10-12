"""Local bug report bundles that can be uploaded to the web portal.

A bundle is one Markdown file: the report fields plus diagnostic details
(versions, interfaces, bindings, tools and recent logs). It is written to the
user's own folder. This module never sends anything over the network.

Home directory paths are replaced with "~" in the log excerpts, but the file
can still contain app names and log lines, so users should review it first.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from intermux import report
from intermux.config import VERSION

log = logging.getLogger("intermux.bugreport")

REPORT_DIR_NAME = "intermux-reports"
LOG_DIR_REL = Path(".local/state/intermux/logs")
LOG_FILES_TO_INCLUDE = 3
LOG_LINES_PER_FILE = 60


def default_report_dir() -> Path:
    """Folder where bundles are saved unless the caller picks another one."""
    return Path(os.path.expanduser("~")) / REPORT_DIR_NAME


def collect_diagnostics(include_logs: bool = True, home: Optional[str] = None) -> Dict[str, Any]:
    """Gather non-personal diagnostic details. Each section degrades to an error note on failure."""
    home = home or os.path.expanduser("~")
    info: Dict[str, Any] = {
        "intermux": VERSION,
        "python": platform.python_version(),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "distribution": _distribution(),
        "session": os.environ.get("XDG_SESSION_TYPE", "unknown") or "unknown",
        "interfaces": _interfaces(),
        "bindings": _bindings(),
        "tools": _tools(),
    }
    if include_logs:
        info["recent_logs"] = _recent_logs(home)
    return info


def _distribution() -> str:
    try:
        from intermux.system.deps import distro_info

        info = distro_info()
        return f"{info.name} {info.version_id}".strip()
    except Exception as exc:
        log.debug("distribution unknown", exc_info=True)
        return f"unknown ({exc.__class__.__name__})"


def _interfaces() -> List[Dict[str, Any]]:
    try:
        from intermux.system import netinfo

        # Names and states only: IP and MAC addresses are left out on purpose.
        return [
            {"name": i.name, "kind": i.kind, "up": i.up, "bindable": i.bindable}
            for i in netinfo.list_interfaces()
        ]
    except Exception as exc:
        log.debug("interface scan failed", exc_info=True)
        return [{"error": exc.__class__.__name__}]


def _bindings() -> List[Dict[str, Any]]:
    try:
        from intermux import state

        return [
            {"iface": b.iface, "app": b.app_name, "profile_mode": b.profile_mode, "namespace": b.namespace}
            for b in state.load_state().bindings
        ]
    except Exception as exc:
        log.debug("state unreadable", exc_info=True)
        return [{"error": exc.__class__.__name__}]


def _tools() -> List[Dict[str, Any]]:
    try:
        from intermux.system import deps

        return [
            {"tool": d.tool, "required": d.required, "present": d.present}
            for d in deps.check_dependencies()
        ]
    except Exception as exc:
        log.debug("dependency check failed", exc_info=True)
        return [{"error": exc.__class__.__name__}]


def _recent_logs(home: str) -> List[Dict[str, Any]]:
    log_dir = Path(home) / LOG_DIR_REL
    try:
        files = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    excerpts = []
    for path in files[:LOG_FILES_TO_INCLUDE]:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-LOG_LINES_PER_FILE:]
        except OSError:
            continue
        text = scrub_text("\n".join(lines), home, _current_user())
        excerpts.append({"file": path.name, "text": text})
    return excerpts


_HEX = r"[0-9A-Fa-f]{1,4}"
_MAC_RE = re.compile(r"(?<![0-9A-Fa-f:-])(?:[0-9A-Fa-f]{2}([:-]))(?:[0-9A-Fa-f]{2}\1){4}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:-])")
_IPV6_RE = re.compile(
    r"(?<![0-9A-Za-z:])(?:"
    rf"(?:{_HEX}:){{1,7}}:(?:{_HEX}(?::{_HEX}){{0,6}})?"
    rf"|::(?:{_HEX}:){{0,6}}{_HEX}"
    rf"|(?:{_HEX}:){{3,7}}{_HEX}"
    r")(?![0-9A-Za-z:])"
)
_IPV4_RE = re.compile(
    r"(?<![0-9.])(?:(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.){3}"
    r"(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])(?![0-9])"
)
_HOME_RE = re.compile(r"/home/[^/\s]+/")
_RUN_USER_RE = re.compile(r"/run/user/[0-9]+")


def scrub_text(text: str, home: Optional[str], username: Optional[str]) -> str:
    """Remove personal data: home path, user name, IP and MAC addresses."""
    if home:
        text = text.replace(home, "~")
    text = _HOME_RE.sub("/home/USER/", text)
    text = _RUN_USER_RE.sub("/run/user/UID", text)
    if username and len(username) >= 2:
        text = re.sub(rf"(?<![0-9A-Za-z_]){re.escape(username)}(?![0-9A-Za-z_])", "USER", text)
    text = _MAC_RE.sub("xx:xx:xx:xx:xx:xx", text)
    text = _IPV6_RE.sub("x:x:x", text)
    text = _IPV4_RE.sub("x.x.x.x", text)
    return text


def _current_user() -> str:
    name = os.environ.get("USER", "")
    if name:
        return name
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        return ""


def _create_unique(directory: Path, stem: str, suffix: str) -> "tuple[int, Path]":
    """Create a new owner-only file; append -2, -3 ... if the name is taken."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    counter = 1
    while True:
        name = f"{stem}{suffix}" if counter == 1 else f"{stem}-{counter}{suffix}"
        path = directory / name
        try:
            return os.open(path, flags, 0o600), path
        except FileExistsError:
            counter += 1


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def build_markdown(payload: Dict[str, str], diagnostics: Dict[str, Any], created: str) -> str:
    """Render the report fields and diagnostics as one Markdown document."""
    lines: List[str] = [
        f"# {payload['title']}",
        "",
        f"- **Type:** {payload['report_type']}",
        f"- **Impact:** {payload.get('impact', 'not given')}",
        f"- **How often:** {payload.get('frequency', 'not given')}",
        f"- **Created:** {created}",
        "",
        "## Description",
        "",
        payload["description"],
        "",
        "## Steps to reproduce",
        "",
        payload.get("steps") or "Not provided.",
        "",
        "## Environment",
        "",
        f"- InterMux {diagnostics.get('intermux')}",
        f"- Python {diagnostics.get('python')}",
        f"- Kernel {diagnostics.get('kernel')} ({diagnostics.get('machine')})",
        f"- Distribution {diagnostics.get('distribution')}",
        f"- Session {diagnostics.get('session')}",
        "",
        "## Interfaces",
        "",
        _table(diagnostics.get("interfaces", []), ["name", "kind", "up", "bindable"]),
        "",
        "## Bindings",
        "",
        _table(diagnostics.get("bindings", []), ["iface", "app", "profile_mode", "namespace"]),
        "",
        "## Tools",
        "",
        _table(diagnostics.get("tools", []), ["tool", "required", "present"]),
    ]
    if "recent_logs" in diagnostics:
        lines += ["", "## Recent logs", ""]
        if not diagnostics["recent_logs"]:
            lines.append("No log files found.")
        for entry in diagnostics["recent_logs"]:
            lines += [f"### {entry['file']}", "", "```", entry["text"], "```", ""]
    lines += [
        "",
        "---",
        f"Generated by InterMux {VERSION}. Review this file before uploading: it can contain app names and log lines.",
        "",
    ]
    return "\n".join(lines)


def _table(rows: List[Dict[str, Any]], columns: List[str]) -> str:
    if not rows:
        return "None."
    if any("error" in row for row in rows):
        return f"Could not read: {rows[0].get('error', 'unknown error')}"
    header = "| " + " | ".join(columns) + " |"
    rule = "| " + " | ".join("---" for _ in columns) + " |"
    body = []
    for row in rows:
        cells = []
        for column in columns:
            value = row.get(column, "")
            if isinstance(value, bool):
                value = _yes_no(value)
            cells.append(str(value).replace("|", "/"))
        body.append("| " + " | ".join(cells) + " |")
    return "\n".join([header, rule, *body])


LOG_LINES_IN_ZIP = 500


def write_bug_zip(directory: Optional[Path] = None, home: Optional[str] = None) -> Path:
    """Write a zip with the system report and the app logs. Returns the zip path.

    No title or description is asked for: the user adds those in the web portal.
    Log lines are redacted (home path replaced with "~") and capped per file.
    """
    import zipfile

    home = home or os.path.expanduser("~")
    created = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    diagnostics = collect_diagnostics(include_logs=False, home=home)
    summary_payload = {
        "title": "InterMux system report",
        "description": "Created with 'Save bug report'. Add your own description when you upload this file.",
        "report_type": "bug",
    }
    user = _current_user()
    summary = scrub_text(build_markdown(summary_payload, diagnostics, created), home, user)

    target_dir = directory or default_report_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")

    log_dir = Path(home) / LOG_DIR_REL
    log_files = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True) \
        if log_dir.is_dir() else []

    # Written with owner-only permissions: the archive contains log lines.
    fd, path = _create_unique(target_dir, f"intermux-bug-report-{stamp}", ".zip")
    with os.fdopen(fd, "wb") as handle, zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("system-report.md", summary)
        for log_path in log_files:
            try:
                lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            text = scrub_text("\n".join(lines[-LOG_LINES_IN_ZIP:]), home, user)
            archive.writestr(f"logs/{log_path.name}", text + "\n")
        if not log_files:
            archive.writestr("logs/README.txt", "No app log files were found.\n")
    return path


def write_bug_report(*, title: Optional[str], description: Optional[str], report_type: str = "bug",
                     impact: Optional[str] = None, frequency: Optional[str] = None,
                     steps: Optional[str] = None, directory: Optional[Path] = None,
                     include_logs: bool = True) -> Path:
    """Validate the fields, collect diagnostics and write the bundle. Returns the file path."""
    payload = report.build_payload(
        title=title, description=description, report_type=report_type,
        impact=impact, frequency=frequency, steps=steps)
    created = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    diagnostics = collect_diagnostics(include_logs=include_logs)
    text = build_markdown(payload, diagnostics, created)

    target_dir = directory or default_report_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = target_dir / f"intermux-report-{stamp}-{payload['report_type']}.md"
    # Written with owner-only permissions: the bundle can contain log lines.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path
