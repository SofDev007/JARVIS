#!/usr/bin/env python3
"""M15 demo — documents flow in by themselves; the dashboard shows depth.

Four acts, no hardware, no browser, no heavy deps:

1. **Format extraction** — a ``.docx`` (built on the fly: it's just a
   zip of XML) and an ``.html`` page are turned into clean text with
   zero dependencies; unsupported types are refused with guidance.
2. **Folder watching** — a watched directory is scanned: two documents
   ingested, an unsupported file skipped, a rescan does nothing
   (content-hash idempotence), and editing a file gets it re-ingested.
   Containment shown: a watch path outside ``files.allowed_roots`` is
   dropped, never read.
3. **Dashboard depth** — the live dashboard now serves ``/api/memory``
   and ``/api/knowledge`` panels; this script reads both like the page
   does.
4. **SSE** — instead of polling, ``/api/stream`` pushes: we open the
   stream, publish an event, and watch it arrive.

Run from the repository root::

    python examples/dashboard_depth_demo.py
"""

from __future__ import annotations

import http.client
import json
import sys
import time
import urllib.request
import zipfile
from pathlib import Path
from tempfile import mkdtemp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from digital_twin.configuration.settings import (  # noqa: E402
    DashboardConfig,
    FilesConfig,
    KnowledgeConfig,
)
from digital_twin.core.bus import EventBus  # noqa: E402
from digital_twin.core.events import Event, Topics  # noqa: E402
from digital_twin.core.registry import ModuleRegistry  # noqa: E402
from digital_twin.dashboard.module import DashboardModule  # noqa: E402
from digital_twin.knowledge.embedding import HashingEmbedder  # noqa: E402
from digital_twin.knowledge.extraction import (  # noqa: E402
    ExtractionError,
    extract_text,
)
from digital_twin.knowledge.store import KnowledgeStore  # noqa: E402
from digital_twin.knowledge.watch import KnowledgeWatchModule  # noqa: E402
from digital_twin.memory.store import MemoryStore  # noqa: E402


def _get(port: int, path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}",
                                timeout=5) as response:
        return json.loads(response.read())


def main() -> int:
    work = Path(mkdtemp(prefix="dtwin-m15-demo-"))

    print("=" * 68)
    print("Act 1 — format extraction, dependency-free")
    print("=" * 68)
    docx = work / "meeting_notes.docx"
    xml = ('<?xml version="1.0"?><w:document xmlns:w="x"><w:body>'
           '<w:p><w:r><w:t>Q3 planning: ship the sandbox, '
           'then the dashboard depth.</w:t></w:r></w:p>'
           '</w:body></w:document>')
    with zipfile.ZipFile(docx, "w") as archive:
        archive.writestr("word/document.xml", xml)
    print(f"    .docx  -> {extract_text(docx)[:60]!r}")
    html = work / "wiki.html"
    html.write_text("<html><body><p>Glaciers store 69% of the world's "
                    "fresh water.</p><script>tracking()</script></body></html>")
    print(f"    .html  -> {extract_text(html)!r}  (script stripped)")
    weird = work / "raw.xyz"
    weird.write_text("???")
    try:
        extract_text(weird)
    except ExtractionError as exc:
        print(f"    .xyz   -> refused: {str(exc)[:60]}…")

    print("\n" + "=" * 68)
    print("Act 2 — folder watching (standing consent via config)")
    print("=" * 68)
    watched = work / "inbox"
    watched.mkdir()
    (watched / "penguins.md").write_text("Emperor penguins dive 500 m.")
    (watched / "glaciers.txt").write_text("Glaciers calve icebergs.")
    (watched / "binary.xyz").write_text("skip me")
    store = KnowledgeStore(work / "k.db", HashingEmbedder(128))
    watcher = KnowledgeWatchModule(
        KnowledgeConfig(watch_paths=[str(watched)]),
        FilesConfig(allowed_roots=[str(work)]),
        store,
    )
    print(f"    first scan : {watcher.scan_once()} ingested "
          f"(the .xyz was skipped)")
    print(f"    rescan     : {watcher.scan_once()} ingested (idempotent)")
    (watched / "penguins.md").write_text("Emperor penguins dive 500 m. "
                                         "They fast for months.")
    print(f"    after edit : {watcher.scan_once()} re-ingested")
    rogue = KnowledgeWatchModule(
        KnowledgeConfig(watch_paths=["/etc"]),
        FilesConfig(allowed_roots=[str(work)]),
        store,
    )
    print(f"    watch /etc (outside allowed_roots): "
          f"{rogue.scan_once()} ingested — dropped at construction")

    print("\n" + "=" * 68)
    print("Act 3 — dashboard panels: /api/memory and /api/knowledge")
    print("=" * 68)
    bus = EventBus()
    bus.start()
    registry = ModuleRegistry(bus)
    memory = MemoryStore(work / "m.db")
    memory.add(kind="semantic", content="Boss prefers dark themes",
               source="demo")
    dashboard = DashboardModule(
        DashboardConfig(enabled=True, port=0), registry,
        memory_store=memory, knowledge_store=store,
    )
    registry.register(dashboard)
    dashboard.start(bus)
    port = dashboard.port
    memory_view = _get(port, "/api/memory")
    print(f"    /api/memory    -> {len(memory_view['entries'])} entries, "
          f"e.g. {memory_view['entries'][0]['content']!r}")
    knowledge_view = _get(port, "/api/knowledge")
    print(f"    /api/knowledge -> {len(knowledge_view['documents'])} docs "
          f"(embedder {knowledge_view['embedder']}): "
          + ", ".join(d["title"] for d in knowledge_view["documents"]))

    print("\n" + "=" * 68)
    print("Act 4 — SSE: the dashboard pushes instead of polling")
    print("=" * 68)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/api/stream")
    response = conn.getresponse()
    print(f"    /api/stream open ({response.getheader('Content-Type')})")
    bus.publish(Event(Topics.INTENT, "demo", {"intent": "sse_works"}))
    seen, deadline = "", time.time() + 6
    while time.time() < deadline and "sse_works" not in seen:
        seen += response.read(64).decode("utf-8", "replace")
    frame = next((line for line in seen.splitlines()
                  if line.startswith("data:") and "sse_works" in line), "")
    print(f"    pushed frame: {frame[:76]}…")
    conn.close()

    dashboard.stop()
    bus.stop()
    print(f"\nDemo complete. (Throwaway workspace at {work} — safe to delete.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
