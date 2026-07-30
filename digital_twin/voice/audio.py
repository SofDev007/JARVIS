"""Microphone audio sources behind one tiny blocking-read interface.

Privacy is structural here: a source is **closed by default** and only
opened for the duration of a listening session (push-to-talk opens it on
trigger and closes it after the utterance) — the microphone is untouched
unless the user asked to be heard, the same philosophy as the gesture
module releasing the camera on pause.

The default implementation uses ``sounddevice`` (PortAudio) lazily; tests
and demos inject :class:`ScriptedAudioSource`. Transient failures return
``None`` from :meth:`read`; unrecoverable ones raise
:class:`AudioSourceError` with install guidance.
"""

from __future__ import annotations

import logging
import queue
import threading
from abc import ABC, abstractmethod
from importlib.util import find_spec

logger = logging.getLogger(__name__)


class AudioSourceError(RuntimeError):
    """The audio source could not be opened or read."""


class AudioSource(ABC):
    """Mono 16-bit PCM chunks via blocking reads."""

    name: str = "abstract"

    @abstractmethod
    def open(self) -> None:
        """Acquire the device (idempotent)."""

    @abstractmethod
    def close(self) -> None:
        """Release the device (idempotent)."""

    @abstractmethod
    def read(self, timeout_s: float) -> bytes | None:
        """Next PCM chunk, or ``None`` if none arrived within the timeout."""


class SoundDeviceSource(AudioSource):
    """Microphone capture via sounddevice/PortAudio (lazy import)."""

    name = "sounddevice"

    def __init__(self, sample_rate: int = 16000, block_ms: int = 30,
                 device: int | None = None):
        self._sample_rate = sample_rate
        self._block = max(1, int(sample_rate * block_ms / 1000))
        self._device = device
        self._stream = None
        self._chunks: queue.Queue = queue.Queue(maxsize=64)

    @staticmethod
    def available() -> bool:
        return find_spec("sounddevice") is not None

    def open(self) -> None:
        if self._stream is not None:
            return
        try:
            import sounddevice
        except Exception as exc:
            raise AudioSourceError(
                "Microphone capture needs sounddevice "
                "(pip install sounddevice; PortAudio required)"
            ) from exc

        def on_audio(indata, frames, time_info, status):  # PortAudio thread
            if status:
                logger.debug("Audio status: %s", status)
            try:
                self._chunks.put_nowait(bytes(indata))
            except queue.Full:
                pass  # drop-oldest semantics live upstream; shed here

        try:
            self._stream = sounddevice.RawInputStream(
                samplerate=self._sample_rate,
                blocksize=self._block,
                device=self._device,
                dtype="int16",
                channels=1,
                callback=on_audio,
            )
            self._stream.start()
        except Exception as exc:
            self._stream = None
            raise AudioSourceError(f"Could not open microphone: {exc}") from exc
        logger.info("Microphone open (%d Hz)", self._sample_rate)

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                logger.debug("Microphone close raced", exc_info=True)
            logger.info("Microphone closed")
        while not self._chunks.empty():
            try:
                self._chunks.get_nowait()
            except queue.Empty:
                break

    def read(self, timeout_s: float) -> bytes | None:
        try:
            return self._chunks.get(timeout=timeout_s)
        except queue.Empty:
            return None


class ScriptedAudioSource(AudioSource):
    """Deterministic source for tests/demos; tracks open/close for privacy
    assertions."""

    name = "scripted"

    def __init__(self, chunks: list[bytes] | None = None):
        self._script = list(chunks or [])
        self._position = 0
        self.opened = 0
        self.closed = 0
        self.is_open = False
        self._lock = threading.Lock()

    def open(self) -> None:
        with self._lock:
            if not self.is_open:
                self.opened += 1
                self.is_open = True

    def close(self) -> None:
        with self._lock:
            if self.is_open:
                self.closed += 1
                self.is_open = False

    def read(self, timeout_s: float) -> bytes | None:
        with self._lock:
            if not self.is_open or self._position >= len(self._script):
                return None
            chunk = self._script[self._position]
            self._position += 1
            return chunk
