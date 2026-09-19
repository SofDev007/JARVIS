"""FrameHub — latest-frame fan-out from perception modules to browsers.

Perception code pushes JPEG bytes under a source name (`hub.sink("camera")`
returns a plain callable, so modules never import dashboard machinery);
the dashboard's MJPEG endpoint waits on the hub and forwards each new
frame to however many browser tabs are watching. Only the *latest* frame
is kept per source — a slow viewer sees dropped frames, never growing
memory, and a stopped producer simply means viewers wait.

It was built for the Python gesture debug view, which is gone: hand tracking
now runs in the Airboard browser page, so no built-in module produces frames
today. The hub stays as generic infrastructure for any future producer.
"""

from __future__ import annotations

import threading
import time
from typing import Callable


class FrameHub:
    """Thread-safe latest-frame store with change notification."""

    def __init__(self) -> None:
        self._frames: dict[str, bytes] = {}
        self._seq: dict[str, int] = {}
        self._condition = threading.Condition()

    # -- producer side -----------------------------------------------------
    def push(self, name: str, jpeg: bytes) -> None:
        if not jpeg:
            return
        with self._condition:
            self._frames[name] = jpeg
            self._seq[name] = self._seq.get(name, 0) + 1
            self._condition.notify_all()

    def sink(self, name: str) -> Callable[[bytes], None]:
        """A bound push callable — hand this to a perception module."""
        return lambda jpeg: self.push(name, jpeg)

    # -- consumer side -----------------------------------------------------
    def sources(self) -> tuple[str, ...]:
        with self._condition:
            return tuple(sorted(self._frames))

    def latest(self, name: str) -> bytes | None:
        with self._condition:
            return self._frames.get(name)

    def wait_next(self, name: str, last_seq: int,
                  timeout_s: float) -> tuple[bytes, int] | None:
        """Block until a frame newer than ``last_seq`` arrives (or timeout).

        Returns ``(jpeg, seq)`` or ``None`` on timeout.
        """
        deadline = time.monotonic() + timeout_s
        with self._condition:
            while self._seq.get(name, 0) <= last_seq:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
            return self._frames[name], self._seq[name]
