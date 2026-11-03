"""Recently routed apps, with the connection and DNS they used, kept in the user's own state directory."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, List, Optional

log = logging.getLogger("intermux.ui")

LIMIT = 20
HISTORY_REL = Path(".local/state/intermux/history.json")


@dataclass
class Item:
    app: str            # absolute path
    name: str
    iface: str = ""
    dns: str = ""       # the DNS field text it was routed with; "" means automatic


def default_path() -> Path:
    return Path(os.path.expanduser("~")) / HISTORY_REL


def _item(raw: Any) -> Optional[Item]:
    if not isinstance(raw, dict) or not isinstance(raw.get("app"), str) or not raw["app"].startswith("/"):
        return None
    return Item(app=raw["app"], name=str(raw.get("name") or os.path.basename(raw["app"])),
                iface=str(raw.get("iface") or ""), dns=str(raw.get("dns") or ""))


class History:
    """Most recent first, one entry per app, at most LIMIT entries."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else default_path()
        self.items: List[Item] = self._load()

    def _load(self) -> List[Item]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        items = [item for item in (_item(raw) for raw in data) if item is not None]
        return items[:LIMIT]

    def find(self, app: str) -> Optional[Item]:
        return next((item for item in self.items if item.app == app), None)

    def add(self, item: Item) -> None:
        self.items = ([item] + [old for old in self.items if old.app != item.app])[:LIMIT]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".history.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump([asdict(i) for i in self.items], fh, indent=2)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("cannot save history to %s: %s", self.path, exc)
