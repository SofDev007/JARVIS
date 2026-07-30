#!/usr/bin/env python3
"""End-to-end demo of the full guarded pipeline — no hardware, no prompts.

Synthetic gesture events flow through the real intent engine into the real
action dispatcher, demonstrating every security outcome:

* a SAFE action executing without any prompt,
* a SENSITIVE action approved at the confirmation gate,
* the same SENSITIVE action denied at the gate,
* an intent with no binding (audited, ignored),

and finishes by printing the audit trail — every decision, with provenance
back to the exact perception event that caused it.

Run from the repository root::

    python examples/intent_to_action_demo.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from digital_twin.automation.dispatcher import ActionDispatcher  # noqa: E402
from digital_twin.automation.registry import ActionRegistry  # noqa: E402
from digital_twin.automation.builtin import register_builtin_actions  # noqa: E402
from digital_twin.configuration.settings import (  # noqa: E402
    AutomationConfig,
    IntentConfig,
)
from digital_twin.core.bus import EventBus  # noqa: E402
from digital_twin.core.events import Event, Topics  # noqa: E402
from digital_twin.reasoning.intent import IntentEngine  # noqa: E402
from digital_twin.security.audit import AuditLog  # noqa: E402
from digital_twin.security.confirmation import ScriptedConfirmation  # noqa: E402
from digital_twin.security.permissions import PermissionPolicy  # noqa: E402


def gesture(name: str) -> Event:
    return Event(Topics.GESTURE, "gesture", {
        "gesture": name, "confidence": 0.95, "hand": "right", "repeat": False,
    })


def main() -> None:
    audit_path = Path(tempfile.mkdtemp()) / "audit.jsonl"

    bus = EventBus()
    bus.start()
    bus.subscribe(
        Topics.ACTION_RESULT,
        lambda e: print(f"   -> RESULT: {e.payload['action']}: "
                        f"{e.payload['status']}  ({e.payload.get('detail', '')})"),
        name="demo.results",
    )
    bus.subscribe(
        Topics.CONFIRMATION,
        lambda e: print(f"   .. confirmation {e.payload['state']}: "
                        f"{e.payload['action']}"),
        name="demo.confirmations",
    )

    engine = IntentEngine(IntentConfig(
        default_context="media",
        mappings={"media": {
            "thumbs_up": "like",
            "open_palm": "assistant_attention",
            "peace": "play_pause",           # deliberately unbound
            "pointing_up": "open_dashboard",
        }},
    ))
    engine.start(bus)

    registry = ActionRegistry()
    register_builtin_actions(registry)
    # Scripted gate: first request approved, second denied — a stand-in for
    # the interactive console/UI prompt so the demo runs unattended.
    confirmation = ScriptedConfirmation([True, False])
    dispatcher = ActionDispatcher(
        config=AutomationConfig(intent_bindings={
            "like": {"action": "log_message",
                     "params": {"message": "Liked the current item."}},
            "assistant_attention": {"action": "notify",
                                    "params": {"title": "Digital Twin",
                                               "message": "At your service."}},
            "open_dashboard": {"action": "open_url",
                               "params": {"url": "https://example.com/dashboard"}},
        }),
        registry=registry,
        policy=PermissionPolicy(
            risk_defaults={"safe": "allow", "sensitive": "confirm",
                           "dangerous": "deny"}),
        confirmation=confirmation,
        audit=AuditLog(audit_path),
    )
    dispatcher.start(bus)

    scenario = [
        ("SAFE action, no prompt needed", "thumbs_up"),
        ("SAFE notify (falls back to log headless)", "open_palm"),
        ("SENSITIVE action -> gate approves", "pointing_up"),
        ("SENSITIVE action -> gate denies", "pointing_up"),
        ("intent with no binding (audited, ignored)", "peace"),
    ]
    for label, name in scenario:
        print(f"\n== {label} ==\n gesture: {name}")
        bus.publish(gesture(name))
        bus.flush()
        time.sleep(0.15)  # let the worker finish before the next step

    print("\n---- audit trail " + "-" * 44)
    for entry in AuditLog(audit_path).tail(20):
        print(f"  {entry['timestamp'][11:19]}  {entry['status']:<10}"
              f" action={entry.get('action')}  intent={entry.get('intent')}"
              f"  perception={str(entry.get('perception_event'))[:8]}")

    dispatcher.stop()
    engine.stop()
    bus.stop()


if __name__ == "__main__":
    main()
