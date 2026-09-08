"""Small persistent store so a restart doesn't re-send messages."""

from __future__ import annotations

import json
import threading
from collections import deque
from pathlib import Path

MAX_REMEMBERED = 20_000


class SeenStore:
    """Bounded, crash-tolerant set of message keys already forwarded."""

    def __init__(self, path: Path, max_items: int = MAX_REMEMBERED):
        self.path = path
        self.max_items = max_items
        self._lock = threading.Lock()
        self._order: deque[str] = deque(maxlen=max_items)
        self._set: set[str] = set()
        self._dirty = 0
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
            keys = data.get("seen", []) if isinstance(data, dict) else []
        except (OSError, ValueError):
            keys = []
        for key in keys[-self.max_items :]:
            self._order.append(key)
            self._set.add(key)

    def add_if_new(self, key: str) -> bool:
        """Return True if this key had not been seen before."""
        with self._lock:
            if key in self._set:
                return False
            if len(self._order) == self._order.maxlen and self._order:
                self._set.discard(self._order[0])
            self._order.append(key)
            self._set.add(key)
            self._dirty += 1
            if self._dirty >= 25:
                self._flush_locked()
            return True

    def _flush_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"seen": list(self._order)}))
        tmp.replace(self.path)
        self._dirty = 0

    def flush(self) -> None:
        with self._lock:
            if self._dirty:
                self._flush_locked()


class Cursors:
    """Per-conversation 'last message I forwarded' timestamps, for polling mode."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, str] = {}
        try:
            loaded = json.loads(self.path.read_text())
            if isinstance(loaded, dict):
                self._data = {k: str(v) for k, v in loaded.items()}
        except (OSError, ValueError):
            self._data = {}

    def get(self, key: str) -> str | None:
        with self._lock:
            return self._data.get(key)

    def set(self, key: str, ts: str) -> None:
        with self._lock:
            current = self._data.get(key)
            # Never move a cursor backwards; out-of-order polls would replay messages.
            if current is None or float(ts) > float(current):
                self._data[key] = str(ts)

    def flush(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data))
            tmp.replace(self.path)
