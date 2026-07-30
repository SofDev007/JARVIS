"""Tests for the context-aware intent engine."""

from __future__ import annotations

import pytest

from digital_twin.configuration.settings import IntentConfig
from digital_twin.core.bus import EventBus
from digital_twin.core.events import Event, Topics
from digital_twin.reasoning.intent import IntentEngine


@pytest.fixture()
def bus():
    bus = EventBus()
    bus.start()
    yield bus
    bus.stop()


@pytest.fixture()
def engine(bus):
    engine = IntentEngine(
        IntentConfig(
            default_context="desktop",
            mappings={
                "presentation": {"thumbs_up": "next_slide"},
                "media": {"thumbs_up": "like", "peace": "play_pause"},
                "desktop": {},
                "*": {"open_palm": "assistant_attention"},
            },
        )
    )
    engine.start(bus)
    yield engine
    engine.stop()


def _gesture(gesture: str, hand: str = "right", confidence: float = 0.9) -> Event:
    return Event(
        Topics.GESTURE,
        "gesture",
        {"gesture": gesture, "confidence": confidence, "hand": hand, "repeat": False},
    )


def _record(bus: EventBus) -> list[Event]:
    intents: list[Event] = []
    bus.subscribe(Topics.INTENT, intents.append)
    return intents


def test_same_gesture_maps_differently_per_context(bus, engine):
    intents = _record(bus)

    engine.set_context("presentation")
    bus.publish(_gesture("thumbs_up"))
    bus.flush(timeout=2.0)

    engine.set_context("media")
    bus.publish(_gesture("thumbs_up"))
    bus.flush(timeout=2.0)

    assert [e.payload["intent"] for e in intents] == ["next_slide", "like"]
    assert [e.payload["context"] for e in intents] == ["presentation", "media"]


def test_unmapped_gesture_produces_no_intent(bus, engine):
    intents = _record(bus)
    bus.publish(_gesture("rock"))  # not mapped anywhere
    bus.flush(timeout=2.0)
    assert intents == []


def test_wildcard_context_fallback(bus, engine):
    intents = _record(bus)
    engine.set_context("desktop")  # empty context table → falls back to "*"
    bus.publish(_gesture("open_palm"))
    bus.flush(timeout=2.0)
    assert [e.payload["intent"] for e in intents] == ["assistant_attention"]


def test_active_context_wins_over_wildcard(bus):
    engine = IntentEngine(
        IntentConfig(
            default_context="media",
            mappings={"media": {"open_palm": "pause"}, "*": {"open_palm": "attention"}},
        )
    )
    engine.start(bus)
    try:
        intents = _record(bus)
        bus.publish(_gesture("open_palm"))
        bus.flush(timeout=2.0)
        assert [e.payload["intent"] for e in intents] == ["pause"]
    finally:
        engine.stop()


def test_context_changes_via_bus_event(bus, engine):
    intents = _record(bus)
    bus.publish(Event(Topics.CONTEXT, "screen", {"context": "presentation"}))
    bus.flush(timeout=2.0)
    assert engine.context == "presentation"

    bus.publish(_gesture("thumbs_up"))
    bus.flush(timeout=2.0)
    assert [e.payload["intent"] for e in intents] == ["next_slide"]


def test_aliases_are_resolved(bus, engine):
    intents = _record(bus)
    bus.publish(_gesture("high_five"))  # alias of open_palm
    bus.flush(timeout=2.0)
    assert [e.payload["intent"] for e in intents] == ["assistant_attention"]


def test_paused_engine_emits_nothing_and_resume_restores(bus, engine):
    intents = _record(bus)
    engine.pause()
    engine.set_context("presentation")
    bus.publish(_gesture("thumbs_up"))
    bus.flush(timeout=2.0)
    assert intents == []

    engine.resume()
    bus.publish(_gesture("thumbs_up"))
    bus.flush(timeout=2.0)
    assert [e.payload["intent"] for e in intents] == ["next_slide"]


def test_malformed_events_are_ignored(bus, engine):
    intents = _record(bus)
    bus.publish(Event(Topics.GESTURE, "gesture", {"nope": True}))
    bus.publish(Event(Topics.CONTEXT, "screen", {"context": 42}))
    bus.flush(timeout=2.0)
    assert intents == []
    assert engine.context == "desktop"


def test_intent_event_carries_provenance(bus, engine):
    intents = _record(bus)
    engine.set_context("media")
    source = _gesture("peace", hand="left", confidence=0.81)
    bus.publish(source)
    bus.flush(timeout=2.0)

    (intent,) = intents
    assert intent.payload["gesture"] == "peace"
    assert intent.payload["hand"] == "left"
    assert intent.payload["confidence"] == 0.81
    assert intent.payload["source_event"] == source.event_id


def test_invalid_set_context_rejected(engine):
    with pytest.raises(ValueError):
        engine.set_context("")
