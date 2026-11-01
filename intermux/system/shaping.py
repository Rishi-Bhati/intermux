"""Bandwidth limits for an interface namespace with a token bucket filter (tc tbf).

tc shapes what a device sends, so the download limit sits on the host end of the veth pair (it transmits
into the namespace) and the upload limit on the namespace end (it transmits out of the namespace).
"""

from __future__ import annotations

from typing import Optional

from intermux.errors import MissingToolError
from intermux.system import commands

MAX_MBIT = 100_000.0          # 100 Gbit/s; anything above is a typo
MIN_MBIT = 0.064              # 64 kbit/s; below that tbf cannot shape sensibly
_NO_QDISC = ("No such file or directory", "Cannot delete qdisc with handle of zero",
             "Cannot find specified qdisc", "Invalid handle")


def tc_bin() -> str:
    found = commands.which("tc")
    if found is None:
        raise MissingToolError("tc is not installed (package iproute2); it is needed for bandwidth limits",
                               code="missing_tool")
    return found


def check_mbit(value: object) -> Optional[float]:
    """A limit from a request: None (no limit) or a number of Mbit/s within range. Raises ValueError."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("a limit must be a number of Mbit/s or null")
    mbit = float(value)
    if mbit != mbit or not MIN_MBIT <= mbit <= MAX_MBIT:   # NaN check, then range
        raise ValueError(f"a limit must be between {MIN_MBIT:g} and {MAX_MBIT:g} Mbit/s")
    return mbit


def tbf_args(mbit: float) -> list:
    """tc arguments after `root` for a tbf at mbit Mbit/s: a burst of about 10 ms, at least 16 KiB."""
    kbit = max(int(round(mbit * 1000)), 1)
    burst = max(kbit * 1000 // 8 // 100, 16 * 1024)
    return ["tbf", "rate", f"{kbit}kbit", "burst", str(burst), "latency", "100ms"]


def _prefix(namespace: Optional[str]) -> list:
    return [commands.ip_bin(), "netns", "exec", namespace] if namespace else []


def set_limit(device: str, mbit: Optional[float], namespace: Optional[str] = None) -> None:
    """Shape what device sends to mbit Mbit/s; None removes the limit. device lives in namespace if given."""
    tc = tc_bin()
    if mbit is None:
        commands.run(_prefix(namespace) + [tc, "qdisc", "del", "dev", device, "root"], tolerate=_NO_QDISC)
        return
    commands.run_checked(_prefix(namespace) + [tc, "qdisc", "replace", "dev", device, "root"] + tbf_args(mbit))


def format_mbit(mbit: Optional[float]) -> str:
    """"20 Mbit/s", "512 kbit/s" or "no limit"."""
    if not mbit:
        return "no limit"
    if mbit < 1:
        return f"{mbit * 1000:g} kbit/s"
    return f"{mbit:g} Mbit/s"
