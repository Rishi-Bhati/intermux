"""Network namespace and veth plumbing for per-application isolation (privileged side)."""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import re
import shutil
import socket
import struct
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from intermux import config
from intermux.errors import SystemCommandError, ValidationError
from intermux.system import procs
from intermux.system.commands import CmdResult, json_cmd, run, run_checked

log = logging.getLogger("intermux.system.netns")

_VETH_NAME_RE = r"veth[01]_[0-9a-f]{8}"
RESOLV_OPTIONS = "timeout:1 attempts:2"
_NS_HANDLE_MISSING = "Cannot open network namespace"
_LINK_MISSING = ("Cannot find device", "does not exist")


# ---------------------------------------------------------------------------
# Pure helpers (no system access)
# ---------------------------------------------------------------------------

def _hash8(iface: str) -> str:
    # md5 is used only to derive stable short names, not for security.
    return hashlib.md5(iface.encode(), usedforsecurity=False).hexdigest()[:8]


def namespace_name(iface: str) -> str:
    """Stable namespace name for an interface: ns_ plus 8 hex characters."""
    return "ns_" + _hash8(iface)


def veth_names(iface: str) -> Tuple[str, str]:
    """Host-side and namespace-side veth names sharing the same 8-hex suffix."""
    h = _hash8(iface)
    return "veth0_" + h, "veth1_" + h


def parse_netns_list(output: str) -> List[str]:
    """Namespace names from `ip netns list` output (first token of each line)."""
    names: List[str] = []
    for line in output.splitlines():
        parts = line.split()
        if parts:
            names.append(parts[0])
    return names


def parse_link_names(entries: Iterable[object]) -> List[str]:
    """Interface names from `ip -j link show` entries, with any '@peer' suffix removed."""
    names: List[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("ifname")
        if isinstance(name, str) and name:
            names.append(name.split("@", 1)[0])
    return names


def _filter(names: Iterable[str], pattern: str) -> List[str]:
    rx = re.compile(pattern)
    return [n for n in names if rx.search(n)]


def _check_namespace(name: str) -> None:
    if not isinstance(name, str) or not re.fullmatch(config.NS_NAME_RE, name):
        raise ValidationError("invalid namespace name: %r" % (name,))


def _check_veth(name: str) -> None:
    if not isinstance(name, str) or not re.fullmatch(_VETH_NAME_RE, name):
        raise ValidationError("invalid veth name: %r" % (name,))


def _check_address(value: str) -> str:
    try:
        return str(ipaddress.ip_address(value))
    except (TypeError, ValueError):
        raise ValidationError("not an IP address: %r" % (value,)) from None


def _check_interface(value: str) -> str:
    try:
        return str(ipaddress.ip_interface(value))
    except (TypeError, ValueError):
        raise ValidationError("not an address with prefix: %r" % (value,)) from None


# ---------------------------------------------------------------------------
# Command helpers
# ---------------------------------------------------------------------------

def _checked(argv: Sequence[str], tolerate: Sequence[str] = ()) -> CmdResult:
    """run_checked that also accepts failures whose stderr contains a tolerated substring."""
    try:
        return run_checked(list(argv), tolerate=tolerate)
    except SystemCommandError as exc:
        if exc.result is not None and any(t in exc.result.stderr for t in tolerate):
            return exc.result
        raise


def _in_ns(namespace: str, *argv: str) -> List[str]:
    return ["ip", "netns", "exec", namespace, "ip", *argv]


def _link_exists(name: str) -> bool:
    return run(["ip", "link", "show", "dev", name], tolerate=_LINK_MISSING).ok


def _link_exists_in_ns(namespace: str, name: str) -> bool:
    argv = _in_ns(namespace, "link", "show", "dev", name)
    return run(argv, tolerate=_LINK_MISSING + (_NS_HANDLE_MISSING,)).ok


# ---------------------------------------------------------------------------
# Namespaces
# ---------------------------------------------------------------------------

def namespace_exists(name: str) -> bool:
    """True when the namespace handle under NETNS_DIR exists."""
    return procs.netns_inode(name) is not None


def ensure_namespace(name: str) -> None:
    """Create the network namespace if it does not exist. Idempotent."""
    _check_namespace(name)
    if namespace_exists(name):
        return
    _checked(["ip", "netns", "add", name], tolerate=("File exists",))
    log.debug("created namespace %s", name)


def delete_namespace(name: str) -> None:
    """Delete the namespace and its /etc/netns directory. Missing namespaces are tolerated."""
    _check_namespace(name)
    _checked(["ip", "netns", "del", name], tolerate=("No such file",))
    etc_dir = config.ETC_NETNS_DIR / name
    if etc_dir.is_dir() and not etc_dir.is_symlink():
        try:
            shutil.rmtree(etc_dir)
        except OSError as exc:
            log.warning("could not remove %s: %s", etc_dir, exc)


def list_namespaces_matching(pattern: str = config.NS_NAME_RE) -> List[str]:
    """Names from `ip netns list` that match the regex pattern. Empty on failure."""
    result = run(["ip", "netns", "list"], tolerate=("command not found",))
    if not result.ok:
        return []
    return _filter(parse_netns_list(result.stdout), pattern)


def write_resolv_conf(namespace: str, servers: Sequence[str]) -> None:
    """Write /etc/netns/<namespace>/resolv.conf with the given nameserver addresses."""
    _check_namespace(namespace)
    addrs: List[str] = []
    for server in servers:
        addr = _check_address(server)
        if addr not in addrs:
            addrs.append(addr)
    if not addrs:
        raise ValidationError("no nameservers given for namespace %s" % namespace)

    directory = config.ETC_NETNS_DIR / namespace
    directory.mkdir(mode=0o755, parents=True, exist_ok=True)
    os.chmod(directory, 0o755)

    # timeout:1 attempts:2: a resolver that silently drops queries costs about 1s before the next one is
    # tried, instead of glibc's default 5s per query.
    body = ("# Generated by InterMux\n" + "".join("nameserver %s\n" % a for a in addrs)
            + "options %s\n" % RESOLV_OPTIONS)
    # Written in place (not renamed) so an existing bind mount inside the namespace keeps seeing the file.
    path = directory / "resolv.conf"
    path.write_text(body)
    os.chmod(path, 0o644)


# ---------------------------------------------------------------------------
# Veth pairs
# ---------------------------------------------------------------------------

def ensure_veth(namespace: str, host_veth: str, ns_veth: str, host_cidr: str,
                ns_cidr: str, gateway: str) -> None:
    """Create and configure a veth pair with the namespace end inside `namespace`. Idempotent."""
    _check_namespace(namespace)
    _check_veth(host_veth)
    _check_veth(ns_veth)
    host_cidr = _check_interface(host_cidr)
    ns_cidr = _check_interface(ns_cidr)
    gateway = _check_address(gateway)

    ensure_namespace(namespace)

    if not _link_exists(host_veth):
        _checked(["ip", "link", "add", host_veth, "type", "veth", "peer", "name", ns_veth],
                 tolerate=("File exists",))
    if not _link_exists_in_ns(namespace, ns_veth):
        _checked(["ip", "link", "set", ns_veth, "netns", namespace], tolerate=("File exists",))

    _checked(["ip", "addr", "replace", host_cidr, "dev", host_veth])
    _checked(["ip", "link", "set", host_veth, "up"])

    _checked(_in_ns(namespace, "addr", "replace", ns_cidr, "dev", ns_veth))
    _checked(_in_ns(namespace, "link", "set", ns_veth, "up"))
    _checked(_in_ns(namespace, "link", "set", "lo", "up"))
    _checked(_in_ns(namespace, "route", "replace", "default", "via", gateway, "dev", ns_veth))


def delete_veth(host_veth: str) -> None:
    """Delete a veth pair by its host end (the peer is removed with it). Missing links are tolerated."""
    _check_veth(host_veth)
    _checked(["ip", "link", "del", host_veth], tolerate=_LINK_MISSING)


def list_veths_matching(pattern: str = config.VETH_HOST_RE) -> List[str]:
    """Host-side veth names from `ip -j link show` that match the regex pattern. Empty on failure."""
    data = json_cmd(["ip", "-j", "link", "show"])
    if not isinstance(data, list):
        return []
    return _filter(parse_link_names(data), pattern)


# ---------------------------------------------------------------------------
# DNS redirect inside the namespace
# ---------------------------------------------------------------------------
#
# `ip netns exec` shows /etc/netns/<ns>/resolv.conf to the app by bind-mounting it over /etc/resolv.conf in
# the app's private mount namespace. When the host later replaces its own /etc/resolv.conf by rename (as
# tailscaled, NetworkManager, resolvconf and systemd-resolved do, for example when a veth link appears), the
# kernel detaches every mount sitting on the replaced file. Long-running apps then read the HOST resolvers
# (100.100.100.100, 127.0.0.53, an IPv6 server...), which are unreachable from the namespace, and DNS fails.
# A freshly started `ip netns exec` gets a new bind mount, so short root checks keep working.
#
# The rules below live in the namespace's own netfilter (removed with the namespace) and make the content of
# /etc/resolv.conf irrelevant: IPv4 DNS sent anywhere other than the namespace's own resolvers is rewritten
# to the first of them. Queries to loopback stubs (127.0.0.53) need route_localnet and a source rewrite so
# they can leave through the veth.

_PROBE_QUERY_ID = 0x494D


def dns_probe_query() -> bytes:
    """A recursive query for the root NS set: any working recursive resolver answers it."""
    return struct.pack("!HHHHHH", _PROBE_QUERY_ID, 0x0100, 1, 0, 0, 0) + b"\x00" + struct.pack("!HH", 2, 1)


def dns_probe_ok(reply: bytes) -> bool:
    """True for a reply to dns_probe_query with rcode NOERROR or NXDOMAIN (REFUSED/SERVFAIL are not usable)."""
    if len(reply) < 12:
        return False
    qid, flags = struct.unpack("!HH", reply[:4])
    return qid == _PROBE_QUERY_ID and bool(flags & 0x8000) and (flags & 0xF) in (0, 3)


def _netns_socket(namespace: str) -> socket.socket:
    """A UDP socket created inside `namespace` (setns in a helper thread; the socket keeps its namespace)."""
    box: Dict[str, Any] = {}

    def worker() -> None:
        try:
            own = os.open("/proc/thread-self/ns/net", os.O_RDONLY)
            target = os.open(str(config.NETNS_DIR / namespace), os.O_RDONLY)
            try:
                os.setns(target, os.CLONE_NEWNET)
                try:
                    box["sock"] = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                finally:
                    os.setns(own, os.CLONE_NEWNET)
            finally:
                os.close(own)
                os.close(target)
        except (OSError, AttributeError) as exc:
            box["error"] = exc

    thread = threading.Thread(target=worker, name="intermux-dns-probe")
    thread.start()
    thread.join()
    if "sock" not in box:
        raise SystemCommandError(f"cannot open a socket in {namespace}: {box.get('error')}")
    return box["sock"]


def probe_dns(namespace: str, servers: Sequence[str], timeout: float = 1.5) -> List[str]:
    """IPv4 servers (in the given order) that answered a DNS query sent from inside the namespace."""
    _check_namespace(namespace)
    addrs = [a for a in (_check_address(s) for s in servers) if ":" not in a]
    if not addrs:
        return []
    answered: set = set()
    sock = _netns_socket(namespace)
    try:
        sock.settimeout(0.1)
        query = dns_probe_query()
        for addr in addrs:
            try:
                sock.sendto(query, (addr, 53))
            except OSError:
                pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and len(answered) < len(addrs):
            try:
                reply, peer = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if peer[0] in addrs and dns_probe_ok(reply):
                answered.add(peer[0])
    finally:
        sock.close()
    return [a for a in addrs if a in answered]


def dns_redirect_restore(servers: Sequence[str], ns_veth: str, ns_addr: str) -> str:
    """iptables-restore input for the namespace nat table that sends stray IPv4 DNS to servers[0]."""
    _check_veth(ns_veth)
    ns_addr = _check_address(ns_addr)
    addrs: List[str] = []
    for server in servers:
        addr = _check_address(server)
        if ":" not in addr and addr not in addrs:
            addrs.append(addr)
    if not addrs:
        raise ValidationError("no IPv4 nameserver to redirect DNS to")
    lines = ["*nat", ":PREROUTING ACCEPT [0:0]", ":INPUT ACCEPT [0:0]", ":OUTPUT ACCEPT [0:0]",
             ":POSTROUTING ACCEPT [0:0]"]
    for proto in ("udp", "tcp"):
        for addr in addrs:
            lines.append(f"-A OUTPUT -d {addr}/32 -p {proto} -m {proto} --dport 53 -j RETURN")
        lines.append(f"-A OUTPUT -p {proto} -m {proto} --dport 53 -j DNAT --to-destination {addrs[0]}")
    lines.append(f"-A POSTROUTING -s 127.0.0.0/8 -o {ns_veth} -j SNAT --to-source {ns_addr}")
    lines.append("COMMIT")
    return "\n".join(lines) + "\n"


def ensure_dns_redirect(namespace: str, servers: Sequence[str], ns_veth: str, ns_addr: str,
                        iptables: str) -> None:
    """Load the DNS redirect into the namespace's nat table (replacing it atomically). Idempotent."""
    _check_namespace(namespace)
    body = dns_redirect_restore(servers, ns_veth, ns_addr)
    restore = iptables + "-restore"
    if not os.path.exists(restore):
        restore = shutil.which(os.path.basename(restore)) or restore
    sysctl = shutil.which("sysctl") or "/usr/sbin/sysctl"
    _checked(["ip", "netns", "exec", namespace, sysctl, "-q", "-w",
              f"net.ipv4.conf.{ns_veth}.route_localnet=1"])
    result = run(["ip", "netns", "exec", namespace, restore], input_text=body)
    if not result.ok:
        raise SystemCommandError(f"{restore} in {namespace} failed: {result.stderr.strip()}", result=result)
