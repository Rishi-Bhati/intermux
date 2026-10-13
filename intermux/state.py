from __future__ import annotations

import fcntl
import ipaddress
import json
import logging
import os
import re
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from intermux.config import LOCK_FILE, STATE_FILE, SUBNET_BASE_OCTET

log = logging.getLogger("intermux.state")

_REQUIRED_BINDING_KEYS = ("iface", "app", "namespace", "subnet", "table", "priority")
_POOL = ipaddress.IPv4Network(f"10.{SUBNET_BASE_OCTET}.0.0/16")
_MAX_TABLE = 0x7FFFFFFF
_MAX_PRIORITY = 32765


def valid_pool_subnet(value: Any) -> bool:
    """True when value is a canonical /24 inside the InterMux pool with third octet 1..254."""
    if not isinstance(value, str):
        return False
    try:
        net = ipaddress.IPv4Network(value, strict=True)
    except ValueError:
        return False
    return net.prefixlen == 24 and net.subnet_of(_POOL) and 1 <= net.network_address.packed[2] <= 254


@dataclass
class Binding:
    iface: str
    app: str                 # absolute path of the application binary
    app_name: str            # basename of app
    namespace: str           # e.g. ns_1a2b3c4d
    subnet: str              # e.g. 10.200.7.0/24
    table: int
    priority: int
    profile_mode: str = "default"    # "default" | "separate"
    log_path: str = ""
    created_at: str = ""             # ISO 8601 UTC
    pid: Optional[int] = None        # pid of the launched process (session leader); None in old state


@dataclass
class Adoption:
    id: str                  # 8..32 lowercase hex
    iface: str
    app: str                 # absolute path
    app_name: str
    slot: int
    group: str               # adopt.group_rel(id)
    mark: int
    table: int
    priority: int
    uid: int                 # owner of the adoption
    pids: List[int] = field(default_factory=list)   # snapshot at adoption time
    created_at: str = ""     # ISO 8601 UTC


@dataclass
class State:
    version: int = 1
    subnets: Dict[str, str] = field(default_factory=dict)   # iface -> cidr
    bindings: List[Binding] = field(default_factory=list)
    ip_forward_prev: Optional[str] = None                   # value before we first enabled forwarding
    adoptions: List[Adoption] = field(default_factory=list)

    def bindings_for_iface(self, iface: str) -> List[Binding]:
        return [b for b in self.bindings if b.iface == iface]

    def find_binding(self, iface: str, app: str) -> Optional[Binding]:
        for b in self.bindings:
            if b.iface == iface and b.app == app:
                return b
        return None

    def find_adoption(self, adopt_id: str) -> Optional[Adoption]:
        for a in self.adoptions:
            if a.id == adopt_id:
                return a
        return None

    def adoptions_for_iface(self, iface: str) -> List[Adoption]:
        return [a for a in self.adoptions if a.iface == iface]


def state_to_dict(state: State) -> Dict[str, Any]:
    """Convert State to a JSON-safe dict."""
    return asdict(state)


def state_from_dict(data: Dict[str, Any]) -> State:
    """Build State from a dict, ignoring unknown keys and defaulting missing ones. Raises ValueError if not a dict."""
    if not isinstance(data, dict):
        raise ValueError("state must be a JSON object")

    subnets: Dict[str, str] = {}
    raw_subnets = data.get("subnets")
    if isinstance(raw_subnets, dict):
        for iface, cidr in raw_subnets.items():
            if isinstance(iface, str) and valid_pool_subnet(cidr):
                subnets[iface] = cidr
            else:
                log.warning("ignoring malformed subnet entry: %r -> %r", iface, cidr)

    bindings: List[Binding] = []
    raw_bindings = data.get("bindings")
    if isinstance(raw_bindings, list):
        for item in raw_bindings:
            binding = _binding_from_dict(item)
            if binding is not None:
                bindings.append(binding)

    adoptions: List[Adoption] = []
    raw_adoptions = data.get("adoptions")
    if isinstance(raw_adoptions, list):
        for item in raw_adoptions:
            adoption = _adoption_from_dict(item)
            if adoption is not None:
                adoptions.append(adoption)

    ip_prev = data.get("ip_forward_prev")
    return State(
        version=_to_int(data.get("version"), 1),
        subnets=subnets,
        bindings=bindings,
        ip_forward_prev=None if ip_prev is None else str(ip_prev),
        adoptions=adoptions,
    )


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _binding_from_dict(item: Any) -> Optional[Binding]:
    if not isinstance(item, dict):
        log.warning("ignoring malformed binding entry: %r", item)
        return None
    missing = [k for k in _REQUIRED_BINDING_KEYS if item.get(k) in (None, "")]
    if missing:
        log.warning("ignoring binding missing %s: %r", ", ".join(missing), item)
        return None
    try:
        table = int(item["table"])
        priority = int(item["priority"])
    except (TypeError, ValueError):
        log.warning("ignoring binding with non-integer table/priority: %r", item)
        return None
    if not valid_pool_subnet(item["subnet"]):
        log.warning("ignoring binding with invalid subnet: %r", item)
        return None
    if not 1 <= table <= _MAX_TABLE or not 1 <= priority <= _MAX_PRIORITY:
        log.warning("ignoring binding with out-of-range table/priority: %r", item)
        return None
    raw_pid = item.get("pid")
    pid: Optional[int] = None
    if raw_pid is not None and not isinstance(raw_pid, bool):
        try:
            pid = int(raw_pid)
        except (TypeError, ValueError):
            pid = None
        if pid is not None and pid <= 1:
            pid = None
    app = str(item["app"])
    return Binding(
        iface=str(item["iface"]),
        app=app,
        app_name=str(item.get("app_name") or os.path.basename(app)),
        namespace=str(item["namespace"]),
        subnet=str(item["subnet"]),
        table=table,
        priority=priority,
        profile_mode=str(item.get("profile_mode") or "default"),
        log_path=str(item.get("log_path") or ""),
        created_at=str(item.get("created_at") or ""),
        pid=pid,
    )


_ADOPT_ID_RE = re.compile(r"^[0-9a-f]{8,32}$")
_REQUIRED_ADOPTION_KEYS = ("id", "iface", "app", "slot", "group", "mark", "table", "priority", "uid")


def _adoption_from_dict(item: Any) -> Optional[Adoption]:
    if not isinstance(item, dict):
        log.warning("ignoring malformed adoption entry: %r", item)
        return None
    missing = [k for k in _REQUIRED_ADOPTION_KEYS if item.get(k) in (None, "")]
    if missing:
        log.warning("ignoring adoption missing %s: %r", ", ".join(missing), item)
        return None
    adopt_id = str(item["id"])
    if not _ADOPT_ID_RE.fullmatch(adopt_id):
        log.warning("ignoring adoption with invalid id: %r", item)
        return None
    try:
        slot = int(item["slot"])
        mark = int(item["mark"])
        table = int(item["table"])
        priority = int(item["priority"])
        if isinstance(item["uid"], bool):
            raise ValueError("uid must be an integer")
        uid = int(item["uid"])
        raw_pids = item.get("pids") or []
        if not isinstance(raw_pids, list):
            raise ValueError("pids must be a list")
        pids = [int(p) for p in raw_pids]
    except (TypeError, ValueError):
        log.warning("ignoring adoption with non-integer fields: %r", item)
        return None
    app = str(item["app"])
    return Adoption(
        id=adopt_id,
        iface=str(item["iface"]),
        app=app,
        app_name=str(item.get("app_name") or os.path.basename(app)),
        slot=slot,
        group=str(item["group"]),
        mark=mark,
        table=table,
        priority=priority,
        uid=uid,
        pids=pids,
        created_at=str(item.get("created_at") or ""),
    )


def load_state(path: Path = STATE_FILE) -> State:
    """Load state; missing file gives an empty State. Invalid content is quarantined and gives an empty State."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return State()
    except OSError as exc:
        # Not moved aside: an unprivileged reader must not disturb the file it cannot read.
        log.warning("cannot read state file %s: %s", path, exc)
        return State()

    try:
        return state_from_dict(json.loads(raw.decode("utf-8")))
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError are ValueError subclasses
        _quarantine(path, exc)
        return State()


def _quarantine(path: Path, reason: Exception) -> None:
    dest = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
    try:
        os.replace(path, dest)
    except OSError as exc:
        log.warning("state file %s is invalid (%s) and could not be moved aside: %s", path, reason, exc)
        return
    log.warning("state file %s is invalid (%s); moved to %s", path, reason, dest)


def save_state(state: State, path: Path = STATE_FILE) -> None:
    """Atomically write state as JSON (temp file in the same directory, then os.replace)."""
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    payload = json.dumps(state_to_dict(state), indent=2, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextmanager
def state_lock(path: Path = LOCK_FILE) -> Iterator[None]:
    """Exclusive flock on path for the duration of the block. Blocks until acquired."""
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
