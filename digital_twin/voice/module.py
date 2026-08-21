"""Voice perception: speech as just another input modality.

Final utterances are published as ``perception.voice`` events — the
reasoner consumes them through the same path as typed chat, and neither
the dispatcher nor the planner can tell the difference. Third modality,
zero core changes: this module is the architecture's strongest proof.

Listening lifecycle (privacy is structural):

* **push_to_talk** (default): the microphone opens only when listening is
  triggered — a ``voice.control`` event, a configured intent (open palm by
  default), or the programmatic API — and closes again after one final
  utterance, an explicit stop, or the utterance timeout. Mic untouched
  otherwise.
* **continuous**: listening while RUNNING; utterances stream as the
  engine finalises them. Pause closes the microphone entirely.

Extras that make it feel alive:

* live partials on ``perception.voice.partial`` (future UI captioning),
* **barge-in**: a partial transcript while the assistant is speaking stops
  the current utterance immediately,
* **spoken replies**: when enabled, ``chat.response`` events are voiced by
  publishing a gated ``action.execute {speak}`` — the assistant's own
  voice passes the permission system, so one rule mutes it.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from digital_twin.configuration.settings import VoiceConfig
from digital_twin.core.bus import Subscription
from digital_twin.core.events import Event, Topics
from digital_twin.core.module import BaseModule, ModuleState
from digital_twin.voice.audio import AudioSource
from digital_twin.voice.synthesis import SpeechSynthesizer
from digital_twin.voice.transcriber import Transcriber

logger = logging.getLogger(__name__)

MODE_PUSH_TO_TALK = "push_to_talk"
MODE_CONTINUOUS = "continuous"


class VoicePerceptionModule(BaseModule):
    """Publishes spoken utterances; owns the listening lifecycle."""

    name = "voice"
    topics = (Topics.VOICE, Topics.VOICE_PARTIAL, Topics.ACTION_EXECUTE)

    def __init__(
        self,
        config: VoiceConfig,
        source_factory: Callable[[], AudioSource] | None = None,
        transcriber_factory: Callable[[], Transcriber] | None = None,
        synthesizer: SpeechSynthesizer | None = None,
    ):
        super().__init__()
        self._config = config
        self._source_factory = source_factory or self._default_source
        self._transcriber_factory = transcriber_factory or self._default_transcriber
        self._synthesizer = synthesizer

        self._source: AudioSource | None = None
        self._transcriber: Transcriber | None = None
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._listen = threading.Event()
        self._listen_deadline = 0.0
        self._subscriptions: list[Subscription] = []
        self._counts: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Default factories (lazy; fail loudly with guidance)
    # ------------------------------------------------------------------
    def _default_source(self) -> AudioSource:
        from digital_twin.voice.audio import SoundDeviceSource

        return SoundDeviceSource(
            sample_rate=self._config.sample_rate,
            block_ms=self._config.block_ms,
        )

    def _default_transcriber(self) -> Transcriber:
        from digital_twin.voice.transcriber import build_transcriber

        return build_transcriber(
            self._config.engine,
            model_path=self._config.model_path,
            whisper_model=self._config.whisper_model,
            sample_rate=self._config.sample_rate,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def _on_start(self) -> None:
        assert self._bus is not None
        # Resolve the engine now: a missing model FAILS start with download
        # guidance (registry isolates it) instead of failing mid-utterance.
        self._transcriber = self._transcriber_factory()
        self._source = self._source_factory()

        self._stop.clear()
        self._listen.clear()
        self._worker = threading.Thread(
            target=self._listen_loop, name="voice", daemon=True
        )
        self._worker.start()

        self._subscriptions = [
            self._bus.subscribe(Topics.VOICE_CONTROL, self._on_control,
                                name="voice.control"),
            self._bus.subscribe(Topics.INTENT, self._on_intent,
                                name="voice.intents"),
        ]
        if self._config.speak_replies:
            self._subscriptions.append(
                self._bus.subscribe(Topics.CHAT_RESPONSE, self._on_reply,
                                    name="voice.replies"))
        if self._config.mode == MODE_CONTINUOUS:
            self.start_listening()

    def _on_stop(self) -> None:
        for subscription in self._subscriptions:
            subscription.cancel()
        self._subscriptions = []
        self._stop.set()
        self._listen.set()  # unblock the worker's wait
        if self._worker is not None:
            self._worker.join(timeout=3.0)
            self._worker = None
        if self._source is not None:
            self._source.close()
            self._source = None
        self._transcriber = None
        if self._synthesizer is not None:
            self._synthesizer.stop()

    # Pause = privacy: default hooks tear everything down (microphone
    # closed, speech stopped); resume rebuilds from factories.

    # ------------------------------------------------------------------
    # Listening control
    # ------------------------------------------------------------------
    def start_listening(self) -> None:
        """Open the microphone and begin an utterance session."""
        if self.state not in (ModuleState.RUNNING, ModuleState.STARTING):
            return
        if self._synthesizer is not None:
            self._synthesizer.stop()  # barge-in on trigger
        self._listen_deadline = time.monotonic() + self._config.max_utterance_s
        self._listen.set()

    def stop_listening(self) -> None:
        """Close the session; a pending final transcript is flushed."""
        self._listen.clear()

    def toggle_listening(self) -> None:
        """Flip the listening state (the push-to-talk primitive)."""
        if self._listen.is_set():
            self.stop_listening()
        else:
            self.start_listening()

    @property
    def listening(self) -> bool:
        """Whether the microphone session is active."""
        return self._listen.is_set()

    # ------------------------------------------------------------------
    # Bus handlers
    # ------------------------------------------------------------------
    def _on_control(self, event: Event) -> None:
        if self.state is not ModuleState.RUNNING:
            return
        command = event.payload.get("command")
        if command == "start":
            self.start_listening()
        elif command == "stop":
            self.stop_listening()
        elif command == "toggle":
            self.toggle_listening()
        else:
            logger.warning("Ignoring malformed voice control: %s", event)

    def _on_intent(self, event: Event) -> None:
        if self.state is not ModuleState.RUNNING:
            return
        if event.payload.get("intent") in self._config.listen_intents:
            self.toggle_listening()

    def _on_reply(self, event: Event) -> None:
        if self.state is not ModuleState.RUNNING:
            return
        text = event.payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        # Use the spoken_source from the event (set by the reasoner) to
        # route to the correct TTS backend. System phrases get Jarvis
        # pre-cache check; chat output goes straight to Piper.
        spoken_source = event.payload.get("spoken_source", "chat")
        self._publish(Event(
            topic=Topics.ACTION_EXECUTE,
            source=self.name,
            payload={
                "action": "speak",
                "params": {"text": text.strip(), "source": spoken_source},
                "label": "speak reply",
                "source_event": event.event_id,
            },
        ))

    # ------------------------------------------------------------------
    # Worker: the microphone session loop
    # ------------------------------------------------------------------
    def _listen_loop(self) -> None:
        while not self._stop.is_set():
            if not self._listen.wait(timeout=0.2):
                continue
            if self._stop.is_set():
                return
            self._run_session()

    def _run_session(self) -> None:
        source, transcriber = self._source, self._transcriber
        if source is None or transcriber is None:
            self._listen.clear()
            return
        try:
            source.open()
        except Exception as exc:
            logger.error("Microphone unavailable: %s", exc)
            self._count("mic_errors")
            self._listen.clear()
            return
        self._count("sessions")
        logger.info("Listening…")
        try:
            while self._listen.is_set() and not self._stop.is_set():
                if time.monotonic() > self._listen_deadline:
                    logger.info("Utterance timeout; closing microphone")
                    self._count("timeouts")
                    break
                chunk = source.read(timeout_s=0.1)
                if chunk is None:
                    continue
                result = transcriber.feed(chunk)
                if result is None:
                    continue
                if result.final:
                    self._emit_final(result.text)
                    if self._config.mode == MODE_PUSH_TO_TALK:
                        break
                    self._listen_deadline = (
                        time.monotonic() + self._config.max_utterance_s)
                else:
                    self._emit_partial(result.text)
            else:
                # Session ended by stop_listening(): flush a pending final.
                tail = transcriber.flush()
                if tail is not None and tail.text:
                    self._emit_final(tail.text)
                return
            tail = transcriber.flush()
            if tail is not None and tail.text:
                self._emit_final(tail.text)
        finally:
            self._listen.clear()
            source.close()
            logger.info("Microphone released")

    # ------------------------------------------------------------------
    def _emit_partial(self, text: str) -> None:
        if self._synthesizer is not None and self._synthesizer.speaking:
            self._synthesizer.stop()  # barge-in: the user talks, we shut up
            self._count("barge_ins")
        self._publish(Event(topic=Topics.VOICE_PARTIAL, source=self.name,
                            payload={"text": text}))

    def _emit_final(self, text: str) -> None:
        self._count("utterances")
        logger.info("Heard: %s", text)
        self._publish(Event(topic=Topics.VOICE, source=self.name,
                            payload={"text": text}))

    def _count(self, key: str) -> None:
        self._counts[key] = self._counts.get(key, 0) + 1

    # ------------------------------------------------------------------
    def _metrics(self) -> dict[str, Any]:
        metrics: dict[str, Any] = {
            "mode": self._config.mode,
            "listening": self.listening,
            **self._counts,
        }
        if self._transcriber is not None:
            metrics["engine"] = self._transcriber.name
        if self._synthesizer is not None:
            metrics["speaking"] = self._synthesizer.speaking
        return metrics
