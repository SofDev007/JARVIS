"""Audit log: an append-only record of everything the assistant tried to do.

Every action request — allowed, confirmed, denied, failed, dropped — lands
here as one JSON line with full provenance: the intent event id and the
original perception event id, so any executed action can be traced back to
the exact gesture (or, later, voice command) that caused it.

Design choices:

* **JSONL, append-only.** Trivially greppable, machine-parseable, and no
  in-place mutation — the audit trail is evidence, not state.
* **Size-based rollover** to timestamped files keeps the active log
  bounded without ever discarding history.
* **Never breaks the pipeline.** A full disk or unwritable path logs an
  error and drops the entry; auditing failures must not stop the assistant
  (they are, however, loudly visible in the normal logs).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class AuditLog:
    """Thread-safe append-only JSONL audit writer with rollover."""

    def __init__(self, path: str | Path, max_bytes: int = 5_000_000):
        self._path = Path(path)
        self._max_bytes = max(1024, int(max_bytes))
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        """Location of the active audit file."""
        return self._path

    # ------------------------------------------------------------------
    def record(self, **fields: Any) -> dict[str, Any]:
        """Append one entry (timestamp added automatically) and return it."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **fields,
        }
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with self._lock:
            try:
                self._rollover_if_needed(len(line) + 1)
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                logger.exception("Audit write failed; entry dropped: %s", line)
        return entry

    def tail(self, count: int = 20) -> list[dict[str, Any]]:
        """Return the last ``count`` entries of the active file (oldest first)."""
        with self._lock:
            try:
                lines = self._path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                return []
            except OSError:
                logger.exception("Audit read failed")
                return []
        entries: list[dict[str, Any]] = []
        for line in lines[-count:]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("Skipping corrupt audit line: %.80s", line)
        return entries

    # ------------------------------------------------------------------
    def _rollover_if_needed(self, incoming: int) -> None:
        try:
            size = self._path.stat().st_size
        except FileNotFoundError:
            return
        if size + incoming <= self._max_bytes:
            return
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        rolled = self._path.with_name(f"{self._path.stem}-{stamp}{self._path.suffix}")
        counter = 1
        while rolled.exists():  # same-second rollovers
            rolled = self._path.with_name(
                f"{self._path.stem}-{stamp}-{counter}{self._path.suffix}"
            )
            counter += 1
        self._path.rename(rolled)
        logger.info("Audit log rolled over to %s", rolled.name)
