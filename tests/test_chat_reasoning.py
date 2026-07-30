"""Tests for chat perception and the LLM reasoner (M7).

The important assertions are architectural: the model can only nominate
allow-listed intents; nominations flow through the real dispatcher gates;
memory is consulted on the way in and taught on the way out; model
failures degrade to apologetic responses.
"""

from __future__ import annotations

import json
import time

import pytest

from digital_twin.automation.dispatcher import ActionDispatcher
from digital_twin.automation.registry import ActionRegistry, ActionSpec
from digital_twin.configuration.settings import (
    AutomationConfig,
    ChatConfig,
    LLMConfig,
    MemoryConfig,
)
from digital_twin.core.bus import EventBus
from digital_twin.core.events import Event, Topics
from digital_twin.memory.module import MemoryModule
from digital_twin.perception.chat.module import ChatPerceptionModule
from digital_twin.reasoning.chat_reasoner import ChatReasoner
from digital_twin.reasoning.llm import LLMError, ScriptedModel
from digital_twin.security.audit import AuditLog
from digital_twin.security.confirmation import ScriptedConfirmation
from digital_twin.security.permissions import PermissionPolicy, RiskLevel


@pytest.fixture()
def bus():
    bus = EventBus()
    bus.start()
    yield bus
    bus.stop()


def _decision(reply="ok", intent=None, remember=None, reasoning="because"):
    return json.dumps({"reply": reply, "intent": intent,
                       "remember": remember, "reasoning": reasoning})


def _wait(predicate, timeout=5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _record(bus, topic):
    events = []
    bus.subscribe(topic, events.append)
    return events


# ---------------------------------------------------------------------------
# Chat perception module
# ---------------------------------------------------------------------------
def test_submit_publishes_chat_events(bus):
    module = ChatPerceptionModule(ChatConfig(console=False))
    module.start(bus)
    try:
        events = _record(bus, Topics.CHAT)
        assert module.submit("  hello there  ") is True
        assert module.submit("   ") is False
        bus.flush(timeout=2.0)
        (event,) = events
        assert event.payload == {"text": "hello there", "user": "user"}
    finally:
        module.stop()


def test_console_reader_not_started_without_tty(bus):
    module = ChatPerceptionModule(ChatConfig(console=True))
    module.start(bus)  # pytest stdin is not a TTY
    try:
        assert module.status().metrics["console"] is False
    finally:
        module.stop()


def test_paused_chat_refuses_input(bus):
    module = ChatPerceptionModule(ChatConfig(console=False))
    module.start(bus)
    try:
        events = _record(bus, Topics.CHAT)
        module.pause()
        assert module.submit("ignored") is False
        module.resume()
        assert module.submit("heard") is True
        bus.flush(timeout=2.0)
        assert [e.payload["text"] for e in events] == ["heard"]
    finally:
        module.stop()


# ---------------------------------------------------------------------------
# Reasoner
# ---------------------------------------------------------------------------
def _reasoner(bus, model, allowed=("next_slide", "play_pause"), memory=None,
              config=None):
    reasoner = ChatReasoner(
        config or LLMConfig(memory_results=3),
        model=model,
        allowed_intents=allowed,
        memory=memory,
    )
    reasoner.start(bus)
    return reasoner


def test_reply_published_with_reasoning(bus):
    model = ScriptedModel([_decision(reply="Hello Boss", reasoning="greeting")])
    reasoner = _reasoner(bus, model)
    try:
        responses = _record(bus, Topics.CHAT_RESPONSE)
        bus.publish(Event(Topics.CHAT, "chat", {"text": "hi", "user": "u"}))
        assert _wait(lambda: responses)
        payload = responses[0].payload
        assert payload["text"] == "Hello Boss"
        assert payload["reasoning"] == "greeting"
    finally:
        reasoner.stop()


def test_persona_is_injected_into_the_system_prompt(bus):
    """KNOWA's identity (config.persona) must reach the model verbatim —
    the wiring that makes the assistant *be* KNOWA."""
    model = ScriptedModel([_decision(reply="Activated. How can I help?")])
    reasoner = _reasoner(bus, model)
    try:
        bus.publish(Event(Topics.CHAT, "chat", {"text": "KNOWA", "user": "u"}))
        assert _wait(lambda: model.calls)
        system = model.calls[0]["system"]
        assert LLMConfig().persona in system
        assert "KNOWA" in system and 'address the user as "boss"' in system.lower()
    finally:
        reasoner.stop()


def test_allowed_intent_is_published_with_provenance(bus):
    model = ScriptedModel([_decision(reply="Advancing", intent="next_slide")])
    reasoner = _reasoner(bus, model)
    try:
        intents = _record(bus, Topics.INTENT)
        chat_event = Event(Topics.CHAT, "chat", {"text": "next slide please",
                                                 "user": "u"})
        bus.publish(chat_event)
        assert _wait(lambda: intents)
        payload = intents[0].payload
        assert payload["intent"] == "next_slide"
        assert payload["source_event"] == chat_event.event_id
    finally:
        reasoner.stop()


def test_unknown_intent_from_model_is_dropped(bus):
    model = ScriptedModel([_decision(reply="Wiping disk!",
                                     intent="wipe_disk")])
    reasoner = _reasoner(bus, model)
    try:
        intents = _record(bus, Topics.INTENT)
        responses = _record(bus, Topics.CHAT_RESPONSE)
        bus.publish(Event(Topics.CHAT, "chat", {"text": "do bad things"}))
        assert _wait(lambda: responses)
        bus.flush(timeout=2.0)
        assert intents == []  # hallucinated intent never left the reasoner
        assert reasoner.status().metrics["intents_rejected"] == 1
    finally:
        reasoner.stop()


def test_memory_recall_enters_prompt_and_facts_persist(bus, tmp_path):
    memory = MemoryModule(MemoryConfig(db_path=str(tmp_path / "m.db")))
    memory.start(bus)
    memory.remember_fact("Boss uses IntelliJ for Java work",
                         tags=("preference",))
    model = ScriptedModel([_decision(
        reply="Noted", remember="Boss prefers window seat flights")])
    reasoner = _reasoner(bus, model, memory=memory)
    try:
        responses = _record(bus, Topics.CHAT_RESPONSE)
        bus.publish(Event(Topics.CHAT, "chat",
                          {"text": "remember my IntelliJ preference stuff"}))
        assert _wait(lambda: responses)
        # In: the stored fact reached the system prompt.
        system = model.calls[0]["system"]
        assert "IntelliJ" in system
        # Out: the model's remember field was persisted as semantic memory.
        hits = memory.store.search("window seat", kinds=("semantic",))
        assert hits and "window seat" in hits[0].record.content
        assert responses[0].payload["remembered"].startswith("Boss prefers")
    finally:
        reasoner.stop()
        memory.stop()


def test_conversation_history_is_bounded_and_used(bus):
    replies = [_decision(reply=f"r{i}") for i in range(4)]
    model = ScriptedModel(replies)
    reasoner = _reasoner(bus, model,
                         config=LLMConfig(history_turns=1, memory_results=0))
    try:
        responses = _record(bus, Topics.CHAT_RESPONSE)
        for i in range(4):
            bus.publish(Event(Topics.CHAT, "chat", {"text": f"q{i}"}))
            assert _wait(lambda: len(responses) == i + 1)
        # history_turns=1 → prompt holds at most one past exchange + new msg.
        last_messages = model.calls[-1]["messages"]
        assert len(last_messages) <= 3
        assert last_messages[-1] == ("user", "q3")
    finally:
        reasoner.stop()


def test_context_events_steer_the_prompt(bus):
    model = ScriptedModel([_decision()])
    reasoner = _reasoner(bus, model)
    try:
        responses = _record(bus, Topics.CHAT_RESPONSE)
        bus.publish(Event(Topics.CONTEXT, "screen", {"context": "coding"}))
        bus.flush(timeout=2.0)
        bus.publish(Event(Topics.CHAT, "chat", {"text": "what now?"}))
        assert _wait(lambda: responses)
        assert "Current application context: coding" in model.calls[0]["system"]
    finally:
        reasoner.stop()


def test_llm_failure_degrades_to_apology(bus):
    class BrokenModel(ScriptedModel):
        def complete(self, *args, **kwargs):
            raise LLMError("model on fire")

    reasoner = _reasoner(bus, BrokenModel())
    try:
        responses = _record(bus, Topics.CHAT_RESPONSE)
        bus.publish(Event(Topics.CHAT, "chat", {"text": "hello?"}))
        assert _wait(lambda: responses)
        assert "model on fire" in responses[0].payload["text"]
        assert reasoner.is_active  # still running, not crashed
    finally:
        reasoner.stop()


def test_paused_reasoner_ignores_chat(bus):
    model = ScriptedModel([_decision()])
    reasoner = _reasoner(bus, model)
    try:
        responses = _record(bus, Topics.CHAT_RESPONSE)
        reasoner.pause()
        bus.publish(Event(Topics.CHAT, "chat", {"text": "anyone home?"}))
        bus.flush(timeout=2.0)
        time.sleep(0.1)
        assert responses == []
        reasoner.resume()
        bus.publish(Event(Topics.CHAT, "chat", {"text": "now?"}))
        assert _wait(lambda: responses)
    finally:
        reasoner.stop()


# ---------------------------------------------------------------------------
# The full chain: chat → reasoner → dispatcher → gated action
# ---------------------------------------------------------------------------
def test_chat_request_executes_action_through_all_gates(bus, tmp_path):
    executed = []
    registry = ActionRegistry()
    registry.register(ActionSpec(
        name="advance", description="t", risk=RiskLevel.SAFE,
        handler=lambda p: executed.append("advance") or "done"))
    dispatcher = ActionDispatcher(
        config=AutomationConfig(intent_bindings={
            "next_slide": {"action": "advance"}}),
        registry=registry,
        policy=PermissionPolicy(risk_defaults={"safe": "allow"}),
        confirmation=ScriptedConfirmation(),
        audit=AuditLog(tmp_path / "audit.jsonl"),
    )
    dispatcher.start(bus)

    chat = ChatPerceptionModule(ChatConfig(console=False))
    chat.start(bus)
    model = ScriptedModel([_decision(reply="On it", intent="next_slide")])
    reasoner = _reasoner(bus, model, allowed=("next_slide",))

    results = _record(bus, Topics.ACTION_RESULT)
    try:
        chat.submit("go to the next slide please")
        assert _wait(lambda: executed)
        assert _wait(lambda: results)
        payload = results[0].payload
        assert payload["status"] == "completed"
        assert payload["intent"] == "next_slide"
        # Provenance chain: action result → chat event that caused it.
        assert payload["perception_event"]
        audit = AuditLog(tmp_path / "audit.jsonl").tail()
        assert any(entry["status"] == "completed" for entry in audit)
    finally:
        reasoner.stop()
        chat.stop()
        dispatcher.stop()
