"""Live throughput from the kernel's per-interface byte counters. Unprivileged: reads sysfs only.

Interfaces count received bytes as download. The host end of a namespace veth pair is the other way
round: what the host end transmits is what the namespace downloads, so its counters are swapped.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Dict, Iterable, List, Mapping, Optional, Tuple

SYS_CLASS_NET = Path("/sys/class/net")
HISTORY_SAMPLES = 120          # two minutes at one sample per second
MIN_SCALE = 100 * 1024         # graphs never zoom in below 100 KB/s, so idle noise stays flat
IDLE_RATE = 8 * 1024           # below this a link counts as idle


@dataclass(frozen=True)
class Counters:
    rx: int
    tx: int


def _read_int(path: Path) -> Optional[int]:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def read_counters(names: Iterable[str], base: Path = SYS_CLASS_NET) -> Dict[str, Counters]:
    """Byte counters of each named device; devices that do not exist (or vanish) are left out."""
    found: Dict[str, Counters] = {}
    for name in names:
        if not name or "/" in name or name in (".", ".."):
            continue
        stats = Path(base) / name / "statistics"
        rx = _read_int(stats / "rx_bytes")
        tx = _read_int(stats / "tx_bytes")
        if rx is not None and tx is not None:
            found[name] = Counters(rx, tx)
    return found


def link_speed_bytes(name: str, base: Path = SYS_CLASS_NET) -> Optional[float]:
    """Negotiated link speed in bytes per second, or None when the driver does not report one."""
    mbit = _read_int(Path(base) / name / "speed")
    if mbit is None or mbit <= 0 or mbit > 1_000_000:
        return None
    return mbit * 1_000_000 / 8


def iface_sample(counters: Counters) -> Tuple[int, int]:
    """(download, upload) bytes of a physical interface."""
    return counters.rx, counters.tx


def veth_sample(counters: Counters) -> Tuple[int, int]:
    """(download, upload) bytes of a namespace, from the host end of its veth pair."""
    return counters.tx, counters.rx


class RateTracker:
    """Turns successive (download, upload) byte totals into rates, keeping a fixed-length history."""

    def __init__(self, history: int = HISTORY_SAMPLES) -> None:
        self.size = history
        self._last: Dict[str, Tuple[float, int, int]] = {}
        self._first: Dict[str, Tuple[int, int]] = {}
        self._history: Dict[str, Deque[Tuple[float, float]]] = {}

    def update(self, samples: Mapping[str, Tuple[int, int]], now: float) -> None:
        """Record one sample per name. Names missing from `samples` are forgotten."""
        for name in list(self._last):
            if name not in samples:
                self.forget(name)
        for name, (down, up) in samples.items():
            series = self._history.get(name)
            if series is None:
                series = deque([(0.0, 0.0)] * self.size, maxlen=self.size)
                self._history[name] = series
                self._first[name] = (down, up)
            previous = self._last.get(name)
            self._last[name] = (now, down, up)
            if previous is None:
                continue
            elapsed = now - previous[0]
            if elapsed <= 0:
                continue
            # A counter that went backwards was reset (device re-created): count from zero, not negative.
            d_rate = max(down - previous[1], 0) / elapsed
            u_rate = max(up - previous[2], 0) / elapsed
            if down < previous[1] or up < previous[2]:
                self._first[name] = (down, up)
            series.append((d_rate, u_rate))

    def forget(self, name: str) -> None:
        self._last.pop(name, None)
        self._first.pop(name, None)
        self._history.pop(name, None)

    def names(self) -> List[str]:
        return list(self._history)

    def rate(self, name: str) -> Tuple[float, float]:
        series = self._history.get(name)
        return series[-1] if series else (0.0, 0.0)

    def history(self, name: str) -> List[Tuple[float, float]]:
        series = self._history.get(name)
        return list(series) if series else [(0.0, 0.0)] * self.size

    def transferred(self, name: str) -> Tuple[int, int]:
        """Bytes moved since this tracker first saw the name (or since its counters were reset)."""
        last = self._last.get(name)
        first = self._first.get(name)
        if last is None or first is None:
            return 0, 0
        return max(last[1] - first[0], 0), max(last[2] - first[1], 0)

    def peak(self, name: str) -> float:
        series = self._history.get(name)
        return max((max(d, u) for d, u in series), default=0.0) if series else 0.0


def nice_ceiling(value: float, floor: float = MIN_SCALE) -> float:
    """A round graph ceiling at or above value (bytes/s): 1, 2 or 5 times a power of ten of KB, MB, GB...

    Rounding happens in the same 1024-based units the labels use, so the ceiling reads "5 MB/s", not "4.8".
    """
    value = max(float(value), float(floor), 1.0)
    unit = 1.0
    while value / unit >= 1000 and unit < 1024 ** 4:
        unit *= 1024
    scaled = value / unit
    exponent = math.floor(math.log10(scaled))
    for step in (1, 2, 5, 10):
        candidate = step * 10 ** exponent
        if candidate >= scaled * 0.999:
            return float(candidate * unit)
    return float(10 ** (exponent + 1) * unit)


_UNITS = ("B", "KB", "MB", "GB", "TB")


def split_rate(rate: float) -> Tuple[str, str]:
    """A rate as (number, unit), e.g. (2.4, "MB/s"); three significant figures at most."""
    value = max(float(rate), 0.0)
    unit = 0
    while value >= 1000 and unit < len(_UNITS) - 1:
        value /= 1024
        unit += 1
    if unit == 0:
        text = f"{value:.0f}"
    elif value < 10:
        text = f"{value:.1f}"
    else:
        text = f"{value:.0f}"
    return text, f"{_UNITS[unit]}/s"


def format_rate(rate: float) -> str:
    number, unit = split_rate(rate)
    return f"{number} {unit}"


def format_bytes(count: float) -> str:
    value = max(float(count), 0.0)
    unit = 0
    while value >= 1000 and unit < len(_UNITS) - 1:
        value /= 1024
        unit += 1
    if unit == 0:
        return f"{value:.0f} B"
    return f"{value:.1f} {_UNITS[unit]}" if value < 100 else f"{value:.0f} {_UNITS[unit]}"


def scale_label(scale: float) -> str:
    """Short label for the top of a graph, e.g. "5 MB/s"."""
    number, unit = split_rate(scale)
    if number.endswith(".0"):
        number = number[:-2]
    return f"{number} {unit}"
