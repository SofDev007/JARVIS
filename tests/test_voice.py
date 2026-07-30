"""Tests for the voice module (M9).

Architectural assertions first: the microphone opens only during a
listening session (privacy is structural), barge-in kills speech the
moment the user talks over it, spoken replies pass the permission gates
(one rule mutes the assistant), and the reasoner treats an utterance
exactly like typed chat — third modality, zero core changes.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import types

import pytest

from digital_twin.automation.dispatcher import ActionDispatcher
from digital_twin.automation.registry import ActionRegistry
from digital_twin.configuration.settings import (
    AutomationConfig,
    LLMConfig,
    VoiceConfig,
)
from digital_twin.core.bus import EventBus
from digital_twin.core.events import Event, Topics
from digital_twin.reasoning.chat_reasoner import ChatReasoner
from digital_twin.reasoning.llm import ScriptedModel
from digital_twin.security.audit import AuditLog
from digital_twin.security.confirmation import ScriptedConfirmation
from digital_twin.security.permissions import PermissionPolicy
from digital_twin.voice.actions import register_voice_actions
from digital_twin.voice.audio import ScriptedAudioSource
from digital_twin.voice.module import VoicePerceptionModule
from digital_twin.voice.synthesis import (
    FakeSynthesizer,
    SpeechSynthesizer,
    SynthesisError,
)
from digital_twin.voice.transcriber import (
    ScriptedTranscriber,
    TranscriberError,
    TranscriptChunk,
    VoskTranscriber,
    WhisperTranscriber,
)


def _wait(predicate, timeout=5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------
class _FakePopen:
    def __init__(self, command, **kwargs):
        self.command = command
        self._returncode = None

    def poll(self):
        return self._returncode

    def terminate(self):
        self._returncode = -15

    def wait(self, timeout=None):
        self._returncode = -15
        return self._returncode


def test_synthesizer_espeak_command_and_interrupt(monkeypatch):
    monkeypatch.setattr("shutil.which",
                        lambda name: "/usr/bin/espeak-ng"
                        if name == "espeak-ng" else None)
    launched = []

    def fake_popen(command, **kwargs):
        process = _FakePopen(command)
        launched.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    synthesizer = SpeechSynthesizer(backend="auto", rate_wpm=200)
    detail = synthesizer.speak("hello boss")
    assert "espeak-ng" in detail
    assert launched[0].command == ["espeak-ng", "-s", "200", "hello boss"]
    assert synthesizer.speaking is True
    assert synthesizer.stop() is True
    assert synthesizer.speaking is False
    assert synthesizer.interruptions == 1


def test_synthesizer_new_utterance_replaces_current(monkeypatch):
    monkeypatch.setattr("shutil.which",
                        lambda name: "/usr/bin/espeak" if name == "espeak" else None)
    launched = []
    monkeypatch.setattr(subprocess, "Popen",
                        lambda command, **kwargs: launched.append(
                            _FakePopen(command)) or launched[-1])
    synthesizer = SpeechSynthesizer()
    synthesizer.speak("first")
    synthesizer.speak("second")
    assert launched[0].poll() is not None  # first was terminated
    assert synthesizer.utterances == 2


def test_synthesizer_without_backend_raises_guidance(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(SynthesisError, match="espeak-ng"):
        SpeechSynthesizer().speak("hi")


# ---------------------------------------------------------------------------
# Vosk transcriber (fake vosk module injected)
# ---------------------------------------------------------------------------
class _FakeRecognizer:
    def __init__(self, model, rate):
        self.rate = rate
        self.script = [
            (False, {"partial": "next"}),
            (False, {"partial": "next slide"}),
            (True, {"text": "next slide please"}),
        ]

    def AcceptWaveform(self, chunk):
        self._current = self.script.pop(0) if self.script else (False, {"partial": ""})
        return self._current[0]

    def Result(self):
        return json.dumps(self._current[1])

    def PartialResult(self):
        return json.dumps(self._current[1])

    def FinalResult(self):
        return json.dumps({"text": ""})


def _install_fake_vosk(monkeypatch):
    fake = types.ModuleType("vosk")
    fake.Model = lambda path: object()
    fake.KaldiRecognizer = _FakeRecognizer
    fake.SetLogLevel = lambda level: None
    monkeypatch.setitem(sys.modules, "vosk", fake)


def test_vosk_transcriber_streams_partials_then_final(monkeypatch, tmp_path):
    _install_fake_vosk(monkeypatch)
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    transcriber = VoskTranscriber(model_dir, sample_rate=16000)
    outputs = [transcriber.feed(b"x") for _ in range(3)]
    assert outputs[0] == TranscriptChunk("next", final=False)
    assert outputs[1] == TranscriptChunk("next slide", final=False)
    assert outputs[2] == TranscriptChunk("next slide please", final=True)


def test_vosk_missing_model_gives_download_guidance(tmp_path):
    with pytest.raises(TranscriberError, match="alphacephei"):
        VoskTranscriber(tmp_path / "nope")


# ---------------------------------------------------------------------------
# Whisper transcriber (fake whisper module injected; batch-only, no partials)
# ---------------------------------------------------------------------------
class _FakeWhisperModel:
    def transcribe(self, audio, language=None, fp16=False):
        assert audio.dtype.name == "float32"
        return {"text": "next slide please"}


def _install_fake_whisper(monkeypatch):
    fake = types.ModuleType("whisper")
    fake.load_model = lambda size: _FakeWhisperModel()
    monkeypatch.setitem(sys.modules, "whisper", fake)


def test_whisper_transcriber_buffers_then_transcribes_on_flush(monkeypatch):
    _install_fake_whisper(monkeypatch)
    transcriber = WhisperTranscriber(model_size="tiny", sample_rate=16000)
    assert transcriber.feed(b"\x00\x00" * 8) is None
    assert transcriber.feed(b"\x00\x00" * 8) is None
    assert transcriber.flush() == TranscriptChunk(
        "next slide please", final=True)
    assert transcriber.flush() is None  # buffer drained


# ---------------------------------------------------------------------------
# Voice module: listening lifecycle
# ---------------------------------------------------------------------------
@pytest.fixture()
def bus():
    bus = EventBus()
    bus.start()
    yield bus
    bus.stop()


_UTTERANCE = [
    TranscriptChunk("next", final=False),
    TranscriptChunk("next slide", final=False),
    TranscriptChunk("next slide please", final=True),
]


def _module(bus, outputs=None, source=None, synthesizer=None, **config_kwargs):
    source = source or ScriptedAudioSource([b"a"] * 20)
    module = VoicePerceptionModule(
        VoiceConfig(**config_kwargs),
        source_factory=lambda: source,
        transcriber_factory=lambda: ScriptedTranscriber(
            list(outputs if outputs is not None else _UTTERANCE)),
        synthesizer=synthesizer,
    )
    module.start(bus)
    return module, source


def _record(bus, topic):
    events = []
    bus.subscribe(topic, events.append)
    return events


def test_push_to_talk_session_publishes_and_releases_mic(bus):
    module, source = _module(bus)
    finals = _record(bus, Topics.VOICE)
    partials = _record(bus, Topics.VOICE_PARTIAL)
    try:
        assert source.opened == 0  # privacy: mic untouched before trigger
        module.start_listening()
        assert _wait(lambda: finals and source.closed == 1)
        bus.flush(timeout=2.0)
        assert finals[0].payload == {"text": "next slide please"}
        assert [event.payload["text"] for event in partials] == [
            "next", "next slide"]
        assert module.listening is False  # PTT auto-closed after the final
        assert module.status().metrics["utterances"] == 1
    finally:
        module.stop()


def test_voice_control_and_intent_toggle_listening(bus):
    module, source = _module(bus, outputs=[])
    try:
        bus.publish(Event(Topics.VOICE_CONTROL, "test", {"command": "start"}))
        assert _wait(lambda: module.listening and source.is_open)
        bus.publish(Event(Topics.VOICE_CONTROL, "test", {"command": "stop"}))
        assert _wait(lambda: not module.listening and source.closed == 1)

        # The open-palm intent is the default push-to-talk button.
        bus.publish(Event(Topics.INTENT, "intent",
                          {"intent": "assistant_attention"}))
        assert _wait(lambda: module.listening)
    finally:
        module.stop()


def test_stop_listening_flushes_pending_final(bus):
    outputs = [TranscriptChunk("save the", final=False),
               None, None, None, None, None,
               TranscriptChunk("save the file", final=True)]
    module, _ = _module(bus, outputs=outputs)
    finals = _record(bus, Topics.VOICE)
    try:
        module.start_listening()
        assert _wait(lambda: module.status().metrics.get("sessions") == 1)
        assert _wait(lambda: len(module._transcriber.fed) >= 2)
        module.stop_listening()
        assert _wait(lambda: finals)
        assert finals[0].payload["text"] == "save the file"
    finally:
        module.stop()


def test_utterance_timeout_closes_microphone(bus):
    module, source = _module(bus, outputs=[], max_utterance_s=0.15)
    try:
        module.start_listening()
        assert _wait(lambda: source.closed == 1 and not module.listening)
        assert module.status().metrics["timeouts"] == 1
    finally:
        module.stop()


def test_continuous_mode_streams_utterances(bus):
    outputs = [TranscriptChunk("first", final=True),
               TranscriptChunk("second", final=True)]
    module, _ = _module(bus, outputs=outputs, mode="continuous")
    finals = _record(bus, Topics.VOICE)
    try:
        assert _wait(lambda: len(finals) == 2)
        assert [event.payload["text"] for event in finals] == ["first", "second"]
        assert module.listening is True
    finally:
        module.stop()


def test_barge_in_stops_speech_on_partial(bus):
    synthesizer = FakeSynthesizer()
    synthesizer.speak("a very long reply that should be interrupted")
    module, _ = _module(bus, synthesizer=synthesizer, listen_intents=[])
    try:
        assert synthesizer.speaking is True
        # Triggering listening barge-ins; here we go further and let a
        # partial arrive mid-speech.
        synthesizer._fake_speaking = True
        module.start_listening()
        assert _wait(lambda: synthesizer.interruptions >= 1)
        assert synthesizer.speaking is False
    finally:
        module.stop()


def test_pause_closes_everything(bus):
    synthesizer = FakeSynthesizer()
    module, source = _module(bus, outputs=[], synthesizer=synthesizer)
    try:
        module.start_listening()
        assert _wait(lambda: source.is_open)
        synthesizer.speak("talking")
        module.pause()
        assert not source.is_open
        assert synthesizer.speaking is False
        assert module.listening is False
    finally:
        module.stop()


def test_broken_engine_fails_start_with_guidance(bus):
    def broken():
        raise TranscriberError("model not found; download from alphacephei")

    module = VoicePerceptionModule(VoiceConfig(), transcriber_factory=broken)
    with pytest.raises(TranscriberError):
        module.start(bus)
    assert module.status().state.value == "failed"
    assert "alphacephei" in module.status().detail


# ---------------------------------------------------------------------------
# Spoken replies through the gates + the full voice chain
# ---------------------------------------------------------------------------
def _dispatcher(bus, tmp_path, synthesizer, permissions=None):
    registry = ActionRegistry()
    register_voice_actions(registry, synthesizer)
    dispatcher = ActionDispatcher(
        config=AutomationConfig(intent_bindings={}),
        registry=registry,
        policy=PermissionPolicy(
            risk_defaults={"safe": "allow"}, overrides=permissions or {}),
        confirmation=ScriptedConfirmation(),
        audit=AuditLog(tmp_path / "audit.jsonl"),
    )
    dispatcher.start(bus)
    return dispatcher


def test_replies_are_spoken_via_gated_action(bus, tmp_path):
    synthesizer = FakeSynthesizer()
    dispatcher = _dispatcher(bus, tmp_path, synthesizer)
    module, _ = _module(bus, outputs=[], synthesizer=synthesizer)
    try:
        bus.publish(Event(Topics.CHAT_RESPONSE, "reasoner",
                          {"text": "All done, Boss.", "reasoning": ""}))
        assert _wait(lambda: synthesizer.spoken == ["All done, Boss."])
        audit = AuditLog(tmp_path / "audit.jsonl").tail()
        assert any(entry.get("action") == "speak"
                   and entry["status"] == "completed" for entry in audit)
    finally:
        module.stop()
        dispatcher.stop()


def test_one_permission_rule_mutes_the_assistant(bus, tmp_path):
    synthesizer = FakeSynthesizer()
    dispatcher = _dispatcher(bus, tmp_path, synthesizer,
                             permissions={"speak": "deny"})
    module, _ = _module(bus, outputs=[], synthesizer=synthesizer)
    results = _record(bus, Topics.ACTION_RESULT)
    try:
        bus.publish(Event(Topics.CHAT_RESPONSE, "reasoner",
                          {"text": "You cannot hear me."}))
        assert _wait(lambda: results)
        assert results[0].payload["status"] == "denied"
        assert synthesizer.spoken == []
    finally:
        module.stop()
        dispatcher.stop()


def test_speak_action_validation():
    registry = ActionRegistry()
    register_voice_actions(registry, FakeSynthesizer())
    validate = registry.get("speak").validate
    validate({"text": "hello"})
    for bad in ("", "x" * 1001, "bell\x07"):
        with pytest.raises(ValueError):
            validate({"text": bad})


def test_full_voice_chain_utterance_to_reply(bus, tmp_path):
    """Speak → transcribe → reason → reply spoken back. Zero core changes."""
    synthesizer = FakeSynthesizer()
    dispatcher = _dispatcher(bus, tmp_path, synthesizer)
    model = ScriptedModel([json.dumps({
        "reply": "Advancing now.", "intent": None,
        "reasoning": "spoken request"})])
    reasoner = ChatReasoner(LLMConfig(memory_results=0), model=model,
                            allowed_intents=())
    reasoner.start(bus)
    module, _ = _module(bus, synthesizer=synthesizer)
    try:
        module.start_listening()
        assert _wait(lambda: synthesizer.spoken == ["Advancing now."])
        # The reasoner received the utterance as ordinary text.
        assert model.calls[0]["messages"][-1] == ("user", "next slide please")
    finally:
        module.stop()
        reasoner.stop()
        dispatcher.stop()
