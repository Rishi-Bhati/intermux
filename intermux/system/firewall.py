"""NAT, forwarding rules and ip_forward sysctl for namespace subnets."""
from __future__ import annotations

import logging
import shlex
from ipaddress import IPv4Network
from pathlib import Path
from typing import List, NamedTuple, Optional, Tuple

from intermux.errors import MissingToolError, ValidationError
from intermux.subnets import POOL
from intermux.system.commands import run, run_checked, which

log = logging.getLogger("intermux.firewall")

IP_FORWARD_PATH = Path("/proc/sys/net/ipv4/ip_forward")
_IPTABLES_CANDIDATES = ("iptables", "iptables-nft", "iptables-legacy")
_NAT_CHAINS = frozenset({"PREROUTING", "POSTROUTING"})
_MISSING_RULE = ("does a matching rule exist", "Bad rule")
_SELECTOR_FLAGS = ("-s", "-d")


class NatRule(NamedTuple):
    table: str              # "nat" or "filter"
    chain: str
    spec: Tuple[str, ...]   # match and target arguments
    insert: bool            # True: -I (top of chain); False: -A (append)


def nat_rules(subnet: IPv4Network, iface: str) -> List[NatRule]:
    """The three rules intermux needs for a namespace subnet, in apply order."""
    s = str(subnet)
    return [
        NatRule("nat", "POSTROUTING", ("-s", s, "-o", iface, "-j", "MASQUERADE"), insert=False),
        NatRule("filter", "FORWARD", ("-s", s, "-j", "ACCEPT"), insert=True),
        NatRule("filter", "FORWARD",
                ("-d", s, "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"),
                insert=True),
    ]


def _table_args(table: str) -> List[str]:
    return [] if table == "filter" else ["-t", table]


def rule_args(rule: NatRule, op: str) -> List[str]:
    """Arguments after the iptables binary that apply `op` (-C, -A, -I or -D) to `rule`."""
    return [*_table_args(rule.table), op, rule.chain, *rule.spec]


def is_pool_subnet(value: str) -> bool:
    """True when value is a /24 inside POOL whose third octet is 1..254 (a namespace subnet)."""
    try:
        net = IPv4Network(value, strict=True)
    except ValueError:
        return False
    return net.prefixlen == 24 and net.subnet_of(POOL) and 1 <= net.network_address.packed[2] <= 254


def _selects_pool(tokens: List[str]) -> bool:
    """True when a non-negated -s or -d match in an iptables rule is an intermux subnet."""
    for index, token in enumerate(tokens[:-1]):
        if token not in _SELECTOR_FLAGS or not is_pool_subnet(tokens[index + 1]):
            continue
        if index > 0 and tokens[index - 1] == "!":
            continue
        return True
    return False


def filter_intermux_lines(text: str) -> List[str]:
    """Append rules (`-A ...`) from `iptables -S` output whose -s or -d is an intermux subnet."""
    lines: List[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            tokens = shlex.split(line)
        except ValueError:
            continue
        if len(tokens) >= 2 and tokens[0] == "-A" and _selects_pool(tokens):
            lines.append(line)
    return lines


def delete_rule_args(line: str) -> Optional[List[str]]:
    """Turn a `-A CHAIN ...` line from `iptables -S` into the arguments of the matching -D; None if not a rule."""
    try:
        tokens = shlex.split(line)
    except ValueError:
        return None
    if len(tokens) < 2 or tokens[0] != "-A":
        return None
    table = "nat" if tokens[1] in _NAT_CHAINS else "filter"
    return [*_table_args(table), "-D", *tokens[1:]]


def iptables_cmd() -> str:
    """Path of the first available iptables variant; MissingToolError if none is installed."""
    for name in _IPTABLES_CANDIDATES:
        path = which(name)
        if path:
            return path
    raise MissingToolError("iptables not found; install the iptables package", code="missing_tool")


def _iptables_or_none() -> Optional[str]:
    try:
        return iptables_cmd()
    except MissingToolError:
        log.debug("iptables not installed; no intermux rules can exist")
        return None


def ensure_nat(subnet: IPv4Network, iface: str) -> None:
    """Add the MASQUERADE and FORWARD rules for `subnet` unless they already exist."""
    ipt = iptables_cmd()
    for rule in nat_rules(subnet, iface):
        if run([ipt, *rule_args(rule, "-C")], tolerate=_MISSING_RULE).ok:
            continue
        run_checked([ipt, *rule_args(rule, "-I" if rule.insert else "-A")])


def remove_nat(subnet: IPv4Network, iface: str) -> None:
    """Delete the rules added by ensure_nat. Missing rules are not errors."""
    ipt = _iptables_or_none()
    if ipt is None:
        return
    for rule in nat_rules(subnet, iface):
        run([ipt, *rule_args(rule, "-D")], tolerate=_MISSING_RULE)


def list_intermux_rules() -> List[str]:
    """Full iptables rule lines in nat POSTROUTING and filter FORWARD that mention the intermux pool."""
    ipt = _iptables_or_none()
    if ipt is None:
        return []
    lines: List[str] = []
    for args in (["-t", "nat", "-S", "POSTROUTING"], ["-S", "FORWARD"]):
        result = run([ipt, *args])
        if result.ok:
            lines.extend(filter_intermux_lines(result.stdout))
    return lines


def delete_rule_line(line: str) -> None:
    """Delete the rule described by one `iptables -S` line; unparseable lines are logged and skipped."""
    args = delete_rule_args(line)
    if args is None:
        log.warning("cannot derive a delete command from %r", line)
        return
    run([iptables_cmd(), *args], tolerate=_MISSING_RULE)


def get_ip_forward() -> str:
    """Current value of net.ipv4.ip_forward ("0" or "1")."""
    return IP_FORWARD_PATH.read_text().strip()


def set_ip_forward(value: str) -> None:
    """Write net.ipv4.ip_forward; value must be "0" or "1"."""
    if value not in ("0", "1"):
        raise ValidationError(f"ip_forward must be '0' or '1', got {value!r}")
    IP_FORWARD_PATH.write_text(value)


def enable_forwarding() -> str:
    """Turn on IPv4 forwarding and return the previous value."""
    previous = get_ip_forward()
    set_ip_forward("1")
    return previous
