"""Wake-word detector — the always-on front end to push-to-talk.

A deliberately small module: it holds its *own* microphone and
transcriber, listens continuously for one configured phrase, and when it
hears it publishes ``voice.control {command: start}`` — pressing the
same push-to-talk button a gesture or API call would. It never publishes
utterances and never reaches the reasoner; recognised audio that is not
the wake phrase is discarded immediately.

Why a separate module rather than a mode of the voice module:

* **Privacy is legible.** "Always listening" lives in exactly one place
  you can see, name in the module list, and disable
  (``voice.wake_word: ""``). The voice module's own privacy property —
  the microphone opens only for a real utterance — is preserved: the
  detector triggers it, it does not replace it.
* **Composability.** It reuses the ``Transcriber`` and ``AudioSource``
  interfaces (so a scripted transcriber drives it in tests with no
  hardware) and the existing ``voice.control`` trigger path, so the
  voice module needs no changes at all.

Matching is intentionally forgiving: the phrase is normalised
(lower-cased, punctuation stripped) and matched as a substring of the
rolling transcript, because wake-word recognition on a small offline
model is noisy and a missed trigger is more annoying than a rare false
one (which only opens a gated listening session anyway).
"""

from __future__ import annotations

import logging
import re
import threading

from digital_twin.configuration.settings import VoiceConfig
from digital_twin.core.events import Event, Topics
from digital_twin.core.module import BaseModule
from digital_twin.voice.audio import AudioSource, AudioSourceError
from digital_twin.voice.transcriber import Transcriber

logger = logging.getLogger(__name__)


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


class WakeWordModule(BaseModule):
    """Listen for one phrase; press push-to-talk when heard."""

    name = "wake_word"
    topics = ()

    def __init__(self, config: VoiceConfig,
                 audio_source: AudioSource | None = None,
                 transcriber: Transcriber | None = None):
        super().__init__()
        self._config = config
        self._phrase = _normalise(config.wake_word)
        if not self._phrase:
            raise ValueError("WakeWordModule requires a non-empty wake_word")
        self._audio = audio_source
        self._transcriber = transcriber
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._detections = 0

    # ------------------------------------------------------------------
    def _on_start(self) -> None:
        self._stop.clear()
        if self._audio is None:
            self._audio = self._build_audio()
        if self._transcriber is None:
            self._transcriber = self._build_transcriber()
        self._thread = threading.Thread(
            target=self._loop, name="wake-word", daemon=True)
        self._thread.start()

    def _on_stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        if self._audio is not None:
            try:
                self._audio.close()
            except Exception:
                pass

    def _on_pause(self) -> None:
        self._on_stop()

    def _on_resume(self) -> None:
        self._on_start()

    # ------------------------------------------------------------------
    def _build_audio(self) -> AudioSource:
        from digital_twin.voice.audio import SoundDeviceSource

        return SoundDeviceSource(
            sample_rate=self._config.sample_rate,
            block_ms=self._config.block_ms)

    def _build_transcriber(self) -> Transcriber:
        from digital_twin.voice.transcriber import build_transcriber

        return build_transcriber(
            self._config.engine,
            model_path=self._config.model_path,
            whisper_model=self._config.whisper_model,
            sample_rate=self._config.sample_rate,
        )

    # ------------------------------------------------------------------
    def _loop(self) -> None:
        try:
            self._audio.open()
        except AudioSourceError as exc:
            self._fail(f"wake word microphone unavailable: {exc}")
            return
        logger.info("Wake word active: listening for %r", self._config.wake_word)
        rolling = ""
        while not self._stop.is_set():
            try:
                chunk = self._audio.read(timeout_s=0.5)
            except AudioSourceError as exc:
                self._fail(f"wake word audio error: {exc}")
                return
            if chunk is None:
                continue
            result = self._transcriber.feed(chunk)
            if result is None or not result.text:
                continue
            rolling = _normalise((rolling + " " + result.text))[-120:]
            if self._phrase in rolling:
                self._trigger()
                rolling = ""
                self._transcriber.reset()

    def _trigger(self) -> None:
        self._detections += 1
        logger.info("Wake word heard — starting a listening session")
        if self._bus is not None:
            self._bus.publish(Event(
                topic=Topics.VOICE_CONTROL, source=self.name,
                payload={"command": "start", "reason": "wake_word"}))

    def _metrics(self) -> dict:
        return {"phrase": self._config.wake_word,
                "detections": self._detections}
