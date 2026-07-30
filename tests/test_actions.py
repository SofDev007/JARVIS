"""Tests for the action pipeline: registry, built-ins, dispatcher security flow."""

from __future__ import annotations

import time

import pytest

from digital_twin.automation.builtin import register_builtin_actions
from digital_twin.automation.dispatcher import ActionDispatcher
from digital_twin.automation.registry import ActionRegistry, ActionSpec
from digital_twin.configuration.settings import (
    AutomationConfig,
    IntentConfig,
    SecurityConfig,
)
from digital_twin.core.bus import EventBus
from digital_twin.core.events import Event, Topics
from digital_twin.reasoning.intent import IntentEngine
from digital_twin.security.audit import AuditLog
from digital_twin.security.confirmation import ScriptedConfirmation
from digital_twin.security.permissions import PermissionPolicy, RiskLevel


# ---------------------------------------------------------------------------
# Registry and built-ins
# ---------------------------------------------------------------------------
def _spec(name="x", risk=RiskLevel.SAFE, handler=lambda p: "ok", **kw) -> ActionSpec:
    return ActionSpec(name=name, description="test", risk=risk, handler=handler, **kw)


def test_registry_rejects_duplicates():
    registry = ActionRegistry()
    registry.register(_spec("a"))
    with pytest.raises(ValueError):
        registry.register(_spec("a"))
    assert registry.get("a") is not None and registry.get("nope") is None


def test_builtin_actions_registered_with_expected_risk():
    registry = ActionRegistry()
    register_builtin_actions(registry, applications={"editor": "vi"})
    assert set(registry.names) == {
        "log_message", "notify", "open_url", "open_application", "get_time",
    }
    assert registry.get("log_message").risk is RiskLevel.SAFE
    assert registry.get("open_url").risk is RiskLevel.SENSITIVE
    assert registry.get("get_time").risk is RiskLevel.SAFE


def test_open_url_validation_blocks_bad_schemes():
    registry = ActionRegistry()
    register_builtin_actions(registry)
    validate = registry.get("open_url").validate
    validate({"url": "https://example.com"})
    for bad in ("file:///etc/passwd", "javascript:alert(1)", "ftp://x",
                "https://e x.com", "", None):
        with pytest.raises(ValueError):
            validate({"url": bad})


def test_open_application_enforces_allow_list():
    registry = ActionRegistry()
    register_builtin_actions(registry, applications={"editor": "vi"})
    validate = registry.get("open_application").validate
    validate({"app": "editor"})
    with pytest.raises(ValueError):
        validate({"app": "bash -c 'rm -rf /'"})
    with pytest.raises(ValueError):
        validate({"app": "unlisted"})


def test_log_message_handler_returns_detail():
    registry = ActionRegistry()
    register_builtin_actions(registry)
    assert registry.get("log_message").handler({"message": "hello"}) == "hello"
    with pytest.raises(ValueError):
        registry.get("log_message").validate({"message": "   "})


# ---------------------------------------------------------------------------
# Dispatcher pipeline
# ---------------------------------------------------------------------------
@pytest.fixture()
def bus():
    bus = EventBus()
    bus.start()
    yield bus
    bus.stop()


class Harness:
    """A dispatcher wired with recording test actions and scripted security."""

    def __init__(self, bus, tmp_path, answers=None, permissions=None,
                 bindings=None, action_timeout_s=5.0):
        self.executed: list[tuple[str, dict]] = []
        registry = ActionRegistry()
        registry.register(_spec(
            "record", handler=lambda p: self.executed.append(("record", dict(p)))
            or "recorded"))
        registry.register(_spec(
            "record_guarded", risk=RiskLevel.SENSITIVE,
            handler=lambda p: self.executed.append(("record_guarded", dict(p)))
            or "recorded"))
        registry.register(_spec(
            "record_dangerous", risk=RiskLevel.DANGEROUS,
            handler=lambda p: self.executed.append(("record_dangerous", dict(p)))
            or "recorded"))
        registry.register(_spec("explode", handler=self._explode))
        registry.register(_spec("sleepy", handler=lambda p: time.sleep(2.0)))
        registry.register(_spec(
            "picky", validate=self._validate_picky, handler=lambda p: "ok"))

        self.confirmation = ScriptedConfirmation(answers or [])
        self.audit = AuditLog(tmp_path / "audit.jsonl")
        self.dispatcher = ActionDispatcher(
            config=AutomationConfig(
                intent_bindings=bindings if bindings is not None else {
                    "go": {"action": "record", "params": {"n": 1}},
                    "guarded": {"action": "record_guarded"},
                    "danger": {"action": "record_dangerous"},
                    "boom": {"action": "explode"},
                    "slow": {"action": "sleepy"},
                    "fussy": {"action": "picky", "params": {"wrong": True}},
                    "ghost": {"action": "no_such_action"},
                },
                action_timeout_s=action_timeout_s,
            ),
            registry=registry,
            policy=PermissionPolicy(
                risk_defaults={"safe": "allow", "sensitive": "confirm",
                               "dangerous": "deny"},
                overrides=permissions or {},
            ),
            confirmation=self.confirmation,
            audit=self.audit,
            confirmation_timeout_s=1.0,
        )
        self.results: list[Event] = []
        bus.subscribe(Topics.ACTION_RESULT, self.results.append)
        self.dispatcher.start(bus)

    @staticmethod
    def _explode(params):
        raise RuntimeError("kaboom")

    @staticmethod
    def _validate_picky(params):
        if "wrong" in params:
            raise ValueError("wrong param")

    def statuses(self):
        return [e.payload["status"] for e in self.results]


def _intent(name: str, **extra) -> Event:
    payload = {"intent": name, "context": "test", "gesture": "thumbs_up",
               "source_event": "perception123", **extra}
    return Event(Topics.INTENT, "intent", payload)


def _wait(predicate, timeout=5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_allowed_action_executes_with_provenance(bus, tmp_path):
    h = Harness(bus, tmp_path)
    try:
        event = _intent("go")
        bus.publish(event)
        assert _wait(lambda: h.executed)
        assert _wait(lambda: h.results)
        bus.flush(timeout=2.0)

        assert h.executed == [("record", {"n": 1})]
        (result,) = h.results
        assert result.payload["status"] == "completed"
        assert result.payload["intent_event"] == event.event_id
        assert result.payload["perception_event"] == "perception123"
        (entry,) = [e for e in h.audit.tail() if e["status"] == "completed"]
        assert entry["action"] == "record"
        assert entry["perception_event"] == "perception123"
        assert entry["duration_ms"] >= 0
        assert h.confirmation.requests == []  # SAFE never prompts
    finally:
        h.dispatcher.stop()


def test_confirmation_approved_executes(bus, tmp_path):
    h = Harness(bus, tmp_path, answers=[True])
    try:
        bus.publish(_intent("guarded"))
        assert _wait(lambda: h.executed)
        assert h.confirmation.requests[0][0] == "record_guarded"
        assert _wait(lambda: "completed" in h.statuses())
    finally:
        h.dispatcher.stop()


def test_confirmation_denied_blocks_execution(bus, tmp_path):
    h = Harness(bus, tmp_path, answers=[False])
    try:
        bus.publish(_intent("guarded"))
        assert _wait(lambda: h.results)
        assert h.statuses() == ["rejected"]
        assert h.executed == []
        assert any(e["status"] == "rejected" for e in h.audit.tail())
    finally:
        h.dispatcher.stop()


def test_policy_deny_never_reaches_confirmation(bus, tmp_path):
    h = Harness(bus, tmp_path, answers=[True])
    try:
        bus.publish(_intent("danger"))
        assert _wait(lambda: h.results)
        assert h.statuses() == ["denied"]
        assert h.confirmation.requests == []
        assert h.executed == []
    finally:
        h.dispatcher.stop()


def test_unbound_intent_is_audited_without_result_event(bus, tmp_path):
    h = Harness(bus, tmp_path)
    try:
        bus.publish(_intent("mystery"))
        assert _wait(lambda: any(
            e["status"] == "unbound" for e in h.audit.tail()))
        bus.flush(timeout=2.0)
        assert h.results == []
    finally:
        h.dispatcher.stop()


def test_unknown_action_and_invalid_params_reported(bus, tmp_path):
    h = Harness(bus, tmp_path)
    try:
        bus.publish(_intent("ghost"))
        bus.publish(_intent("fussy"))
        assert _wait(lambda: len(h.results) == 2)
        assert sorted(h.statuses()) == ["invalid", "unknown_action"]
        assert h.executed == []
    finally:
        h.dispatcher.stop()


def test_failing_handler_reports_failed(bus, tmp_path):
    h = Harness(bus, tmp_path)
    try:
        bus.publish(_intent("boom"))
        assert _wait(lambda: h.results)
        (result,) = h.results
        assert result.payload["status"] == "failed"
        assert "kaboom" in result.payload["detail"]
    finally:
        h.dispatcher.stop()


def test_slow_action_times_out(bus, tmp_path):
    h = Harness(bus, tmp_path, action_timeout_s=0.2)
    try:
        bus.publish(_intent("slow"))
        assert _wait(lambda: h.results)
        assert h.statuses() == ["timeout"]
        assert any(e["status"] == "timeout" for e in h.audit.tail())
    finally:
        h.dispatcher.stop()


def test_paused_dispatcher_ignores_new_intents(bus, tmp_path):
    h = Harness(bus, tmp_path)
    try:
        h.dispatcher.pause()
        bus.publish(_intent("go"))
        bus.flush(timeout=2.0)
        time.sleep(0.1)
        assert h.executed == []
        h.dispatcher.resume()
        bus.publish(_intent("go"))
        assert _wait(lambda: h.executed)
    finally:
        h.dispatcher.stop()


def test_metrics_expose_outcomes(bus, tmp_path):
    h = Harness(bus, tmp_path)
    try:
        bus.publish(_intent("go"))
        assert _wait(lambda: h.executed)
        assert _wait(
            lambda: h.dispatcher.status().metrics["outcomes"].get("completed") == 1
        )
        assert h.dispatcher.status().metrics["actions_available"] >= 6
    finally:
        h.dispatcher.stop()


def test_full_chain_gesture_to_intent_to_action(bus, tmp_path):
    """The platform contract end to end: perception → reasoning → automation."""
    engine = IntentEngine(IntentConfig(
        default_context="media",
        mappings={"media": {"thumbs_up": "go"}},
    ))
    engine.start(bus)
    h = Harness(bus, tmp_path)
    try:
        gesture = Event(Topics.GESTURE, "gesture", {
            "gesture": "thumbs_up", "confidence": 0.97,
            "hand": "right", "repeat": False,
        })
        bus.publish(gesture)
        assert _wait(lambda: h.executed)
        assert _wait(lambda: h.results)
        bus.flush(timeout=2.0)

        (result,) = h.results
        assert result.payload["status"] == "completed"
        # Provenance reaches all the way back to the perception event.
        assert result.payload["perception_event"] == gesture.event_id
    finally:
        h.dispatcher.stop()
        engine.stop()
