#!/usr/bin/env python3
"""Conversational demo — talk, be understood, act through the gates.

A scripted language model (no API key needed) stands in for Claude; every
other component is real: chat perception, the reasoner, memory, the
dispatcher with its permission pipeline. The transcript shows all four
reasoning outcomes:

1. plain conversation (reply only),
2. teaching a fact — persisted to semantic memory,
3. recalling it later — memory is injected into the prompt,
4. "next slide please" — the model nominates an allow-listed intent, which
   executes through the SAME gates a gesture would,
5. a hostile/hallucinated intent — dropped before it ever reaches the bus.

With a real key: get a free one at https://aistudio.google.com/apikey,
``export GEMINI_API_KEY=... && python main.py`` and just type into the
kernel terminal (or set ``llm.provider: anthropic``/``ollama`` instead).

Run from the repository root::

    python examples/chat_demo.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from tempfile import mkdtemp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from digital_twin.automation.dispatcher import ActionDispatcher  # noqa: E402
from digital_twin.automation.registry import ActionRegistry, ActionSpec  # noqa: E402
from digital_twin.configuration.settings import (  # noqa: E402
    AutomationConfig,
    ChatConfig,
    LLMConfig,
    MemoryConfig,
)
from digital_twin.core.bus import EventBus  # noqa: E402
from digital_twin.core.events import Topics  # noqa: E402
from digital_twin.memory.module import MemoryModule  # noqa: E402
from digital_twin.perception.chat.module import ChatPerceptionModule  # noqa: E402
from digital_twin.reasoning.chat_reasoner import ChatReasoner  # noqa: E402
from digital_twin.reasoning.llm import ScriptedModel  # noqa: E402
from digital_twin.security.audit import AuditLog  # noqa: E402
from digital_twin.security.confirmation import ScriptedConfirmation  # noqa: E402
from digital_twin.security.permissions import (  # noqa: E402
    PermissionPolicy,
    RiskLevel,
)


def decision(reply, intent=None, remember=None, reasoning=""):
    return json.dumps({"reply": reply, "intent": intent,
                       "remember": remember, "reasoning": reasoning})


def main() -> None:
    workdir = Path(mkdtemp())
    bus = EventBus()
    bus.start()

    memory = MemoryModule(MemoryConfig(db_path=str(workdir / "memory.db")))
    memory.start(bus)

    registry = ActionRegistry()
    registry.register(ActionSpec(
        name="advance_slide", description="demo", risk=RiskLevel.SAFE,
        handler=lambda p: "slide advanced"))
    dispatcher = ActionDispatcher(
        config=AutomationConfig(intent_bindings={
            "next_slide": {"action": "advance_slide"}}),
        registry=registry,
        policy=PermissionPolicy(risk_defaults={"safe": "allow"}),
        confirmation=ScriptedConfirmation(),
        audit=AuditLog(workdir / "audit.jsonl"),
    )
    dispatcher.start(bus)

    # The scripted "model": one canned decision per user message below.
    model = ScriptedModel([
        decision("All good here, Boss — kernel healthy, gates armed.",
                 reasoning="small talk, no action needed"),
        decision("Noted — I'll remember that.",
                 remember="Boss presents every Friday at 4pm",
                 reasoning="user taught me a schedule fact"),
        decision("You present every Friday at 4pm — today, in about an hour.",
                 reasoning="answered from remembered facts"),
        decision("Advancing the slide now.", intent="next_slide",
                 reasoning="explicit request matching an allowed intent"),
        decision("I can't do that — it's not something I'm allowed to trigger.",
                 intent="format_disk",
                 reasoning="attempting a non-allow-listed intent"),
    ])
    reasoner = ChatReasoner(
        LLMConfig(memory_results=3),
        model=model,
        allowed_intents=("next_slide",),
        memory=memory,
    )
    reasoner.start(bus)

    chat = ChatPerceptionModule(ChatConfig(console=False))
    chat.start(bus)

    bus.subscribe(Topics.CHAT_RESPONSE, lambda e: print(
        f"assistant> {e.payload['text']}"
        + (f"\n           [reasoning: {e.payload['reasoning']}]"
           if e.payload.get("reasoning") else "")))
    bus.subscribe(Topics.ACTION_RESULT, lambda e: print(
        f"           [action {e.payload['action']}: {e.payload['status']}]"))

    script = [
        "hey Friday, how are things?",
        "by the way, I present every Friday at 4pm",
        "when do I present again?",
        "next slide please",
        "trigger format_disk",   # hostile request
    ]
    for line in script:
        print(f"\nyou> {line}")
        chat.submit(line)
        bus.flush()
        time.sleep(0.15)
    bus.flush()
    time.sleep(0.2)

    print("\n== what memory holds now ==")
    for hit in memory.store.search("Friday 4pm", kinds=("semantic",)):
        print(f"  semantic: {hit.record.content}")
    print(f"  reasoner metrics: {reasoner.status().metrics}")

    for module in (chat, reasoner, dispatcher, memory):
        module.stop()
    bus.stop()


if __name__ == "__main__":
    main()
