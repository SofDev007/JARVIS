"""
jarvis_voice.py
JARVIS-style voice module for Friday (voice-driven personal AI assistant)

Uses Coqui XTTS-v2 (community-maintained fork: coqui-tts) for local,
zero-cost voice cloning. Point REFERENCE_VOICE at a short clip of a
calm, British-accented voice and this module handles the rest.

SETUP
-----
    pip install coqui-tts torch torchaudio soundfile

First run downloads the ~1.8 GB XTTS-v2 model automatically from
Hugging Face — do this once with a working internet connection.
After that it runs fully offline, in line with Friday's zero-cost,
local-first design.

GETTING A REFERENCE VOICE CLIP (do this before running)
---------------------------------------------------------
XTTS-v2 needs a 6-20 second, single-speaker, clean WAV clip to clone
the voice style from. For a JARVIS-style tone (calm, precise, British
RP), a good zero-cost option is a short clip from a public-domain
audiobook narration on LibriVox (librivox.org) read by a British male
narrator — search "LibriVox British male narrator" and pick any
non-fiction reading. Trim ~10 seconds of clean speech with no music
or background noise, save as reference_voice.wav in mono, 22050Hz+.

Do NOT use a clip of the actual film character's voice (Paul Bettany's
JARVIS performance) — that's a copyrighted, identifiable performance,
not just a style. A narrator's clip gets you the same tone without
that problem.

USAGE
-----
    from jarvis_voice import JarvisVoice

    voice = JarvisVoice(reference_wav="voices/reference_voice.wav")
    voice.speak("Systems online. How can I help, boss?")
"""

import os
import hashlib
from pathlib import Path
from typing import Optional

import torch
from TTS.api import TTS


class JarvisVoice:
    def __init__(
        self,
        reference_wav: str,
        language: str = "en",
        output_dir: str = "audio_out",
        device: Optional[str] = None,
    ):
        """
        reference_wav: path to the 6-20 second WAV clip described above.
        language:      XTTS-v2 language code (English = "en").
        output_dir:    where generated audio files are cached.
        device:        "cuda" or "cpu" — auto-detected if not given.
        """
        if not os.path.isfile(reference_wav):
            raise FileNotFoundError(
                f"Reference voice clip not found: {reference_wav}\n"
                "See the module docstring for how to source one."
            )

        self.reference_wav = reference_wav
        self.language = language
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")

        print(f"[JarvisVoice] Loading XTTS-v2 on {self.device} ...")
        self.tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(self.device)
        print("[JarvisVoice] Ready.")

    def _cache_path(self, text: str) -> Path:
        # Cache identical lines so Friday doesn't regenerate audio for
        # repeated phrases like "Yes, boss?" or "Done."
        key = hashlib.sha1(f"{text}|{self.reference_wav}".encode()).hexdigest()[:16]
        return self.output_dir / f"{key}.wav"

    def synthesize(self, text: str) -> str:
        """Generates speech for `text` and returns the path to the WAV file."""
        out_path = self._cache_path(text)
        if out_path.exists():
            return str(out_path)

        self.tts.tts_to_file(
            text=text,
            speaker_wav=self.reference_wav,
            language=self.language,
            file_path=str(out_path),
        )
        return str(out_path)

    def speak(self, text: str, player: str = "auto"):
        """Synthesizes and immediately plays the audio."""
        wav_path = self.synthesize(text)
        self._play(wav_path, player)

    @staticmethod
    def _play(wav_path: str, player: str):
        # Cross-platform playback without extra Python audio dependencies.
        if player == "auto":
            player = "windows" if os.name == "nt" else "aplay"

        if player == "windows":
            import winsound
            winsound.PlaySound(wav_path, winsound.SND_FILENAME)
        else:
            os.system(f'{player} "{wav_path}" >/dev/null 2>&1')


if __name__ == "__main__":
    # Quick manual test — adjust the path to wherever you saved your clip.
    voice = JarvisVoice(reference_wav="voices/reference_voice.wav")
    voice.speak("Good evening, boss. All systems are online and ready.")
