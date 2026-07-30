"""Milestone 2 tests: calibration, custom gestures, profiles, debug view.

Everything remains hardware-free; the debug-window test relies on this
environment being headless to exercise the self-disable path.
"""

from __future__ import annotations

import textwrap
import time

import pytest

from digital_twin.configuration.settings import (
    AppConfig,
    GestureModuleConfig,
    load_config,
)
from digital_twin.core.bus import EventBus
from digital_twin.core.events import Topics
from digital_twin.perception.gesture.custom import load_custom_gesture_modules
from digital_twin.perception.gesture.module import GesturePerceptionModule

from gesturesense.gesture.base import registered_names

from tests import fixtures
from tests.test_gesture_module import (
    _FakeCamera,
    _FakeTracker,
    _hand,
    _output,
    _record,
    _wait_for,
)


@pytest.fixture()
def bus():
    bus = EventBus()
    bus.start()
    yield bus
    bus.stop()


def _module(bus, **config_kwargs) -> GesturePerceptionModule:
    module = GesturePerceptionModule(
        GestureModuleConfig(**config_kwargs),
        camera_factory=lambda: _FakeCamera(total_frames=0),  # inert: these
        tracker_factory=_FakeTracker,                        # tests drive
        # _process_output directly; the poll thread must race nothing.
    )
    module.start(bus)
    return module


# ---------------------------------------------------------------------------
# Calibration filters
# ---------------------------------------------------------------------------
def test_disabled_gestures_are_never_published(bus):
    module = _module(bus, disabled_gestures=["thumbs_up"])
    try:
        events = _record(bus, Topics.GESTURE)
        up, palm = fixtures.thumbs_up(), fixtures.open_palm()
        module._process_output(_output(1, _hand(up, "Right", "Thumbs Up")))
        module._process_output(_output(2, _hand(palm, "Right", "Open Palm")))
        bus.flush(timeout=2.0)
        assert [e.payload["gesture"] for e in events] == ["open_palm"]
    finally:
        module.stop()


def test_per_gesture_threshold_filters_low_confidence(bus):
    module = _module(bus, gesture_thresholds={"thumbs_up": 0.9})
    try:
        events = _record(bus, Topics.GESTURE)
        pose = fixtures.thumbs_up()
        module._process_output(_output(1, _hand(pose, "Right", "Thumbs Up", 0.80)))
        module._process_output(_output(2, _hand(pose, "Right", "Thumbs Up", 0.95)))
        # Dip below threshold re-arms the edge, regain publishes again.
        module._process_output(_output(3, _hand(pose, "Right", "Thumbs Up", 0.70)))
        module._process_output(_output(4, _hand(pose, "Right", "Thumbs Up", 0.97)))
        bus.flush(timeout=2.0)
        confidences = [e.payload["confidence"] for e in events]
        assert confidences == [0.95, 0.97]
    finally:
        module.stop()


def test_threshold_only_applies_to_named_gesture(bus):
    module = _module(bus, gesture_thresholds={"thumbs_up": 0.9})
    try:
        events = _record(bus, Topics.GESTURE)
        palm = fixtures.open_palm()
        module._process_output(_output(1, _hand(palm, "Right", "Open Palm", 0.60)))
        bus.flush(timeout=2.0)
        assert [e.payload["gesture"] for e in events] == ["open_palm"]
    finally:
        module.stop()


# ---------------------------------------------------------------------------
# Custom gesture registration
# ---------------------------------------------------------------------------
_RULE_TEMPLATE = textwrap.dedent(
    """
    from gesturesense.gesture.base import GestureRule, register_gesture

    @register_gesture
    class {cls}(GestureRule):
        name = "{name}"

        def score(self, features):
            return 0.0  # inert: never wins classification in other tests
    """
)


def _write_rule(tmp_path, cls: str, name: str):
    path = tmp_path / f"{cls.lower()}.py"
    path.write_text(_RULE_TEMPLATE.format(cls=cls, name=name), encoding="utf-8")
    return path


def test_custom_gesture_file_loads_and_registers(tmp_path):
    path = _write_rule(tmp_path, "TestRuleAlpha", "Test Rule Alpha")
    report = load_custom_gesture_modules([str(path)])
    assert report.loaded == (str(path),)
    assert report.failed == ()
    assert "Test Rule Alpha" in registered_names()


def test_custom_gesture_loading_is_idempotent(tmp_path):
    path = _write_rule(tmp_path, "TestRuleBeta", "Test Rule Beta")
    first = load_custom_gesture_modules([str(path)])
    second = load_custom_gesture_modules([str(path)])  # would raise on re-exec
    assert first.loaded == second.loaded == (str(path),)


def test_broken_custom_gesture_degrades_gracefully(tmp_path):
    good = _write_rule(tmp_path, "TestRuleGamma", "Test Rule Gamma")
    missing = tmp_path / "nope.py"
    broken = tmp_path / "broken.py"
    broken.write_text("raise RuntimeError('bad rule module')", encoding="utf-8")

    report = load_custom_gesture_modules([str(missing), str(broken), str(good)])
    assert report.loaded == (str(good),)
    assert set(report.failed) == {str(missing), str(broken)}
    assert "Test Rule Gamma" in registered_names()


def test_shipped_example_custom_gesture_loads():
    report = load_custom_gesture_modules(
        ["examples/custom_gestures/three_count.py"]
    )
    assert report.failed == ()
    assert "Three Count" in registered_names()


def test_module_reports_custom_gestures_in_metrics(bus, tmp_path):
    good = _write_rule(tmp_path, "TestRuleDelta", "Test Rule Delta")
    module = _module(
        bus,
        custom_gesture_modules=[str(good), str(tmp_path / "missing.py")],
    )
    try:
        metrics = module.status().metrics
        assert metrics["custom_gestures_loaded"] == 1
        assert metrics["custom_gestures_failed"] == [str(tmp_path / "missing.py")]
    finally:
        module.stop()


def test_custom_gesture_events_use_slugified_semantic_id(bus):
    module = _module(bus)
    try:
        events = _record(bus, Topics.GESTURE)
        pose = fixtures.open_palm()  # any pose; gesture name is what matters
        module._process_output(_output(1, _hand(pose, "Right", "Three Count", 0.9)))
        bus.flush(timeout=2.0)
        assert [e.payload["gesture"] for e in events] == ["three_count"]
    finally:
        module.stop()


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------
def _write_config(tmp_path, text: str):
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def test_active_profile_overrides_gesture_and_intent(tmp_path):
    path = _write_config(
        tmp_path,
        """
        profiles:
          active: arnav
          available:
            default: {}
            arnav:
              gesture:
                repeat_interval_s: 0.8
                gesture_thresholds: {thumbs_up: 0.7}
                disabled_gestures: [finger_gun]
              intent:
                default_context: coding
        """,
    )
    config = load_config(path)
    assert config.gesture.repeat_interval_s == 0.8
    assert config.gesture.gesture_thresholds == {"thumbs_up": 0.7}
    assert config.gesture.disabled_gestures == ["finger_gun"]
    assert config.intent.default_context == "coding"
    # Non-profile sections untouched.
    assert config.bus == AppConfig().bus


def test_profile_parameter_overrides_active(tmp_path):
    path = _write_config(
        tmp_path,
        """
        profiles:
          active: default
          available:
            default: {}
            media_mode:
              intent: {default_context: media}
        """,
    )
    assert load_config(path).intent.default_context == "desktop"
    assert load_config(path, profile="media_mode").intent.default_context == "media"


def test_unknown_profile_is_a_hard_error(tmp_path):
    path = _write_config(tmp_path, "profiles: {active: ghost}\n")
    with pytest.raises(ValueError, match="Unknown profile"):
        load_config(path)
    with pytest.raises(ValueError, match="Unknown profile"):
        load_config(None, profile="ghost")


def test_profile_may_only_override_gesture_and_intent(tmp_path):
    path = _write_config(
        tmp_path,
        """
        profiles:
          active: sneaky
          available:
            sneaky:
              bus: {max_queue_size: 1}
        """,
    )
    with pytest.raises(ValueError, match="may only override"):
        load_config(path)


def test_profile_overrides_are_validated(tmp_path):
    path = _write_config(
        tmp_path,
        """
        profiles:
          active: broken
          available:
            broken:
              gesture: {gesture_thresholds: {thumbs_up: 2.0}}
        """,
    )
    with pytest.raises(ValueError, match="gesture_thresholds"):
        load_config(path)


@pytest.mark.parametrize(
    "yaml_text",
    [
        "gesture:\n  gesture_thresholds:\n    thumbs_up: 0\n",
        "gesture:\n  disabled_gestures: [1, 2]\n",
        "gesture:\n  custom_gesture_modules: ['']\n",
        "profiles:\n  available: not_a_mapping\n",
    ],
)
def test_new_invalid_values_raise(tmp_path, yaml_text):
    path = tmp_path / "config.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


# ---------------------------------------------------------------------------
# Debug window (headless self-disable)
# ---------------------------------------------------------------------------
def test_debug_window_self_disables_headless_and_pipeline_survives(bus, monkeypatch):
    # Force the headless path deterministically rather than depending on the
    # absence of a display: this box HAS one (full opencv-python), so we make
    # the pre-flight display probe decline and assert the self-disable LOGIC.
    monkeypatch.setattr(
        "digital_twin.perception.gesture.debug_view.DebugView._display_available",
        staticmethod(lambda: False),
    )
    gestures = _record(bus, Topics.GESTURE)
    module = GesturePerceptionModule(
        GestureModuleConfig(
            debug_window=True, history=5, min_votes=3, poll_interval_s=0.002
        ),
        camera_factory=lambda: _FakeCamera(total_frames=200),
        tracker_factory=lambda: _FakeTracker(hand_frames=60),
    )
    module.start(bus)
    try:
        # Pre-flight display check declines synchronously (probe forced False):
        # no render thread, metric False from the start.
        assert module.status().metrics["debug_window"] is False
        assert _wait_for(lambda: len(gestures) >= 1)
        bus.flush(timeout=2.0)
        assert module.is_active
    finally:
        module.stop()
    assert [e.payload["gesture"] for e in gestures] == ["thumbs_up"]
