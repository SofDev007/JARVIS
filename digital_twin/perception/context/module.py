"""Screen-context perception: the module that makes the assistant hands-free.

Samples the focused window on an interval, classifies it against ordered
config rules (first case-insensitive substring match on title/process
wins), and publishes ``context.changed`` **on change only** — the same
edge-triggered discipline as the gesture module.

This closes the loop that previously needed ``--context``: the intent
engine consumes these events with zero changes (it cannot tell a CLI flag,
a demo script and this module apart — that was always the point).

Like every perception module it publishes facts, not meaning: *which
context is active*, never what any gesture should do there. Privacy
default: window titles/process names stay off the bus unless
``publish_window_info`` is explicitly enabled.

Classification (`classify`) is a pure function, unit-testable without
threads or a real probe; the probe is injectable for tests.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from digital_twin.configuration.settings import ContextPerceptionConfig
from digital_twin.core.events import Event, Topics
from digital_twin.core.module import BaseModule, ModuleState
from digital_twin.perception.context.probe import WindowInfo, WindowProbe

logger = logging.getLogger(__name__)


def compile_rules(rules: list[dict]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Pre-lower rule needles once; order is preserved (first match wins)."""
    return tuple(
        (rule["context"], tuple(needle.lower() for needle in rule["any"]))
        for rule in rules
    )


def classify(
    info: WindowInfo | None,
    compiled_rules: tuple[tuple[str, tuple[str, ...]], ...],
    fallback: str,
) -> str:
    """Map a window to a context string. ``None`` (unknown window) → fallback."""
    if info is None:
        return fallback
    haystack = info.haystack
    for context, needles in compiled_rules:
        if any(needle in haystack for needle in needles):
            return context
    return fallback


class ContextPerceptionModule(BaseModule):
    """Publishes ``context.changed`` from the focused window."""

    name = "screen_context"
    topics = (Topics.CONTEXT,)

    def __init__(
        self,
        config: ContextPerceptionConfig,
        probe_factory: Callable[[], WindowProbe] | None = None,
    ):
        super().__init__()
        self._config = config
        self._probe_factory = probe_factory or self._default_probe
        self._rules = compile_rules(config.rules)

        self._probe: WindowProbe | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        self._current_context: str | None = None
        self._last_window: WindowInfo | None = None
        self._polls = 0

    @staticmethod
    def _default_probe() -> WindowProbe:
        from digital_twin.perception.context.probe import create_window_probe

        return create_window_probe()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def _on_start(self) -> None:
        # A missing probe fails start loudly (registry isolates it); the
        # error message tells the user exactly what to install instead of
        # the module pretending to work while publishing nothing.
        self._probe = self._probe_factory()
        self._current_context = None
        self._last_window = None
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._poll_loop, name="context-poll", daemon=True
        )
        self._thread.start()

    def _on_stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self._probe = None

    # Default pause/resume (full teardown/rebuild) are exactly right here:
    # a paused context module stops sampling window titles entirely.

    # ------------------------------------------------------------------
    # Polling and event derivation
    # ------------------------------------------------------------------
    def _poll_loop(self) -> None:
        # _on_start launches this thread before the state machine flips to
        # RUNNING; without this wait the first sample would be dropped by
        # the state guard in _process_window.
        while self.state is ModuleState.STARTING and not self._stop.wait(0.001):
            pass
        while not self._stop.is_set():
            try:
                self._sample()
            except Exception:  # a bad sample must not kill perception
                logger.exception("Context sample failed")
            self._stop.wait(self._config.poll_interval_s)

    def _sample(self) -> None:
        probe = self._probe
        if probe is None:
            return
        self._process_window(probe.active_window())

    def _process_window(self, info: WindowInfo | None) -> None:
        """Classify one observation and publish on change (pure-ish, testable)."""
        if self.state is not ModuleState.RUNNING:
            return
        self._polls += 1
        self._last_window = info
        context = classify(info, self._rules, self._config.fallback_context)
        if context == self._current_context:
            return
        previous, self._current_context = self._current_context, context

        payload: dict[str, Any] = {"context": context}
        if self._config.publish_window_info and info is not None:
            payload["window_title"] = info.title
            payload["process"] = info.process
        self._publish(Event(topic=Topics.CONTEXT, source=self.name, payload=payload))
        logger.info("Screen context: %s -> %s", previous or "(none)", context)

    # ------------------------------------------------------------------
    def _metrics(self) -> dict[str, Any]:
        metrics: dict[str, Any] = {
            "context": self._current_context or "(none yet)",
            "polls": self._polls,
            "rules": len(self._rules),
        }
        probe = self._probe
        if probe is not None:
            metrics["probe"] = probe.name
        return metrics
