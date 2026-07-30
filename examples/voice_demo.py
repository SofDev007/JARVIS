#!/usr/bin/env python3
"""Voice demo — raise a hand, speak, be understood, hear the answer.

Scripted audio and transcription stand in for the microphone and Vosk
(nothing here needs hardware); the reasoner, dispatcher and gates are
real. Three acts:

1. **Open palm = push-to-talk**: the ``assistant_attention`` intent
   toggles listening; the microphone opens *only* for the session —
   watch the open/close counters.
2. **"next slide please"** streams partials, finalises, flows through the
   reasoner like any chat message, and the reply is spoken back via the
   gated ``speak`` action (audited like everything else).
3. **Barge-in**: the user talks over the assistant — speech stops the
   instant a partial arrives.

Real usage: install espeak-ng, ``pip install vosk sounddevice``, download
a model from https://alphacephei.com/vosk/models into ``models/``, then
``python main.py`` — an open palm is your push-to-talk button.

Run from the repository root::

    python examples/voice_demo.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from tempfile import mkdtemp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from digital_twin.automation.dispatcher import ActionDispatcher  # noqa: E402
from digital_twin.automation.registry import ActionRegistry  # noqa: E402
from digital_twin.configuration.settings import (  # noqa: E402
    AutomationConfig,
    LLMConfig,
    VoiceConfig,
)
from digital_twin.core.bus import EventBus  # noqa: E402
from digital_twin.core.events import Event, Topics  # noqa: E402
from digital_twin.reasoning.chat_reasoner import ChatReasoner  # noqa: E402
from digital_twin.reasoning.llm import ScriptedModel  # noqa: E402
from digital_twin.security.audit import AuditLog  # noqa: E402
from digital_twin.security.confirmation import ScriptedConfirmation  # noqa: E402
from digital_twin.security.permissions import PermissionPolicy  # noqa: E402
from digital_twin.voice.actions import register_voice_actions  # noqa: E402
from digital_twin.voice.audio import ScriptedAudioSource  # noqa: E402
from digital_twin.voice.module import VoicePerceptionModule  # noqa: E402
from digital_twin.voice.synthesis import FakeSynthesizer  # noqa: E402
from digital_twin.voice.transcriber import (  # noqa: E402
    ScriptedTranscriber,
    TranscriptChunk,
)


class PrintingSynthesizer(FakeSynthesizer):
    def speak(self, text):
        print(f"      [tts] speaking: {text!r}")
        return super().speak(text)

    def stop(self):
        stopped = super().stop()
        if stopped:
            print("      [tts] interrupted mid-sentence")
        return stopped


def main() -> None:
    workdir = Path(mkdtemp())
    bus = EventBus()
    bus.start()

    synthesizer = PrintingSynthesizer()
    registry = ActionRegistry()
    register_voice_actions(registry, synthesizer)
    dispatcher = ActionDispatcher(
        config=AutomationConfig(intent_bindings={}),
        registry=registry,
        policy=PermissionPolicy(risk_defaults={"safe": "allow"}),
        confirmation=ScriptedConfirmation(),
        audit=AuditLog(workdir / "audit.jsonl"),
    )
    dispatcher.start(bus)

    model = ScriptedModel([
        json.dumps({
            "reply": "Advancing to the next slide for you now, Boss.",
            "intent": None,
            "reasoning": "spoken request handled conversationally in this demo",
        }),
        json.dumps({
            "reply": "Stopping.",
            "intent": None,
            "reasoning": "user interrupted",
        }),
    ])
    reasoner = ChatReasoner(LLMConfig(memory_results=0), model=model,
                            allowed_intents=())
    reasoner.start(bus)

    source = ScriptedAudioSource([b"chunk"] * 10)
    voice = VoicePerceptionModule(
        VoiceConfig(),
        source_factory=lambda: source,
        transcriber_factory=lambda: ScriptedTranscriber([
            TranscriptChunk("next", final=False),
            TranscriptChunk("next slide", final=False),
            TranscriptChunk("next slide please", final=True),
        ]),
        synthesizer=synthesizer,
    )
    voice.start(bus)

    bus.subscribe(Topics.VOICE_PARTIAL, lambda e: print(
        f"      [hearing…] {e.payload['text']!r}"))
    bus.subscribe(Topics.VOICE, lambda e: print(
        f"   you (voice)> {e.payload['text']}"))
    bus.subscribe(Topics.CHAT_RESPONSE, lambda e: print(
        f"   assistant> {e.payload['text']}"))

    print("== act 1: open palm toggles listening (mic opens per session) ==")
    print(f"   mic opens so far: {source.opened}")
    bus.publish(Event(Topics.INTENT, "intent",
                      {"intent": "assistant_attention", "context": "demo"}))
    bus.flush()
    time.sleep(0.6)

    print("\n== act 2: the utterance flowed voice → reasoner → spoken reply ==")
    time.sleep(0.4)
    bus.flush()
    print(f"   mic opens: {source.opened}, closes: {source.closed} "
          f"(released after the utterance)")
    audit = AuditLog(workdir / "audit.jsonl").tail()
    speaks = [entry for entry in audit if entry.get("action") == "speak"]
    print(f"   audited speak actions: {len(speaks)} "
          f"(status: {speaks[0]['status']})" if speaks else "   (no audit yet)")

    print("\n== act 3: barge-in — talking over the assistant stops it ==")
    print(f"   assistant speaking: {synthesizer.speaking}")
    voice2_source = ScriptedAudioSource([b"chunk"] * 4)
    voice._source_factory = lambda: voice2_source  # fresh scripted mic
    voice._source = voice2_source
    voice._transcriber_factory = lambda: ScriptedTranscriber([
        TranscriptChunk("wait", final=False),
        TranscriptChunk("wait actually stop", final=True),
    ])
    voice._transcriber = voice._transcriber_factory()
    voice.start_listening()
    time.sleep(0.5)
    bus.flush()
    print(f"   interruptions: {synthesizer.interruptions}, "
          f"speaking now: {synthesizer.speaking}")

    for module in (voice, reasoner, dispatcher):
        module.stop()
    bus.stop()


if __name__ == "__main__":
    main()
