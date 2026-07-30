#!/usr/bin/env python3
"""Hands-free context demo — the same thumbs-up, three different meanings.

A scripted window probe simulates the user switching applications; the real
screen-context module classifies each window, the real intent engine
re-interprets the identical gesture accordingly. No camera, no xdotool, no
manual ``--context`` anywhere — which is the entire point of Milestone 4.

Run from the repository root::

    python examples/hands_free_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from digital_twin.configuration.settings import (  # noqa: E402
    ContextPerceptionConfig,
    IntentConfig,
)
from digital_twin.core.bus import EventBus  # noqa: E402
from digital_twin.core.events import Event, Topics  # noqa: E402
from digital_twin.perception.context.module import ContextPerceptionModule  # noqa: E402
from digital_twin.perception.context.probe import WindowInfo, WindowProbe  # noqa: E402
from digital_twin.reasoning.intent import IntentEngine  # noqa: E402


class ScriptedProbe(WindowProbe):
    """Stands in for xdotool/win32/osascript with a fixed 'user session'."""

    name = "scripted"

    def __init__(self):
        self.window: WindowInfo | None = None

    @classmethod
    def available(cls) -> bool:
        return True

    def active_window(self) -> WindowInfo | None:
        return self.window


def main() -> None:
    bus = EventBus()
    bus.start()
    bus.subscribe(
        Topics.INTENT,
        lambda e: print(f"   -> INTENT: {e.payload['intent']!r}"
                        f"  (context={e.payload['context']})"),
        name="demo.intents",
    )

    engine = IntentEngine(IntentConfig())
    engine.start(bus)

    probe = ScriptedProbe()
    context_module = ContextPerceptionModule(
        ContextPerceptionConfig(poll_interval_s=3600),  # we drive it manually
        probe_factory=lambda: probe,
    )
    context_module.start(bus)

    def thumbs_up() -> None:
        print(" gesture: thumbs_up")
        bus.publish(Event(Topics.GESTURE, "gesture", {
            "gesture": "thumbs_up", "confidence": 0.97,
            "hand": "right", "repeat": False,
        }))
        bus.flush()

    session = [
        WindowInfo("Quarterly Review - LibreOffice Impress", "soffice"),
        WindowInfo("lo-fi beats to code to - YouTube - Firefox", "firefox"),
        WindowInfo("dispatcher.py - digital-twin - Visual Studio Code", "code"),
    ]
    for window in session:
        probe.window = window
        print(f"\n== focus: {window.title} ==")
        context_module._process_window(window)  # what one poll tick does
        bus.flush()
        thumbs_up()

    context_module.stop()
    engine.stop()
    bus.stop()


if __name__ == "__main__":
    main()
