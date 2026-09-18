"""Application-wide structured logging setup."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from digital_twin.configuration.settings import LoggingConfig

_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"

# Console stays quiet by design (the kernel's minimal "UI" is
# main.py's Assistant: reply line) — these are the only INFO-level
# loggers allowed through to the terminal: wake word state, microphone
# open/close, and the transcribed utterance. Everything else (module
# lifecycle, action gating, raw event payloads) still reaches the log
# file at the configured level; only the console handler is filtered.
_VOICE_CONSOLE_LOGGERS = {
    "digital_twin.voice.wake",
    "digital_twin.voice.audio",
    "digital_twin.voice.module",
}


class _ConsoleNoiseFilter(logging.Filter):
    """WARNING+ from anywhere always shows; INFO only from the voice
    pipeline's own loggers (see ``_VOICE_CONSOLE_LOGGERS``)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        return record.name in _VOICE_CONSOLE_LOGGERS


def setup_logging(config: LoggingConfig) -> None:
    """Configure root logging with console + rotating file handlers.

    File logging failures (read-only directory, permissions) degrade to
    console-only logging — logging must never take the application down.
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, config.level.upper(), logging.INFO))

    # Idempotent: clear handlers installed by earlier calls (tests, reloads).
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(_FORMAT)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    console.addFilter(_ConsoleNoiseFilter())
    root.addHandler(console)

    if not config.enabled:
        return

    try:
        log_dir = Path(config.directory)
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "digital_twin.log",
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as exc:  # pragma: no cover - environment dependent
        root.warning("File logging disabled (%s); continuing with console only", exc)
