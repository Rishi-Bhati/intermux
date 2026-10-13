"""Policy routing for namespace subnets: per-subnet routing tables and ip rules."""
from __future__ import annotations

import logging
from ipaddress import IPv4Network
from typing import Any, Callable, Dict, Iterable, List, Optional

from intermux.config import PRIORITY_BASE, TABLE_BASE
from intermux.errors import SystemCommandError
from intermux.subnets import POOL
from intermux.system.commands import CmdResult, ip_bin, json_cmd, run, run_checked

log = logging.getLogger("intermux.routing")

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
    data = json_cmd([ip_bin(), "-j", "rule", "show"])
    if not isinstance(data, list):
        return []
    return [rule for rule in data if isinstance(rule, dict)]


def intermux_rules() -> List[Dict[str, Any]]:
    """ip rules owned by intermux (see is_intermux_rule)."""
    return select_intermux_rules(list_rules())


_ROUTE_METRIC = 100          # the interface route; wins while the interface is up
_BLACKHOLE_METRIC = 1000     # fallback when it is gone: drop instead of leaking via another table


def _is_route(route: Any, dst: str, dev: Optional[str], gateway: Optional[str], metric: Optional[int],
              kind: Optional[str] = None) -> bool:
    if not isinstance(route, dict) or route.get("dst") != dst or route.get("type") != kind:
        return False
    if kind == "unreachable":
        return _as_int(route.get("metric")) == metric
    return (route.get("dev") == dev and (route.get("gateway") or None) == (gateway or None)
            and (_as_int(route.get("metric")) or None) == metric)


def wanted_routes(subnet: IPv4Network, iface: str, gateway: Optional[str],
                  host_veth: Optional[str]) -> List[Callable[[Any], bool]]:
    """Predicates for the routes ensure_policy keeps in a namespace table; anything else there is stray."""
    wanted = [
        lambda r: _is_route(r, "default", iface, gateway, _ROUTE_METRIC),
        lambda r: _is_route(r, "default", None, None, _BLACKHOLE_METRIC, "unreachable"),
    ]
    if host_veth:
        wanted.append(lambda r: _is_route(r, str(subnet), host_veth, None, None))
    return wanted


def policy_table_ok(routes: Iterable[Any], subnet: IPv4Network, iface: str, gateway: Optional[str],
                    host_veth: Optional[str]) -> bool:
    """True when `ip -j route show table T` output holds exactly the routes ensure_policy adds."""
    routes = [r for r in routes if isinstance(r, dict)]
    wanted = wanted_routes(subnet, iface, gateway, host_veth)
    return (all(any(w(r) for r in routes) for w in wanted)
            and all(any(w(r) for w in wanted) for r in routes))


def stray_routes(routes: Iterable[Any], subnet: IPv4Network, iface: str, gateway: Optional[str],
                 host_veth: Optional[str]) -> List[Dict[str, Any]]:
    """Routes in a namespace table that ensure_policy did not ask for (an older default via another
    interface with a lower metric would otherwise win and send the namespace out of the wrong uplink)."""
    wanted = wanted_routes(subnet, iface, gateway, host_veth)
    return [r for r in routes if isinstance(r, dict) and not any(w(r) for w in wanted)]


def route_del_args(route: Dict[str, Any], table: int) -> List[str]:
    """`ip route del` arguments for one entry of `ip -j route show table T`."""
    argv = [ip_bin(), "route", "del"]
    kind = route.get("type")
    if isinstance(kind, str) and kind not in ("", "unicast"):
        argv.append(kind)
    argv.append(str(route.get("dst", "default")))
    if route.get("gateway"):
        argv += ["via", str(route["gateway"])]
    if route.get("dev"):
        argv += ["dev", str(route["dev"])]
    metric = _as_int(route.get("metric"))
    if metric is not None:
        argv += ["metric", str(metric)]
    return argv + ["table", str(table)]


def ensure_policy(subnet: IPv4Network, iface: str, table: int, priority: int, gateway: Optional[str],
                  host_veth: Optional[str] = None) -> None:
    """Make `table` hold exactly: the namespace subnet on `host_veth`, the default via `iface`/`gateway`
    and an unreachable fallback; then add the from-subnet rule if missing.

    The subnet route matters because the from-subnet rule also matches the host side 10.200.N.1: without it,
    strict rp_filter rejects the namespace's ARP for its gateway and host-generated ICMP leaves by the uplink.
    New routes are added before stray ones are deleted, so the table is never empty while apps run."""
    if host_veth:
        run_checked([ip_bin(), "route", "replace", str(subnet), "dev", host_veth, "scope", "link",
                     "src", str(subnet.network_address + 1), "table", str(table)])
    route = [ip_bin(), "route", "replace", "default"]
    if gateway:
        route += ["via", gateway]
    route += ["dev", iface, "table", str(table), "metric", str(_ROUTE_METRIC)]
    run_checked(route)
    run_checked([ip_bin(), "route", "replace", "unreachable", "default",
                 "metric", str(_BLACKHOLE_METRIC), "table", str(table)])
    for stray in stray_routes(json_cmd([ip_bin(), "-j", "route", "show", "table", str(table)]), subnet, iface,
                              gateway, host_veth):
        run(route_del_args(stray, table), tolerate=("No such process",))
    if not any(rule_matches(rule, subnet, table, priority) for rule in list_rules()):
        run_checked([ip_bin(), "rule", "add", "from", str(subnet), "lookup", str(table), "priority", str(priority)])


def policy_route_present(table: int, subnet: IPv4Network, iface: str, gateway: Optional[str],
                         host_veth: Optional[str] = None) -> bool:
    """True when `table` holds exactly the routes ensure_policy adds. The kernel deletes the interface route
    when the link goes down or loses its address (Wi-Fi roam, tether re-plug); only the fallback is left then."""
    return policy_table_ok(json_cmd([ip_bin(), "-j", "route", "show", "table", str(table)]), subnet, iface, gateway,
                           host_veth)


def remove_policy(subnet: IPv4Network, table: int, priority: int) -> None:
    """Delete the from-subnet rule and flush its table. Missing rules or tables are not errors."""
    run([ip_bin(), "rule", "del", "from", str(subnet), "lookup", str(table), "priority", str(priority)],
        tolerate=(_RULE_MISSING,))
    flush_table(table)


def flush_table(table: int) -> None:
    """Flush all routes in `table`; a table that does not exist is not an error."""
    _run_tolerating([ip_bin(), "route", "flush", "table", str(table)], (_FLUSH_MISSING,))
