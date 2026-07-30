"""Application-wide structured logging setup."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from gesturesense.config.settings import LoggingConfig

_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"


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
    root.addHandler(console)

    if not config.enabled:
        return

    try:
        log_dir = Path(config.directory)
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "gesturesense.log",
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as exc:  # pragma: no cover - environment dependent
        root.warning("File logging disabled (%s); continuing with console only", exc)
