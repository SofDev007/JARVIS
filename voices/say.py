"""Quick manual test: speak one phrase with the configured JARVIS/Piper voice.

Usage: python voices/say.py "Good evening, boss. How's your day going?"
"""
from __future__ import annotations

import sys

from digital_twin.voice.synthesis import SpeechSynthesizer

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(1)
    synth = SpeechSynthesizer(
        backend="jarvis",
        piper_data_dir="models/piper",
        jarvis_reference_wav="voices/reference_voice.wav",
        jarvis_precache_dir="voices/precache",
    )
    print(synth.speak(sys.argv[1], source="chat"))
