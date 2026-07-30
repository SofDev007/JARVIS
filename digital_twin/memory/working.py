"""Working memory: what just happened, kept in RAM only.

A bounded, time-windowed buffer of recent activity (intents, actions,
context switches) that future reasoning consumes as "current situation"
context. Deliberately volatile — working memory that survives a restart
isn't working memory, it's episodic memory wearing a costume; anything
worth keeping is persisted by the memory module as an episodic record.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class WorkingItem:
    """One recent happening."""

    topic: str
    summary: str
    timestamp: float
    data: dict[str, Any] = field(default_factory=dict)


class WorkingMemory:
    """Thread-safe ring buffer with capacity and age limits."""

    def __init__(self, capacity: int = 200, window_s: float = 3600.0):
        self._items: deque[WorkingItem] = deque(maxlen=max(1, capacity))
        self._window_s = max(1.0, window_s)
        self._lock = threading.Lock()

    def add(
        self,
        topic: str,
        summary: str,
        data: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> None:
        """Record one happening."""
        item = WorkingItem(
            topic=topic,
            summary=summary,
            timestamp=time.time() if now is None else now,
            data=dict(data or {}),
        )
        with self._lock:
            self._items.append(item)

    def recent(self, count: int = 20, now: float | None = None) -> list[WorkingItem]:
        """The last ``count`` items inside the time window, oldest first."""
        now = time.time() if now is None else now
        cutoff = now - self._window_s
        with self._lock:
            fresh = [item for item in self._items if item.timestamp >= cutoff]
        return fresh[-count:]

    def clear(self) -> None:
        """Forget everything (e.g. on pause — privacy over convenience)."""
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
