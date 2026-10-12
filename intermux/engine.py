"""Launch, stop and inspect app bindings. Callers hold state.state_lock()."""

from __future__ import annotations

import fcntl
import ipaddress
import logging
import os
import secrets
import stat
import subprocess
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from intermux import apps, config, state, subnets
from intermux.apps import firefox
from intermux.errors import (ConflictError, InterMuxError, NotFoundError, PermissionDeniedError,
                             ValidationError)
from intermux.system import adopt as adopt_sys
from intermux.system import commands, firewall, netinfo, netns, procs, routing
from intermux.system.session import UserInfo

log = logging.getLogger("intermux.engine")

IP = commands.which("ip") or "/usr/sbin/ip"
RUNUSER = commands.which("runuser") or "/usr/sbin/runuser"
CURL = commands.which("curl") or "/usr/bin/curl"

GRACE = 5.0
PUBLIC_DNS = ("1.1.1.1", "9.9.9.9")
TAILSCALE_TABLE = "52"


def namespace_dns_servers(interface: netinfo.Interface) -> List[str]:
    """DNS servers for a namespace: the bound interface's own gateway first (its router or phone answers
    DNS on that link), then public resolvers. Host resolvers such as Tailscale's 100.100.100.100 are only
    routed through their own interface, so a namespace bound to another interface cannot reach them."""
    gateways = [g for g in interface.gateways if ":" not in g]
    return list(dict.fromkeys(gateways + list(PUBLIC_DNS)))


def order_dns_servers(servers: Sequence[str], answering: Sequence[str]) -> List[str]:
    """Servers that answered the probe first (keeping their order), then the rest; unchanged if none answered."""
    return [s for s in servers if s in answering] + [s for s in servers if s not in answering]


def configure_namespace_dns(ns: str, ns_veth: str, ns_addr: str, servers: List[str]) -> List[str]:
    """Write the namespace resolv.conf (working resolvers first) and redirect stray DNS to the best one.

    Needs the veth, NAT and policy route in place: the probe is sent from inside the namespace, so it only
    counts resolvers reachable through the bound interface. Returns user-facing warnings."""
    warnings: List[str] = []
    try:
        answering = netns.probe_dns(ns, servers)
    except InterMuxError as exc:
        log.warning("DNS probe in %s failed: %s", ns, exc)
        answering = []
    if not answering:
        warnings.append("no DNS server answered through this interface yet; name lookups may fail")
    servers = order_dns_servers(servers, answering)
    netns.write_resolv_conf(ns, servers)
    try:
        netns.ensure_dns_redirect(ns, servers, ns_veth, ns_addr, firewall.iptables_cmd())
    except InterMuxError as exc:
        log.warning("DNS redirect in %s failed: %s", ns, exc)
        warnings.append("DNS may stop working if the host rewrites /etc/resolv.conf")
    return warnings


_RUNNING_POLICIES = ("ask", "close", "separate")


# Popen objects of launched apps, keyed by pid. They are polled (reaped) on the next bind or status call
# so that exited apps do not stay zombies of the helper process.
_launched: Dict[int, "subprocess.Popen[bytes]"] = {}


def _reap_launched() -> None:
    """Best-effort poll of previously launched apps so finished ones are reaped."""
    for pid, proc in list(_launched.items()):
        if proc.poll() is not None:
            del _launched[pid]


def tmpdir_path(namespace: str, uid: int) -> str:
    """Per-namespace, per-user TMPDIR path."""
    return f"/tmp/intermux-{namespace}-{uid}"


def build_launch_argv(namespace: str, user: UserInfo, env: Dict[str, str], app_path: str,
                      extra_args: Sequence[str], ip_bin: str = "ip", runuser_bin: str = "runuser") -> List[str]:
    """Build the argv that runs app_path as the user inside the namespace with a clean environment."""
    return [
        ip_bin, "netns", "exec", namespace, runuser_bin, "-u", user.username, "--",
        "env", "-i", *[f"{k}={v}" for k, v in env.items()],
        f"HOME={user.home}", f"USER={user.username}", f"LOGNAME={user.username}",
        f"PATH={config.APP_PATH_ENV}", f"TMPDIR={tmpdir_path(namespace, user.uid)}",
        app_path, *extra_args,
    ]


def bind(app_path: str, iface: str, *, user: UserInfo, env: Dict[str, str],
         running_policy: str = "ask", allow_unsupported: bool = False) -> Dict[str, Any]:
    """Set up the namespace plumbing for iface and launch app_path inside it; records the binding in state."""
    if running_policy not in _RUNNING_POLICIES:
        raise ValidationError(f"unknown running policy: {running_policy}", code="invalid_request")
    _reap_launched()
    if not os.path.isabs(user.home) or user.home == "/" or not os.path.isdir(user.home):
        raise ValidationError(f"the home directory {user.home} of user {user.username} does not exist; "
                              "InterMux needs it to write the application log", code="no_home")

    interface = _find_interface(iface)
    if not interface.bindable:
        raise ConflictError(f"interface {iface} cannot be bound (needs an up IPv4 address and a default route)",
                            code="not_bindable")

    handler = apps.get_handler(app_path)
    if not handler.supported and not allow_unsupported:
        raise ValidationError(handler.unsupported_reason, code="unsupported_app")

    profile_mode = "default"
    running = procs.find_app_procs(app_path, handler.comm_names(app_path), uid=user.uid)
    if running:
        pids = [p.pid for p in running]
        if running_policy == "ask":
            raise ConflictError("application is already running", code="already_running", data={"pids": pids})
        if running_policy == "close":
            survivors = procs.terminate(procs.descendants(pids, uid=user.uid), grace=GRACE)
            if survivors:
                raise ConflictError("application did not exit after close request",
                                    code="still_running", data={"pids": survivors})
        else:
            profile_mode = "separate"

    st = state.load_state(config.STATE_FILE)
    if iface in st.subnets:
        subnet = ipaddress.IPv4Network(st.subnets[iface])
    else:
        subnet = subnets.pick_subnet(_used_networks(st), preferred=None)
    st.subnets[iface] = str(subnet)

    ns = netns.namespace_name(iface)
    host_v, ns_v = netns.veth_names(iface)
    gateway = interface.gateways[0] if interface.gateways else None
    host_addr = subnets.host_ip(subnet)
    netns.ensure_namespace(ns)
    netns.ensure_veth(ns, host_v, ns_v, f"{host_addr}/{subnet.prefixlen}",
                      f"{subnets.ns_ip(subnet)}/{subnet.prefixlen}", str(host_addr))
    # Forwarding is switched on at every bind (it may have been reset since the last one, e.g. by a reboot).
    # Only the first value seen is recorded; it is never written back (Docker, libvirt or Tailscale may rely on it).
    previous = firewall.enable_forwarding()
    if st.ip_forward_prev is None:
        st.ip_forward_prev = previous
    firewall.ensure_nat(subnet, iface)
    index = subnets.index_of(subnet)
    table = config.TABLE_BASE + index
    priority = config.PRIORITY_BASE + index
    routing.ensure_policy(subnet, iface, table, priority, gateway, host_v)
    warnings = configure_namespace_dns(ns, ns_v, str(subnets.ns_ip(subnet)), namespace_dns_servers(interface))
    # Persist the subnet and forwarding state now so a later failure can still be torn down.
    state.save_state(st, config.STATE_FILE)

    if handler.name == "firefox" and profile_mode == "default" and firefox.default_profile_dir(user.home) is None:
        warnings.append("Firefox profile: default profile not found; a new profile will be used")

    ctx = apps.LaunchContext(user=user, iface=iface, profile_mode=profile_mode)
    handler.prepare(ctx, app_path)
    argv = build_launch_argv(ns, user, env, app_path, handler.extra_args(ctx), ip_bin=IP, runuser_bin=RUNUSER)

    app_name = os.path.basename(app_path)
    _ensure_tmpdir(user, ns)
    try:
        log_fd, log_path = _open_log(user, f"{app_name}-{iface}.log")
    except OSError as exc:
        raise ConflictError(f"cannot write the application log under {user.home}: {exc.strerror or exc}",
                            code="log_unwritable") from exc
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log_fd, stderr=subprocess.STDOUT,
                                start_new_session=True, close_fds=True)
    finally:
        os.close(log_fd)
    _launched[proc.pid] = proc
    time.sleep(1.5)
    if proc.poll() is not None:
        _launched.pop(proc.pid, None)
        raise ConflictError("application exited immediately; see log", code="launch_failed",
                            data={"log_path": log_path})

    binding = state.Binding(
        iface=iface, app=app_path, app_name=app_name, namespace=ns, subnet=str(subnet),
        table=table, priority=priority, profile_mode=profile_mode, log_path=log_path,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    st.bindings = [b for b in st.bindings if not (b.iface == iface and b.app == app_path)]
    st.bindings.append(binding)
    state.save_state(st, config.STATE_FILE)
    return {"binding": asdict(binding), "pid": proc.pid, "log_path": log_path, "warnings": warnings}


def stop_app(iface: str, app: str, *, uid: Optional[int] = None) -> Dict[str, Any]:
    """Terminate the app's processes owned by uid inside the binding's namespace; the binding itself is kept."""
    if uid is None:
        raise ValidationError("uid is required")
    st = state.load_state(config.STATE_FILE)
    binding = st.find_binding(iface, app)
    if binding is None:
        raise NotFoundError(f"no binding for {app} on {iface}")
    pids = _app_pids_in_namespace(binding.namespace, binding.app, uid)
    if not pids:
        return {"stopped": [], "still_running": []}
    targets = procs.descendants(pids, uid=uid)
    survivors = procs.terminate(targets, grace=GRACE)
    return {"stopped": [p for p in targets if p not in survivors], "still_running": survivors}


def kill_app(app: str, force: bool = False, *, uid: Optional[int] = None) -> Dict[str, Any]:
    """Terminate every host instance of app owned by uid (and its children), optionally with SIGKILL."""
    if uid is None:
        raise ValidationError("uid is required")
    found = procs.find_app_procs(app, apps.get_handler(app).comm_names(app), uid=uid)
    if not found:
        return {"stopped": [], "still_running": []}
    targets = procs.descendants([p.pid for p in found], uid=uid)
    survivors = procs.terminate(targets, grace=GRACE, force=force)
    return {"stopped": [p for p in targets if p not in survivors], "still_running": survivors}


def verify(iface: str) -> Dict[str, Any]:
    """Report the public IP seen from the interface's namespace; network failures are returned, not raised."""
    ns = netns.namespace_name(iface)
    if not netns.namespace_exists(ns):
        raise NotFoundError(f"namespace for {iface} is not up")
    res = commands.run([IP, "netns", "exec", ns, CURL, "-4", "-s", "--max-time", "8", config.PUBLIC_IP_URL])
    if not res.ok:
        error = res.stderr.strip() or f"curl exited with status {res.returncode}"
        return {"iface": iface, "public_ip": None, "error": error}
    try:
        public_ip = str(ipaddress.ip_address(res.stdout.strip()))
    except ValueError:
        return {"iface": iface, "public_ip": None, "error": "public IP service returned an invalid address"}
    return {"iface": iface, "public_ip": public_ip, "error": None}


def status() -> List[Dict[str, Any]]:
    """Describe every binding in state with its running pids, namespace and NAT state."""
    _reap_launched()
    st = state.load_state(config.STATE_FILE)
    nat_lines = firewall.list_intermux_rules()
    result: List[Dict[str, Any]] = []
    for b in st.bindings:
        result.append({
            "iface": b.iface,
            "app": b.app,
            "app_name": b.app_name,
            "namespace": b.namespace,
            "subnet": b.subnet,
            "profile_mode": b.profile_mode,
            "log_path": b.log_path,
            "running_pids": _app_pids_in_namespace(b.namespace, b.app),
            "namespace_up": netns.namespace_exists(b.namespace),
            "nat_present": any(b.subnet in line.split() for line in nat_lines),
        })
    return result


def teardown_idle() -> Dict[str, Any]:
    """Drop bindings whose namespace has no processes and tear down interfaces left without bindings."""
    st = state.load_state(config.STATE_FILE)
    removed: List[str] = []
    for a in list(st.adoptions):
        if adopt_sys.read_group_pids(a.group):
            continue
        _release_plumbing(a, [])
        st.adoptions.remove(a)
        removed.append(f"{a.app_name}/{a.iface} (adopted)")
    cidr_for: Dict[str, str] = dict(st.subnets)
    for b in list(st.bindings):
        if procs.pids_in_netns(b.namespace):
            continue
        st.bindings.remove(b)
        cidr_for.setdefault(b.iface, b.subnet)
        removed.append(f"{b.app_name}/{b.iface}")

    active = {b.iface for b in st.bindings}
    torn: List[str] = []
    for iface in sorted(cidr_for):
        if iface in active or procs.pids_in_netns(netns.namespace_name(iface)):
            continue
        _teardown_iface(iface, cidr_for[iface])
        st.subnets.pop(iface, None)
        torn.append(iface)

    if not st.bindings:
        st.ip_forward_prev = None       # forwarding itself is left on: other services may depend on it
    state.save_state(st, config.STATE_FILE)
    return {"removed": removed, "torn_down_ifaces": torn}


def refresh_routes() -> Dict[str, Any]:
    """Re-add the policy route of every bound interface whose route the kernel dropped (link flap, roam,
    tether re-plug) or whose gateway changed. Interfaces that are down are left on their unreachable fallback."""
    st = state.load_state(config.STATE_FILE)
    try:
        current = {i.name: i for i in netinfo.list_interfaces()}
    except (InterMuxError, OSError) as exc:
        log.warning("cannot list interfaces: %s", exc)
        return {"refreshed": []}
    refreshed: List[str] = []
    for iface, cidr in sorted(st.subnets.items()):
        interface = current.get(iface)
        if interface is None or not interface.bindable:
            continue
        net = ipaddress.IPv4Network(cidr)
        index = subnets.index_of(net)
        gateway = interface.gateways[0] if interface.gateways else None
        host_v = netns.veth_names(iface)[0]
        if routing.policy_route_present(config.TABLE_BASE + index, net, iface, gateway, host_v):
            continue
        if _safe(routing.ensure_policy, net, iface, config.TABLE_BASE + index, config.PRIORITY_BASE + index,
                 gateway, host_v):
            refreshed.append(iface)
            log.info("re-added the policy route for %s via %s", iface, gateway)
    return {"refreshed": refreshed}


def reset(kill_apps: bool = False, uid: Optional[int] = None) -> Dict[str, Any]:
    """Remove all InterMux plumbing and state; best effort, failures are logged and skipped.

    kill_apps terminates processes in InterMux namespaces, but only those owned by uid (and their
    uid-owned descendants). If uid is None, no process is killed even when kill_apps is True.
    """
    _reap_launched()
    st = state.load_state(config.STATE_FILE)
    namespaces = _unique(netns.list_namespaces_matching() + [b.namespace for b in st.bindings])
    veths = _unique(netns.list_veths_matching() + [netns.veth_names(b.iface)[0] for b in st.bindings])

    still_running: List[str] = []
    if kill_apps and uid is not None:
        table = procs.list_procs()
        pids: List[int] = []
        for ns in namespaces:
            ino = procs.netns_inode(ns)
            if ino is not None:
                pids.extend(p.pid for p in table if p.netns == ino and p.uid == uid)
        if pids:
            still_running = procs.terminate(procs.descendants(_unique(pids), uid=uid), grace=GRACE)

    # A namespace that still holds processes is kept: removing it would strand those apps with no
    # network, and a later reset could not find them. Their bindings, veth, policy and NAT rules stay.
    busy = [ns for ns in namespaces if procs.pids_in_netns(ns)]
    kept_bindings = [b for b in st.bindings if b.namespace in busy]
    dropped_bindings = [b for b in st.bindings if b.namespace not in busy]
    kept_subnets = {b.subnet for b in kept_bindings}
    kept_veths = {netns.veth_names(b.iface)[0] for b in kept_bindings}

    for a in st.adoptions:
        _safe(_release_plumbing, a, [])
    for table_name in _safe_list(adopt_sys.list_nft_tables):
        slot_text = table_name[len("intermux_adopt"):]
        if slot_text.isdigit():
            _safe(adopt_sys.delete_nft, int(slot_text))

    for b in dropped_bindings:
        _safe(_teardown_binding_plumbing, b)

    for ns in namespaces:
        if ns not in busy:
            _safe(netns.delete_namespace, ns)
    for veth in veths:
        if veth not in kept_veths:
            _safe(netns.delete_veth, veth)

    rules = [r for r in routing.intermux_rules()
             if str(routing.rule_src_network(r)) not in kept_subnets]
    for rule in rules:
        _safe(_remove_rule, rule)

    nat_lines = [line for line in firewall.list_intermux_rules()
                 if not any(subnet in line.split() for subnet in kept_subnets)]
    for line in nat_lines:
        _safe(firewall.delete_rule_line, line)

    # ip_forward is deliberately not restored (main never did): writing 0 here can cut off Docker,
    # libvirt or a Tailscale exit node that also need forwarding.
    _safe(adopt_sys.sweep_leftovers)

    kept_state = state.State(
        bindings=kept_bindings,
        subnets={b.iface: b.subnet for b in kept_bindings},
        ip_forward_prev=st.ip_forward_prev if kept_bindings else None,
    )
    state.save_state(kept_state, config.STATE_FILE)
    return {"namespaces": [ns for ns in namespaces if ns not in busy], "veths": veths,
            "rules": len(rules), "nat_rules": len(nat_lines), "still_running": still_running,
            "kept_namespaces": busy, "kept_apps": [b.app_name for b in kept_bindings]}


def filter_host_netns(table: Sequence[procs.Proc], pids: Sequence[int], host_netns: Optional[int]) -> List[int]:
    """Keep pids whose network namespace is the host one (drops pids inside InterMux namespaces)."""
    by_pid = {p.pid: p for p in table}
    kept: List[int] = []
    for pid in pids:
        proc = by_pid.get(pid)
        if proc is None:
            continue
        if host_netns is not None and proc.netns is not None and proc.netns != host_netns:
            continue
        kept.append(pid)
    return kept


def adopt(iface: str, app: str, *, user: UserInfo) -> Dict[str, Any]:
    """Move the running app's processes into a cgroup whose traffic is routed out of iface."""
    interface = _find_interface(iface)
    if not interface.bindable:
        raise ConflictError(f"interface {iface} cannot be bound (needs an up IPv4 address and a default route)",
                            code="not_bindable")
    if not isinstance(app, str) or not app.startswith("/") or not os.path.exists(app):
        raise ValidationError(f"application path must be an absolute path that exists: {app}")

    handler = apps.get_handler(app)
    found = procs.find_app_procs(app, handler.comm_names(app), uid=user.uid)
    table = procs.list_procs()
    candidates = procs.descendants([p.pid for p in found], table, uid=user.uid) if found else []
    try:
        host_netns: Optional[int] = os.stat("/proc/self/ns/net").st_ino
    except OSError:
        host_netns = None
    pids = filter_host_netns(table, candidates, host_netns)
    if not pids:
        raise NotFoundError(f"no running instances of {app} owned by you", code="not_running")

    st = state.load_state(config.STATE_FILE)
    adopt_id = secrets.token_hex(8)
    slot = adopt_sys.pick_slot(a.slot for a in st.adoptions)
    group = adopt_sys.group_rel(adopt_id)
    gateway = interface.gateways[0] if interface.gateways else None

    def rollback() -> None:
        # Empty list: release only the real members of the group, never candidates that did not join.
        _safe(adopt_sys.release_group, group, [])
        _safe(adopt_sys.delete_nft, slot)
        _safe(adopt_sys.remove_policy, slot)

    moved: List[int] = []
    try:
        adopt_sys.ensure_group(group)
        for pid in pids:
            # Re-check ownership right before the write: the pid may have been reused by another user.
            if _real_uid(pid) != user.uid:
                continue
            moved.extend(adopt_sys.move_pids(group, [pid]))
        if moved:
            adopt_sys.apply_nft(slot, group, iface)
            adopt_sys.apply_policy(slot, iface, gateway)
    except Exception as exc:
        rollback()
        raise ConflictError(f"could not route {os.path.basename(app)} through {iface}: {exc}",
                            code="adopt_failed") from exc
    if not moved:
        rollback()
        raise NotFoundError("the application exited before it could be routed")

    record = state.Adoption(
        id=adopt_id, iface=iface, app=app, app_name=os.path.basename(app), slot=slot, group=group,
        mark=adopt_sys.slot_mark(slot), table=adopt_sys.slot_table(slot),
        priority=adopt_sys.slot_priority(slot), pids=list(moved), uid=user.uid,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    st.adoptions.append(record)
    state.save_state(st, config.STATE_FILE)
    return {"adoption": asdict(record), "moved_pids": list(moved),
            "warnings": ["DNS lookups may still use the default route"]}


def _real_uid(pid: int) -> Optional[int]:
    """Real uid of pid from /proc/<pid>/status, or None if it cannot be read."""
    try:
        with open(f"/proc/{pid}/status", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("Uid:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _release_plumbing(a: state.Adoption, pids: Sequence[int]) -> List[int]:
    """Undo an adoption's cgroup, policy and nft table; each step is attempted even if one fails."""
    released: List[int] = []
    try:
        released = list(adopt_sys.release_group(a.group, pids))
    except (InterMuxError, OSError) as exc:
        log.warning("release_group %s failed: %s", a.group, exc)
    _safe(adopt_sys.remove_policy, a.slot)
    _safe(adopt_sys.delete_nft, a.slot)
    return released


def release_adoption(adopt_id: str, *, user: Optional[UserInfo] = None) -> Dict[str, Any]:
    """Release one adoption: pids go back to the root cgroup and the routing plumbing is removed."""
    st = state.load_state(config.STATE_FILE)
    a = st.find_adoption(adopt_id)
    if a is None:
        raise NotFoundError(f"no adoption with id {adopt_id}")
    if user is not None and user.uid != a.uid:
        raise PermissionDeniedError("this adoption belongs to another user")
    pids = adopt_sys.read_group_pids(a.group)
    if user is not None:
        owned = {p.pid for p in procs.list_procs() if p.uid == user.uid}
        pids = [p for p in pids if p in owned]
    released = _release_plumbing(a, pids)
    remaining = adopt_sys.read_group_pids(a.group)
    if remaining:
        return {"released": released, "still_running": remaining}
    st.adoptions = [x for x in st.adoptions if x.id != adopt_id]
    state.save_state(st, config.STATE_FILE)
    return {"released": released, "still_running": []}


def adoption_status() -> List[Dict[str, Any]]:
    """Describe every adoption with the pids currently in its cgroup."""
    st = state.load_state(config.STATE_FILE)
    result: List[Dict[str, Any]] = []
    for a in st.adoptions:
        pids = adopt_sys.read_group_pids(a.group)
        result.append({"id": a.id, "iface": a.iface, "app_name": a.app_name, "group_pids": pids,
                       "alive": bool(pids), "created_at": a.created_at})
    return result


def _safe_list(fn: Callable[[], List[str]]) -> List[str]:
    try:
        return list(fn())
    except (InterMuxError, OSError) as exc:
        log.warning("%s failed: %s", getattr(fn, "__name__", "call"), exc)
        return []


def _find_interface(name: str) -> netinfo.Interface:
    for interface in netinfo.list_interfaces():
        if interface.name == name:
            return interface
    raise NotFoundError(f"interface not found: {name}")


def _used_networks(st: state.State) -> List[ipaddress.IPv4Network]:
    cidrs: List[str] = list(st.subnets.values())
    for interface in netinfo.list_interfaces():
        cidrs.extend(interface.ipv4_cidrs)
    # Table 52 is Tailscale's: its rule (priority 5270) is consulted before main, so a subnet route there that
    # covers a namespace subnet would steal the namespace's reply traffic.
    for table in ("main", TAILSCALE_TABLE):
        for route in commands.json_cmd([IP, "-j", "route", "show", "table", table],
                                       tolerate=("FIB table does not exist",)):
            dst = route.get("dst") if isinstance(route, dict) else None
            if dst and dst != "default":
                cidrs.append(dst)
    return subnets.used_networks(cidrs)


def _app_pids_in_namespace(namespace: str, app: str, uid: Optional[int] = None) -> List[int]:
    in_ns = set(procs.pids_in_netns(namespace))
    if not in_ns:
        return []
    comm = apps.get_handler(app).comm_names(app)
    return [p.pid for p in procs.find_app_procs(app, comm, uid=uid) if p.pid in in_ns]


def _teardown_iface(iface: str, cidr: str) -> None:
    net = ipaddress.IPv4Network(cidr)
    index = subnets.index_of(net)
    host_v, _ = netns.veth_names(iface)
    firewall.remove_nat(net, iface)
    routing.remove_policy(net, config.TABLE_BASE + index, config.PRIORITY_BASE + index)
    netns.delete_veth(host_v)
    netns.delete_namespace(netns.namespace_name(iface))


def _teardown_binding_plumbing(binding: state.Binding) -> None:
    net = ipaddress.IPv4Network(binding.subnet)
    index = subnets.index_of(net)
    firewall.remove_nat(net, binding.iface)
    routing.remove_policy(net, config.TABLE_BASE + index, config.PRIORITY_BASE + index)
    netns.delete_veth(netns.veth_names(binding.iface)[0])
    netns.delete_namespace(binding.namespace)


def _remove_rule(rule: Dict[str, Any]) -> None:
    net = ipaddress.IPv4Network(f"{rule['src']}/{rule.get('srclen', 32)}", strict=False)
    routing.remove_policy(net, int(rule["table"]), int(rule["priority"]))


def _safe(fn: Callable[..., Any], *args: Any) -> bool:
    try:
        fn(*args)
        return True
    except (InterMuxError, OSError, KeyError, TypeError, ValueError) as exc:
        log.warning("%s%s failed: %s", fn.__name__, args, exc)
        return False


def _unique(items: Sequence[Any]) -> List[Any]:
    seen: Dict[Any, None] = {}
    for item in items:
        seen.setdefault(item, None)
    return list(seen)


def _ensure_tmpdir(user: UserInfo, namespace: str) -> None:
    """Create the per-namespace TMPDIR, or verify an existing one is a real directory owned by the user."""
    path = tmpdir_path(namespace, user.uid)
    created = False
    try:
        os.mkdir(path, 0o700)
        created = True
    except FileExistsError:
        pass
    # O_NOFOLLOW makes a pre-planted symlink fail to open; the fstat checks below cover the rest.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ConflictError(f"unsafe TMPDIR: {path}", code="unsafe_path") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            raise ConflictError(f"unsafe TMPDIR: {path}", code="unsafe_path")
        if created:
            os.fchown(fd, user.uid, user.gid)
        elif st.st_uid != user.uid:
            raise ConflictError(f"TMPDIR {path} is not owned by {user.username}", code="unsafe_path")
    finally:
        os.close(fd)


def _open_log(user: UserInfo, filename: str) -> Tuple[int, str]:
    """Open the app log under the user's home without following symlinks; returns (fd, path)."""
    rel = config.APP_LOG_DIR_REL
    home = Path(user.home)
    dirfd = os.open(str(home), os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = rel.parts
        for i, part in enumerate(parts):
            next_fd = _open_dir_at(dirfd, part, user.uid, user.gid, 0o700 if i == len(parts) - 1 else 0o755)
            os.close(dirfd)
            dirfd = next_fd
        fd, created = _open_log_file(dirfd, filename)
    finally:
        os.close(dirfd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise ConflictError(f"refusing unsafe log file: {filename}", code="unsafe_path")
        if not created and st.st_uid != user.uid:
            raise ConflictError(f"refusing log file not owned by {user.username}: {filename}",
                                code="unsafe_path")
        os.fchown(fd, user.uid, user.gid)
    except BaseException:
        os.close(fd)
        raise
    return fd, str(home / rel / filename)


def _open_log_file(dirfd: int, filename: str) -> Tuple[int, bool]:
    """Open or create the log file relative to dirfd without following symlinks; returns (fd, created)."""
    # O_NONBLOCK keeps a planted FIFO from blocking the open; it is cleared once the file is verified regular.
    flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY
    created = False
    try:
        fd = os.open(filename, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dirfd)
        created = True
    except FileExistsError:
        try:
            fd = os.open(filename, flags, dir_fd=dirfd)
        except OSError as exc:
            raise ConflictError(f"refusing unsafe log file: {filename}", code="unsafe_path") from exc
    except OSError as exc:
        raise ConflictError(f"refusing unsafe log file: {filename}", code="unsafe_path") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise ConflictError(f"refusing unsafe log file: {filename}", code="unsafe_path")
        fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) & ~os.O_NONBLOCK)
    except BaseException:
        os.close(fd)
        raise
    return fd, created


def _open_dir_at(parent_fd: int, name: str, uid: int, gid: int, mode: int) -> int:
    """Open (creating if needed) a directory relative to parent_fd; refuses symlinks and non-directories."""
    created = False
    try:
        os.mkdir(name, mode, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        pass
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        raise ConflictError(f"refusing unsafe path component: {name}", code="launch_failed") from exc
    if created:
        os.fchown(fd, uid, gid)
    return fd
