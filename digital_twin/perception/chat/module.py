"""Chat perception: typed text as just another perception source.

Publishes ``perception.chat`` events — ``{text, user}`` — exactly the way
the gesture module publishes gestures. The reasoning layer consuming them
cannot tell a terminal, a test, or the future dashboard chat box apart,
which is the whole architecture doing its job (this is the first
non-camera input source and required zero core changes).

Input paths:

* :meth:`submit` — programmatic (tests, demos, future UI).
* An optional console reader thread when stdin is an interactive TTY:
  type into the kernel terminal, get answers back. Known limitation,
  tracked: with console chat active, ``security.confirmation: console``
  prompts share stdin with the reader — prefer explicit ``permissions``
  rules or ``auto_deny`` until the dashboard UI owns input routing.
"""

from __future__ import annotations

import logging
import sys
import threading

from digital_twin.configuration.settings import ChatConfig
from digital_twin.core.events import Event, Topics
from digital_twin.core.module import BaseModule, ModuleState

logger = logging.getLogger(__name__)


class ChatPerceptionModule(BaseModule):
    """Turns typed text into ``perception.chat`` events."""

    name = "chat"
    topics = (Topics.CHAT,)

    def __init__(self, config: ChatConfig):
        super().__init__()
        self._config = config
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self._messages = 0

    # ------------------------------------------------------------------
    def submit(self, text: str, user: str = "user") -> bool:
        """Publish one chat message; returns ``False`` if ignored."""
        if self.state is not ModuleState.RUNNING:
            return False
        text = text.strip()
        if not text:
            return False
        self._messages += 1
        self._publish(Event(
            topic=Topics.CHAT,
            source=self.name,
            payload={"text": text, "user": user},
        ))
        return True

    # ------------------------------------------------------------------
    def _on_start(self) -> None:
        if not self._config.console:
            return
        stdin = getattr(sys, "stdin", None)
        if stdin is None or not stdin.isatty():
            logger.info("Chat console reader disabled (stdin is not a TTY); "
                        "programmatic submit() remains available")
            return
        self._stop.clear()
        self._reader = threading.Thread(
            target=self._read_loop, name="chat-console", daemon=True
        )
        self._reader.start()
        logger.info("Chat console active — type into the terminal to talk")

    def _on_stop(self) -> None:
        self._stop.set()
        # The reader may be blocked inside input(); it is a daemon thread
        # and dies with the process — same trade-off as the confirmation
        # prompt, resolved for real by the dashboard UI milestone.
        self._reader = None

    def _on_pause(self) -> None:
        """Paused chat ignores input; the reader thread keeps draining
        lines but ``submit`` refuses them (state guard)."""

    def _on_resume(self) -> None:
        """Nothing to rebuild."""

    # ------------------------------------------------------------------
    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                line = input()
            except (EOFError, KeyboardInterrupt):
                return
            if self._stop.is_set():
                return
            self.submit(line)

    # ------------------------------------------------------------------
    def _metrics(self) -> dict[str, object]:
        return {
            "messages": self._messages,
            "console": bool(self._reader is not None),
        }
