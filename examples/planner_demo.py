#!/usr/bin/env python3
"""Planner demo — decompose, gate every step, learn the workflow.

Three acts, all through the real dispatcher pipeline (a printing backend
stands in for the OS; a scripted model stands in for Claude):

1. **A configured routine** runs stage by stage — the parallel stage's
   steps are published together, the next stage waits for both.
2. **Chat proposes a plan**: "get me ready to present" becomes a
   multi-step plan; its SENSITIVE step (starting the show is a keyboard
   chord) hits the confirmation gate mid-plan.
3. **The workflow was learned**: the successful LLM plan is now a skill
   memory, replayed by a plain-text query — no model call needed.

Run from the repository root::

    python examples/planner_demo.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from tempfile import mkdtemp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from digital_twin.automation.dispatcher import ActionDispatcher  # noqa: E402
from digital_twin.automation.input_actions import register_input_actions  # noqa: E402
from digital_twin.automation.registry import ActionRegistry  # noqa: E402
from digital_twin.configuration.settings import (  # noqa: E402
    AutomationConfig,
    ChatConfig,
    LLMConfig,
    MemoryConfig,
    PlannerConfig,
)
from digital_twin.core.bus import EventBus  # noqa: E402
from digital_twin.core.events import Event, Topics  # noqa: E402
from digital_twin.memory.module import MemoryModule  # noqa: E402
from digital_twin.perception.chat.module import ChatPerceptionModule  # noqa: E402
from digital_twin.planner.module import PlannerModule  # noqa: E402
from digital_twin.reasoning.chat_reasoner import ChatReasoner  # noqa: E402
from digital_twin.reasoning.llm import ScriptedModel  # noqa: E402
from digital_twin.security.audit import AuditLog  # noqa: E402
from digital_twin.security.confirmation import ScriptedConfirmation  # noqa: E402
from digital_twin.security.permissions import PermissionPolicy  # noqa: E402

from examples.presentation_demo import PrintingBackend  # noqa: E402


class AnnouncingConfirmation(ScriptedConfirmation):
    def request(self, action, params, timeout_s):
        approved = super().request(action, params, timeout_s)
        print(f"   .. confirmation {'approved' if approved else 'denied'}:"
              f" {action} {dict(params)}")
        return approved


def main() -> None:
    workdir = Path(mkdtemp())
    bus = EventBus()
    bus.start()

    memory = MemoryModule(MemoryConfig(db_path=str(workdir / "memory.db")))
    memory.start(bus)

    registry = ActionRegistry()
    register_input_actions(registry, backend_factory=PrintingBackend)
    dispatcher = ActionDispatcher(
        config=AutomationConfig(intent_bindings={}),
        registry=registry,
        policy=PermissionPolicy(risk_defaults={
            "safe": "allow", "sensitive": "confirm", "dangerous": "deny"}),
        confirmation=AnnouncingConfirmation([True] * 4),  # both plan runs:
        # approvals are NEVER persisted — a replayed skill is re-gated
        # step by step, exactly like the first run
        audit=AuditLog(workdir / "audit.jsonl"),
    )
    dispatcher.start(bus)

    planner = PlannerModule(
        PlannerConfig(plans={
            "wind_down": {
                "description": "End-of-day media wind down",
                "steps": [
                    {"action": "media_key", "params": {"key": "play_pause"},
                     "label": "Pause media"},
                    {"parallel": [
                        {"action": "media_key", "params": {"key": "mute"},
                         "label": "Mute"},
                        {"action": "nav_key", "params": {"key": "down"},
                         "label": "Scroll a notch"},
                    ]},
                ],
            },
        }),
        memory=memory,
    )
    planner.start(bus)

    model = ScriptedModel([json.dumps({
        "reply": "Setting up presentation mode — focusing the deck and "
                 "starting the show.",
        "plan": {
            "goal": "get ready to present",
            "steps": [
                {"action": "focus_window", "params": {"title": "Impress"},
                 "label": "Focus the deck"},
                {"action": "press_keys", "params": {"keys": ["f5"]},
                 "label": "Start the show"},
            ],
        },
        "reasoning": "multi-step request; no single intent covers it",
    })])
    reasoner = ChatReasoner(
        LLMConfig(memory_results=0),
        model=model,
        allowed_intents=(),
        memory=memory,
        action_catalog=dispatcher.actions_catalog(),
    )
    reasoner.start(bus)
    chat = ChatPerceptionModule(ChatConfig(console=False))
    chat.start(bus)

    bus.subscribe(Topics.PLAN_PROGRESS, lambda e: print(
        f"   [plan {e.payload['plan']!r} ({e.payload['plan_source']})] "
        f"{e.payload['status']}"
        + (f" step {e.payload['step']}" if "step" in e.payload else "")
        + (f" — {e.payload['detail']}" if e.payload.get("detail") else "")
        if e.payload.get("plan_id") else
        f"   [plan {e.payload['plan']!r}] {e.payload['status']} — "
        f"{e.payload.get('detail', '')}"))
    bus.subscribe(Topics.CHAT_RESPONSE, lambda e: print(
        f"assistant> {e.payload['text']}"))

    def settle(seconds=0.6):
        bus.flush()
        time.sleep(seconds)

    print("== act 1: configured routine ==")
    bus.publish(Event(Topics.PLAN_REQUEST, "demo", {"plan": "wind_down"}))
    settle()

    print("\n== act 2: chat proposes a plan (sensitive step gets gated) ==")
    print("you> get me ready to present")
    chat.submit("get me ready to present")
    settle(1.0)

    print("\n== act 3: the workflow was learned — replay it as a skill ==")
    print("   (note: replays are re-confirmed — approvals never persist)")
    print(f"   skills in memory: {memory.store.count('skill')}")
    bus.publish(Event(Topics.PLAN_REQUEST, "demo",
                      {"skill": "ready to present"}))
    settle(1.0)

    for module in (chat, reasoner, planner, dispatcher, memory):
        module.stop()
    bus.stop()


if __name__ == "__main__":
    main()
