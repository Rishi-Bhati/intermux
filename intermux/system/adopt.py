"""Route a running app: cgroup v2 membership, nftables marking and policy routing."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Iterable, List, Optional, Set

from intermux.errors import InterMuxError, MissingToolError, ValidationError
from intermux.system import commands

log = logging.getLogger("intermux.system.adopt")

CGROUP_ROOT = Path("/sys/fs/cgroup")
GROUP_PARENT = "intermux"
SLOT_MIN, SLOT_MAX = 1, 200
MARK_BASE = 0x100
TABLE_BASE = 2000
PRIORITY_BASE = 2000

_ID_RE = re.compile(r"^[0-9a-f]{8,32}$")
_IFACE_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,15}$")
_TABLE_RE = re.compile(r"^intermux_adopt[0-9]+$")
_NFT_MISSING = ("No such file", "does not exist")
_IP_MISSING = ("No such file", "No such process", "FIB table does not exist",
               "Cannot find device", "does not exist")


def group_rel(adopt_id: str) -> str:
    """Return the relative cgroup path for an adoption id."""
    if not isinstance(adopt_id, str) or not _ID_RE.match(adopt_id):
        raise ValidationError("adoption id must be 8-32 lowercase hex characters")
    return f"{GROUP_PARENT}/adopt-{adopt_id}"


def slot_mark(slot: int) -> int:
    return MARK_BASE + slot


def slot_table(slot: int) -> int:
    return TABLE_BASE + slot


def slot_priority(slot: int) -> int:
    return PRIORITY_BASE + slot


def nft_table_name(slot: int) -> str:
    return f"intermux_adopt{slot}"


def pick_slot(used: Iterable[int]) -> int:
    """Return the lowest free slot in SLOT_MIN..SLOT_MAX."""
    taken = set(used)
    for slot in range(SLOT_MIN, SLOT_MAX + 1):
        if slot not in taken:
            return slot
    raise InterMuxError("no free adoption slot", code="no_slot")


def nft_script(slot: int, group_rel: str, iface: str) -> str:
    """Return the nft script that marks the group's packets and masquerades them on iface."""
    if not isinstance(iface, str) or not _IFACE_RE.match(iface):
        raise ValidationError(f"unsafe interface name: {iface!r}")
    parts = [p for p in group_rel.split("/") if p]
    if not parts or any(not re.match(r"^[A-Za-z0-9_.-]+$", p) for p in parts):
        raise ValidationError(f"unsafe cgroup path: {group_rel!r}")
    path = "/".join(parts)
    mark = f"0x{slot_mark(slot):x}"
    name = nft_table_name(slot)
    return (
        f"table inet {name} {{\n"
        f"  chain output {{\n"
        f"    type route hook output priority mangle; policy accept;\n"
        f"    socket cgroupv2 level {len(parts)} \"{path}\" meta mark set {mark}\n"
        f"  }}\n"
        f"  chain postrouting {{\n"
        f"    type nat hook postrouting priority srcnat; policy accept;\n"
        f"    meta mark {mark} oifname \"{iface}\" masquerade\n"
        f"  }}\n"
        f"}}\n"
    )


def _group_dir(rel: str) -> Path:
    return Path(CGROUP_ROOT) / rel


def read_group_pids(group_rel: str) -> List[int]:
    """Return pids listed in the group's cgroup.procs; [] if the group is missing."""
    try:
        text = (_group_dir(group_rel) / "cgroup.procs").read_text()
    except OSError:
        return []
    return [int(t) for t in text.split() if t.isdigit()]


def ensure_group(group_rel: str) -> None:
    """Create the parent and group cgroup directories if missing; cgroup v2 is required."""
    if not (Path(CGROUP_ROOT) / "cgroup.controllers").exists():
        raise InterMuxError("cgroup v2 is required to route a running app", code="cgroup_v1")
    (Path(CGROUP_ROOT) / GROUP_PARENT).mkdir(exist_ok=True)
    _group_dir(group_rel).mkdir(exist_ok=True)


def _write_pid(procs_file: Path, pid: int) -> bool:
    try:
        with open(procs_file, "w") as fh:
            fh.write(str(pid))
        return True
    except OSError as exc:
        log.debug("cannot move pid %d to %s: %s", pid, procs_file, exc)
        return False


def move_pids(group_rel: str, pids: Iterable[int]) -> List[int]:
    """Move pids into the group; pids that vanished are skipped. Returns the pids moved."""
    procs_file = _group_dir(group_rel) / "cgroup.procs"
    return [pid for pid in pids if _write_pid(procs_file, pid)]


def release_group(group_rel: str, pids: Iterable[int] = ()) -> List[int]:
    """Move pids (and anything still in the group) to the root cgroup, then remove the group."""
    wanted: List[int] = []
    for pid in list(pids) + read_group_pids(group_rel):
        if pid not in wanted:
            wanted.append(pid)
    root_procs = Path(CGROUP_ROOT) / "cgroup.procs"
    moved = [pid for pid in wanted if _write_pid(root_procs, pid)]
    if not read_group_pids(group_rel) or not moved:
        try:
            _group_dir(group_rel).rmdir()
        except OSError as exc:
            log.debug("cannot remove group %s: %s", group_rel, exc)
    return moved


def _nft_bin() -> str:
    path = commands.which("nft")
    if not path:
        raise MissingToolError("nft is not installed")
    return path


def _ip_bin() -> str:
    return commands.ip_bin()


def apply_nft(slot: int, group_rel: str, iface: str) -> None:
    """Load the marking table for the slot."""
    script = nft_script(slot, group_rel, iface)
    commands.run_checked([_nft_bin(), "-f", "-"], input_text=script)


def delete_nft(slot: int) -> None:
    """Delete the slot's nft table; a missing table is fine."""
    nft = commands.which("nft")
    if not nft:
        return
    commands.run_checked([nft, "delete", "table", "inet", nft_table_name(slot)],
                         tolerate=_NFT_MISSING)


def list_nft_tables() -> List[str]:
    """Return names of intermux_adopt<N> tables currently loaded."""
    nft = commands.which("nft")
    if not nft:
        return []
    data = commands.json_cmd([nft, "-j", "list", "tables"])
    names: List[str] = []
    items = data.get("nftables", []) if isinstance(data, dict) else []
    for item in items:
        table = item.get("table") if isinstance(item, dict) else None
        name = table.get("name") if isinstance(table, dict) else None
        if isinstance(name, str) and _TABLE_RE.match(name) and name not in names:
            names.append(name)
    return names


_ROUTE_METRIC = 100          # the interface default; wins while the link is up
_BLACKHOLE_METRIC = 1000     # kill switch: drop instead of falling through to the main table
_V6_MISSING = _IP_MISSING + ("Address family not supported", "Operation not supported",
                             "Protocol not supported", "RTNETLINK answers: Not supported")


def rule_args(slot: int, family: int = 4, action: str = "add") -> List[str]:
    """argv (without the ip binary) for the slot's fwmark rule; family 6 adds the `-6` flag."""
    argv = ["-6"] if family == 6 else []
    return argv + ["rule", action, "fwmark", hex(slot_mark(slot)), "lookup", str(slot_table(slot)),
                   "priority", str(slot_priority(slot))]


def route_commands(slot: int, iface: str, gateway: Optional[str]) -> List[List[str]]:
    """argv lists (without the ip binary) that fill the slot's tables: IPv4 interface default at metric 100,
    IPv4 unreachable fallback at metric 1000, and an IPv6 unreachable default (no IPv6 leak)."""
    table = str(slot_table(slot))
    route = ["route", "replace", "default"]
    if gateway:
        route += ["via", gateway]
    route += ["dev", iface, "table", table, "metric", str(_ROUTE_METRIC)]
    return [
        route,
        ["route", "replace", "unreachable", "default", "metric", str(_BLACKHOLE_METRIC), "table", table],
        ["-6", "route", "replace", "unreachable", "default", "table", table],
    ]


def _has_rule(data: Any, slot: int) -> bool:
    if not isinstance(data, list):
        return False
    mark = slot_mark(slot)
    for rule in data:
        if not isinstance(rule, dict):
            continue
        try:
            fw = int(str(rule.get("fwmark", "")).split("/")[0], 0)
        except ValueError:
            continue
        if (fw == mark and str(rule.get("table")) == str(slot_table(slot))
                and rule.get("priority") == slot_priority(slot)):
            return True
    return False


def _rule_exists(slot: int, family: int = 4) -> bool:
    argv = [_ip_bin()] + (["-6"] if family == 6 else []) + ["-j", "rule", "show"]
    return _has_rule(commands.json_cmd(argv), slot)


def table_ok(routes: Any, iface: str, gateway: Optional[str]) -> bool:
    """True when `ip -j route show table T` holds the interface default and the unreachable fallback."""
    if not isinstance(routes, list):
        return False
    have_iface = have_fallback = False
    for r in routes:
        if not isinstance(r, dict) or r.get("dst") != "default":
            continue
        if r.get("type") == "unreachable":
            have_fallback = True
        elif r.get("dev") == iface and (r.get("gateway") or None) == (gateway or None):
            have_iface = True
    return have_iface and have_fallback


def _v6_step(argv: List[str]) -> None:
    """Run an IPv6 command; a host without IPv6 support has nothing to leak, so that is not an error."""
    commands.run_checked(argv, tolerate=_V6_MISSING)


def apply_policy(slot: int, iface: str, gateway: Optional[str]) -> None:
    """Install the fwmark rules (IPv4, plus an IPv6 blackhole) and the slot's routes. Safe to repeat."""
    if not isinstance(iface, str) or not _IFACE_RE.match(iface):
        raise ValidationError(f"unsafe interface name: {iface!r}")
    ip = _ip_bin()
    if not _rule_exists(slot):
        commands.run_checked([ip] + rule_args(slot))
    for argv in route_commands(slot, iface, gateway):
        if argv[0] == "-6":
            _v6_step([ip] + argv)
        else:
            commands.run_checked([ip] + argv)
    try:
        v6_rule = _rule_exists(slot, 6)
    except Exception as exc:  # noqa: BLE001 - IPv6 may be disabled
        log.warning("cannot list IPv6 rules: %s", exc)
        return
    if not v6_rule:
        _v6_step([ip] + rule_args(slot, 6))


def refresh_policy(slot: int, iface: str, gateway: Optional[str]) -> bool:
    """Re-add the slot's routes and rules when the kernel dropped them (link flap). True if it re-applied."""
    ip = _ip_bin()
    try:
        routes = commands.json_cmd([ip, "-j", "route", "show", "table", str(slot_table(slot))])
        healthy = table_ok(routes, iface, gateway) and _rule_exists(slot) and _rule_exists(slot, 6)
    except Exception as exc:  # noqa: BLE001
        log.warning("cannot inspect adoption table %d: %s", slot_table(slot), exc)
        return False
    if healthy:
        return False
    apply_policy(slot, iface, gateway)
    return True


def refresh_adoptions(adoptions: Iterable[Any], interfaces: Any) -> List[int]:
    """Refresh every adoption whose interface is present and bindable. `interfaces` maps name -> object with
    `.bindable` and `.gateways`. Down interfaces stay on the unreachable fallback. Returns refreshed slots."""
    done: List[int] = []
    for a in adoptions:
        interface = interfaces.get(a.iface)
        if interface is None or not getattr(interface, "bindable", False):
            continue
        gateways = getattr(interface, "gateways", None) or []
        try:
            if refresh_policy(a.slot, a.iface, gateways[0] if gateways else None):
                done.append(a.slot)
                log.info("re-added the adoption route for %s", a.iface)
        except Exception as exc:  # noqa: BLE001 - one bad adoption must not stop the rest
            log.warning("cannot refresh adoption %s: %s", a.slot, exc)
    return done


def remove_policy(slot: int) -> None:
    """Remove the slot's ip rules (IPv4 and IPv6) and flush its tables; missing entries are fine."""
    ip = _ip_bin()
    table = str(slot_table(slot))
    commands.run_checked([ip] + rule_args(slot, 4, "del"), tolerate=_IP_MISSING)
    commands.run_checked([ip, "route", "flush", "table", table], tolerate=_IP_MISSING)
    commands.run_checked([ip] + rule_args(slot, 6, "del"), tolerate=_V6_MISSING)
    commands.run_checked([ip, "-6", "route", "flush", "table", table], tolerate=_V6_MISSING)


def leftover_rules(rules: Any) -> List[dict]:
    """Filter `ip -j rule show` output to InterMux adoption rules (priority 2001..2200, fwmark 0x101..0x1c8)."""
    found: List[dict] = []
    if not isinstance(rules, list):
        return found
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        try:
            prio = int(rule.get("priority"))
            fw = int(str(rule.get("fwmark", "")).split("/")[0], 0)
        except (TypeError, ValueError):
            continue
        if (PRIORITY_BASE + SLOT_MIN <= prio <= PRIORITY_BASE + SLOT_MAX
                and MARK_BASE + SLOT_MIN <= fw <= MARK_BASE + SLOT_MAX):
            found.append(rule)
    return found


def _sweep_v6() -> None:
    """Delete leftover IPv6 adoption rules and flush their tables; listed separately so they go even when
    the IPv4 rule is already gone. Never raises."""
    ip = _ip_bin()
    try:
        rules = leftover_rules(commands.json_cmd([ip, "-6", "-j", "rule", "show"]))
    except Exception as exc:  # noqa: BLE001 - IPv6 may be disabled
        log.debug("cannot list IPv6 rules: %s", exc)
        return
    for rule in rules:
        try:
            fw = int(str(rule["fwmark"]).split("/")[0], 0)
            prio = int(rule["priority"])
            table = int(str(rule.get("table")))
            _v6_step([ip, "-6", "rule", "del", "fwmark", hex(fw), "lookup", str(table), "priority", str(prio)])
            if TABLE_BASE + SLOT_MIN <= table <= TABLE_BASE + SLOT_MAX:
                _v6_step([ip, "-6", "route", "flush", "table", str(table)])
        except Exception as exc:  # noqa: BLE001
            log.warning("cannot delete leftover IPv6 rule %r: %s", rule, exc)


def sweep_leftovers() -> None:
    """Remove adoption ip rules, their route tables and empty cgroups that survive without state. Never raises."""
    ip = _ip_bin()
    tables: Set[int] = set()
    try:
        rules = leftover_rules(commands.json_cmd([ip, "-j", "rule", "show"]))
    except Exception as exc:  # noqa: BLE001 - best effort
        log.warning("cannot list ip rules: %s", exc)
        rules = []
    for rule in rules:
        try:
            fw = int(str(rule["fwmark"]).split("/")[0], 0)
            prio = int(rule["priority"])
            table = int(str(rule.get("table")))
            commands.run_checked([ip, "rule", "del", "fwmark", hex(fw), "lookup", str(table),
                                  "priority", str(prio)], tolerate=_IP_MISSING)
            if TABLE_BASE + SLOT_MIN <= table <= TABLE_BASE + SLOT_MAX:
                tables.add(table)
        except Exception as exc:  # noqa: BLE001
            log.warning("cannot delete leftover rule %r: %s", rule, exc)
    # Flush only tables no remaining rule refers to.
    try:
        still_used = {str(r.get("table")) for r in commands.json_cmd([ip, "-j", "rule", "show"])
                      if isinstance(r, dict)}
    except Exception as exc:  # noqa: BLE001
        log.warning("cannot re-list ip rules: %s", exc)
        still_used = set()
    for table in sorted(tables):
        if str(table) in still_used:
            continue
        try:
            commands.run_checked([ip, "route", "flush", "table", str(table)], tolerate=_IP_MISSING)
        except Exception as exc:  # noqa: BLE001
            log.warning("cannot flush table %d: %s", table, exc)
    _sweep_v6()
    try:
        for d in sorted((Path(CGROUP_ROOT) / GROUP_PARENT).glob("adopt-*")):
            try:
                d.rmdir()
            except OSError as exc:
                log.debug("cannot remove %s: %s", d, exc)
    except OSError as exc:
        log.debug("cannot scan cgroups: %s", exc)
