#!/usr/bin/env python3
"""End-to-end demo of the perception → reasoning event chain — no hardware.

Publishes synthetic gesture events (exactly what the camera-backed gesture
module would publish) and shows how the *same* gesture is interpreted
differently depending on the active application context:

    presentation:  thumbs_up  → next_slide
    media:         thumbs_up  → like
    coding:        thumbs_up  → accept_suggestion
    any context:   open_palm  → assistant_attention   (wildcard fallback)

Run from the repository root::

    python examples/gesture_to_intent_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from digital_twin.configuration.settings import IntentConfig  # noqa: E402
from digital_twin.core.bus import EventBus  # noqa: E402
from digital_twin.core.events import Event, Topics  # noqa: E402
from digital_twin.reasoning.intent import IntentEngine  # noqa: E402


def gesture_event(gesture: str, hand: str = "right", confidence: float = 0.97) -> Event:
    """Build the exact event the gesture perception module publishes."""
    return Event(
        topic=Topics.GESTURE,
        source="gesture",
        payload={
            "gesture": gesture,
            "confidence": confidence,
            "hand": hand,
            "repeat": False,
        },
    )


def main() -> None:
    bus = EventBus()
    bus.start()

    engine = IntentEngine(IntentConfig())
    engine.start(bus)

    bus.subscribe(
        Topics.INTENT,
        lambda e: print(
            f"   -> INTENT: {e.payload['intent']!r}"
            f"  (context={e.payload['context']}, from {e.payload['gesture']}"
            f" @ {e.payload['confidence']})"
        ),
        name="demo.printer",
    )

    scenario: list[tuple[str, str]] = [
        ("presentation", "thumbs_up"),
        ("presentation", "thumbs_down"),
        ("media", "thumbs_up"),
        ("media", "peace"),
        ("coding", "thumbs_up"),
        ("desktop", "thumbs_up"),   # unmapped in this context: no intent
        ("desktop", "open_palm"),   # wildcard fallback fires
        ("media", "high_five"),     # alias -> open_palm -> wildcard
    ]

    current_context = None
    for context, gesture in scenario:
        if context != current_context:
            current_context = context
            print(f"\n== context: {context} ==")
            # Exactly how a future screen-context module would announce it.
            bus.publish(Event(Topics.CONTEXT, "demo", {"context": context}))
            bus.flush()
        print(f" gesture: {gesture}")
        bus.publish(gesture_event(gesture))
        bus.flush()

    print(f"\nBus stats: {bus.stats}")
    engine.stop()
    bus.stop()


if __name__ == "__main__":
    main()
