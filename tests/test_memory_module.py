"""Tests for the memory module, the user-control CLI, and the full chain."""

from __future__ import annotations

import json
import time

import pytest

from digital_twin.configuration.settings import (
    AutomationConfig,
    IntentConfig,
    MemoryConfig,
)
from digital_twin.core.bus import EventBus
from digital_twin.core.events import Event, Topics
from digital_twin.memory import cli
from digital_twin.memory.module import MemoryModule
from digital_twin.memory.store import MemoryStore
from digital_twin.reasoning.intent import IntentEngine


@pytest.fixture()
def bus():
    bus = EventBus()
    bus.start()
    yield bus
    bus.stop()


@pytest.fixture()
def module(bus, tmp_path):
    module = MemoryModule(MemoryConfig(db_path=str(tmp_path / "memory.db")))
    module.start(bus)
    yield module
    module.stop()


def _result(status="completed", action="nav_key", intent="next_slide",
            context="presentation", detail="pressed right x1") -> Event:
    return Event(Topics.ACTION_RESULT, "actions", {
        "status": status, "action": action, "intent": intent,
        "context": context, "detail": detail,
        "intent_event": "i1", "perception_event": "p1",
    })


def _wait(predicate, timeout=5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------------------
# Module behaviour
# ---------------------------------------------------------------------------
def test_action_results_become_searchable_episodic_records(bus, module):
    stored: list[Event] = []
    bus.subscribe(Topics.MEMORY, stored.append)

    bus.publish(_result())
    assert bus.flush(timeout=2.0)

    (record,) = module.store.list(kind="episodic")
    assert "nav_key completed" in record.content
    assert "next_slide" in record.content
    assert record.data["perception_event"] == "p1"  # provenance preserved
    assert set(record.tags) == {"completed", "nav_key", "presentation"}

    hits = module.store.search("presentation nav_key")
    assert hits[0].record.id == record.id
    (announcement,) = stored
    assert announcement.payload["memory_id"] == record.id


def test_refusals_stored_with_higher_importance_than_successes(bus, module):
    bus.publish(_result(status="completed"))
    bus.publish(_result(status="rejected", detail="confirmation denied"))
    bus.flush(timeout=2.0)

    by_status = {
        record.tags[0]: record for record in module.store.list(kind="episodic")
    }
    assert by_status["rejected"].importance > by_status["completed"].importance


def test_working_memory_collects_activity_feed(bus, module):
    bus.publish(Event(Topics.CONTEXT, "screen_context", {"context": "media"}))
    bus.publish(Event(Topics.INTENT, "intent", {
        "intent": "like", "context": "media", "gesture": "thumbs_up"}))
    bus.publish(_result(action="log_message", intent="like", context="media"))
    bus.flush(timeout=2.0)

    topics = [item.topic for item in module.working.recent(10)]
    assert topics == [Topics.CONTEXT, Topics.INTENT, Topics.ACTION_RESULT]


def test_pause_stops_recording_and_clears_working(bus, module):
    bus.publish(_result())
    bus.flush(timeout=2.0)
    module.pause()
    assert len(module.working) == 0

    bus.publish(_result())
    bus.flush(timeout=2.0)
    assert module.store.count("episodic") == 1  # nothing recorded while paused

    module.resume()
    bus.publish(_result())
    bus.flush(timeout=2.0)
    assert module.store.count("episodic") == 2


def test_remember_fact_and_metrics(bus, module):
    module.remember_fact("Boss prefers dark themes", tags=("ui",))
    metrics = module.status().metrics
    assert metrics["semantic"] == 1
    assert module.store.list(kind="semantic")[0].source == "user"


def test_prune_once_enforces_config(bus, tmp_path):
    module = MemoryModule(MemoryConfig(
        db_path=str(tmp_path / "m.db"),
        episodic_max_records=10,
    ))
    module.start(bus)
    try:
        for i in range(15):
            bus.publish(_result(detail=f"event {i}"))
        assert _wait(lambda: module.store.count("episodic") == 15)
        assert module._prune_once() == 5
        assert module.store.count("episodic") == 10
    finally:
        module.stop()


def test_store_survives_module_restart(bus, tmp_path):
    config = MemoryConfig(db_path=str(tmp_path / "m.db"))
    module = MemoryModule(config)
    module.start(bus)
    bus.publish(_result())
    bus.flush(timeout=2.0)
    module.stop()

    module.start(bus)
    try:
        assert module.store.count("episodic") == 1
    finally:
        module.stop()


# ---------------------------------------------------------------------------
# CLI (user review/edit/delete controls)
# ---------------------------------------------------------------------------
@pytest.fixture()
def cli_env(tmp_path):
    """A config file pointing at a seeded store."""
    db_path = tmp_path / "memory.db"
    store = MemoryStore(db_path)
    kept = store.add(kind="episodic",
                     content="Action press_keys rejected in presentation",
                     tags=("rejected",))
    store.add(kind="semantic", content="Boss prefers concise answers")
    store.close()

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"memory:\n  db_path: {db_path}\n", encoding="utf-8"
    )
    return config_path, kept


def _run(config_path, *argv) -> tuple[int, str]:
    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = cli.main(["--config", str(config_path), *argv])
    return code, buffer.getvalue()


def test_cli_list_search_show(cli_env):
    config_path, kept = cli_env
    code, out = _run(config_path, "list")
    assert code == 0 and "press_keys rejected" in out and "concise answers" in out

    code, out = _run(config_path, "search", "rejected presentation")
    assert code == 0 and "press_keys rejected" in out

    code, out = _run(config_path, "show", kept.id[:12])  # prefix resolution
    assert code == 0 and "tags: rejected" in out


def test_cli_remember_edit_delete(cli_env):
    config_path, kept = cli_env
    code, out = _run(config_path, "remember", "Prefers dark themes",
                     "--tags", "ui")
    assert code == 0 and "Stored:" in out

    code, out = _run(config_path, "edit", kept.id, "--importance", "0.9")
    assert code == 0 and "imp=0.90" in out

    code, _ = _run(config_path, "delete", kept.id)
    assert code == 0
    code, _ = _run(config_path, "delete", kept.id)
    assert code == 1  # already gone


def test_cli_clear_requires_yes_and_export(cli_env, tmp_path):
    config_path, _ = cli_env
    code, _ = _run(config_path, "clear", "--kind", "episodic")
    assert code == 2  # refused without --yes
    code, out = _run(config_path, "clear", "--kind", "episodic", "--yes")
    assert code == 0 and "Deleted 1" in out

    out_file = tmp_path / "dump.json"
    code, out = _run(config_path, "export", "--out", str(out_file))
    assert code == 0
    dump = json.loads(out_file.read_text())
    assert len(dump) == 1 and dump[0]["kind"] == "semantic"

    code, out = _run(config_path, "stats")
    assert code == 0 and "semantic   1" in out


# ---------------------------------------------------------------------------
# Full chain: gesture → intent → action → episodic memory
# ---------------------------------------------------------------------------
def test_full_chain_lands_in_episodic_memory(bus, tmp_path):
    from tests.test_actions import Harness

    memory = MemoryModule(MemoryConfig(db_path=str(tmp_path / "memory.db")))
    memory.start(bus)
    engine = IntentEngine(IntentConfig(
        default_context="media", mappings={"media": {"thumbs_up": "go"}}))
    engine.start(bus)
    harness = Harness(bus, tmp_path)
    try:
        gesture = Event(Topics.GESTURE, "gesture", {
            "gesture": "thumbs_up", "confidence": 0.97,
            "hand": "right", "repeat": False,
        })
        bus.publish(gesture)
        assert _wait(lambda: memory.store.count("episodic") == 1)
        bus.flush(timeout=2.0)

        (record,) = memory.store.list(kind="episodic")
        assert "record completed" in record.content
        # Provenance chain intact: memory → perception event.
        assert record.data["perception_event"] == gesture.event_id
        assert memory.store.search("intent go")[0].record.id == record.id
    finally:
        harness.dispatcher.stop()
        engine.stop()
        memory.stop()
