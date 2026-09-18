"""Folder watcher — notices documents in watched dirs and queues new/
changed ones for human review before they enter the RAG corpus.

A plain :class:`BaseModule` (start/stop/pause like everything else) that
polls `knowledge.watch_paths` on a timer, extracts text from supported
files, and — for anything whose content isn't already in the store —
offers it to an :class:`IngestionQuarantine` instead of writing it
straight in. The write only happens when a human calls :meth:`approve`
(THREAT_MODEL.md §4.1 control #5: watching a directory is standing
consent to *notice* files, not to admit their content into the corpus
unattended). The heavy lifting — extraction, chunking, embedding,
dedup-by-content-hash — belongs to the store and the extractor; this
module notices files, holds candidates for review, and announces what
happened on the event bus.

Safety follows the file-action model exactly: every watched directory
must resolve inside `files.allowed_roots` (validated at construction; an
out-of-bounds watch path is dropped with a warning, never silently
honored), and only known-supported suffixes are touched. Idempotence is
the store's content hash, so a rescan of unchanged (or already-approved,
or already-rejected) content offers nothing new — the watcher can poll
cheaply forever.

The watcher does not go through the dispatcher's per-action confirmation
gate: that gate blocks a worker on a short timeout and denies by default
when nobody answers, which is the wrong shape for "a document found at
3am should still be here to review at 9am". The quarantine queue is a
deliberately different, un-timed primitive for that reason. Nothing here
can write, move or delete a user file — it only reads, and only ever
writes to the knowledge store, and only once approved.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from digital_twin.automation.file_actions import resolve_roots
from digital_twin.configuration.settings import FilesConfig, KnowledgeConfig
from digital_twin.core.events import Event, Topics
from digital_twin.core.module import BaseModule
from digital_twin.knowledge.extraction import (
    ExtractionError,
    extract_text,
    is_supported,
)
from digital_twin.knowledge.quarantine import IngestionQuarantine
from digital_twin.knowledge.store import KnowledgeStore, compute_content_hash

logger = logging.getLogger(__name__)


class KnowledgeWatchModule(BaseModule):
    """Poll watched folders and auto-ingest new/changed documents."""

    name = "knowledge_watch"
    topics = ()

    def __init__(self, knowledge: KnowledgeConfig, files: FilesConfig,
                 store: KnowledgeStore):
        super().__init__()
        self._config = knowledge
        self._store = store
        self._quarantine = IngestionQuarantine()
        self._interval = knowledge.watch_interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._queued_last = 0

        roots = resolve_roots(files.allowed_roots)
        self._dirs: list[Path] = []
        for raw in knowledge.watch_paths:
            resolved = Path(raw).expanduser().resolve()
            if not any(resolved == root or root in resolved.parents
                       for root in roots):
                logger.warning(
                    "knowledge.watch_paths entry %s is outside "
                    "files.allowed_roots — ignored", resolved)
                continue
            self._dirs.append(resolved)

    # ------------------------------------------------------------------
    def _on_start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="knowledge-watch", daemon=True)
        self._thread.start()

    def _on_stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def _on_pause(self) -> None:
        self._on_stop()

    def _on_resume(self) -> None:
        self._on_start()

    # ------------------------------------------------------------------
    def _loop(self) -> None:
        # An immediate first scan, then on the interval.
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception:  # a scan must never kill the module
                logger.exception("knowledge watch scan failed")
            self._stop.wait(self._interval)

    def scan_once(self) -> int:
        """Scan every watched dir once; return how many files were newly
        queued for review (not ingested — see :meth:`approve`).

        Exposed (not just the loop calls it) so tests and the dashboard
        can trigger a deterministic scan.
        """
        queued = 0
        for directory in self._dirs:
            if not directory.is_dir():
                continue
            for path in sorted(directory.rglob("*")):
                if self._stop.is_set():
                    break
                if not path.is_file() or not is_supported(path):
                    continue
                if self._consider(path):
                    queued += 1
        self._queued_last = queued
        if queued:
            logger.info("knowledge watch queued %d file(s) for review", queued)
        return queued

    def _consider(self, path: Path) -> bool:
        """Extract text and, if its content isn't already in the store,
        offer it to the quarantine. Never writes to the store itself."""
        try:
            text = extract_text(path)
        except (ExtractionError, OSError) as exc:
            logger.warning("watch: skipping %s (%s)", path.name, exc)
            return False
        if self._store.find_by_content_hash(text) is not None:
            return False  # identical content already ingested — no-op
        content_hash = compute_content_hash(text)
        quarantine_id = self._quarantine.offer(
            title=path.name, source=str(path), text=text,
            content_hash=content_hash)
        if quarantine_id is None:
            return False  # already pending, or previously rejected
        if self._bus is not None:
            self._bus.publish(Event(
                topic=Topics.MODULE, source=self.name,
                payload={"event": "quarantined", "title": path.name,
                         "id": quarantine_id}))
        return True

    # ------------------------------------------------------------------
    # Human review — the dashboard (or a test) drives these.
    def pending(self) -> list[dict]:
        """Documents currently waiting for an approve/reject decision."""
        return self._quarantine.pending()

    def approve(self, quarantine_id: str) -> bool:
        """Write a pending document to the store. ``False`` if unknown."""
        entry = self._quarantine.pop(quarantine_id)
        if entry is None:
            return False
        doc_id, chunks, created = self._store.ingest(
            title=entry.title, text=entry.text, source=entry.source,
            replace_source=True)
        if created and self._bus is not None:
            self._bus.publish(Event(
                topic=Topics.MODULE, source=self.name,
                payload={"event": "ingested", "title": entry.title,
                         "doc_id": doc_id, "chunks": chunks}))
        return True

    def reject(self, quarantine_id: str) -> bool:
        """Discard a pending document; its content won't be re-offered."""
        return self._quarantine.reject(quarantine_id)

    def _metrics(self) -> dict:
        return {"watched_dirs": len(self._dirs),
                "last_scan_queued": self._queued_last,
                "pending_review": len(self._quarantine.pending())}
