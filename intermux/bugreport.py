"""Local bug report zip that can be uploaded to the web portal.

The zip holds a Markdown system report (versions, interfaces, bindings and tools) and the
recent app logs. It is written to the user's own folder. This module never sends anything
over the network.

Home directory paths are replaced with "~" and addresses are scrubbed, but the files can
still contain app names and log lines, so users should review them first.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import stat
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from intermux.config import VERSION

log = logging.getLogger("intermux.bugreport")

REPORT_DIR_NAME = "intermux-reports"
LOG_DIR_REL = Path(".local/state/intermux/logs")
LOG_FILES_TO_INCLUDE = 3
LOG_LINES_PER_FILE = 60
LOG_TAIL_BYTES = 256 * 1024


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


def _mtime(path: Path) -> float:
    """Modification time, or 0 for a file that vanished or cannot be read (an app may rotate its log)."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _log_files(log_dir: Path) -> List[Path]:
    """Log files newest first; [] when the folder is missing or unreadable."""
    try:
        return sorted(log_dir.glob("*.log"), key=_mtime, reverse=True)
    except OSError:
        return []


def _read_log_tail(path: Path, max_lines: int, max_bytes: int = LOG_TAIL_BYTES) -> Optional[List[str]]:
    """Last max_lines lines from the final max_bytes of a regular file; None if it is unsafe or unreadable.

    Symlinks, FIFOs and other non-regular files are skipped. Only the tail is read.
    """
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        start = max(0, st.st_size - max_bytes)
        os.lseek(fd, start, os.SEEK_SET)
        chunks = []
        remaining = max_bytes
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError:
        return None
    finally:
        os.close(fd)
    text = b"".join(chunks).decode("utf-8", errors="replace")
    lines = text.splitlines()
    if start > 0 and lines:
        lines = lines[1:]  # the first line may be cut in half
    return lines[-max_lines:]


def _recent_logs(home: str) -> List[Dict[str, Any]]:
    files = _log_files(Path(home) / LOG_DIR_REL)
    excerpts = []
    for path in files[:LOG_FILES_TO_INCLUDE]:
        lines = _read_log_tail(path, LOG_LINES_PER_FILE)
        if lines is None:
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
_URL_QUERY_RE = re.compile(r"(https?://[^\s?#\"'<>]*)\?[^\s\"'<>]*", re.IGNORECASE)
_TOKEN_RE = re.compile(r"\br[sp]k_[0-9A-Fa-f]+")


def scrub_text(text: str, home: Optional[str], username: Optional[str]) -> str:
    """Remove personal data: home path, user name, IP and MAC addresses, URL queries and report tokens."""
    if home:
        text = text.replace(home, "~")
    text = _HOME_RE.sub("/home/USER/", text)
    text = _RUN_USER_RE.sub("/run/user/UID", text)
    if username and len(username) >= 2:
        text = re.sub(rf"(?<![0-9A-Za-z_]){re.escape(username)}(?![0-9A-Za-z_])", "USER", text)
    text = _TOKEN_RE.sub("TOKEN", text)
    text = _URL_QUERY_RE.sub(r"\1?QUERY", text)
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

    log_files = _log_files(Path(home) / LOG_DIR_REL)

    # Written with owner-only permissions: the archive contains log lines.
    fd, path = _create_unique(target_dir, f"intermux-bug-report-{stamp}", ".zip")
    with os.fdopen(fd, "wb") as handle, zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("system-report.md", summary)
        for log_path in log_files:
            lines = _read_log_tail(log_path, LOG_LINES_IN_ZIP)
            if lines is None:
                continue
            text = scrub_text("\n".join(lines), home, user)
            archive.writestr(f"logs/{log_path.name}", text + "\n")
        if not log_files:
            archive.writestr("logs/README.txt", "No app log files were found.\n")
    return path

