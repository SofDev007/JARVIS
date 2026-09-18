"""Tests for M15 document ingestion: format extraction (dependency-free
paths) and the folder watcher (containment, idempotence, filtering, and
the ingestion quarantine — THREAT_MODEL.md §4.1 control #5)."""

from __future__ import annotations

import time
import zipfile
from pathlib import Path

import pytest

from digital_twin.configuration.settings import FilesConfig, KnowledgeConfig
from digital_twin.core.bus import EventBus
from digital_twin.knowledge.embedding import HashingEmbedder
from digital_twin.knowledge.extraction import (
    ExtractionError,
    extract_text,
    is_supported,
    supported_suffixes,
)
from digital_twin.knowledge.store import KnowledgeStore
from digital_twin.knowledge.watch import KnowledgeWatchModule


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
def test_text_and_markdown(tmp_path):
    md = tmp_path / "a.md"
    md.write_text("# Heading\n\nSome body text.")
    assert "body text" in extract_text(md)


def test_docx_without_dependency(tmp_path):
    docx = tmp_path / "d.docx"
    xml = ('<?xml version="1.0"?><w:document xmlns:w="x"><w:body>'
           '<w:p><w:r><w:t>Hello </w:t><w:t>world</w:t></w:r></w:p>'
           '<w:p><w:r><w:t>Line two</w:t></w:r></w:p></w:body></w:document>')
    with zipfile.ZipFile(docx, "w") as archive:
        archive.writestr("word/document.xml", xml)
    text = extract_text(docx)
    assert "Hello world" in text and "Line two" in text


def test_docx_rejects_non_zip(tmp_path):
    fake = tmp_path / "fake.docx"
    fake.write_text("not a zip")
    with pytest.raises(ExtractionError, match="docx"):
        extract_text(fake)


def test_html_strips_scripts(tmp_path):
    html = tmp_path / "p.html"
    html.write_text("<html><head><style>x{}</style></head><body>"
                    "<p>Visible</p><script>hidden()</script>"
                    "<div>Also visible</div></body></html>")
    text = extract_text(html)
    assert "Visible" in text and "Also visible" in text
    assert "hidden" not in text and "x{}" not in text


def test_unsupported_type(tmp_path):
    weird = tmp_path / "x.xyz"
    weird.write_text("data")
    assert not is_supported(weird)
    with pytest.raises(ExtractionError, match="unsupported"):
        extract_text(weird)


def test_pdf_without_backend_guides(tmp_path, monkeypatch):
    import builtins
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)
    real_import = builtins.__import__

    def blocking(name, *args, **kwargs):
        if name == "pypdf":
            raise ImportError("blocked")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocking)
    pdf = tmp_path / "d.pdf"
    pdf.write_bytes(b"%PDF-1.4 ...")
    with pytest.raises(ExtractionError, match="pdftotext|pypdf"):
        extract_text(pdf)


def test_supported_suffixes_include_common_formats():
    suffixes = supported_suffixes()
    for expected in (".txt", ".md", ".pdf", ".docx", ".html"):
        assert expected in suffixes


# ---------------------------------------------------------------------------
# Folder watcher
# ---------------------------------------------------------------------------
@pytest.fixture()
def store(tmp_path):
    return KnowledgeStore(tmp_path / "k.db", HashingEmbedder(128),
                          chunk_chars=400, chunk_overlap=50)


@pytest.fixture()
def bus():
    bus = EventBus()
    bus.start()
    yield bus
    bus.stop()


def _watcher(watch_dir, store, interval=30.0):
    return KnowledgeWatchModule(
        KnowledgeConfig(watch_paths=[str(watch_dir)],
                        watch_interval_s=interval),
        FilesConfig(allowed_roots=[str(watch_dir.parent)]),
        store,
    )


def test_watch_queues_supported_files_without_ingesting(tmp_path, store):
    watch = tmp_path / "docs"
    watch.mkdir()
    (watch / "a.md").write_text("First document about penguins.")
    (watch / "b.txt").write_text("Second document about glaciers.")
    (watch / "ignore.xyz").write_text("unsupported")
    watcher = _watcher(watch, store)
    assert watcher.scan_once() == 2          # queued, not ingested
    assert store.documents() == []           # nothing written without approval
    titles = {p["title"] for p in watcher.pending()}
    assert titles == {"a.md", "b.txt"}


def test_approve_ingests_the_reviewed_content(tmp_path, store):
    watch = tmp_path / "docs"
    watch.mkdir()
    (watch / "a.md").write_text("stable content")
    watcher = _watcher(watch, store)
    watcher.scan_once()
    [pending] = watcher.pending()
    assert watcher.approve(pending["id"]) is True
    assert [doc.title for doc in store.documents()] == ["a.md"]
    assert watcher.pending() == []


def test_reject_discards_and_does_not_requeue(tmp_path, store):
    watch = tmp_path / "docs"
    watch.mkdir()
    (watch / "a.md").write_text("junk content")
    watcher = _watcher(watch, store)
    watcher.scan_once()
    [pending] = watcher.pending()
    assert watcher.reject(pending["id"]) is True
    assert store.documents() == []
    assert watcher.scan_once() == 0          # same content not re-offered
    assert watcher.pending() == []


def test_unknown_quarantine_id_is_refused(tmp_path, store):
    watcher = _watcher(tmp_path, store)
    assert watcher.approve("q999") is False
    assert watcher.reject("q999") is False


def test_watch_is_idempotent(tmp_path, store):
    watch = tmp_path / "docs"
    watch.mkdir()
    (watch / "a.md").write_text("stable content")
    watcher = _watcher(watch, store)
    assert watcher.scan_once() == 1
    assert watcher.scan_once() == 0          # already pending → no re-queue
    watcher.approve(watcher.pending()[0]["id"])
    assert watcher.scan_once() == 0          # already ingested → no re-queue
    (watch / "a.md").write_text("changed content now")
    assert watcher.scan_once() == 1          # changed → queued again


def test_watch_refuses_paths_outside_allowed_roots(tmp_path, store):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("should not be read")
    watcher = KnowledgeWatchModule(
        KnowledgeConfig(watch_paths=[str(outside)]),
        FilesConfig(allowed_roots=[str(tmp_path / "allowed")]),
        store,
    )
    assert watcher.scan_once() == 0          # dir dropped at construction
    assert store.documents() == []


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_watch_announces_quarantine_and_ingest_on_the_bus(tmp_path, store, bus):
    watch = tmp_path / "docs"
    watch.mkdir()
    (watch / "a.md").write_text("hello")
    watcher = _watcher(watch, store)
    seen = []
    from digital_twin.core.events import Topics
    bus.subscribe(Topics.MODULE, seen.append)
    watcher.start(bus)
    try:
        watcher.scan_once()
        assert _wait_for(lambda: any(
            event.payload.get("event") == "quarantined"
            and event.payload.get("title") == "a.md" for event in seen))
        watcher.approve(watcher.pending()[0]["id"])
        assert _wait_for(lambda: any(
            event.payload.get("event") == "ingested"
            and event.payload.get("title") == "a.md" for event in seen))
    finally:
        watcher.stop()


def test_edited_file_replaces_stale_version(tmp_path, store):
    watch = tmp_path / "docs"
    watch.mkdir()
    (watch / "a.md").write_text("Version one about penguins.")
    watcher = _watcher(watch, store)
    watcher.scan_once()
    watcher.approve(watcher.pending()[0]["id"])
    (watch / "a.md").write_text("Version two about penguins, corrected.")
    watcher.scan_once()
    watcher.approve(watcher.pending()[0]["id"])
    docs = store.documents()
    assert len(docs) == 1                    # replaced, not accumulated
    assert docs[0].title == "a.md"
    hits = store.search("penguins", top_k=5)
    assert all("Version two" in hit.content for hit in hits)


def test_chat_notes_still_accumulate(store):
    store.ingest(title="note", text="First separate note.", source="chat")
    store.ingest(title="note", text="Second separate note.", source="chat")
    assert len(store.documents()) == 2       # chat source never replaces
