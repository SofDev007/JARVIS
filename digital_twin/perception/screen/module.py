"""Screen reading: what the user can see, as one gated snapshot of text.

The design decision that matters here is what this module is **not**: it
is not a polling sensor. Window titles (M4) are one line; the full screen
is passwords, private messages, medical records — the most sensitive
perception surface the assistant will ever have. So:

* The screen is captured **only** inside :meth:`read_now`, which is
  invoked exclusively by the ``read_screen`` action — SENSITIVE, so every
  single capture passes the permission policy, the confirmation gate and
  the audit log. There is no timer, no subscription that triggers capture,
  and no ungated code path to a screenshot (the same structural-privacy
  discipline as the M9 push-to-talk microphone).
* The screenshot file lives in a fresh private temp directory and is
  deleted in a ``finally`` block the moment OCR returns — success or not.
* The audit trail records **character counts, never the text**; the text
  itself travels once, bounded by ``max_chars``, on ``perception.screen``
  where the reasoner picks it up.

Backends resolve lazily on first use (M5 pattern): the kernel starts on
machines without scrot/tesseract, and a triggered ``read_screen`` fails as
an audited ``failed`` result carrying install guidance.
"""

from __future__ import annotations

import logging
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

from digital_twin.configuration.settings import ScreenReadingConfig
from digital_twin.core.events import Event, Topics
from digital_twin.core.module import BaseModule, ModuleState
from digital_twin.perception.screen.capture import (
    ScreenCapturer,
    create_screen_capturer,
)
from digital_twin.perception.screen.ocr import (
    TextRecognizer,
    create_text_recognizer,
)

logger = logging.getLogger(__name__)


def normalize_ocr_text(raw: str) -> str:
    """Collapse OCR noise: strip each line, drop blank runs, keep order."""
    lines = [line.strip() for line in raw.splitlines()]
    return "\n".join(line for line in lines if line)


class ScreenReadingModule(BaseModule):
    """Publishes ``perception.screen`` — but only when explicitly asked."""

    name = "screen_reader"
    topics = (Topics.SCREEN,)

    def __init__(
        self,
        config: ScreenReadingConfig,
        capturer_factory: Callable[[], ScreenCapturer] | None = None,
        recognizer_factory: Callable[[], TextRecognizer] | None = None,
    ):
        super().__init__()
        self._config = config
        self._capturer_factory = capturer_factory or (
            lambda: create_screen_capturer(config.capture_backend)
        )
        self._recognizer_factory = recognizer_factory or (
            lambda: create_text_recognizer(config.ocr_language)
        )
        self._capturer: ScreenCapturer | None = None
        self._recognizer: TextRecognizer | None = None
        self._resolve_lock = threading.Lock()
        self._reads = 0
        self._last_chars = 0

    # ------------------------------------------------------------------
    # Lifecycle — deliberately trivial: nothing runs until asked.
    # ------------------------------------------------------------------
    def _on_start(self) -> None:
        """No threads, no polling — the module only acts on demand."""

    def _on_stop(self) -> None:
        with self._resolve_lock:
            self._capturer = None
            self._recognizer = None

    def _on_pause(self) -> None:
        """A paused screen reader refuses read_now (state guard)."""

    def _on_resume(self) -> None:
        """Nothing to rebuild."""

    # ------------------------------------------------------------------
    # The one capability — called by the gated read_screen action only.
    # ------------------------------------------------------------------
    def read_now(self) -> str:
        """Capture the screen once, OCR it, publish the text; return a
        privacy-safe summary (counts only — this string reaches the audit
        log and action results, the text itself must not)."""
        if self.state is not ModuleState.RUNNING:
            raise RuntimeError("screen reader is not running")
        capturer, recognizer = self._resolve()

        tmp_dir = Path(tempfile.mkdtemp(prefix="dtwin-screen-"))
        shot = tmp_dir / "capture.png"
        try:
            capturer.capture(shot, self._config.capture_timeout_s)
            raw = recognizer.recognize(shot, self._config.ocr_timeout_s)
        finally:
            # Structural privacy: the screenshot never outlives this call.
            try:
                shot.unlink(missing_ok=True)
                tmp_dir.rmdir()
            except OSError:
                logger.warning("Could not remove screenshot temp dir %s", tmp_dir)

        text = normalize_ocr_text(raw)
        truncated = len(text) > self._config.max_chars
        if truncated:
            text = text[: self._config.max_chars]
        self._reads += 1
        self._last_chars = len(text)
        self._publish(Event(
            topic=Topics.SCREEN,
            source=self.name,
            payload={"text": text, "chars": len(text), "truncated": truncated},
        ))
        summary = f"read {len(text)} characters from the screen"
        if truncated:
            summary += f" (truncated to {self._config.max_chars})"
        return summary

    # ------------------------------------------------------------------
    def _resolve(self) -> tuple[ScreenCapturer, TextRecognizer]:
        """Create backends once, on first use, thread-safely (M5 pattern)."""
        with self._resolve_lock:
            if self._capturer is None:
                self._capturer = self._capturer_factory()
            if self._recognizer is None:
                self._recognizer = self._recognizer_factory()
            return self._capturer, self._recognizer

    def _metrics(self) -> dict[str, Any]:
        metrics: dict[str, Any] = {
            "reads": self._reads,
            "last_chars": self._last_chars,
        }
        if self._capturer is not None:
            metrics["capture_backend"] = self._capturer.name
        if self._recognizer is not None:
            metrics["ocr_backend"] = self._recognizer.name
        return metrics
