"""Text recognition over screenshots: tesseract behind a small interface.

Mirrors the M9 transcriber design: one ABC, one real backend that shells
out to the ``tesseract`` binary (no ``pytesseract`` dependency — a
subprocess is auditable and trivially replaceable), and a scripted double
for hardware-free tests. OCR happens **locally**; screen content never
leaves the machine.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path

logger = logging.getLogger(__name__)


class OCRError(RuntimeError):
    """Recognition failed or the OCR engine is unavailable."""


class TextRecognizer(ABC):
    """Backend interface: image file → recognized text."""

    name: str = "abstract"

    @classmethod
    @abstractmethod
    def available(cls) -> bool:
        """Whether this backend can work in the current environment."""

    @abstractmethod
    def recognize(self, image_path: Path, timeout_s: float) -> str:
        """Return the text visible in ``image_path`` (may be empty)."""


class TesseractRecognizer(TextRecognizer):
    """Local OCR via the ``tesseract`` command-line binary."""

    name = "tesseract"

    def __init__(self, language: str = "eng"):
        self._language = language

    @classmethod
    def available(cls) -> bool:
        return shutil.which("tesseract") is not None

    def recognize(self, image_path: Path, timeout_s: float) -> str:
        command = ["tesseract", str(image_path), "stdout",
                   "-l", self._language]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=timeout_s
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise OCRError(f"tesseract failed: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or "").strip()[:200]
            raise OCRError(
                f"tesseract exited {result.returncode}: {detail or '(no output)'}"
            )
        return result.stdout


class ScriptedRecognizer(TextRecognizer):
    """Test double: pops canned texts; records every image it was fed."""

    name = "scripted"

    def __init__(self, texts: list[str] | None = None):
        self._texts = list(texts or [])
        self.recognized: list[Path] = []

    @classmethod
    def available(cls) -> bool:
        return True

    def recognize(self, image_path: Path, timeout_s: float) -> str:
        self.recognized.append(image_path)
        if not self._texts:
            return ""
        return self._texts.pop(0) if len(self._texts) > 1 else self._texts[0]


def create_text_recognizer(language: str = "eng") -> TextRecognizer:
    """Return a usable recognizer or raise with install guidance."""
    if TesseractRecognizer.available():
        logger.info("OCR backend: tesseract (language=%s)", language)
        return TesseractRecognizer(language=language)
    raise OCRError(
        "tesseract is not installed. Install it to enable screen reading: "
        "Linux: sudo apt install tesseract-ocr; macOS: brew install "
        "tesseract; Windows: https://github.com/UB-Mannheim/tesseract. "
        "Alternatively disable screen_reading in the configuration."
    )
