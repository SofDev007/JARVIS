"""Folder watcher — auto-ingest documents that appear in watched dirs.

A plain :class:`BaseModule` (start/stop/pause like everything else) that
polls `knowledge.watch_paths` on a timer and ingests supported files it
has not already seen at their current content. The heavy lifting —
extraction, chunking, embedding, idempotence — belongs to the store and
the extractor; this module only *notices* files and hands them over,
then announces what it did on the event bus.

Safety follows the file-action model exactly: every watched directory
must resolve inside `files.allowed_roots` (validated at construction; an
out-of-bounds watch path is dropped with a warning, never silently
honored), and only known-supported suffixes are touched. Idempotence is
the store's content hash, so a rescan of unchanged files does nothing —
the watcher can poll cheaply forever.

The watcher does not go through the permission gate per file: watching a
directory *is* the standing consent, declared once in config, exactly as
`open_application`'s allow-list or `files.allowed_roots` are. Each
ingest is still audited by the store, and nothing here can write, move
or delete a user file — it only reads.
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
from digital_twin.knowledge.store import KnowledgeStore

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
        self._interval = knowledge.watch_interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ingested_last = 0

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
        """Scan every watched dir once; return how many files were ingested.

        Exposed (not just the loop calls it) so tests and the dashboard
        can trigger a deterministic scan.
        """
        ingested = 0
        for directory in self._dirs:
            if not directory.is_dir():
                continue
            for path in sorted(directory.rglob("*")):
                if self._stop.is_set():
                    break
                if not path.is_file() or not is_supported(path):
                    continue
                if self._ingest(path):
                    ingested += 1
        self._ingested_last = ingested
        if ingested:
            logger.info("knowledge watch ingested %d file(s)", ingested)
        return ingested

    def _ingest(self, path: Path) -> bool:
        try:
            text = extract_text(path)
        except (ExtractionError, OSError) as exc:
            logger.warning("watch: skipping %s (%s)", path.name, exc)
            return False
        try:
            doc_id, chunks, created = self._store.ingest(
                title=path.name, text=text, source=str(path),
                replace_source=True)
        except Exception as exc:
            logger.warning("watch: ingest failed for %s (%s)", path.name, exc)
            return False
        if created and self._bus is not None:
            self._bus.publish(Event(
                topic=Topics.MODULE, source=self.name,
                payload={"event": "ingested", "title": path.name,
                         "doc_id": doc_id, "chunks": chunks}))
        return created

    def _metrics(self) -> dict:
        return {"watched_dirs": len(self._dirs),
                "last_scan_ingested": self._ingested_last}
