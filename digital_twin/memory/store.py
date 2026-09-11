"""The memory store: durable, searchable, prunable, user-controllable.

SQLite (WAL mode) with one table for all memory kinds — ``episodic``
(things that happened), ``semantic`` (facts and preferences), and later
``skill``. Content and structured data pass through the configured codec
(plaintext or encrypted); metadata stays queryable.

**Search** is a decode-and-rank scan rather than an index: stores are kept
small by pruning (thousands of records, single-digit milliseconds to
scan), it works identically for encrypted content, and it lets the ranking
blend factors an FTS index can't:

    score = text match  +  recency decay  +  importance  +  usage boost

Returned records get their access statistics bumped, so memories that keep
proving useful survive pruning longer — a small, honest form of relevance
learning. Vector/semantic retrieval arrives with the knowledge engine
milestone; this ranking is deliberately lexical and inspectable.

**User control is non-negotiable** (spec: review/edit/delete): every
record can be listed, read, edited, deleted and exported — see also the
CLI in :mod:`digital_twin.memory.cli`, which works against the same file
even while the assistant is offline.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

from digital_twin.memory.codec import CodecError, MemoryCodec, PlainCodec
from digital_twin.security.privacy import PrivacyTier

logger = logging.getLogger(__name__)

KINDS = ("episodic", "semantic", "skill")
_PRIVACY_TIERS = {tier.value for tier in PrivacyTier}

# M20: privacy_tier defaults to 'local_only' at the schema level too, so any
# row written by code that predates this column still reads as the safe
# default. No migration path exists in this codebase (no ALTER TABLE
# precedent) — an existing data/memory.db predating M20 needs deleting to
# pick up the new column.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    content BLOB NOT NULL,
    data BLOB NOT NULL,
    source TEXT NOT NULL,
    importance REAL NOT NULL,
    created_at REAL NOT NULL,
    last_accessed_at REAL NOT NULL,
    access_count INTEGER NOT NULL DEFAULT 0,
    tags TEXT NOT NULL,
    privacy_tier TEXT NOT NULL DEFAULT 'local_only'
);
CREATE INDEX IF NOT EXISTS idx_memories_kind_created
    ON memories(kind, created_at DESC);
"""


@dataclass(frozen=True)
class MemoryRecord:
    """One decoded memory."""

    id: str
    kind: str
    content: str
    data: dict[str, Any]
    source: str
    importance: float
    created_at: float
    last_accessed_at: float
    access_count: int
    tags: tuple[str, ...]
    privacy_tier: str = "local_only"


@dataclass(frozen=True)
class ScoredMemory:
    """A search hit with its ranking score."""

    record: MemoryRecord
    score: float


class MemoryStore:
    """Thread-safe SQLite-backed store for all persistent memory kinds."""

    def __init__(
        self,
        db_path: str | Path,
        codec: MemoryCodec | None = None,
        search_half_life_days: float = 7.0,
    ):
        self._codec = codec or PlainCodec()
        self._half_life_days = max(0.1, search_half_life_days)
        self._lock = threading.RLock()

        path = Path(db_path)
        if str(db_path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(db_path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock, self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.executescript(_SCHEMA)
        logger.info("Memory store open at %s (codec: %s)", db_path, self._codec.name)

    # ------------------------------------------------------------------
    # Create / read / update / delete
    # ------------------------------------------------------------------
    def add(
        self,
        kind: str,
        content: str,
        data: Mapping[str, Any] | None = None,
        source: str = "unknown",
        importance: float = 0.5,
        tags: Iterable[str] = (),
        privacy_tier: str = "local_only",
    ) -> MemoryRecord:
        """Persist one memory and return the stored record.

        ``privacy_tier`` defaults to ``local_only`` (THREAT_MODEL.md §4.8) —
        cloud-eligible content requires an explicit opt-in at a human-facing
        write surface (the memory CLI's ``--privacy-tier`` flag), never a
        config default a caller could silently loosen.
        """
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
        if not content or not content.strip():
            raise ValueError("memory content must be non-empty")
        if not 0.0 <= importance <= 1.0:
            raise ValueError("importance must be within 0..1")
        if privacy_tier not in _PRIVACY_TIERS:
            raise ValueError(
                f"privacy_tier must be one of {_PRIVACY_TIERS}, got {privacy_tier!r}")

        now = time.time()
        record = MemoryRecord(
            id=uuid.uuid4().hex,
            kind=kind,
            content=content,
            data=dict(data or {}),
            source=source,
            importance=float(importance),
            created_at=now,
            last_accessed_at=now,
            access_count=0,
            tags=tuple(str(tag) for tag in tags),
            privacy_tier=privacy_tier,
        )
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.id,
                    record.kind,
                    self._codec.encode(record.content),
                    self._codec.encode(json.dumps(record.data, default=str)),
                    record.source,
                    record.importance,
                    record.created_at,
                    record.last_accessed_at,
                    record.access_count,
                    json.dumps(list(record.tags)),
                    record.privacy_tier,
                ),
            )
        return record

    def get(self, memory_id: str, touch: bool = False) -> MemoryRecord | None:
        """Fetch one record; ``touch`` bumps its access statistics."""
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
        if row is None:
            return None
        if touch:
            self._touch([memory_id])
        return self._decode(row)

    def list(
        self, kind: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[MemoryRecord]:
        """Newest-first listing, optionally filtered by kind."""
        query = "SELECT * FROM memories"
        params: list[Any] = []
        if kind is not None:
            query += " WHERE kind = ?"
            params.append(kind)
        query += " ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return [self._decode(row) for row in rows]

    def count(self, kind: str | None = None) -> int:
        """Number of stored records, optionally for one kind."""
        with self._lock:
            if kind is None:
                row = self._connection.execute(
                    "SELECT COUNT(*) FROM memories"
                ).fetchone()
            else:
                row = self._connection.execute(
                    "SELECT COUNT(*) FROM memories WHERE kind = ?", (kind,)
                ).fetchone()
        return int(row[0])

    def update(
        self,
        memory_id: str,
        content: str | None = None,
        importance: float | None = None,
        tags: Iterable[str] | None = None,
    ) -> MemoryRecord:
        """User-edit a record (content, importance and/or tags)."""
        record = self.get(memory_id)
        if record is None:
            raise KeyError(f"No memory with id {memory_id!r}")
        if content is not None and not content.strip():
            raise ValueError("memory content must be non-empty")
        if importance is not None and not 0.0 <= importance <= 1.0:
            raise ValueError("importance must be within 0..1")

        updated = replace(
            record,
            content=content if content is not None else record.content,
            importance=float(importance) if importance is not None else record.importance,
            tags=tuple(tags) if tags is not None else record.tags,
        )
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE memories SET content=?, importance=?, tags=? WHERE id=?",
                (
                    self._codec.encode(updated.content),
                    updated.importance,
                    json.dumps(list(updated.tags)),
                    memory_id,
                ),
            )
        return updated

    def delete(self, memory_id: str) -> bool:
        """Remove one record; ``True`` if it existed."""
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "DELETE FROM memories WHERE id = ?", (memory_id,)
            )
        return cursor.rowcount > 0

    def clear(self, kind: str | None = None) -> int:
        """Remove everything (optionally of one kind); returns count removed."""
        with self._lock, self._connection:
            if kind is None:
                cursor = self._connection.execute("DELETE FROM memories")
            else:
                cursor = self._connection.execute(
                    "DELETE FROM memories WHERE kind = ?", (kind,)
                )
        return cursor.rowcount

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    def search(
        self,
        query: str,
        kinds: Iterable[str] | None = None,
        limit: int = 5,
        now: float | None = None,
    ) -> list[ScoredMemory]:
        """Ranked lexical search; returned hits get their access stats bumped."""
        tokens = [token for token in query.lower().split() if token]
        if not tokens:
            raise ValueError("search query must contain at least one word")
        now = time.time() if now is None else now
        wanted = tuple(kinds) if kinds else KINDS

        with self._lock:
            placeholders = ",".join("?" * len(wanted))
            rows = self._connection.execute(
                f"SELECT * FROM memories WHERE kind IN ({placeholders})", wanted
            ).fetchall()

        scored: list[ScoredMemory] = []
        phrase = " ".join(tokens)
        for row in rows:
            try:
                record = self._decode(row)
            except CodecError:
                logger.warning("Skipping undecodable memory %s", row["id"])
                continue
            haystack = f"{record.content} {' '.join(record.tags)}".lower()
            matched = sum(1 for token in tokens if token in haystack)
            if matched == 0:
                continue
            text_score = matched / len(tokens) + (0.5 if phrase in haystack else 0.0)
            age_days = max(0.0, now - record.created_at) / 86400.0
            recency = math.exp(-age_days / self._half_life_days)
            usage = min(record.access_count, 10) / 10.0
            score = (
                text_score
                + 0.5 * recency
                + 0.5 * record.importance
                + 0.2 * usage
            )
            scored.append(ScoredMemory(record=record, score=round(score, 4)))

        scored.sort(key=lambda hit: hit.score, reverse=True)
        top = scored[:limit]
        if top:
            self._touch([hit.record.id for hit in top], now=now)
        return top

    # ------------------------------------------------------------------
    # Pruning and export
    # ------------------------------------------------------------------
    def prune(
        self,
        kind: str,
        max_records: int | None = None,
        retention_days: float | None = None,
        now: float | None = None,
    ) -> int:
        """Delete expired records, then enforce the cap keeping the most
        important / most recently useful. Returns number deleted."""
        now = time.time() if now is None else now
        deleted = 0
        with self._lock, self._connection:
            if retention_days is not None:
                cutoff = now - retention_days * 86400.0
                cursor = self._connection.execute(
                    "DELETE FROM memories WHERE kind = ? AND created_at < ?",
                    (kind, cutoff),
                )
                deleted += cursor.rowcount
            if max_records is not None:
                cursor = self._connection.execute(
                    """
                    DELETE FROM memories WHERE id IN (
                        SELECT id FROM memories WHERE kind = ?
                        ORDER BY importance DESC, last_accessed_at DESC
                        LIMIT -1 OFFSET ?
                    )
                    """,
                    (kind, max(0, max_records)),
                )
                deleted += cursor.rowcount
        if deleted:
            logger.info("Pruned %d %s memories", deleted, kind)
        return deleted

    def export(self, kind: str | None = None) -> list[dict[str, Any]]:
        """Decoded, JSON-ready dump of the store (user data portability)."""
        records = self.list(kind=kind, limit=self.count(kind))
        return [
            {
                "id": record.id,
                "kind": record.kind,
                "content": record.content,
                "data": record.data,
                "source": record.source,
                "importance": record.importance,
                "created_at": record.created_at,
                "last_accessed_at": record.last_accessed_at,
                "access_count": record.access_count,
                "tags": list(record.tags),
                "privacy_tier": record.privacy_tier,
            }
            for record in records
        ]

    def close(self) -> None:
        """Close the underlying connection."""
        with self._lock:
            self._connection.close()

    # ------------------------------------------------------------------
    def _touch(self, memory_ids: list[str], now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock, self._connection:
            self._connection.executemany(
                "UPDATE memories SET last_accessed_at = ?, "
                "access_count = access_count + 1 WHERE id = ?",
                [(now, memory_id) for memory_id in memory_ids],
            )

    def _decode(self, row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            id=row["id"],
            kind=row["kind"],
            content=self._codec.decode(row["content"]),
            data=json.loads(self._codec.decode(row["data"])),
            source=row["source"],
            importance=row["importance"],
            created_at=row["created_at"],
            last_accessed_at=row["last_accessed_at"],
            access_count=row["access_count"],
            tags=tuple(json.loads(row["tags"])),
            privacy_tier=row["privacy_tier"],
        )
