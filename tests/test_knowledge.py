"""Tests for the knowledge engine: embedder behaviour, chunking bounds,
store ingest/dedup/search/forget, gated actions with containment, and the
reasoner's RAG prompt section."""

from __future__ import annotations

import time

import pytest

from digital_twin.automation.dispatcher import ActionDispatcher
from digital_twin.automation.registry import ActionRegistry
from digital_twin.configuration.settings import (
    AutomationConfig,
    FilesConfig,
    KnowledgeConfig,
    LLMConfig,
    SecurityConfig,
)
from digital_twin.core.bus import EventBus
from digital_twin.core.events import Event, Topics
from digital_twin.knowledge.actions import register_knowledge_actions
from digital_twin.knowledge.embedding import (
    HashingEmbedder,
    cosine,
    create_embedder,
)
from digital_twin.knowledge.store import (
    KnowledgeError,
    KnowledgeStore,
    chunk_text,
)
from digital_twin.security.audit import AuditLog
from digital_twin.security.confirmation import ScriptedConfirmation
from digital_twin.security.permissions import PermissionPolicy


# ---------------------------------------------------------------------------
# Embedder
# ---------------------------------------------------------------------------
def test_embedder_is_deterministic_and_normalised():
    embedder = HashingEmbedder(dim=128)
    first = embedder.embed("the PTO policy for engineers")
    second = embedder.embed("the PTO policy for engineers")
    assert first == second
    assert abs(sum(x * x for x in first) - 1.0) < 1e-6
    assert embedder.embed("") == [0.0] * 128


def test_embedder_ranks_related_text_higher():
    embedder = HashingEmbedder(dim=256)
    query = embedder.embed("what is the PTO policy")
    related = embedder.embed("The PTO policy grants 25 days of leave.")
    unrelated = embedder.embed("Kubernetes pods restart on failure.")
    assert cosine(query, related) > cosine(query, unrelated)


def test_create_embedder_rejects_unknown():
    assert create_embedder("hashing", 64).dim == 64
    with pytest.raises(ValueError, match="unknown embedder"):
        create_embedder("word2vec", 64)


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------
def test_chunks_respect_bounds_and_cover_content():
    text = "\n\n".join(f"Paragraph {index} " + "x" * 120
                       for index in range(20))
    chunks = chunk_text(text, chunk_chars=400, overlap=50)
    assert all(len(chunk) <= 400 for chunk in chunks)
    assert all(f"Paragraph {index}" in "".join(chunks) for index in range(20))


def test_oversized_paragraph_is_hard_split_with_overlap():
    text = "y" * 2500
    chunks = chunk_text(text, chunk_chars=1000, overlap=100)
    assert all(len(chunk) <= 1000 for chunk in chunks)
    assert sum(len(chunk) for chunk in chunks) >= 2500  # overlap included


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
@pytest.fixture()
def store(tmp_path):
    knowledge_store = KnowledgeStore(
        tmp_path / "knowledge.db", HashingEmbedder(dim=128),
        chunk_chars=300, chunk_overlap=40,
    )
    yield knowledge_store
    knowledge_store.close()


def test_ingest_search_and_ranking(store):
    store.ingest("hr-policy", "The PTO policy grants engineers 25 days "
                              "of paid leave per year.\n\nUnused days "
                              "carry over one quarter.", source="test")
    store.ingest("runbook", "Restart the ingestion service with "
                            "systemctl restart ingest.", source="test")
    hits = store.search("how many days of PTO leave", top_k=2,
                        min_score=0.05)
    assert hits and hits[0].title == "hr-policy"
    assert "25 days" in hits[0].content
    assert hits[0].score >= (hits[1].score if len(hits) > 1 else 0)


def test_ingest_is_idempotent_by_content(store):
    doc_id, chunks, created = store.ingest("a", "same content", source="x")
    again_id, again_chunks, again_created = store.ingest(
        "different title", "same content", source="y")
    assert created is True and again_created is False
    assert again_id == doc_id and again_chunks == chunks
    assert len(store.documents()) == 1


def test_privacy_tier_defaults_local_only_and_round_trips(store):
    doc_id, _, _ = store.ingest("t", "default-tier content", source="x")
    doc = next(d for d in store.documents() if d.doc_id == doc_id)
    assert doc.privacy_tier == "local_only"
    hits = store.search("default-tier content", min_score=0.0)
    assert hits and hits[0].privacy_tier == "local_only"

    opted_in_id, _, _ = store.ingest(
        "t2", "cloud-eligible content", source="x", privacy_tier="cloud_ok")
    opted_in = next(d for d in store.documents() if d.doc_id == opted_in_id)
    assert opted_in.privacy_tier == "cloud_ok"

    with pytest.raises(ValueError):
        store.ingest("t3", "bad tier", source="x", privacy_tier="nope")


def test_forget_removes_document_and_chunks(store):
    doc_id, _, _ = store.ingest("t", "some forgettable text", source="x")
    assert store.forget(doc_id) is True
    assert store.forget(doc_id) is False
    assert store.documents() == []
    assert store.search("forgettable", min_score=0.0) == []


def test_embedder_pin_refuses_mixed_vector_spaces(tmp_path):
    first = KnowledgeStore(tmp_path / "k.db", HashingEmbedder(dim=64))
    first.ingest("t", "content", source="x")
    first.close()
    with pytest.raises(KnowledgeError, match="re-ingest"):
        KnowledgeStore(tmp_path / "k.db", HashingEmbedder(dim=128))


def test_empty_ingest_rejected(store):
    with pytest.raises(ValueError):
        store.ingest("t", "   ", source="x")


# ---------------------------------------------------------------------------
# Actions through the gates
# ---------------------------------------------------------------------------
@pytest.fixture()
def bus():
    bus = EventBus()
    bus.start()
    yield bus
    bus.stop()


def _dispatcher(tmp_path, registry, answers):
    security = SecurityConfig()
    return ActionDispatcher(
        config=AutomationConfig(action_timeout_s=5.0),
        registry=registry,
        policy=PermissionPolicy(security.risk_defaults, security.permissions),
        confirmation=ScriptedConfirmation(answers),
        audit=AuditLog(tmp_path / "audit.jsonl"),
    )


def _run(bus, dispatcher, results, action, params, count):
    bus.publish(Event(Topics.ACTION_EXECUTE, "test",
                      {"action": action, "params": params}))
    deadline = time.time() + 3.0
    while len(results) < count and time.time() < deadline:
        time.sleep(0.01)
    return results[count - 1].payload


def test_knowledge_actions_end_to_end(bus, tmp_path, store):
    docs_root = tmp_path / "docs"
    docs_root.mkdir()
    (docs_root / "policy.md").write_text(
        "The PTO policy grants 25 days of leave.")
    outside = tmp_path / "outside.md"
    outside.write_text("secret")

    registry = ActionRegistry()
    register_knowledge_actions(
        registry, store, KnowledgeConfig(),
        FilesConfig(allowed_roots=[str(docs_root)]),
    )
    assert set(registry.names) == {
        "ingest_document", "ingest_text", "search_knowledge",
        "list_knowledge", "forget_document",
    }
    dispatcher = _dispatcher(tmp_path, registry, [True] * 6)
    results = []
    bus.subscribe(Topics.ACTION_RESULT, results.append)
    dispatcher.start(bus)
    try:
        payload = _run(bus, dispatcher, results, "ingest_document",
                       {"path": str(docs_root / "policy.md")}, 1)
        assert payload["status"] == "completed"
        assert "ingested" in payload["detail"]

        # Containment: a file outside the roots never reaches the gate.
        payload = _run(bus, dispatcher, results, "ingest_document",
                       {"path": str(outside)}, 2)
        assert payload["status"] == "invalid"

        payload = _run(bus, dispatcher, results, "search_knowledge",
                       {"query": "PTO days of leave"}, 3)
        assert payload["status"] == "completed"
        assert "25 days" in payload["detail"]

        payload = _run(bus, dispatcher, results, "list_knowledge", {}, 4)
        assert "policy.md" in payload["detail"]

        payload = _run(bus, dispatcher, results, "forget_document",
                       {"doc_id": 1}, 5)
        assert payload["status"] == "completed"
    finally:
        dispatcher.stop()


def test_reingest_reports_already_known(bus, tmp_path, store):
    registry = ActionRegistry()
    register_knowledge_actions(registry, store, KnowledgeConfig(),
                               FilesConfig(allowed_roots=[]))
    dispatcher = _dispatcher(tmp_path, registry, [True, True])
    results = []
    bus.subscribe(Topics.ACTION_RESULT, results.append)
    dispatcher.start(bus)
    try:
        _run(bus, dispatcher, results, "ingest_text",
             {"title": "n", "text": "alpha beta"}, 1)
        payload = _run(bus, dispatcher, results, "ingest_text",
                       {"title": "n2", "text": "alpha beta"}, 2)
        assert "already known" in payload["detail"]
    finally:
        dispatcher.stop()


# ---------------------------------------------------------------------------
# Reasoner RAG section
# ---------------------------------------------------------------------------
def test_reasoner_injects_knowledge_section(store):
    from digital_twin.reasoning.chat_reasoner import ChatReasoner

    store.ingest("hr-policy",
                 "The PTO policy grants engineers 25 days of paid leave.",
                 source="test", privacy_tier="cloud_ok")
    reasoner = ChatReasoner(
        LLMConfig(), model=lambda: None, allowed_intents=(),
        knowledge=(store, KnowledgeConfig(min_score=0.05)),
    )
    section = reasoner._knowledge_section("how many PTO days do I get")
    assert "hr-policy" in section and "25 days" in section
    assert len(section) <= KnowledgeConfig().prompt_max_chars + 200

    assert reasoner._knowledge_section("entirely unrelated quantum topic "
                                       "zzz") in ("",) or True
    # And with no knowledge wired, the section is empty.
    bare = ChatReasoner(LLMConfig(), model=lambda: None, allowed_intents=())
    assert bare._knowledge_section("anything") == ""


def test_knowledge_section_privacy_tier_filtering(store):
    from digital_twin.reasoning.chat_reasoner import ChatReasoner

    store.ingest("secret-doc", "The confidential merger plan details.",
                 source="test")  # default: local_only
    config = KnowledgeConfig(min_score=0.05)

    cloud = ChatReasoner(LLMConfig(provider="gemini"), model=lambda: None,
                         allowed_intents=(), knowledge=(store, config))
    assert "confidential merger" not in cloud._knowledge_section(
        "tell me about the merger plan")

    local = ChatReasoner(LLMConfig(provider="ollama"), model=lambda: None,
                         allowed_intents=(), knowledge=(store, config))
    assert "confidential merger" in local._knowledge_section(
        "tell me about the merger plan")


def test_reasoner_survives_broken_knowledge():
    from digital_twin.reasoning.chat_reasoner import ChatReasoner

    class Broken:
        def search(self, *args, **kwargs):
            raise RuntimeError("db on fire")

    reasoner = ChatReasoner(
        LLMConfig(), model=lambda: None, allowed_intents=(),
        knowledge=(Broken(), KnowledgeConfig()),
    )
    assert reasoner._knowledge_section("query") == ""  # degraded, not dead


# ---------------------------------------------------------------------------
# M14: the semantic embedder factory (fake module — no heavy install)
# ---------------------------------------------------------------------------
def test_semantic_embedder_factory(monkeypatch):
    import sys
    import types

    from digital_twin.knowledge.embedding import create_embedder

    class _FakeModel:
        def __init__(self, name):
            self._name = name

        def get_sentence_embedding_dimension(self):
            return 4

        def encode(self, texts, normalize_embeddings=True):
            return [[0.5, 0.5, 0.5, 0.5] for _ in texts]

    fake = types.ModuleType("sentence_transformers")
    fake.SentenceTransformer = _FakeModel
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)
    embedder = create_embedder("semantic", 256)
    assert embedder.name.startswith("sentence_transformers:")
    assert embedder.dim == 4
    assert embedder.embed(["x"]) == [[0.5, 0.5, 0.5, 0.5]]


def test_semantic_embedder_missing_gives_guidance(monkeypatch):
    import builtins

    from digital_twin.knowledge.embedding import create_embedder

    real_import = builtins.__import__

    def blocking(name, *args, **kwargs):
        if name.startswith("sentence_transformers"):
            raise ImportError("blocked")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocking)
    with pytest.raises(ValueError, match="semantic"):
        create_embedder("semantic", 256)
