"""The ``speak`` action — even the assistant's own voice passes the gates.

Spoken output is registered as a normal SAFE action, which buys three
things for free: it appears in the audit log like everything else, users
can *mute the assistant* with one permission rule
(``security.permissions: {speak: deny}``), and the LLM/planner can use it
in plans without any special casing. The synthesizer instance is shared
with the voice module (kernel-level composition) so barge-in interruption
can kill an utterance the action started.
"""

from __future__ import annotations

from typing import Any, Mapping

from digital_twin.automation.registry import ActionRegistry, ActionSpec
from digital_twin.security.permissions import RiskLevel
from digital_twin.voice.synthesis import SpeechSynthesizer

MAX_SPEAK_CHARS = 1000


def register_voice_actions(
    registry: ActionRegistry, synthesizer: SpeechSynthesizer
) -> None:
    """Register spoken-output actions onto ``registry``."""

    def validate_speak(params: Mapping[str, Any]) -> None:
        text = params.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("speak requires a non-empty 'text' string")
        if len(text) > MAX_SPEAK_CHARS:
            raise ValueError(f"speak: text exceeds {MAX_SPEAK_CHARS} characters")
        if any(ord(ch) < 32 and ch not in "\t " for ch in text):
            raise ValueError("speak: control characters are not allowed")

    def handle_speak(params: Mapping[str, Any]) -> str:
        return synthesizer.speak(str(params["text"]).strip())

    registry.register(ActionSpec(
        name="speak",
        description="Say text aloud (non-blocking; interruptible by voice).",
        risk=RiskLevel.SAFE,
        handler=handle_speak,
        validate=validate_speak,
    ))
