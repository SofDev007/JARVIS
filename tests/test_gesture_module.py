"""Tests for the gesture perception module.

Two layers: deterministic unit tests of the event-derivation logic
(``_process_output`` called directly with hand-built inference outputs),
and one threaded integration test running the real GestureSense inference
worker against a fake camera and fake tracker — no hardware, no MediaPipe.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import numpy as np
import pytest

from digital_twin.configuration.settings import GestureModuleConfig
from digital_twin.core.bus import EventBus
from digital_twin.core.events import Event, Topics
from digital_twin.perception.gesture.module import GesturePerceptionModule
from digital_twin.perception.gesture.semantics import SEMANTIC_IDS, resolve, to_semantic

from gesturesense.core.pipeline import HandResult, InferenceOutput
from gesturesense.gesture.base import registered_names
from gesturesense.gesture.engine import RecognizedGesture
from gesturesense.gesture.features import extract_features
from gesturesense.vision.hand_tracker import HandObservation

from tests import fixtures


# ---------------------------------------------------------------------------
# Semantics
# ---------------------------------------------------------------------------
def test_every_gesturesense_gesture_has_a_semantic_id():
    """Guards against library/display-name drift breaking the event layer.

    Subset (not equality): custom gestures registered elsewhere in the test
    session legitimately extend the registry and fall back to slugified
    ids. What must never happen is a curated id pointing at a display name
    the library no longer registers.
    """
    assert set(SEMANTIC_IDS).issubset(set(registered_names()))


def test_unknown_display_name_slugifies_deterministically():
    assert to_semantic("Some New Gesture!") == "some_new_gesture"


def test_aliases_resolve():
    assert resolve("high_five") == "open_palm"
    assert resolve("victory") == "peace"
    assert resolve("thumbs_up") == "thumbs_up"


# ---------------------------------------------------------------------------
# Helpers to build inference outputs
# ---------------------------------------------------------------------------
def _hand(
    pose: np.ndarray, handedness: str, gesture_name: str | None, confidence: float = 0.9
) -> HandResult:
    observation = HandObservation(landmarks=pose, handedness=handedness, score=0.99)
    features = extract_features(pose, handedness)
    gesture = (
        RecognizedGesture(name=gesture_name, confidence=confidence, handedness=handedness)
        if gesture_name
        else None
    )
    return HandResult(observation=observation, features=features, gesture=gesture)


def _output(seq: int, *hands: HandResult) -> InferenceOutput:
    return InferenceOutput(frame_seq=seq, hands=tuple(hands), inference_ms=5.0)


@pytest.fixture()
def bus():
    bus = EventBus()
    bus.start()
    yield bus
    bus.stop()


@pytest.fixture()
def started_module(bus):
    """Module attached to the bus with no real pipeline (unit-test mode)."""
    module = GesturePerceptionModule(
        GestureModuleConfig(),
        camera_factory=lambda: _FakeCamera(total_frames=0),  # inert: unit
        tracker_factory=_FakeTracker,                        # tests drive
        # _process_output directly; the poll thread must race nothing.
    )
    module.start(bus)
    yield module
    module.stop()


def _record(bus: EventBus, pattern: str) -> list[Event]:
    events: list[Event] = []
    bus.subscribe(pattern, events.append)
    return events


# ---------------------------------------------------------------------------
# Event derivation unit tests
# ---------------------------------------------------------------------------
def test_gesture_event_schema_matches_platform_contract(bus, started_module):
    events = _record(bus, Topics.GESTURE)
    pose = fixtures.thumbs_up()
    started_module._process_output(_output(1, _hand(pose, "Right", "Thumbs Up", 0.97)))
    bus.flush(timeout=2.0)

    (event,) = events
    data = event.to_dict()
    assert data["module"] == "gesture"
    assert data["gesture"] == "thumbs_up"
    assert data["hand"] == "right"
    assert data["confidence"] == pytest.approx(0.97)
    assert data["repeat"] is False


def test_edge_triggering_no_repeat_for_held_gesture(bus, started_module):
    events = _record(bus, Topics.GESTURE)
    pose = fixtures.thumbs_up()
    for seq in range(1, 6):
        started_module._process_output(_output(seq, _hand(pose, "Right", "Thumbs Up")))
    bus.flush(timeout=2.0)
    assert len(events) == 1  # held gesture publishes once


def test_gesture_change_publishes_new_event(bus, started_module):
    events = _record(bus, Topics.GESTURE)
    up, palm = fixtures.thumbs_up(), fixtures.open_palm()
    started_module._process_output(_output(1, _hand(up, "Right", "Thumbs Up")))
    started_module._process_output(_output(2, _hand(palm, "Right", "Open Palm")))
    bus.flush(timeout=2.0)
    assert [e.payload["gesture"] for e in events] == ["thumbs_up", "open_palm"]


def test_gesture_gap_re_arms_the_edge(bus, started_module):
    events = _record(bus, Topics.GESTURE)
    pose = fixtures.thumbs_up()
    started_module._process_output(_output(1, _hand(pose, "Right", "Thumbs Up")))
    started_module._process_output(_output(2, _hand(pose, "Right", None)))  # unstable gap
    started_module._process_output(_output(3, _hand(pose, "Right", "Thumbs Up")))
    bus.flush(timeout=2.0)
    assert [e.payload["gesture"] for e in events] == ["thumbs_up", "thumbs_up"]


def test_stale_frame_seq_is_ignored(bus, started_module):
    events = _record(bus, Topics.GESTURE)
    pose = fixtures.thumbs_up()
    output = _output(1, _hand(pose, "Right", "Thumbs Up"))
    started_module._process_output(output)
    started_module._process_output(output)  # same frame polled twice
    bus.flush(timeout=2.0)
    assert len(events) == 1


def test_hand_presence_events(bus, started_module):
    events = _record(bus, Topics.HAND)
    pose = fixtures.open_palm()
    started_module._process_output(_output(1, _hand(pose, "Right", None)))
    started_module._process_output(
        _output(2, _hand(pose, "Right", None), _hand(pose, "Left", None))
    )
    started_module._process_output(_output(3))  # all hands gone
    bus.flush(timeout=2.0)

    seen = [(e.payload["hand"], e.payload["present"]) for e in events]
    assert seen == [
        ("right", True),
        ("left", True),
        ("left", False),
        ("right", False),
    ]


def test_repeat_interval_republishes_held_gesture(bus):
    module = GesturePerceptionModule(
        GestureModuleConfig(repeat_interval_s=1.0),
        camera_factory=lambda: _FakeCamera(total_frames=0),
        tracker_factory=_FakeTracker,
    )
    module.start(bus)
    try:
        events = _record(bus, Topics.GESTURE)
        pose = fixtures.thumbs_up()
        module._process_output(_output(1, _hand(pose, "Right", "Thumbs Up")), now=100.0)
        module._process_output(_output(2, _hand(pose, "Right", "Thumbs Up")), now=100.5)
        module._process_output(_output(3, _hand(pose, "Right", "Thumbs Up")), now=101.1)
        bus.flush(timeout=2.0)
        assert [e.payload["repeat"] for e in events] == [False, True]
    finally:
        module.stop()


def test_two_hands_tracked_independently(bus, started_module):
    events = _record(bus, Topics.GESTURE)
    up, peace = fixtures.thumbs_up(), fixtures.peace_sign()
    started_module._process_output(
        _output(
            1,
            _hand(up, "Right", "Thumbs Up"),
            _hand(peace, "Left", "Peace / Victory"),
        )
    )
    bus.flush(timeout=2.0)
    seen = {(e.payload["hand"], e.payload["gesture"]) for e in events}
    assert seen == {("right", "thumbs_up"), ("left", "peace")}


def test_paused_module_publishes_nothing(bus, started_module):
    events = _record(bus, "perception.*")
    started_module.pause()
    started_module._process_output(
        _output(1, _hand(fixtures.thumbs_up(), "Right", "Thumbs Up"))
    )
    bus.flush(timeout=2.0)
    assert events == []
    started_module.resume()  # must rebuild cleanly from factories
    assert started_module.is_active


# ---------------------------------------------------------------------------
# Integration: real InferenceWorker + fake camera/tracker
# ---------------------------------------------------------------------------
@dataclass
class _Frame:
    image: np.ndarray
    seq: int
    timestamp: float


class _FakeCamera:
    """Emits one synthetic frame per ``latest`` call, from a bounded reel."""

    def __init__(self, total_frames: int = 200):
        self._seq = 0
        self._total = total_frames
        self._lock = threading.Lock()
        self._image = np.zeros((48, 64, 3), dtype=np.uint8)

    def start(self) -> None:  # ThreadedCamera API
        pass

    def stop(self) -> None:
        pass

    def latest(self, newer_than: int = -1):
        with self._lock:
            if self._seq >= self._total:
                return None
            self._seq += 1
            return _Frame(image=self._image, seq=self._seq, timestamp=time.time())


class _FakeTracker:
    """Scripted observations: thumbs-up hand for a while, then no hands."""

    def __init__(self, hand_frames: int = 60):
        self._hand_frames = hand_frames
        self._calls = 0
        self._pose = fixtures.thumbs_up()

    def process(self, image) -> list[HandObservation]:
        self._calls += 1
        if self._calls <= self._hand_frames:
            return [HandObservation(landmarks=self._pose, handedness="Right", score=0.99)]
        return []

    def close(self) -> None:
        pass


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_full_pipeline_with_fake_camera_and_tracker(bus):
    """Camera → tracker → engine → module → bus, end to end, no hardware."""
    gestures = _record(bus, Topics.GESTURE)
    hands = _record(bus, Topics.HAND)

    module = GesturePerceptionModule(
        GestureModuleConfig(history=5, min_votes=3, poll_interval_s=0.002),
        camera_factory=lambda: _FakeCamera(total_frames=200),
        tracker_factory=lambda: _FakeTracker(hand_frames=60),
    )
    module.start(bus)
    try:
        assert _wait_for(lambda: any(not h.payload["present"] for h in hands))
        bus.flush(timeout=2.0)
    finally:
        module.stop()

    assert [e.payload["gesture"] for e in gestures] == ["thumbs_up"]
    presence = [(e.payload["hand"], e.payload["present"]) for e in hands]
    assert presence == [("right", True), ("right", False)]
    assert module.status().metrics["hands_visible"] == 0
