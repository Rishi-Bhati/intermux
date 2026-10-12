"""Policy routing for namespace subnets: per-subnet routing tables and ip rules."""
from __future__ import annotations

import logging
from ipaddress import IPv4Network
from typing import Any, Dict, Iterable, List, Optional

from intermux.config import PRIORITY_BASE, SUBNET_BASE_OCTET, TABLE_BASE
from intermux.errors import SystemCommandError
from intermux.system.commands import CmdResult, json_cmd, run, run_checked, which

log = logging.getLogger("intermux.routing")

IP = which("ip") or "/usr/sbin/ip"
POOL = IPv4Network(f"10.{SUBNET_BASE_OCTET}.0.0/16")
_PRIORITY_MIN = PRIORITY_BASE + 1
_PRIORITY_MAX = PRIORITY_BASE + 254
_RULE_MISSING = "No such"
_FLUSH_MISSING = "FIB table does not exist"


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def rule_src_network(rule: Dict[str, Any]) -> Optional[IPv4Network]:
    """Source selector of an `ip -j rule show` entry as an IPv4 network; None for 'all' or unparsable."""
    src = rule.get("src")
    if not isinstance(src, str) or src in ("", "all"):
        return None
    if "/" not in src:
        srclen = _as_int(rule.get("srclen"))
        if srclen is not None:
            src = f"{src}/{srclen}"
    try:
        return IPv4Network(src, strict=False)
    except ValueError:
        return None


def rule_priority(rule: Dict[str, Any]) -> Optional[int]:
    """Priority of an ip rule entry, or None if missing."""
    return _as_int(rule.get("priority"))


def rule_table(rule: Dict[str, Any]) -> Optional[int]:
    """Numeric lookup table of an ip rule entry, or None for named tables such as 'main'."""
    return _as_int(rule.get("table", rule.get("lookup")))


def rule_matches(rule: Dict[str, Any], subnet: Any, table: int, priority: int) -> bool:
    """True when the rule selects `subnet`, looks up `table` and has `priority`."""
    return (
        rule_src_network(rule) == IPv4Network(str(subnet), strict=False)
        and rule_table(rule) == table
        and rule_priority(rule) == priority
    )


def is_intermux_rule(rule: Dict[str, Any]) -> bool:
    """True only for the exact rule intermux adds for namespace n: from 10.200.n.0/24, table TABLE_BASE+n, priority PRIORITY_BASE+n."""
    src = rule_src_network(rule)
    if src is None or src.prefixlen != 24 or not src.subnet_of(POOL):
        return False
    index = src.network_address.packed[2]
    return (
        1 <= index <= 254
        and rule_table(rule) == TABLE_BASE + index
        and rule_priority(rule) == PRIORITY_BASE + index
    )


def select_intermux_rules(rules: Iterable[Any]) -> List[Dict[str, Any]]:
    """Keep only the dict entries that are intermux rules."""
    return [rule for rule in rules if isinstance(rule, dict) and is_intermux_rule(rule)]


def _run_tolerating(argv: List[str], markers: tuple) -> CmdResult:
    result = run(argv, tolerate=markers)
    if result.ok or any(m in result.stderr or m in result.stdout for m in markers):
        return result
    detail = result.stderr.strip() or f"exit status {result.returncode}"
    raise SystemCommandError(f"{argv[0]} {' '.join(argv[1:3])} failed: {detail}", result=result)


def list_rules() -> List[Dict[str, Any]]:
    """All ip rules as parsed by `ip -j rule show`; [] on failure."""
    data = json_cmd([IP, "-j", "rule", "show"])
    if not isinstance(data, list):
        return []
    return [rule for rule in data if isinstance(rule, dict)]


def intermux_rules() -> List[Dict[str, Any]]:
    """ip rules owned by intermux (see is_intermux_rule)."""
    return select_intermux_rules(list_rules())


_ROUTE_METRIC = 100          # the interface route; wins while the interface is up
_BLACKHOLE_METRIC = 1000     # fallback when it is gone: drop instead of leaking via another table


def ensure_policy(subnet: IPv4Network, iface: str, table: int, priority: int, gateway: Optional[str]) -> None:
    """Point the default route of `table` at `iface` (via `gateway`), add an unreachable fallback, and add the from-subnet rule if missing."""
    route = [IP, "route", "replace", "default"]
    if gateway:
        route += ["via", gateway]
    route += ["dev", iface, "table", str(table), "metric", str(_ROUTE_METRIC)]
    run_checked(route)
    run_checked([IP, "route", "replace", "unreachable", "default",
                 "metric", str(_BLACKHOLE_METRIC), "table", str(table)])
    if not any(rule_matches(rule, subnet, table, priority) for rule in list_rules()):
        run_checked([IP, "rule", "add", "from", str(subnet), "lookup", str(table), "priority", str(priority)])


def remove_policy(subnet: IPv4Network, table: int, priority: int) -> None:
    """Delete the from-subnet rule and flush its table. Missing rules or tables are not errors."""
    run([IP, "rule", "del", "from", str(subnet), "lookup", str(table), "priority", str(priority)],
        tolerate=(_RULE_MISSING,))
    flush_table(table)


def flush_table(table: int) -> None:
    """Flush all routes in `table`; a table that does not exist is not an error."""
    _run_tolerating([IP, "route", "flush", "table", str(table)], (_FLUSH_MISSING,))
