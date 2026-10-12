"""Upstream DNS server discovery for the resolv.conf files written into namespaces."""

from __future__ import annotations

import ipaddress
import logging
import re
from pathlib import Path
from typing import Callable, Iterable, List, Optional

from intermux.system.commands import run

log = logging.getLogger("intermux.system.dns")

RESOLVED_UPSTREAM_CONF = Path("/run/systemd/resolve/resolv.conf")
ETC_RESOLV_CONF = Path("/etc/resolv.conf")
FALLBACK_SERVERS = ("1.1.1.1", "8.8.8.8")

_RESOLVECTL_LINE_RE = re.compile(r"^\s*(Global|Link \d+ \([^)]*\)):\s*(.*?)\s*$")


def _clean(tokens: Iterable[str]) -> List[str]:
    """Keep valid, non-loopback addresses in order, without duplicates."""
    out: List[str] = []
    for token in tokens:
        # Drop scope ids (fe80::1%eth0) and resolved server-name suffixes (1.1.1.1#host).
        addr = token.split("%", 1)[0].split("#", 1)[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.is_loopback:
            continue
        text = str(ip)
        if text not in out:
            out.append(text)
    return out


def parse_resolvectl_dns(output: str) -> List[str]:
    """Addresses from `resolvectl dns` lines such as 'Global: a b' or 'Link 2 (eth0): a b'."""
    found: List[str] = []
    for line in output.splitlines():
        match = _RESOLVECTL_LINE_RE.match(line)
        if match:
            found.extend(match.group(2).split())
    return _clean(found)


def parse_resolv_conf(text: str) -> List[str]:
    """Addresses from 'nameserver' lines of a resolv.conf body, skipping loopback stubs."""
    found: List[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "nameserver":
            found.append(parts[1])
    return _clean(found)


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _resolvectl_servers() -> List[str]:
    result = run(["resolvectl", "dns"], timeout=5.0, tolerate=("command not found", "Failed to"))
    if not result.ok:
        return []
    return parse_resolvectl_dns(result.stdout)


def _resolved_upstream_servers() -> List[str]:
    text = _read_text(RESOLVED_UPSTREAM_CONF)
    return parse_resolv_conf(text) if text is not None else []


def _etc_resolv_servers() -> List[str]:
    text = _read_text(ETC_RESOLV_CONF)
    return parse_resolv_conf(text) if text is not None else []


def upstream_dns_servers() -> List[str]:
    """Real upstream resolvers for namespaces: resolvectl, then systemd-resolved upstream file, then resolv.conf."""
    finders: List[Callable[[], List[str]]] = [
        _resolvectl_servers,
        _resolved_upstream_servers,
        _etc_resolv_servers,
    ]
    for finder in finders:
        servers = finder()
        if servers:
            return servers
    log.info("no upstream DNS servers found; using fallback %s", ", ".join(FALLBACK_SERVERS))
    return list(FALLBACK_SERVERS)
