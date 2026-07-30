"""Performance probes for the debug overlay and FPS logging.

``psutil`` is optional: when unavailable, CPU/memory readouts simply report
as unavailable instead of adding a hard dependency.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator

try:  # pragma: no cover - environment dependent
    import psutil

    _PROCESS = psutil.Process()
    _PROCESS.cpu_percent(interval=None)  # prime the sampler
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]
    _PROCESS = None


class StageTimings:
    """EMA-smoothed wall-clock timings for named pipeline stages (ms)."""

    def __init__(self, alpha: float = 0.15):
        self._alpha = alpha
        self._timings: dict[str, float] = {}

    @contextmanager
    def measure(self, stage: str) -> Iterator[None]:
        """Context manager recording the duration of ``stage``."""
        start = time.perf_counter()
        try:
            yield
        finally:
            self.record(stage, (time.perf_counter() - start) * 1000.0)

    def record(self, stage: str, elapsed_ms: float) -> None:
        """Record one sample for ``stage`` with EMA smoothing."""
        previous = self._timings.get(stage)
        if previous is None:
            self._timings[stage] = elapsed_ms
        else:
            self._timings[stage] = previous + self._alpha * (elapsed_ms - previous)

    def snapshot(self) -> dict[str, float]:
        """Copy of current smoothed timings in milliseconds."""
        return dict(self._timings)


def system_usage() -> tuple[float | None, float | None]:
    """Return ``(cpu_percent, memory_mb)`` for this process, if measurable."""
    if _PROCESS is None:
        return None, None
    try:  # pragma: no cover - environment dependent
        cpu = _PROCESS.cpu_percent(interval=None)
        mem = _PROCESS.memory_info().rss / (1024 * 1024)
        return cpu, mem
    except Exception:  # pragma: no cover - psutil edge cases
        return None, None
