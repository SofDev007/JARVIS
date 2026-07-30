"""Tests for the event envelope and topic matching."""

from __future__ import annotations

import json

import pytest

from digital_twin.core.events import Event, Topics, topic_matches


def test_event_to_dict_matches_platform_contract():
    event = Event(
        topic=Topics.GESTURE,
        source="gesture",
        payload={"gesture": "thumbs_up", "confidence": 0.97, "hand": "right"},
    )
    data = event.to_dict()
    assert data["module"] == "gesture"
    assert data["type"] == "perception.gesture"
    assert data["gesture"] == "thumbs_up"
    assert data["confidence"] == 0.97
    assert data["hand"] == "right"
    assert "T" in data["timestamp"]  # ISO-8601
    json.dumps(data)  # must be JSON-serialisable


def test_event_ids_are_unique_and_timestamps_set():
    a, b = Event("t", "s"), Event("t", "s")
    assert a.event_id != b.event_id
    assert a.timestamp > 0


def test_payload_is_immutable():
    event = Event("t", "s", payload={"k": 1})
    with pytest.raises(TypeError):
        event.payload["k"] = 2  # type: ignore[index]


def test_reserved_payload_keys_rejected():
    with pytest.raises(ValueError):
        Event("t", "s", payload={"module": "spoofed"})
    with pytest.raises(ValueError):
        Event("t", "s", payload={"timestamp": 0})


def test_empty_topic_or_source_rejected():
    with pytest.raises(ValueError):
        Event("", "s")
    with pytest.raises(ValueError):
        Event("t", "")


@pytest.mark.parametrize(
    "pattern,topic,expected",
    [
        ("*", "anything.at.all", True),
        ("perception.gesture", "perception.gesture", True),
        ("perception.gesture", "perception.hand", False),
        ("perception.*", "perception.gesture", True),
        ("perception.*", "perception.gesture.extra", True),
        ("perception.*", "perception", True),
        ("perception.*", "perceptionX.gesture", False),
        ("perception.*", "intent.detected", False),
    ],
)
def test_topic_matches(pattern, topic, expected):
    assert topic_matches(pattern, topic) is expected
