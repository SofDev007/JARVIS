"""Ingestion quarantine: folder-watched documents wait for a human
"approve" before they enter the RAG corpus.

THREAT_MODEL.md §4.1 control #5, explicitly scoped out of M21: an
untrusted document reaching the corpus is bounded once it's in (tainted,
refused as sole justification for a DANGEROUS action) but nothing gated
*whether it gets in* in the first place — the watcher wrote straight to
the store on a timer, no human in the loop on *when*.

This is deliberately not :class:`~digital_twin.security.confirmation.
ConfirmationProvider` — that blocks a dispatcher worker on a short
timeout and denies by default when nobody answers in time. A discovered
document should sit here indefinitely until a human actually reviews it;
timing out and silently discarding it would just be a different way of
skipping the review.
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass


@dataclass
class PendingIngestion:
    """One document the watcher found but has not written to the store."""

    quarantine_id: str
    title: str
    source: str
    text: str
    content_hash: str
    discovered_at: float

    def to_json(self) -> dict:
        return {
            "id": self.quarantine_id,
            "title": self.title,
            "source": self.source,
            "chars": len(self.text),
            "discovered_at": round(self.discovered_at, 1),
        }


class IngestionQuarantine:
    """Queue of documents awaiting an approve/reject decision.

    Not persisted across restarts — a restart re-scans and re-offers
    anything still unresolved, same as the watcher already does for
    anything it hasn't ingested yet. A rejection is remembered by content
    hash so the same content isn't re-offered every scan interval.
    """

    def __init__(self) -> None:
        self._pending: dict[str, PendingIngestion] = {}
        self._by_hash: dict[str, str] = {}
        self._rejected_hashes: set[str] = set()
        self._lock = threading.Lock()
        self._ids = itertools.count(1)

    def offer(self, *, title: str, source: str, text: str,
              content_hash: str) -> str | None:
        """Queue one document for review. Returns its id, or ``None`` if
        it's already pending or was previously rejected (same content)."""
        with self._lock:
            if content_hash in self._rejected_hashes:
                return None
            if content_hash in self._by_hash:
                return None
            entry = PendingIngestion(
                quarantine_id=f"q{next(self._ids)}",
                title=title, source=source, text=text,
                content_hash=content_hash, discovered_at=time.time())
            self._pending[entry.quarantine_id] = entry
            self._by_hash[content_hash] = entry.quarantine_id
            return entry.quarantine_id

    def pending(self) -> list[dict]:
        """JSON-ready snapshot of everything awaiting a decision."""
        with self._lock:
            return [entry.to_json() for entry in self._pending.values()]

    def pop(self, quarantine_id: str) -> PendingIngestion | None:
        """Remove and return one entry — approve/reject both consume it."""
        with self._lock:
            entry = self._pending.pop(quarantine_id, None)
            if entry is not None:
                self._by_hash.pop(entry.content_hash, None)
            return entry

    def reject(self, quarantine_id: str) -> bool:
        entry = self.pop(quarantine_id)
        if entry is None:
            return False
        with self._lock:
            self._rejected_hashes.add(entry.content_hash)
        return True
