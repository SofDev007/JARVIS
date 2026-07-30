"""The knowledge store: documents → chunks → vectors → ranked recall.

Design decisions, stated:

* **SQLite, like memory.** One local file, WAL mode, no server. Chunk
  embeddings are stored as float32 BLOBs (``array`` module — no numpy).
* **Chunking is paragraph-first.** Paragraphs are packed into chunks of
  at most ``chunk_chars`` with ``chunk_overlap`` characters of tail
  carried into the next chunk, so a fact straddling a boundary is
  findable from either side. Oversized paragraphs are hard-split.
* **Dedup by content hash.** Re-ingesting an identical document is a
  no-op that returns the existing id — ingestion is idempotent, which
  matters once a "watch this folder" producer exists.
* **Search is brute-force cosine** over all chunks. Deliberate: at
  personal-corpus scale (thousands of chunks) a full scan is
  milliseconds; an ANN index (FAISS/hnswlib) is a heavy dep that earns
  its place only past ~100k chunks. The embedder name+dimension are
  recorded and enforced so vector spaces never silently mix.
* **The store never talks to the bus.** The reasoner pulls from it per
  message; actions mutate it through the normal gates. No knowledge
  content travels as events.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import time
from array import array
from dataclasses import dataclass
from pathlib import Path

from digital_twin.knowledge.embedding import Embedder, cosine

logger = logging.getLogger(__name__)


class KnowledgeError(RuntimeError):
    """Store unavailable, incompatible embedder, or bad input."""


@dataclass(frozen=True)
class DocumentInfo:
    """Metadata for one ingested document."""

    doc_id: int
    title: str
    source: str
    chunks: int
    created_at: float


@dataclass(frozen=True)
class KnowledgeHit:
    """One ranked search result."""

    doc_id: int
    title: str
    source: str
    position: int
    content: str
    score: float


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    content TEXT NOT NULL,
    embedding BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
"""


def chunk_text(text: str, chunk_chars: int, overlap: int) -> list[str]:
    """Paragraph-first packing with tail overlap (see module docstring)."""
    paragraphs = [
        paragraph.strip()
        for paragraph in text.replace("\r\n", "\n").split("\n\n")
        if paragraph.strip()
    ]
    pieces: list[str] = []
    for paragraph in paragraphs:
        while len(paragraph) > chunk_chars:  # oversized: hard split
            pieces.append(paragraph[:chunk_chars])
            paragraph = paragraph[chunk_chars - overlap:]
        pieces.append(paragraph)

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current}\n\n{piece}" if current else piece
        if len(candidate) <= chunk_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = (current[-overlap:] + "\n\n" + piece
                       if overlap else piece)
            while len(current) > chunk_chars:  # overlap + huge piece
                chunks.append(current[:chunk_chars])
                current = current[chunk_chars - overlap:]
        else:
            current = piece
    if current:
        chunks.append(current)
    return chunks


class KnowledgeStore:
    """Thread-safe SQLite-backed document/chunk/vector store."""

    def __init__(
        self,
        db_path: str | Path,
        embedder: Embedder,
        *,
        chunk_chars: int = 800,
        chunk_overlap: int = 100,
    ):
        self._embedder = embedder
        self._chunk_chars = chunk_chars
        self._overlap = min(chunk_overlap, max(0, chunk_chars // 2))
        path = Path(db_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._connection.executescript(_SCHEMA)
            self._check_embedder_pin()
            self._connection.commit()
        logger.info("Knowledge store ready at %s (%s, dim=%d)",
                    path, embedder.name, embedder.dim)

    def _check_embedder_pin(self) -> None:
        """Refuse to mix vector spaces (see module docstring)."""
        expected = f"{self._embedder.name}:{self._embedder.dim}"
        row = self._connection.execute(
            "SELECT value FROM meta WHERE key = 'embedder'").fetchone()
        if row is None:
            self._connection.execute(
                "INSERT INTO meta (key, value) VALUES ('embedder', ?)",
                (expected,))
        elif row[0] != expected:
            raise KnowledgeError(
                f"knowledge DB was built with embedder {row[0]!r} but the "
                f"configuration says {expected!r} — re-ingest into a fresh "
                "knowledge.db_path or restore the old embedder settings"
            )

    # ------------------------------------------------------------------
    def ingest(self, title: str, text: str, source: str = "chat",
               replace_source: bool = False) -> tuple[int, int, bool]:
        """Store one document; returns ``(doc_id, chunk_count, created)``.

        Idempotent: identical content returns the existing document.
        With ``replace_source`` (file-backed ingestion), any *older*
        documents from the same ``source`` are forgotten first — an
        edited file replaces its previous version instead of leaving
        stale chunks recallable beside the new ones. Chat-sourced notes
        never set this: distinct notes legitimately accumulate.
        """
        title = (title or "untitled").strip()[:200]
        text = (text or "").strip()
        if not text:
            raise ValueError("cannot ingest empty text")
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self._lock:
            row = self._connection.execute(
                "SELECT id, (SELECT COUNT(*) FROM chunks WHERE doc_id = documents.id) "
                "FROM documents WHERE content_hash = ?",
                (content_hash,)).fetchone()
            if row is not None:
                return row[0], row[1], False
            if replace_source and source:
                stale = self._connection.execute(
                    "SELECT id FROM documents WHERE source = ?",
                    (source,)).fetchall()
                for (stale_id,) in stale:
                    self._connection.execute(
                        "DELETE FROM chunks WHERE doc_id = ?", (stale_id,))
                    self._connection.execute(
                        "DELETE FROM documents WHERE id = ?", (stale_id,))
                if stale:
                    logger.info("Replacing %d stale version(s) of %s",
                                len(stale), source)
            cursor = self._connection.execute(
                "INSERT INTO documents (title, source, content_hash, created_at) "
                "VALUES (?, ?, ?, ?)",
                (title, source, content_hash, time.time()))
            doc_id = cursor.lastrowid
            chunks = chunk_text(text, self._chunk_chars, self._overlap)
            for position, content in enumerate(chunks):
                blob = array("f", self._embedder.embed(content)).tobytes()
                self._connection.execute(
                    "INSERT INTO chunks (doc_id, position, content, embedding) "
                    "VALUES (?, ?, ?, ?)",
                    (doc_id, position, content, blob))
            self._connection.commit()
        logger.info("Ingested %r (%d chunks) from %s", title, len(chunks),
                    source)
        return doc_id, len(chunks), True

    def search(self, query: str, top_k: int = 3,
               min_score: float = 0.1) -> list[KnowledgeHit]:
        """Rank all chunks by cosine similarity to ``query``."""
        query = (query or "").strip()
        if not query:
            return []
        query_vector = self._embedder.embed(query)
        with self._lock:
            rows = self._connection.execute(
                "SELECT c.doc_id, d.title, d.source, c.position, c.content, "
                "c.embedding FROM chunks c JOIN documents d ON d.id = c.doc_id"
            ).fetchall()
        hits: list[KnowledgeHit] = []
        for doc_id, title, source, position, content, blob in rows:
            vector = array("f")
            vector.frombytes(blob)
            score = cosine(query_vector, list(vector))
            if score >= min_score:
                hits.append(KnowledgeHit(doc_id, title, source, position,
                                         content, round(score, 4)))
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:max(1, top_k)]

    def documents(self) -> list[DocumentInfo]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT d.id, d.title, d.source, COUNT(c.id), d.created_at "
                "FROM documents d LEFT JOIN chunks c ON c.doc_id = d.id "
                "GROUP BY d.id ORDER BY d.created_at DESC").fetchall()
        return [DocumentInfo(*row) for row in rows]

    def forget(self, doc_id: int) -> bool:
        """Remove one document and its chunks; ``True`` if it existed."""
        with self._lock:
            self._connection.execute(
                "DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            cursor = self._connection.execute(
                "DELETE FROM documents WHERE id = ?", (doc_id,))
            self._connection.commit()
        removed = cursor.rowcount > 0
        if removed:
            logger.info("Forgot knowledge document %d", doc_id)
        return removed

    def close(self) -> None:
        with self._lock:
            self._connection.close()
