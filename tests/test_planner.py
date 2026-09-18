"""Tests for the planner (M8): parsing, choreography, gates, skills.

The architectural assertions matter most: every plan step passes the real
dispatcher pipeline (a SENSITIVE step demands confirmation even mid-plan;
a denied step aborts), progress is fully observable, cancellation and
timeouts work, and successful LLM plans become replayable skills.
"""

from __future__ import annotations

import json
import time

import pytest

from digital_twin.automation.dispatcher import ActionDispatcher
from digital_twin.automation.registry import ActionRegistry, ActionSpec
from digital_twin.configuration.settings import (
    AutomationConfig,
    LLMConfig,
    MemoryConfig,
    PlannerConfig,
    load_config,
)
from digital_twin.core.bus import EventBus
from digital_twin.core.events import Event, Topics
from digital_twin.memory.module import MemoryModule
from digital_twin.planner.plan import (
    Plan,
    PlanStep,
    parse_stages,
    plan_from_config,
    plan_from_llm,
    plan_from_skill,
)
from digital_twin.planner.module import PlannerModule
from digital_twin.reasoning.chat_reasoner import ChatReasoner, parse_model_reply
from digital_twin.reasoning.llm import ScriptedModel
from digital_twin.security.audit import AuditLog
from digital_twin.security.confirmation import ScriptedConfirmation
from digital_twin.security.permissions import PermissionPolicy, RiskLevel


# ---------------------------------------------------------------------------
# Plan model / parsing
# ---------------------------------------------------------------------------
def test_parse_sequential_and_parallel_stages():
    stages = parse_stages(
        [
            {"action": "a"},
            {"parallel": [{"action": "b"}, {"action": "c", "params": {"k": 1}}]},
            {"action": "d", "label": "finish"},
        ],
        max_steps=10,
    )
    assert [len(stage) for stage in stages] == [1, 2, 1]
    assert stages[1][1].params == {"k": 1}
    assert stages[2][0].describe() == "finish"


@pytest.mark.parametrize("raw,message", [
    ([], "non-empty"),
    ([{"params": {}}], "action"),
    ([{"action": "a", "params": "nope"}], "params"),
    ([{"parallel": []}], "parallel"),
    ([{"action": "a"}] * 5, "limit is 4"),
])
def test_parse_rejects_bad_structures(raw, message):
    with pytest.raises(ValueError, match=message):
        parse_stages(raw, max_steps=4)


def test_plan_from_config_and_llm_and_skill_roundtrip():
    config_plan = plan_from_config(
        "routine",
        {"description": "d", "on_error": "continue",
         "steps": [{"action": "a"}, {"parallel": [{"action": "b"},
                                                  {"action": "c"}]}]},
        max_steps=10,
    )
    assert config_plan.total_steps == 3 and config_plan.on_error == "continue"

    llm_plan = plan_from_llm(
        {"goal": "tidy up", "steps": [{"action": "a"}, {"action": "b"}]},
        max_steps=10,
    )
    assert llm_plan.source == "llm" and llm_plan.total_steps == 2
    with pytest.raises(ValueError, match="parallel"):
        plan_from_llm({"goal": "g", "steps": [{"parallel": [{"action": "a"}]}]},
                      max_steps=10)

    replayed = plan_from_skill(config_plan.to_data(), max_steps=10)
    assert replayed.stages == config_plan.stages
    assert replayed.source == "skill"


def test_broken_config_plan_fails_config_load(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "planner:\n  plans:\n    broken:\n      steps:\n        - {params: {}}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="planner.plans.broken"):
        load_config(path)


# ---------------------------------------------------------------------------
# Harness: real dispatcher + planner on a live bus
# ---------------------------------------------------------------------------
@pytest.fixture()
def bus():
    bus = EventBus()
    bus.start()
    yield bus
    bus.stop()


class Rig:
    def __init__(self, bus, tmp_path, plans=None, answers=None,
                 intent_triggers=None, accept_llm_plans=True,
                 step_timeout_s=5.0, memory=None, deny=()):
        self.executed: list[str] = []
        registry = ActionRegistry()
        for name in ("alpha", "beta", "gamma"):
            registry.register(ActionSpec(
                name=name, description=f"{name} test action",
                risk=RiskLevel.SAFE,
                handler=lambda p, n=name: self.executed.append(n) or "ok"))
        registry.register(ActionSpec(
            name="guarded", description="needs confirmation",
            risk=RiskLevel.SENSITIVE,
            handler=lambda p: self.executed.append("guarded") or "ok"))
        registry.register(ActionSpec(
            name="slow", description="sleeps", risk=RiskLevel.SAFE,
            handler=lambda p: time.sleep(2.0)))

        self.confirmation = ScriptedConfirmation(answers or [])
        overrides = {name: "deny" for name in deny}
        self.dispatcher = ActionDispatcher(
            config=AutomationConfig(intent_bindings={}),
            registry=registry,
            policy=PermissionPolicy(
                risk_defaults={"safe": "allow", "sensitive": "confirm"},
                overrides=overrides),
            confirmation=self.confirmation,
            audit=AuditLog(tmp_path / "audit.jsonl"),
            confirmation_timeout_s=1.0,
        )
        self.dispatcher.start(bus)

        self.planner = PlannerModule(
            PlannerConfig(
                plans=plans or {},
                intent_triggers=intent_triggers or {},
                accept_llm_plans=accept_llm_plans,
                step_timeout_s=step_timeout_s,
            ),
            memory=memory,
        )
        self.planner.start(bus)
        self.progress: list[Event] = []
        bus.subscribe(Topics.PLAN_PROGRESS, self.progress.append)

    def statuses(self):
        return [e.payload["status"] for e in self.progress]

    def stop(self):
        self.planner.stop()
        self.dispatcher.stop()


def _wait(predicate, timeout=6.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


_ROUTINE = {
    "routine": {
        "description": "three step routine",
        "steps": [{"action": "alpha", "label": "first"},
                  {"parallel": [{"action": "beta"}, {"action": "gamma"}]}],
    }
}


def test_config_plan_runs_stages_in_order_through_gates(bus, tmp_path):
    rig = Rig(bus, tmp_path, plans=_ROUTINE)
    try:
        bus.publish(Event(Topics.PLAN_REQUEST, "test", {"plan": "routine"}))
        assert _wait(lambda: "completed" in rig.statuses())
        # Stage order respected: alpha strictly before the parallel pair.
        assert rig.executed[0] == "alpha"
        assert set(rig.executed[1:]) == {"beta", "gamma"}
        assert rig.statuses().count("step_completed") == 3
        assert rig.statuses()[0] == "started"
        # Audit rows carry the plan correlation fields.
        audit = AuditLog(tmp_path / "audit.jsonl").tail()
        planned = [entry for entry in audit if entry.get("plan_id")]
        assert len(planned) == 3 and {e["step"] for e in planned} == {0, 1, 2}
    finally:
        rig.stop()


def test_sensitive_step_requires_confirmation_mid_plan(bus, tmp_path):
    plans = {"careful": {"steps": [{"action": "alpha"},
                                   {"action": "guarded"}]}}
    rig = Rig(bus, tmp_path, plans=plans, answers=[True])
    try:
        bus.publish(Event(Topics.PLAN_REQUEST, "test", {"plan": "careful"}))
        assert _wait(lambda: "completed" in rig.statuses())
        assert rig.confirmation.requests[0][0] == "guarded"
        assert rig.executed == ["alpha", "guarded"]
    finally:
        rig.stop()


def test_denied_step_aborts_plan_by_default(bus, tmp_path):
    plans = {"blocked": {"steps": [{"action": "alpha"},
                                   {"action": "beta"},
                                   {"action": "gamma"}]}}
    rig = Rig(bus, tmp_path, plans=plans, deny=("beta",))
    try:
        bus.publish(Event(Topics.PLAN_REQUEST, "test", {"plan": "blocked"}))
        assert _wait(lambda: "failed" in rig.statuses())
        assert "step_failed" in rig.statuses()
        assert rig.executed == ["alpha"]  # gamma never ran
    finally:
        rig.stop()


def test_on_error_continue_finishes_with_errors(bus, tmp_path):
    plans = {"stubborn": {"on_error": "continue",
                          "steps": [{"action": "alpha"},
                                    {"action": "beta"},
                                    {"action": "gamma"}]}}
    rig = Rig(bus, tmp_path, plans=plans, deny=("beta",))
    try:
        bus.publish(Event(Topics.PLAN_REQUEST, "test", {"plan": "stubborn"}))
        assert _wait(lambda: "completed_with_errors" in rig.statuses())
        assert rig.executed == ["alpha", "gamma"]
    finally:
        rig.stop()


def test_cancel_stops_remaining_stages(bus, tmp_path):
    plans = {"long": {"steps": [{"action": "slow"}, {"action": "alpha"}]}}
    rig = Rig(bus, tmp_path, plans=plans)
    try:
        bus.publish(Event(Topics.PLAN_REQUEST, "test", {"plan": "long"}))
        assert _wait(lambda: "started" in rig.statuses())
        plan_id = rig.progress[0].payload["plan_id"]
        bus.publish(Event(Topics.PLAN_CANCEL, "test", {"plan_id": plan_id}))
        assert _wait(lambda: "cancelled" in rig.statuses())
        time.sleep(2.2)  # let the in-flight slow step finish
        assert "alpha" not in rig.executed  # second stage never published
    finally:
        rig.stop()


def test_stage_timeout_fails_plan(bus, tmp_path):
    plans = {"stuck": {"steps": [{"action": "slow"}]}}
    # dispatcher action timeout (default 30s in AutomationConfig here) >
    # planner stage timeout, so the planner gives up first.
    rig = Rig(bus, tmp_path, plans=plans, step_timeout_s=0.3)
    try:
        bus.publish(Event(Topics.PLAN_REQUEST, "test", {"plan": "stuck"}))
        assert _wait(lambda: "failed" in rig.statuses(), timeout=4.0)
        (failure,) = [e for e in rig.progress
                      if e.payload["status"] == "failed"]
        assert "timed out" in failure.payload["detail"]
    finally:
        rig.stop()


def test_unknown_plan_and_bad_request_are_announced(bus, tmp_path):
    rig = Rig(bus, tmp_path)
    try:
        bus.publish(Event(Topics.PLAN_REQUEST, "test", {"plan": "ghost"}))
        bus.publish(Event(Topics.PLAN_REQUEST, "test", {"nonsense": True}))
        assert _wait(lambda: len(rig.progress) >= 2)
        assert sorted(rig.statuses()) == ["failed", "rejected"]
        assert rig.executed == []
    finally:
        rig.stop()


def test_intent_trigger_starts_routine(bus, tmp_path):
    rig = Rig(bus, tmp_path, plans=_ROUTINE,
              intent_triggers={"presentation_ready": "routine"})
    try:
        bus.publish(Event(Topics.INTENT, "intent",
                          {"intent": "presentation_ready", "context": "x"}))
        assert _wait(lambda: "completed" in rig.statuses())
        assert rig.executed[0] == "alpha"
    finally:
        rig.stop()


def test_llm_plan_respects_config_switch(bus, tmp_path):
    rig = Rig(bus, tmp_path, accept_llm_plans=False)
    try:
        bus.publish(Event(Topics.PLAN_REQUEST, "reasoner",
                          {"goal": "g", "steps": [{"action": "alpha"}]}))
        assert _wait(lambda: "rejected" in rig.statuses())
        assert rig.executed == []
    finally:
        rig.stop()


def test_successful_llm_plan_is_saved_and_replayable_as_skill(bus, tmp_path):
    memory = MemoryModule(MemoryConfig(db_path=str(tmp_path / "m.db")))
    memory.start(bus)
    rig = Rig(bus, tmp_path, memory=memory)
    try:
        bus.publish(Event(Topics.PLAN_REQUEST, "reasoner", {
            "goal": "prepare the morning workspace",
            "steps": [{"action": "alpha", "label": "one"},
                      {"action": "beta", "label": "two"}],
        }))
        assert _wait(lambda: "completed" in rig.statuses())
        assert _wait(lambda: memory.store.count("skill") == 1)

        rig.executed.clear()
        rig.progress.clear()
        bus.publish(Event(Topics.PLAN_REQUEST, "chat",
                          {"skill": "morning workspace"}))
        assert _wait(lambda: "completed" in rig.statuses())
        assert rig.executed == ["alpha", "beta"]
        replay = [e for e in rig.progress if e.payload["status"] == "started"]
        assert replay[0].payload["plan_source"] == "skill"
    finally:
        rig.stop()
        memory.stop()


def test_llm_plan_tainted_flag_carries_to_every_step(bus, tmp_path):
    """THREAT_MODEL.md §4.1: a PLAN_REQUEST's ``tainted`` flag must survive
    the hop into each step's republished ACTION_EXECUTE event. Tainted SAFE
    steps now require confirmation (§4.1's ALLOW-escalation rule), so this
    rig needs queued answers where the untainted rig above didn't."""
    rig = Rig(bus, tmp_path, answers=[True, True])
    executes: list[Event] = []
    bus.subscribe(Topics.ACTION_EXECUTE, executes.append)
    try:
        bus.publish(Event(Topics.PLAN_REQUEST, "reasoner", {
            "goal": "tainted plan",
            "steps": [{"action": "alpha", "label": "one"},
                      {"action": "beta", "label": "two"}],
            "tainted": True,
        }))
        assert _wait(lambda: "completed" in rig.statuses())
        assert len(executes) == 2
        assert all(event.payload["tainted"] is True for event in executes)
    finally:
        rig.stop()


def test_llm_plan_untainted_flag_defaults_false(bus, tmp_path):
    rig = Rig(bus, tmp_path)
    executes: list[Event] = []
    bus.subscribe(Topics.ACTION_EXECUTE, executes.append)
    try:
        bus.publish(Event(Topics.PLAN_REQUEST, "reasoner", {
            "goal": "untainted plan",
            "steps": [{"action": "alpha", "label": "one"}],
        }))
        assert _wait(lambda: "completed" in rig.statuses())
        assert executes and executes[0].payload["tainted"] is False
    finally:
        rig.stop()


def test_skill_request_without_match_fails_cleanly(bus, tmp_path):
    memory = MemoryModule(MemoryConfig(db_path=str(tmp_path / "m.db")))
    memory.start(bus)
    rig = Rig(bus, tmp_path, memory=memory)
    try:
        bus.publish(Event(Topics.PLAN_REQUEST, "chat", {"skill": "unicorns"}))
        assert _wait(lambda: "failed" in rig.statuses())
        assert rig.executed == []
    finally:
        rig.stop()
        memory.stop()


# ---------------------------------------------------------------------------
# Reasoner proposes plans
# ---------------------------------------------------------------------------
def test_parse_model_reply_sanitises_plan_field():
    good = parse_model_reply(json.dumps({
        "reply": "on it", "plan": {"goal": "tidy",
                                   "steps": [{"action": "a", "params": {}}]},
    }))
    assert good["plan"]["goal"] == "tidy"
    bad = parse_model_reply(json.dumps({
        "reply": "x", "plan": {"goal": "g", "steps": [{"params": {}}]},
    }))
    assert bad["plan"] is None


def test_reasoner_publishes_plan_request_with_catalog(bus):
    model = ScriptedModel([json.dumps({
        "reply": "I'll set that up.",
        "plan": {"goal": "workspace prep",
                 "steps": [{"action": "alpha", "params": {},
                            "label": "step one"}]},
        "reasoning": "multi-step request",
    })])
    reasoner = ChatReasoner(
        LLMConfig(memory_results=0),
        model=model,
        allowed_intents=(),
        action_catalog=(("alpha", "safe", "does alpha"),),
    )
    reasoner.start(bus)
    requests: list[Event] = []
    responses: list[Event] = []
    bus.subscribe(Topics.PLAN_REQUEST, requests.append)
    bus.subscribe(Topics.CHAT_RESPONSE, responses.append)
    try:
        chat = Event(Topics.CHAT, "chat", {"text": "prep my workspace"})
        bus.publish(chat)
        assert _wait(lambda: requests and responses)
        payload = requests[0].payload
        assert payload["goal"] == "workspace prep"
        assert payload["source_event"] == chat.event_id
        assert responses[0].payload["plan_requested"] == "workspace prep"
        # The catalog reached the prompt.
        assert "alpha (safe): does alpha" in model.calls[0]["system"]
    finally:
        reasoner.stop()


def test_reasoner_without_catalog_never_requests_plans(bus):
    model = ScriptedModel([json.dumps({
        "reply": "x",
        "plan": {"goal": "g", "steps": [{"action": "alpha", "params": {}}]},
    })])
    reasoner = ChatReasoner(LLMConfig(memory_results=0), model=model,
                            allowed_intents=())
    reasoner.start(bus)
    requests: list[Event] = []
    responses: list[Event] = []
    bus.subscribe(Topics.PLAN_REQUEST, requests.append)
    bus.subscribe(Topics.CHAT_RESPONSE, responses.append)
    try:
        bus.publish(Event(Topics.CHAT, "chat", {"text": "do things"}))
        assert _wait(lambda: responses)
        bus.flush(timeout=2.0)
        assert requests == []
    finally:
        reasoner.stop()
