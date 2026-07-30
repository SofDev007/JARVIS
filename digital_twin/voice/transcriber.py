"""Streaming speech-to-text behind one small interface.

The reference backend is **Vosk**: fully offline, so raw audio never
leaves the machine — the privacy-first default this project keeps
choosing. The engine is replaceable exactly like the LLM: implement
:class:`Transcriber` and nothing upstream changes (a Whisper backend is a
future drop-in).

Feed PCM chunks, get :class:`TranscriptChunk` items back — partials while
the user speaks, a final when the engine detects the utterance ended.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

VOSK_MODELS_URL = "https://alphacephei.com/vosk/models"


class TranscriberError(RuntimeError):
    """The speech engine is unavailable or misconfigured."""


@dataclass(frozen=True)
class TranscriptChunk:
    """A piece of recognised speech."""

    text: str
    final: bool


class Transcriber(ABC):
    """Streaming recogniser: PCM in, transcript chunks out."""

    name: str = "abstract"

    @abstractmethod
    def feed(self, chunk: bytes) -> TranscriptChunk | None:
        """Process one PCM chunk; may yield a partial or final transcript."""

    @abstractmethod
    def flush(self) -> TranscriptChunk | None:
        """End the utterance; return the final transcript if any."""

    def reset(self) -> None:
        """Prepare for a fresh utterance (default: flush and discard)."""
        self.flush()


class VoskTranscriber(Transcriber):
    """Offline recognition via a local Vosk model."""

    name = "vosk"

    def __init__(self, model_path: str | Path, sample_rate: int = 16000):
        path = Path(model_path)
        if not path.exists():
            raise TranscriberError(
                f"Vosk model not found at {path}. Download a model (e.g. "
                f"vosk-model-small-en-us-0.15) from {VOSK_MODELS_URL} and "
                f"unzip it there, or point voice.model_path at it."
            )
        try:
            import vosk
        except Exception as exc:
            raise TranscriberError(
                "Speech recognition needs the vosk package "
                "(pip install vosk)"
            ) from exc
        vosk.SetLogLevel(-1)
        self._recognizer = vosk.KaldiRecognizer(
            vosk.Model(str(path)), sample_rate
        )
        self._last_partial = ""
        logger.info("Vosk model loaded from %s (%d Hz)", path, sample_rate)

    def feed(self, chunk: bytes) -> TranscriptChunk | None:
        if self._recognizer.AcceptWaveform(chunk):
            self._last_partial = ""
            text = json.loads(self._recognizer.Result()).get("text", "").strip()
            return TranscriptChunk(text=text, final=True) if text else None
        partial = json.loads(
            self._recognizer.PartialResult()
        ).get("partial", "").strip()
        if partial and partial != self._last_partial:
            self._last_partial = partial
            return TranscriptChunk(text=partial, final=False)
        return None

    def flush(self) -> TranscriptChunk | None:
        self._last_partial = ""
        text = json.loads(self._recognizer.FinalResult()).get("text", "").strip()
        return TranscriptChunk(text=text, final=True) if text else None


class WhisperTranscriber(Transcriber):
    """Offline recognition via a local OpenAI Whisper model.

    Whisper has no streaming API: unlike Vosk it can't emit partials as
    audio arrives, so :meth:`feed` only buffers PCM and the model runs
    once, in :meth:`flush`, on the whole utterance. Fine for
    ``push_to_talk`` (the default mode already flushes once per session);
    ``perception.voice.partial`` simply never fires with this backend.
    """

    name = "whisper"

    def __init__(self, model_size: str = "tiny", sample_rate: int = 16000,
                 language: str | None = "en"):
        try:
            import whisper
        except Exception as exc:
            raise TranscriberError(
                "Speech recognition needs the openai-whisper package "
                "(pip install openai-whisper)"
            ) from exc
        self._model = whisper.load_model(model_size)
        self._language = language
        self._buffer = bytearray()
        logger.info("Whisper model '%s' loaded (%d Hz)", model_size, sample_rate)

    def feed(self, chunk: bytes) -> TranscriptChunk | None:
        self._buffer.extend(chunk)
        return None

    def flush(self) -> TranscriptChunk | None:
        if not self._buffer:
            return None
        import numpy as np

        audio = (np.frombuffer(bytes(self._buffer), dtype=np.int16)
                 .astype(np.float32) / 32768.0)
        self._buffer.clear()
        result = self._model.transcribe(audio, language=self._language,
                                         fp16=False)
        text = result.get("text", "").strip()
        return TranscriptChunk(text=text, final=True) if text else None

    def reset(self) -> None:
        self._buffer.clear()


def build_transcriber(engine: str, *, model_path: str | Path,
                       whisper_model: str, sample_rate: int) -> Transcriber:
    """Construct the configured backend — the one place that branches."""
    if engine == "whisper":
        return WhisperTranscriber(model_size=whisper_model,
                                   sample_rate=sample_rate)
    return VoskTranscriber(model_path, sample_rate=sample_rate)


class ScriptedTranscriber(Transcriber):
    """Deterministic recogniser: one scripted output per fed chunk."""

    name = "scripted"

    def __init__(self, outputs: list[TranscriptChunk | None]):
        self._outputs = list(outputs)
        self.fed: list[bytes] = []
        self.flushes = 0

    def feed(self, chunk: bytes) -> TranscriptChunk | None:
        self.fed.append(chunk)
        return self._outputs.pop(0) if self._outputs else None

    def flush(self) -> TranscriptChunk | None:
        self.flushes += 1
        while self._outputs:
            item = self._outputs.pop(0)
            if item is not None and item.final:
                return item
        return None
