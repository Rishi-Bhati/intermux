"""NAT, forwarding rules and ip_forward sysctl for namespace subnets."""
from __future__ import annotations

import logging
import re
import shlex
from ipaddress import IPv4Network
from pathlib import Path
from typing import List, NamedTuple, Optional, Tuple

from intermux.config import VETH_HOST_RE
from intermux.errors import MissingToolError, ValidationError
from intermux.subnets import POOL
from intermux.system.commands import run, run_checked, which

log = logging.getLogger("intermux.firewall")

IP_FORWARD_PATH = Path("/proc/sys/net/ipv4/ip_forward")
_IPTABLES_CANDIDATES = ("iptables", "iptables-nft", "iptables-legacy")
_NAT_CHAINS = frozenset({"PREROUTING", "POSTROUTING"})
_MISSING_RULE = ("does a matching rule exist", "Bad rule")
_SELECTOR_FLAGS = ("-s", "-d")
TRUSTED_ZONE = "trusted"
_FIREWALLD_MISSING = ("UNKNOWN_INTERFACE", "ZONE_CONFLICT", "NOT_ENABLED", "not running")


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


def firewalld_cmd() -> Optional[str]:
    """Path of firewall-cmd when firewalld is installed and running; None otherwise."""
    path = which("firewall-cmd")
    if path is None:
        return None
    return path if run([path, "--state"], timeout=15, tolerate=("not running",)).ok else None


def _check_host_veth(name: str) -> None:
    if not isinstance(name, str) or not re.fullmatch(VETH_HOST_RE, name):
        raise ValidationError(f"invalid veth name: {name!r}")


def trust_veth(host_veth: str) -> bool:
    """Put the host end of a namespace veth in firewalld's trusted zone (runtime only).

    firewalld filters forwarding in its own nftables table, where the iptables FORWARD ACCEPT rules above have
    no effect. A veth in no zone lands in the default zone, which forwards only to interfaces of that same
    zone; traffic to any other uplink (a wg-quick tunnel, a tether in another zone) hits firewalld's final
    reject and the app has no network. False when firewalld refused; True when it accepted or is not running."""
    _check_host_veth(host_veth)
    fwcmd = firewalld_cmd()
    if fwcmd is None:
        return True
    return run([fwcmd, f"--zone={TRUSTED_ZONE}", f"--change-interface={host_veth}"]).ok


def untrust_veth(host_veth: str) -> None:
    """Undo trust_veth. An interface firewalld does not know is not an error."""
    _check_host_veth(host_veth)
    fwcmd = firewalld_cmd()
    if fwcmd is None:
        return
    run([fwcmd, f"--zone={TRUSTED_ZONE}", f"--remove-interface={host_veth}"], tolerate=_FIREWALLD_MISSING)


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
