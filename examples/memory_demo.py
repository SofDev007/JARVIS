#!/usr/bin/env python3
"""Memory demo — the assistant remembers what it did, and you stay in charge.

A short simulated session (gestures → intents → actions) runs through the
real pipeline with the memory module listening. Afterwards we ask the
store what happened: ranked search over episodic memory, the working-
memory activity buffer, a user-taught semantic fact — and finally the user
control that matters most: deleting a memory and proving it's gone.

Run from the repository root::

    python examples/memory_demo.py

The same inspection is available anytime against the real database via
``python -m digital_twin.memory stats|list|search|delete ...``.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path
from tempfile import mkdtemp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from digital_twin.automation.dispatcher import ActionDispatcher  # noqa: E402
from digital_twin.automation.registry import ActionRegistry, ActionSpec  # noqa: E402
from digital_twin.configuration.settings import (  # noqa: E402
    AutomationConfig,
    IntentConfig,
    MemoryConfig,
)
from digital_twin.core.bus import EventBus  # noqa: E402
from digital_twin.core.events import Event, Topics  # noqa: E402
from digital_twin.memory.module import MemoryModule  # noqa: E402
from digital_twin.reasoning.intent import IntentEngine  # noqa: E402
from digital_twin.security.audit import AuditLog  # noqa: E402
from digital_twin.security.confirmation import ScriptedConfirmation  # noqa: E402
from digital_twin.security.permissions import (  # noqa: E402
    PermissionPolicy,
    RiskLevel,
)


def main() -> None:
    workdir = Path(mkdtemp())
    bus = EventBus()
    bus.start()

    engine = IntentEngine(IntentConfig(
        default_context="media",
        mappings={"media": {"thumbs_up": "like", "peace": "play_pause"},
                  "presentation": {"thumbs_up": "next_slide"}},
    ))
    engine.start(bus)

    registry = ActionRegistry()
    registry.register(ActionSpec(
        name="do_thing", description="demo action", risk=RiskLevel.SAFE,
        handler=lambda params: f"did {params.get('what')}",
    ))
    dispatcher = ActionDispatcher(
        config=AutomationConfig(intent_bindings={
            "like": {"action": "do_thing", "params": {"what": "like"}},
            "play_pause": {"action": "do_thing", "params": {"what": "play/pause"}},
            "next_slide": {"action": "do_thing", "params": {"what": "next slide"}},
        }),
        registry=registry,
        policy=PermissionPolicy(risk_defaults={"safe": "allow"}),
        confirmation=ScriptedConfirmation(),
        audit=AuditLog(workdir / "audit.jsonl"),
    )
    dispatcher.start(bus)

    memory = MemoryModule(MemoryConfig(db_path=str(workdir / "memory.db")))
    memory.start(bus)

    def gesture(name: str, context: str) -> None:
        bus.publish(Event(Topics.CONTEXT, "demo", {"context": context}))
        bus.flush()
        bus.publish(Event(Topics.GESTURE, "gesture", {
            "gesture": name, "confidence": 0.95, "hand": "right",
            "repeat": False,
        }))
        bus.flush()
        time.sleep(0.1)  # let the action worker finish and memory persist

    print("== simulated session ==")
    gesture("thumbs_up", "media")          # like
    gesture("peace", "media")              # play/pause
    gesture("thumbs_up", "presentation")   # next slide
    bus.flush()
    time.sleep(0.2)

    store = memory.store

    print("\n== episodic recall: search 'slide' ==")
    for hit in store.search("slide", kinds=("episodic",), limit=3):
        stamp = datetime.fromtimestamp(hit.record.created_at).strftime("%H:%M:%S")
        print(f"  [{stamp}] ({hit.score:.2f}) {hit.record.content}")

    print("\n== working memory (recent activity) ==")
    for item in reversed(memory.working.recent(4)):
        print(f"  {item.topic}: {item.summary[:70]}")

    print("\n== teaching a fact ==")
    fact = memory.remember_fact(
        "Boss prefers dark theme and Hinglish replies",
        tags=("preference",),
    )
    print(f"  stored semantic memory {fact.id[:8]}: {fact.content!r}")

    print("\n== user control: forget it again ==")
    store.delete(fact.id)
    remaining = store.count("semantic")
    print(f"  deleted {fact.id[:8]}; semantic memories left: {remaining}")

    counts = {kind: store.count(kind) for kind in ("episodic", "semantic", "working")}
    print(f"\n== stats ==\n  {counts}")

    memory.stop()
    dispatcher.stop()
    engine.stop()
    bus.stop()


if __name__ == "__main__":
    main()
