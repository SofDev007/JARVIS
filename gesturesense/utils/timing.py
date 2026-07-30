"""Timing utilities: FPS measurement and frame-rate limiting."""

from __future__ import annotations

import time


class FpsCounter:
    """Exponential-moving-average FPS counter.

    EMA over instantaneous frame intervals gives a stable readout that still
    reacts to real slowdowns within a second or so, without the sawtooth of
    fixed-window counters.
    """

    def __init__(self, alpha: float = 0.1):
        self._alpha = alpha
        self._last_tick: float | None = None
        self._interval: float | None = None

    def tick(self, now: float | None = None) -> float:
        """Register a frame; returns the current FPS estimate."""
        now = time.perf_counter() if now is None else now
        if self._last_tick is not None:
            interval = max(now - self._last_tick, 1e-6)
            if self._interval is None:
                self._interval = interval
            else:
                self._interval += self._alpha * (interval - self._interval)
        self._last_tick = now
        return self.fps

    @property
    def fps(self) -> float:
        """Current FPS estimate (``0.0`` until two ticks have been seen)."""
        if self._interval is None:
            return 0.0
        return 1.0 / self._interval

    def reset(self) -> None:
        """Clear state (e.g. after a camera reconnect)."""
        self._last_tick = None
        self._interval = None


class RateLimiter:
    """Sleep-based frame-rate cap for the render loop."""

    def __init__(self, max_fps: float):
        self._min_interval = 1.0 / max(max_fps, 1e-6)
        self._next_deadline: float | None = None

    def wait(self) -> None:
        """Sleep just enough to keep the loop at or under the target rate."""
        now = time.perf_counter()
        if self._next_deadline is None:
            self._next_deadline = now + self._min_interval
            return
        delay = self._next_deadline - now
        if delay > 0:
            time.sleep(delay)
            self._next_deadline += self._min_interval
        else:
            # Fell behind — resynchronise instead of accumulating debt.
            self._next_deadline = now + self._min_interval
