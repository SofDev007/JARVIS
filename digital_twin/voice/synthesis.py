"""Speech synthesis: the assistant's voice, non-blocking and interruptible.

Two-tier TTS architecture for CPU-only machines:

* **Piper** (default): fast local synthesis for LLM-generated replies
* **Jarvis** (XTTS-v2): pre-cached premium voice for system phrases only

The `source` parameter in `speak()` determines the routing:
* `source="system"`: check Jarvis pre-cache first, fall back to Piper
* `source="chat"`: always use Piper directly (LLM output won't match precache)

Subprocess backends (espeak-ng, say, powershell) remain for systems without
Piper/Jarvis setup. :meth:`speak` returns immediately (a long sentence must
never stall the action worker); :attr:`speaking` polls the child process;
:meth:`stop` terminates it — which is what makes **barge-in** possible.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)


class SynthesisError(RuntimeError):
    """No usable text-to-speech backend."""


class SpeechSynthesizer:
    """Platform TTS behind one non-blocking, interruptible surface."""

    def __init__(
        self,
        backend: str = "auto",
        rate_wpm: int = 180,
        *,
        piper_voice: str = "en_GB-alan-medium",
        piper_data_dir: str = "models/piper",
        jarvis_reference_wav: str = "voices/reference_voice.wav",
        jarvis_precache_dir: str = "voices/precache",
        precached_phrases: list[str] | None = None,
    ):
        self._backend = backend
        self._rate = rate_wpm
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self.utterances = 0
        self.interruptions = 0
        # Playback interval, tracked across every backend (not just the
        # subprocess ones `speaking` reflects) so a wake-word loopback guard
        # can tell "still playing" from "finished a moment ago". Read/written
        # without `_lock` on purpose: `speak()` holds `_lock` for the full
        # blocking duration of Piper/Jarvis playback, so a lock-guarded read
        # here would just block until playback ends instead of observing it.
        self._speak_started: float | None = None
        self._speak_ended: float | None = None

        # Piper config
        self._piper_voice = piper_voice
        self._piper_data_dir = Path(piper_data_dir)
        self._piper = None  # Lazy-loaded PiperVoice instance

        # Jarvis pre-cache config
        self._jarvis_reference_wav = jarvis_reference_wav
        self._jarvis_precache_dir = Path(jarvis_precache_dir)
        self._precached_phrases = set(precached_phrases or [])

        # Validate Jarvis setup if that backend is selected
        if backend == "jarvis":
            if not os.path.isfile(jarvis_reference_wav):
                logger.warning(
                    "Jarvis backend selected but reference wav not found: %s. "
                    "Will fall back to Piper for all speech.",
                    jarvis_reference_wav
                )
            if not self._jarvis_precache_dir.exists():
                logger.warning(
                    "Jarvis pre-cache dir does not exist: %s. "
                    "Run 'python -m digital_twin.voice.precache' to generate.",
                    jarvis_precache_dir
                )

    # ------------------------------------------------------------------
    def _command(self, text: str) -> list[str]:
        """Build subprocess command for legacy backends (espeak, say, powershell)."""
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
    def speak(
        self,
        text: str,
        source: Literal["system", "chat"] = "chat",
    ) -> str:
        """Start speaking ``text``; returns immediately. Replaces any
        utterance already in progress.

        ``source`` determines voice routing:
        * ``"system"``: check Jarvis pre-cache, fall back to Piper
        * ``"chat"``: always use Piper (LLM output, won't match precache)
        """
        with self._lock:
            self._terminate_locked()
            self._speak_started = time.monotonic()
            self._speak_ended = None
            try:
                if self._backend == "piper":
                    result = self._speak_piper(text)
                elif self._backend == "jarvis":
                    result = self._speak_jarvis(text, source)
                elif self._backend in ("auto", "espeak-ng", "espeak", "say", "powershell"):
                    result = self._speak_subprocess(text)
                else:
                    raise SynthesisError(f"Unknown tts backend {self._backend!r}")
                self.utterances += 1
                return result
            except SynthesisError:
                raise
            except Exception as exc:
                raise SynthesisError(f"TTS failed: {exc}") from exc
            finally:
                self._speak_started = None
                self._speak_ended = time.monotonic()

    def _speak_piper(self, text: str) -> str:
        """Synthesize with Piper TTS (fast, local, CPU-friendly)."""
        if self._piper is None:
            try:
                from piper import PiperVoice
            except ImportError as exc:
                raise SynthesisError(
                    "piper-tts not installed. Run: pip install piper-tts"
                ) from exc

            # Find or download the voice model
            voice_path = self._piper_data_dir / f"{self._piper_voice}.onnx"
            if not voice_path.exists():
                logger.info(
                    "Piper voice not found at %s, downloading...",
                    voice_path
                )
                voice_path = self._download_piper_voice()

            logger.info("Loading Piper voice: %s", voice_path)
            self._piper = PiperVoice.load(str(voice_path))

        # Generate audio in memory and play. piper-tts >=1.8 synthesizes
        # straight into an open wave.Wave_write (no more synthesize_stream_raw).
        import io
        import wave

        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wav_file:
            self._piper.synthesize_wav(text, wav_file, syn_config=self._piper_syn_config())
        wav_data = wav_buffer.getvalue()

        if len(wav_data) <= 44:  # empty audio: just the WAV header
            return f"piper: synthesized 0 bytes for {len(text)} chars"

        self._play_wav_bytes(wav_data)
        return f"piper: {len(text)} chars via {self._piper_voice}"

    # Piper's own defaults are flat/metronomic by design (fast, predictable
    # CPU synthesis); nudging these up trades a little of that predictability
    # for natural-sounding variance. Values beyond this range start sounding
    # slurred (noise) or rushed/sluggish (length), which reads as *more*
    # artificial, not less.
    _NOISE_SCALE = 0.75      # per-phoneme acoustic variance (was Piper default ~0.667)
    _NOISE_W_SCALE = 0.9     # timing/rhythm variance (was Piper default ~0.8)
    _LENGTH_SCALE_MIN = 0.85
    _LENGTH_SCALE_MAX = 1.15

    def _piper_syn_config(self):
        """Map ``tts_rate_wpm`` onto Piper's ``length_scale`` (playback
        duration; pitch/tone is untouched, so speed changes don't distort
        the voice), clamped to a subtle range — large deviations sound
        rushed or sluggish rather than more natural. 175 wpm is Piper's own
        neutral pace → scale 1.0. ``noise_scale``/``noise_w_scale`` add
        natural variance Piper's defaults lack, which is most of what reads
        as "robotic" at the default settings."""
        from piper.config import SynthesisConfig

        length_scale = max(self._LENGTH_SCALE_MIN,
                            min(self._LENGTH_SCALE_MAX, 175.0 / max(self._rate, 1)))
        return SynthesisConfig(
            length_scale=length_scale,
            noise_scale=self._NOISE_SCALE,
            noise_w_scale=self._NOISE_W_SCALE,
        )

    def _download_piper_voice(self) -> Path:
        """Download Piper voice from HuggingFace."""
        import urllib.request

        self._piper_data_dir.mkdir(parents=True, exist_ok=True)

        # Construct HuggingFace URL
        # Format: https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/low/en_GB-alan-low.onnx
        # Voice names are "{lang}_{REGION}-{name}-{quality}", e.g. "en_GB-alan-low".
        parts = self._piper_voice.split("-")
        if len(parts) >= 3:
            lang_region = parts[0]  # e.g., "en_GB"
            lang = lang_region.split("_")[0]  # e.g., "en"
            name = parts[1]  # e.g., "alan"
            quality = parts[2] if len(parts) > 2 else "low"
        else:
            # Fallback to default structure
            lang, lang_region, name, quality = "en", "en_GB", "alan", "low"

        base_url = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
        onnx_url = f"{base_url}/{lang}/{lang_region}/{name}/{quality}/{self._piper_voice}.onnx"
        json_url = f"{base_url}/{lang}/{lang_region}/{name}/{quality}/{self._piper_voice}.onnx.json"

        onnx_path = self._piper_data_dir / f"{self._piper_voice}.onnx"
        json_path = self._piper_data_dir / f"{self._piper_voice}.onnx.json"

        logger.info("Downloading Piper voice from %s", onnx_url)
        urllib.request.urlretrieve(onnx_url, onnx_path)

        logger.info("Downloading Piper voice config from %s", json_url)
        urllib.request.urlretrieve(json_url, json_path)

        return onnx_path

    def _speak_jarvis(self, text: str, source: str) -> str:
        """Use Jarvis (XTTS-v2) pre-cache, fall back to Piper for uncached."""
        if source == "system":
            # Check pre-cache for exact match
            cached_path = self._precache_path(text)
            if cached_path.exists():
                self._play_wav(str(cached_path))
                return f"jarvis (cached): {len(text)} chars"
            else:
                logger.warning(
                    "System phrase not in Jarvis pre-cache: %r. Falling back to Piper.",
                    text[:50]
                )

        # Fall back to Piper for chat or uncached system phrases
        return self._speak_piper(text)

    def _precache_path(self, text: str) -> Path:
        """Get cache file path for a phrase (matches jarvis_voice.py logic)."""
        key = hashlib.sha1(f"{text}|{self._jarvis_reference_wav}".encode()).hexdigest()[:16]
        return self._jarvis_precache_dir / f"{key}.wav"

    def _speak_subprocess(self, text: str) -> str:
        """Synthesize via subprocess backend (espeak, say, powershell)."""
        command = self._command(text)
        try:
            self._process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise SynthesisError(f"TTS launch failed: {exc}") from exc
        return f"speaking {len(text)} characters via {command[0]}"

    def _play_wav(self, wav_path: str) -> None:
        """Play a WAV file using platform tools."""
        if sys.platform == "win32":
            import winsound
            winsound.PlaySound(wav_path, winsound.SND_FILENAME)
        elif shutil.which("aplay"):
            subprocess.run(["aplay", wav_path], check=True, capture_output=True)
        elif shutil.which("afplay"):
            subprocess.run(["afplay", wav_path], check=True, capture_output=True)
        else:
            # Last resort: use a subprocess that exits immediately
            subprocess.run([sys.executable, "-c", f"import winsound; winsound.PlaySound(r'{wav_path}', 1)"])

    def _play_pcm(self, audio_bytes: bytes, sample_rate: int = 22050) -> None:
        """Play raw PCM audio (int16) by wrapping in WAV and using platform tools."""
        import io
        import wave

        # Wrap PCM in WAV format
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)  # 16-bit
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(audio_bytes)

        self._play_wav_bytes(wav_buffer.getvalue())

    def _play_wav_bytes(self, wav_data: bytes) -> None:
        """Play a complete in-memory WAV file using platform tools."""
        # Play via platform method
        if sys.platform == "win32":
            # Windows: write temp file and play with winsound
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp.write(wav_data)
                tmp_path = tmp.name
            try:
                import winsound
                winsound.PlaySound(tmp_path, winsound.SND_FILENAME)
            finally:
                os.unlink(tmp_path)
        elif shutil.which("aplay"):
            subprocess.run(["aplay"], input=wav_data, check=True, capture_output=True)
        elif shutil.which("afplay"):
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp.write(wav_data)
                tmp_path = tmp.name
            try:
                subprocess.run(["afplay", tmp_path], check=True, capture_output=True)
            finally:
                os.unlink(tmp_path)
        else:
            logger.warning("No audio playback method available")

    @property
    def speaking(self) -> bool:
        """Whether an utterance is currently playing."""
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def is_speaking_or_recent(self, tail_s: float) -> bool:
        """``True`` if playback is in progress, or finished within ``tail_s``
        seconds — the signal a wake-word loopback guard suppresses on.
        Covers the blocking Piper/Jarvis path (``_speak_started``/``_speak_ended``)
        and the async subprocess path (``speaking``, since a subprocess
        backend's own player keeps running after ``speak()`` returns)."""
        if self._speak_started is not None or self.speaking:
            return True
        ended = self._speak_ended
        return ended is not None and (time.monotonic() - ended) < tail_s

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
        self._fake_ended_at: float | None = None

    def speak(self, text: str, source: Literal["system", "chat"] = "chat") -> str:
        self.spoken.append(text)
        self.utterances += 1
        self._fake_speaking = True
        self._fake_ended_at = None
        return f"speaking {len(text)} characters via fake"

    @property
    def speaking(self) -> bool:
        return self._fake_speaking

    def stop(self) -> bool:
        was_speaking = self._fake_speaking
        self._fake_speaking = False
        if was_speaking:
            self.interruptions += 1
            self._fake_ended_at = time.monotonic()
        return was_speaking

    def is_speaking_or_recent(self, tail_s: float) -> bool:
        if self._fake_speaking:
            return True
        ended = self._fake_ended_at
        return ended is not None and (time.monotonic() - ended) < tail_s
