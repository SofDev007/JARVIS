"""Speech synthesis: the assistant's voice, non-blocking and interruptible.

Subprocess backends only (espeak-ng / espeak on Linux, ``say`` on macOS,
SAPI via PowerShell on Windows) — zero Python dependencies, same pattern
as ``notify``. :meth:`speak` returns immediately (a long sentence must
never stall the action worker); :attr:`speaking` polls the child process;
:meth:`stop` terminates it — which is what makes **barge-in** possible:
the voice module kills the current utterance the moment the user starts
talking over it.

Missing backends raise :class:`SynthesisError` with install guidance at
*use* time, so kernels start everywhere and the failure is an audited
action result, not a crash.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import threading

logger = logging.getLogger(__name__)


class SynthesisError(RuntimeError):
    """No usable text-to-speech backend."""


class SpeechSynthesizer:
    """Platform TTS behind one non-blocking, interruptible surface."""

    def __init__(self, backend: str = "auto", rate_wpm: int = 175):
        self._backend = backend
        self._rate = rate_wpm
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self.utterances = 0
        self.interruptions = 0

    # ------------------------------------------------------------------
    def _command(self, text: str) -> list[str]:
        backend = self._backend
        if backend == "auto":
            if sys.platform == "darwin" and shutil.which("say"):
                backend = "say"
            elif shutil.which("espeak-ng"):
                backend = "espeak-ng"
            elif shutil.which("espeak"):
                backend = "espeak"
            elif sys.platform == "win32":
                backend = "powershell"
            else:
                raise SynthesisError(
                    "No text-to-speech backend found. Install espeak-ng "
                    "(sudo apt install espeak-ng) or set voice.tts_backend."
                )
        if backend in ("espeak-ng", "espeak"):
            if not shutil.which(backend):
                raise SynthesisError(f"{backend} is not installed")
            return [backend, "-s", str(self._rate), text]
        if backend == "say":
            if not shutil.which("say"):
                raise SynthesisError("macOS 'say' not available")
            return ["say", "-r", str(self._rate), text]
        if backend == "powershell":
            safe = text.replace("'", " ").replace('"', " ")
            script = (
                "Add-Type -AssemblyName System.Speech; "
                "$v = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                f"$v.Rate = 1; $v.Speak('{safe}')"
            )
            return ["powershell", "-NoProfile", "-Command", script]
        raise SynthesisError(f"Unknown tts backend {backend!r}")

    # ------------------------------------------------------------------
    def speak(self, text: str) -> str:
        """Start speaking ``text``; returns immediately. Replaces any
        utterance already in progress."""
        command = self._command(text)
        with self._lock:
            self._terminate_locked()
            try:
                self._process = subprocess.Popen(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as exc:
                raise SynthesisError(f"TTS launch failed: {exc}") from exc
            self.utterances += 1
        return f"speaking {len(text)} characters via {command[0]}"

    @property
    def speaking(self) -> bool:
        """Whether an utterance is currently playing."""
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def stop(self) -> bool:
        """Interrupt the current utterance; ``True`` if one was playing."""
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                return False
            self._terminate_locked()
            self.interruptions += 1
            return True

    def _terminate_locked(self) -> None:
        process, self._process = self._process, None
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=1.0)
            except Exception:
                logger.debug("TTS terminate raced", exc_info=True)


class FakeSynthesizer(SpeechSynthesizer):
    """Test double: records utterances; ``speaking`` is script-controlled."""

    def __init__(self):
        super().__init__(backend="fake")
        self.spoken: list[str] = []
        self._fake_speaking = False

    def speak(self, text: str) -> str:
        self.spoken.append(text)
        self.utterances += 1
        self._fake_speaking = True
        return f"speaking {len(text)} characters via fake"

    @property
    def speaking(self) -> bool:
        return self._fake_speaking

    def stop(self) -> bool:
        was_speaking = self._fake_speaking
        self._fake_speaking = False
        if was_speaking:
            self.interruptions += 1
        return was_speaking
