"""Process table access: list processes, match app processes, namespace membership, and termination."""

from __future__ import annotations

import logging
import os
import signal
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence

from intermux import config

log = logging.getLogger("intermux.system.procs")

_POLL_INTERVAL = 0.2
_FORCE_WAIT = 2.0
_DEAD_STATES = ("Z", "X")


@dataclass
class Proc:
    pid: int
    ppid: int
    comm: str
    uid: int
    exe: Optional[str]
    netns: Optional[int]


def _read_bytes(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _status_ids(pid: int) -> Optional[tuple]:
    """Return (ppid, real uid) from /proc/<pid>/status, or None (missing, unreadable, or exited)."""
    raw = _read_bytes(f"/proc/{pid}/status")
    if raw is None:
        return None
    ppid = uid = None
    state = ""
    try:
        for line in raw.decode("utf-8", errors="replace").splitlines():
            if line.startswith("PPid:"):
                ppid = int(line.split()[1])
            elif line.startswith("Uid:"):
                uid = int(line.split()[1])
            elif line.startswith("State:"):
                state = line.split()[1] if len(line.split()) > 1 else ""
    except (ValueError, IndexError):
        return None
    if ppid is None or uid is None:
        return None
    if state[:1] in ("Z", "X"):  # zombie or dead: already exited, not a running application
        return None
    return ppid, uid


def _exe(pid: int) -> Optional[str]:
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return None


def _netns(pid: int) -> Optional[int]:
    try:
        return os.stat(f"/proc/{pid}/ns/net").st_ino
    except OSError:
        return None


def _proc_state(pid: int) -> Optional[str]:
    raw = _read_bytes(f"/proc/{pid}/stat")
    if raw is None:
        return None
    # The comm field may contain spaces or parentheses, so split after the last ')'.
    rest = raw.decode("utf-8", errors="replace").rpartition(")")[2].split()
    return rest[0] if rest else None


def _alive(pid: int) -> bool:
    state = _proc_state(pid)
    return state is not None and state not in _DEAD_STATES


def list_procs() -> List[Proc]:
    """Return every readable process from /proc."""
    try:
        entries = os.listdir("/proc")
    except OSError:
        log.warning("cannot list /proc")
        return []
    procs: List[Proc] = []
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        ids = _status_ids(pid)
        comm_raw = _read_bytes(f"/proc/{pid}/comm")
        if ids is None or comm_raw is None:
            continue
        ppid, uid = ids
        procs.append(Proc(
            pid=pid,
            ppid=ppid,
            comm=comm_raw.decode("utf-8", errors="replace").rstrip("\n"),
            uid=uid,
            exe=_exe(pid),
            netns=_netns(pid),
        ))
    return procs


def netns_inode(name: str) -> Optional[int]:
    """Return the inode of the named network namespace handle, or None if missing."""
    if not name or "/" in name or name in (".", ".."):
        return None
    try:
        return os.stat(os.path.join(os.fspath(config.NETNS_DIR), name)).st_ino
    except OSError:
        return None


def pids_in_netns(name: str) -> List[int]:
    """Return pids running inside the named network namespace."""
    ino = netns_inode(name)
    if ino is None:
        return []
    return [p.pid for p in list_procs() if p.netns == ino]


def find_app_procs(app_path: str, comm_names: Sequence[str] = (), uid: Optional[int] = None) -> List[Proc]:
    """Return processes whose comm is in comm_names or whose exe is app_path.

    When uid is given, only processes with that real uid are returned. Never includes this process.
    """
    names = set(comm_names) if comm_names else {os.path.basename(app_path)[:15]}
    real_app = os.path.realpath(app_path)
    me = os.getpid()
    matches: List[Proc] = []
    for proc in list_procs():
        if proc.pid == me:
            continue
        if uid is not None and proc.uid != uid:
            continue
        if proc.comm in names or (proc.exe and os.path.realpath(proc.exe) == real_app):
            matches.append(proc)
    return matches


def descendants(pids: Iterable[int], procs: Optional[List[Proc]] = None,
                uid: Optional[int] = None) -> List[int]:
    """Return the given pids plus all of their descendants, without duplicates.

    When uid is given, only descendants whose real uid matches are included (the given pids are kept as is).
    """
    table = list_procs() if procs is None else procs
    children: Dict[int, List[int]] = {}
    for proc in table:
        if uid is not None and proc.uid != uid:
            continue
        children.setdefault(proc.ppid, []).append(proc.pid)

    result: List[int] = []
    seen = set()
    queue = list(pids)
    index = 0
    while index < len(queue):
        pid = queue[index]
        index += 1
        if pid in seen:
            continue
        seen.add(pid)
        result.append(pid)
        queue.extend(children.get(pid, []))
    return result


def _signal(pids: Sequence[int], sig: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            continue
        except PermissionError:
            log.warning("no permission to signal pid %d", pid)


def _wait_for_exit(pids: Sequence[int], timeout: float) -> List[int]:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        alive = [pid for pid in pids if _alive(pid)]
        if not alive:
            return []
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return alive
        time.sleep(min(_POLL_INTERVAL, remaining))


def terminate(pids: Iterable[int], *, grace: float = 5.0, force: bool = False) -> List[int]:
    """SIGTERM the pids, wait up to `grace` seconds, optionally SIGKILL survivors. Returns pids still alive."""
    me = os.getpid()
    targets: List[int] = []
    for pid in pids:
        # pid <= 1 is never a valid target here (0 signals the process group, 1 is init).
        if pid > 1 and pid != me and pid not in targets:
            targets.append(pid)
    if not targets:
        return []

    _signal(targets, signal.SIGTERM)
    alive = _wait_for_exit(targets, grace)
    if alive and force:
        log.warning("sending SIGKILL to pids %s", alive)
        _signal(alive, signal.SIGKILL)
        alive = _wait_for_exit(alive, _FORCE_WAIT)
    return alive


def proc_environ(pid: int) -> Dict[str, str]:
    """Return the initial environment of a process from /proc/<pid>/environ; {} if unreadable."""
    raw = _read_bytes(f"/proc/{pid}/environ")
    if raw is None:
        return {}
    env: Dict[str, str] = {}
    for item in raw.split(b"\0"):
        if not item:
            continue
        key, sep, value = item.decode("utf-8", errors="replace").partition("=")
        if sep and key:
            env.setdefault(key, value)
    return env
