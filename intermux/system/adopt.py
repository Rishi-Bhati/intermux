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
    return commands.which("ip") or "/usr/sbin/ip"


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


def _rule_exists(slot: int) -> bool:
    data = commands.json_cmd([_ip_bin(), "-j", "rule", "show"])
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


def apply_policy(slot: int, iface: str, gateway: Optional[str]) -> None:
    """Add the fwmark rule and the default route in the slot's table."""
    if not isinstance(iface, str) or not _IFACE_RE.match(iface):
        raise ValidationError(f"unsafe interface name: {iface!r}")
    ip = _ip_bin()
    if not _rule_exists(slot):
        commands.run_checked([ip, "rule", "add", "fwmark", hex(slot_mark(slot)), "lookup",
                              str(slot_table(slot)), "priority", str(slot_priority(slot))])
    route = [ip, "route", "replace", "default"]
    if gateway:
        route += ["via", gateway]
    route += ["dev", iface, "table", str(slot_table(slot))]
    commands.run_checked(route)


def remove_policy(slot: int) -> None:
    """Remove the slot's ip rule and flush its route table; missing entries are fine."""
    ip = _ip_bin()
    commands.run_checked([ip, "rule", "del", "fwmark", hex(slot_mark(slot)), "lookup",
                          str(slot_table(slot)), "priority", str(slot_priority(slot))],
                         tolerate=_IP_MISSING)
    commands.run_checked([ip, "route", "flush", "table", str(slot_table(slot))],
                         tolerate=_IP_MISSING)


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
    try:
        for d in sorted((Path(CGROUP_ROOT) / GROUP_PARENT).glob("adopt-*")):
            try:
                d.rmdir()
            except OSError as exc:
                log.debug("cannot remove %s: %s", d, exc)
    except OSError as exc:
        log.debug("cannot scan cgroups: %s", exc)
