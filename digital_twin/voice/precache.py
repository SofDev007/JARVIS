"""Pre-cache system phrases with JARVIS voice (XTTS-v2).

Run this once after setup to generate premium voice audio for system phrases:

    python -m digital_twin.voice.precache

The script reads precached_phrases from config (or a phrases.txt file) and
synthesizes each one using the XTTS-v2 model, saving .wav files to the
precache directory. On CPU, this takes a few seconds per phrase.

Usage:
    python -m digital_twin.voice.precache [--config CONFIG.YAML]
    python -m digital_twin.voice.precache --list  # show phrases to cache
    python -m digital_twin.voice.precache --missing  # only generate missing
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def load_config_phrases(config_path: Path | None = None) -> list[str]:
    """Load precached_phrases from config file."""
    from digital_twin.configuration.settings import load_config

    config = load_config(config_path or Path("config/default_config.yaml"))
    return list(config.voice.precached_phrases)


def load_phrases_file(phrases_path: Path) -> list[str]:
    """Load phrases from a text file (one per line, # comments ignored)."""
    lines = []
    for line in phrases_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def precache_phrases(
    phrases: list[str],
    reference_wav: str,
    output_dir: str,
    force: bool = False,
) -> tuple[int, int]:
    """Generate .wav files for all phrases using XTTS-v2.

    Returns (generated, skipped) count.
    """
    import os

    if not os.path.isfile(reference_wav):
        print(f"ERROR: Reference wav not found: {reference_wav}")
        print("Provide a 6-20 second clean mono WAV clip of a British male narrator.")
        print("See voices/jarvis_voice.py docstring for guidance.")
        return 0, 0

    # Lazy import XTTS (heavy dependency)
    try:
        import torch
        from TTS.api import TTS
    except ImportError as exc:
        print(f"ERROR: Missing dependency: {exc}")
        print("Install with: pip install coqui-tts torch torchaudio soundfile")
        return 0, 0

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Loading XTTS-v2 on {device}...")
    tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(device)

    generated, skipped = 0, 0

    for phrase in phrases:
        # Match the hash logic from jarvis_voice.py
        import hashlib
        key = hashlib.sha1(f"{phrase}|{reference_wav}".encode()).hexdigest()[:16]
        wav_path = output_path / f"{key}.wav"

        if wav_path.exists() and not force:
            print(f"  [skip] {phrase[:50]}...")
            skipped += 1
            continue

        print(f"  [gen]  {phrase[:50]}...")
        tts.tts_to_file(
            text=phrase,
            speaker_wav=reference_wav,
            language="en",
            file_path=str(wav_path),
        )
        generated += 1

    return generated, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="digital_twin.voice.precache",
        description="Pre-generate JARVIS voice audio for system phrases.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to config YAML (default: config/default_config.yaml)",
    )
    parser.add_argument(
        "--phrases",
        type=Path,
        default=None,
        help="Path to phrases.txt file (overrides config)",
    )
    parser.add_argument(
        "--reference-wav",
        type=str,
        default=None,
        help="Reference voice clip (overrides config)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Output directory (overrides config)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        dest="list_phrases",
        help="List phrases to cache without generating",
    )
    parser.add_argument(
        "--missing",
        action="store_true",
        help="Only generate phrases not already cached",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate even if .wav already exists",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # Load phrases
    if args.phrases:
        phrases = load_phrases_file(args.phrases)
    else:
        phrases = load_config_phrases(args.config)

    if not phrases:
        print("No phrases to cache. Add phrases to config.voice.precached_phrases")
        print("or provide a --phrases file.")
        return 1

    if args.list_phrases:
        print(f"Phrases to cache ({len(phrases)}):")
        for p in phrases:
            print(f"  - {p}")
        return 0

    # Load paths from config
    from digital_twin.configuration.settings import load_config
    config = load_config(args.config or Path("config/default_config.yaml"))

    reference_wav = args.reference_wav or config.voice.jarvis_reference_wav
    output_dir = args.output_dir or config.voice.jarvis_precache_dir

    print(f"Reference voice: {reference_wav}")
    print(f"Output directory: {output_dir}")
    print(f"Phrases: {len(phrases)}")
    print()

    generated, skipped = precache_phrases(
        phrases,
        reference_wav,
        output_dir,
        force=args.force or not args.missing,
    )

    print()
    print(f"Done: {generated} generated, {skipped} skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
