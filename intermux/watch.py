"""Root watcher: tears down bindings whose applications have exited, so routing is reset automatically.

Runs as a systemd service (packaging/linux/intermux-watch.service) or as `intermux watch` under root.
Each pass is the same as "Teardown idle": a binding with no processes left in its namespace is removed,
and its routing, NAT rules and namespace go with it once no other application uses that interface.

A pass first runs a cheap, lock-free check (one stat per process, no state write). The full teardown,
which takes the state lock, scans /proc several times and rewrites the state file, only runs when
that check finds something idle. Without the check the watcher would fsync the state file every pass.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from typing import Any, Callable, Dict, Iterable, Optional, Set

from intermux import config, engine, state
from intermux.system import adopt as adopt_sys
from intermux.system import procs

log = logging.getLogger("intermux.watch")

DEFAULT_INTERVAL = 3.0


def live_netns_inodes(proc_dir: str = "/proc") -> Set[int]:
    """Inodes of every network namespace that has at least one process (one stat per process)."""
    found: Set[int] = set()
    try:
        entries = os.listdir(proc_dir)
    except OSError:
        return found
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            found.add(os.stat(f"{proc_dir}/{entry}/ns/net").st_ino)
        except OSError:
            continue
    return found


def needs_teardown(st: state.State, live: Set[int], ns_inode: Callable[[str], Optional[int]],
                   group_pids: Callable[[str], Iterable[int]]) -> bool:
    """True when a pass of teardown_idle would change something. Pure apart from the injected lookups.

    Something is idle when a binding's namespace has no processes (or is gone), when an interface has a
    subnet but no binding (plumbing left by a failed launch), or when an adoption's cgroup is empty.
    """
    bound_ifaces = {b.iface for b in st.bindings}
    for binding in st.bindings:
        ino = ns_inode(binding.namespace)
        if ino is None or ino not in live:
            return True
    if any(iface not in bound_ifaces for iface in st.subnets):
        return True
    return any(not list(group_pids(a.group)) for a in st.adoptions)


def _default_check() -> bool:
    st = state.load_state(config.STATE_FILE)
    if not (st.bindings or st.subnets or st.adoptions):
        return False
    return needs_teardown(st, live_netns_inodes(), procs.netns_inode, adopt_sys.read_group_pids)


def _has_routes() -> bool:
    return bool(state.load_state(config.STATE_FILE).subnets)


def run_once(teardown: Optional[Callable[[], Dict[str, Any]]] = None,
             refresh: Optional[Callable[[], Dict[str, Any]]] = None, *,
             check: Optional[Callable[[], bool]] = None,
             routes: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
    """One pass: tear down idle bindings, then re-add policy routes the kernel dropped.

    `check` (default: the cheap idle check) decides whether teardown runs; `routes` (default: state has
    subnets) decides whether the route refresh runs. Neither step writes state unless something changed.
    Returns the merged reports, or {} when nothing needed doing. Errors propagate to run().
    """
    idle = (check or _default_check)()
    want_refresh = (routes or _has_routes)()
    if not idle and not want_refresh:
        return {}
    teardown = teardown or engine.teardown_idle
    refresh = refresh or engine.refresh_routes
    report: Dict[str, Any] = {}
    with state.state_lock():
        if idle:
            report.update(teardown())
        if want_refresh:
            report.update(refresh())
    removed = report.get("removed") or []
    if removed:
        log.info("released idle bindings: %s", ", ".join(str(r) for r in removed))
    torn = report.get("torn_down_ifaces") or []
    if torn:
        log.info("removed routing for: %s", ", ".join(str(t) for t in torn))
    return report


def run(interval: float = DEFAULT_INTERVAL, *, iterations: Optional[int] = None,
        sleep: Optional[Callable[[float], Any]] = None) -> int:
    """Loop until SIGTERM/SIGINT (or `iterations` passes, for tests). Returns 0 on a clean stop."""
    stop = threading.Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    # Waiting on the event (not time.sleep) lets a stop request end the wait at once.
    wait = sleep or stop.wait
    passes = 0
    while not stop.is_set():
        try:
            run_once()
        except Exception:  # keep watching: one bad pass must not stop the service
            log.exception("teardown pass failed; retrying")
        passes += 1
        if iterations is not None and passes >= iterations:
            break
        wait(interval)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return run()

