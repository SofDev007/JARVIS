#!/usr/bin/env python3
"""Presentation control demo — the M5 payoff, end to end, no hardware.

Simulated session: focus lands on Impress (context module classifies it),
then thumbs-up gestures advance slides through the *real* dispatcher with
**zero confirmation prompts** (arrow keys are SAFE by design), a
thumbs-down goes back, and a closed fist ends the show — which is a
keyboard chord, so it hits the confirmation gate before Escape is pressed.

A fake input backend prints what would reach the OS; swap in xdotool on a
real machine and this is live presentation control.

Run from the repository root::

    python examples/presentation_demo.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from tempfile import mkdtemp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from digital_twin.automation.dispatcher import ActionDispatcher  # noqa: E402
from digital_twin.automation.input_actions import register_input_actions  # noqa: E402
from digital_twin.automation.input_backend import InputBackend  # noqa: E402
from digital_twin.automation.registry import ActionRegistry  # noqa: E402
from digital_twin.configuration.settings import (  # noqa: E402
    AutomationConfig,
    ContextPerceptionConfig,
    IntentConfig,
)
from digital_twin.core.bus import EventBus  # noqa: E402
from digital_twin.core.events import Event, Topics  # noqa: E402
from digital_twin.perception.context.module import ContextPerceptionModule  # noqa: E402
from digital_twin.perception.context.probe import WindowInfo, WindowProbe  # noqa: E402
from digital_twin.reasoning.intent import IntentEngine  # noqa: E402
from digital_twin.security.audit import AuditLog  # noqa: E402
from digital_twin.security.confirmation import ScriptedConfirmation  # noqa: E402
from digital_twin.security.permissions import PermissionPolicy  # noqa: E402


class PrintingBackend(InputBackend):
    """Shows what would be synthesised instead of touching the OS."""

    name = "printing"

    @classmethod
    def available(cls) -> bool:
        return True

    def press_keys(self, keys):
        print(f"      [os] key press: {'+'.join(keys)}")

    def type_text(self, text):
        print(f"      [os] type: {text!r}")

    def set_clipboard(self, text):
        print(f"      [os] clipboard <- {text!r}")

    def focus_window(self, title_substring):
        print(f"      [os] focus window ~ {title_substring!r}")
        return True


class AnnouncingConfirmation(ScriptedConfirmation):
    """Prints the gate synchronously so the demo output reads in true order."""

    def request(self, action, params, timeout_s):
        print(f"   .. confirmation required: {action}")
        approved = super().request(action, params, timeout_s)
        print(f"   .. confirmation {'approved' if approved else 'denied'}: {action}")
        return approved


class FixedProbe(WindowProbe):
    name = "fixed"

    def __init__(self, window: WindowInfo):
        self.window = window

    @classmethod
    def available(cls) -> bool:
        return True

    def active_window(self):
        return self.window


def main() -> None:
    bus = EventBus()
    bus.start()
    bus.subscribe(Topics.INTENT, lambda e: print(
        f"   -> intent: {e.payload['intent']} (context={e.payload['context']})"))
    bus.subscribe(Topics.ACTION_RESULT, lambda e: print(
        f"   => {e.payload['action']}: {e.payload['status']}"
        f" ({e.payload.get('detail', '')})"))

    engine = IntentEngine(IntentConfig())
    engine.start(bus)

    context = ContextPerceptionModule(
        ContextPerceptionConfig(poll_interval_s=3600),
        probe_factory=lambda: FixedProbe(
            WindowInfo("Quarterly Review - LibreOffice Impress", "soffice")),
    )
    context.start(bus)

    registry = ActionRegistry()
    register_input_actions(registry, backend_factory=PrintingBackend)
    dispatcher = ActionDispatcher(
        config=AutomationConfig(),  # default bindings: nav_key / press_keys
        registry=registry,
        policy=PermissionPolicy(risk_defaults={
            "safe": "allow", "sensitive": "confirm", "dangerous": "deny"}),
        confirmation=AnnouncingConfirmation([True]),  # approve ending the show
        audit=AuditLog(Path(mkdtemp()) / "audit.jsonl"),
    )
    dispatcher.start(bus)

    context._process_window(context._probe.window)  # one poll tick
    bus.flush()

    def gesture(name: str) -> None:
        print(f"\n gesture: {name}")
        bus.publish(Event(Topics.GESTURE, "gesture", {
            "gesture": name, "confidence": 0.96, "hand": "right",
            "repeat": False,
        }))
        bus.flush()
        time.sleep(0.1)  # let the action worker report

    print("== presenting: three slides forward, one back, then end ==")
    for _ in range(3):
        gesture("thumbs_up")
    gesture("thumbs_down")
    gesture("closed_fist")  # end_presentation → press_keys escape → gate

    dispatcher.stop()
    context.stop()
    engine.stop()
    bus.stop()


if __name__ == "__main__":
    main()
