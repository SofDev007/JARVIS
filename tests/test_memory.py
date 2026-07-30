"""Tests for memory primitives: codecs, store, working memory."""

from __future__ import annotations

import json
import threading

import pytest

from digital_twin.memory.codec import (
    CodecError,
    FernetCodec,
    PlainCodec,
    create_codec,
)
from digital_twin.memory.store import MemoryStore
from digital_twin.memory.working import WorkingMemory
from tests._keyfile import assert_key_owner_only


# ---------------------------------------------------------------------------
# Codecs
# ---------------------------------------------------------------------------
def test_plain_codec_roundtrip():
    codec = PlainCodec()
    assert codec.decode(codec.encode("héllo wörld")) == "héllo wörld"


def test_fernet_codec_roundtrip_and_key_permissions(tmp_path):
    key_path = tmp_path / "keys" / "memory.key"
    codec = create_codec(True, key_path)
    blob = codec.encode("secret preference")
    assert b"secret" not in blob
    assert codec.decode(blob) == "secret preference"
    assert key_path.exists()
    assert_key_owner_only(key_path)
    # Same key file → decodable by a new codec instance.
    assert FernetCodec(key_path).decode(blob) == "secret preference"


def test_fernet_wrong_key_raises(tmp_path):
    first = FernetCodec(tmp_path / "a.key")
    other = FernetCodec(tmp_path / "b.key")
    with pytest.raises(CodecError, match="decrypt"):
        other.decode(first.encode("secret"))


def test_encryption_without_library_fails_fast(tmp_path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_crypto(name, *args, **kwargs):
        if name.startswith("cryptography"):
            raise ImportError("gone")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_crypto)
    with pytest.raises(CodecError, match="cryptography"):
        create_codec(True, tmp_path / "k")


# ---------------------------------------------------------------------------
# Store CRUD
# ---------------------------------------------------------------------------
@pytest.fixture()
def store(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    yield store
    store.close()


def test_add_get_roundtrip_with_data_and_tags(store):
    record = store.add(
        kind="episodic",
        content="Action nav_key completed",
        data={"action": "nav_key", "duration_ms": 3.2},
        source="memory",
        importance=0.5,
        tags=("completed", "nav_key"),
    )
    fetched = store.get(record.id)
    assert fetched == record
    assert fetched.data["duration_ms"] == 3.2
    assert fetched.tags == ("completed", "nav_key")


def test_validation_rejects_bad_records(store):
    with pytest.raises(ValueError):
        store.add(kind="nope", content="x")
    with pytest.raises(ValueError):
        store.add(kind="semantic", content="   ")
    with pytest.raises(ValueError):
        store.add(kind="semantic", content="x", importance=1.5)


def test_persistence_across_reopen(tmp_path):
    path = tmp_path / "memory.db"
    store = MemoryStore(path)
    record = store.add(kind="semantic", content="Boss prefers dark themes")
    store.close()

    reopened = MemoryStore(path)
    assert reopened.get(record.id).content == "Boss prefers dark themes"
    reopened.close()


def test_list_count_delete_clear(store):
    for i in range(5):
        store.add(kind="episodic", content=f"event {i}")
    store.add(kind="semantic", content="a fact")

    assert store.count() == 6
    assert store.count("episodic") == 5
    newest = store.list(kind="episodic", limit=2)
    assert [record.content for record in newest] == ["event 4", "event 3"]

    assert store.delete(newest[0].id) is True
    assert store.delete("nonexistent") is False
    assert store.clear(kind="episodic") == 4  # 5 added, 1 already deleted
    assert store.count() == 1  # the semantic fact survives


def test_update_edits_content_importance_tags(store):
    record = store.add(kind="semantic", content="likes tea", importance=0.4)
    updated = store.update(record.id, content="likes coffee",
                           importance=0.9, tags=("beverage",))
    fetched = store.get(record.id)
    assert fetched.content == "likes coffee"
    assert fetched.importance == 0.9
    assert fetched.tags == ("beverage",)
    assert updated == fetched
    with pytest.raises(KeyError):
        store.update("nonexistent", content="x")
    with pytest.raises(ValueError):
        store.update(record.id, importance=2.0)


def test_store_is_thread_safe(store):
    def writer(worker: int) -> None:
        for i in range(30):
            store.add(kind="episodic", content=f"w{worker} event {i}")

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert store.count("episodic") == 120


# ---------------------------------------------------------------------------
# Search ranking
# ---------------------------------------------------------------------------
def test_search_ranks_text_match_recency_importance(store):
    old = store.add(kind="episodic", content="presentation escape pressed")
    fresh = store.add(kind="episodic", content="presentation escape pressed")
    unrelated = store.add(kind="episodic", content="volume changed")
    # Backdate the old record by 30 days.
    with store._lock, store._connection:
        store._connection.execute(
            "UPDATE memories SET created_at = created_at - ? WHERE id = ?",
            (30 * 86400.0, old.id),
        )

    hits = store.search("presentation escape", limit=10)
    ids = [hit.record.id for hit in hits]
    assert unrelated.id not in ids
    assert ids.index(fresh.id) < ids.index(old.id)  # recency wins the tie


def test_search_importance_breaks_ties(store):
    minor = store.add(kind="episodic", content="clipboard set", importance=0.1)
    major = store.add(kind="episodic", content="clipboard set", importance=0.9)
    hits = store.search("clipboard", limit=2, now=minor.created_at)
    assert hits[0].record.id == major.id


def test_search_filters_kinds_and_matches_tags(store):
    store.add(kind="episodic", content="did a thing", tags=("presentation",))
    fact = store.add(kind="semantic", content="prefers big fonts",
                     tags=("presentation",))
    hits = store.search("presentation", kinds=("semantic",))
    assert [hit.record.id for hit in hits] == [fact.id]


def test_search_bumps_access_stats(store):
    record = store.add(kind="semantic", content="unique needle here")
    store.search("needle")
    store.search("needle")
    assert store.get(record.id).access_count == 2


def test_search_rejects_empty_query(store):
    with pytest.raises(ValueError):
        store.search("   ")


# ---------------------------------------------------------------------------
# Pruning and export
# ---------------------------------------------------------------------------
def test_prune_retention_and_cap(store):
    keep = store.add(kind="episodic", content="recent important", importance=0.9)
    expired = store.add(kind="episodic", content="ancient history")
    with store._lock, store._connection:
        store._connection.execute(
            "UPDATE memories SET created_at = created_at - ? WHERE id = ?",
            (120 * 86400.0, expired.id),
        )
    for i in range(8):
        store.add(kind="episodic", content=f"filler {i}", importance=0.2)

    deleted = store.prune(kind="episodic", max_records=5, retention_days=90)
    assert deleted == 5  # 1 expired + 4 over cap
    assert store.count("episodic") == 5
    assert store.get(keep.id) is not None       # high importance survived
    assert store.get(expired.id) is None


def test_prune_never_touches_other_kinds(store):
    store.add(kind="semantic", content="a fact to keep")
    for i in range(5):
        store.add(kind="episodic", content=f"e{i}")
    store.prune(kind="episodic", max_records=2)
    assert store.count("semantic") == 1


def test_export_is_decoded_json(tmp_path):
    store = MemoryStore(tmp_path / "m.db")
    store.add(kind="semantic", content="exportable fact", tags=("x",))
    dump = store.export()
    json.dumps(dump)  # serialisable
    assert dump[0]["content"] == "exportable fact"
    store.close()


# ---------------------------------------------------------------------------
# Encrypted store end to end
# ---------------------------------------------------------------------------
def test_encrypted_store_keeps_content_out_of_the_file(tmp_path):
    db_path = tmp_path / "memory.db"
    store = MemoryStore(db_path, codec=create_codec(True, tmp_path / "k"))
    store.add(kind="semantic", content="ultrasecret preference",
              data={"note": "alsosecret"})
    # Search still works (decrypt-and-scan path).
    hits = store.search("ultrasecret")
    assert len(hits) == 1
    store.close()

    raw = db_path.read_bytes()
    for wal in (db_path.with_suffix(".db-wal"),):
        if wal.exists():
            raw += wal.read_bytes()
    assert b"ultrasecret" not in raw
    assert b"alsosecret" not in raw


# ---------------------------------------------------------------------------
# Working memory
# ---------------------------------------------------------------------------
def test_working_memory_capacity_and_window():
    working = WorkingMemory(capacity=3, window_s=60)
    for i in range(5):
        working.add("t", f"item {i}", now=1000.0 + i)
    recent = working.recent(10, now=1010.0)
    assert [item.summary for item in recent] == ["item 2", "item 3", "item 4"]

    stale = WorkingMemory(capacity=10, window_s=5)
    stale.add("t", "old", now=1000.0)
    stale.add("t", "new", now=1004.0)
    assert [i.summary for i in stale.recent(10, now=1008.0)] == ["new"]

    stale.clear()
    assert len(stale) == 0
