"""Module contract: standardized lifecycle for every assistant subsystem.

A *module* is any replaceable unit — a perception source (gestures, voice,
screen), a reasoning component, or an automation engine. All of them share
one small lifecycle so the kernel can start, stop, enable and disable them
uniformly, and so a failing module degrades gracefully instead of taking
the assistant down.

State machine::

    CREATED ──start──▶ RUNNING ──pause──▶ PAUSED
       ▲                 │  ▲               │
       │                 │  └────resume─────┘
       │               stop
    (re-startable) ◀── STOPPED        any hook raising → FAILED
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from digital_twin.core.bus import EventBus

logger = logging.getLogger(__name__)


class ModuleState(Enum):
    """Lifecycle state of a module."""

    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class InvalidStateError(RuntimeError):
    """Raised when a lifecycle transition is not allowed from the current state."""


@dataclass(frozen=True)
class ModuleStatus:
    """Point-in-time health report for one module."""

    name: str
    state: ModuleState
    detail: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)


class BaseModule:
    """Base class implementing the thread-safe lifecycle state machine.

    Subclasses override the ``_on_*`` hooks; the public methods enforce the
    valid transitions, record failures and stay idempotent where that is
    the safe behaviour (repeated ``stop`` is a no-op, repeated ``start``
    from ``RUNNING`` raises).
    """

    #: Unique module name; subclasses must override.
    name: str = ""
    #: Topics this module publishes (introspection/documentation).
    topics: tuple[str, ...] = ()

    def __init__(self) -> None:
        if not self.name:
            raise ValueError(f"{type(self).__name__} must define a non-empty name")
        self._state = ModuleState.CREATED
        self._state_lock = threading.RLock()
        self._detail = ""
        self._bus: EventBus | None = None

    # ------------------------------------------------------------------
    # Public lifecycle API
    # ------------------------------------------------------------------
    def start(self, bus: EventBus) -> None:
        """Start the module and attach it to the event bus."""
        with self._state_lock:
            if self._state not in (ModuleState.CREATED, ModuleState.STOPPED):
                raise InvalidStateError(
                    f"{self.name}: cannot start from {self._state.value}"
                )
            self._state = ModuleState.STARTING
            self._bus = bus
        try:
            self._on_start()
        except Exception as exc:
            self._fail(f"start failed: {exc}")
            raise
        with self._state_lock:
            self._state = ModuleState.RUNNING
            self._detail = ""
        logger.info("Module %s started", self.name)

    def stop(self) -> None:
        """Stop the module and release its resources (idempotent)."""
        with self._state_lock:
            if self._state in (ModuleState.CREATED, ModuleState.STOPPED):
                return
            self._state = ModuleState.STOPPING
        try:
            self._on_stop()
        except Exception as exc:
            self._fail(f"stop failed: {exc}")
            raise
        with self._state_lock:
            self._state = ModuleState.STOPPED
        logger.info("Module %s stopped", self.name)

    def pause(self) -> None:
        """Disable the module at runtime, releasing what it can (idempotent)."""
        with self._state_lock:
            if self._state is ModuleState.PAUSED:
                return
            if self._state is not ModuleState.RUNNING:
                raise InvalidStateError(
                    f"{self.name}: cannot pause from {self._state.value}"
                )
        try:
            self._on_pause()
        except Exception as exc:
            self._fail(f"pause failed: {exc}")
            raise
        with self._state_lock:
            self._state = ModuleState.PAUSED
        logger.info("Module %s paused", self.name)

    def resume(self) -> None:
        """Re-enable a paused module (idempotent for RUNNING)."""
        with self._state_lock:
            if self._state is ModuleState.RUNNING:
                return
            if self._state is not ModuleState.PAUSED:
                raise InvalidStateError(
                    f"{self.name}: cannot resume from {self._state.value}"
                )
        try:
            self._on_resume()
        except Exception as exc:
            self._fail(f"resume failed: {exc}")
            raise
        with self._state_lock:
            self._state = ModuleState.RUNNING
        logger.info("Module %s resumed", self.name)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    @property
    def state(self) -> ModuleState:
        """Current lifecycle state (thread-safe)."""
        with self._state_lock:
            return self._state

    @property
    def is_active(self) -> bool:
        """Whether the module is currently running (not paused/stopped)."""
        return self.state is ModuleState.RUNNING

    def status(self) -> ModuleStatus:
        """Health report including subclass-provided metrics."""
        with self._state_lock:
            state, detail = self._state, self._detail
        try:
            metrics = self._metrics()
        except Exception:  # metrics must never break health reporting
            logger.exception("Module %s metrics failed", self.name)
            metrics = {}
        return ModuleStatus(name=self.name, state=state, detail=detail, metrics=metrics)

    # ------------------------------------------------------------------
    # Hooks for subclasses
    # ------------------------------------------------------------------
    def _on_start(self) -> None:
        """Acquire resources and begin producing/consuming events."""

    def _on_stop(self) -> None:
        """Release all resources."""

    def _on_pause(self) -> None:
        """Suspend work; release expensive resources where possible."""
        self._on_stop()

    def _on_resume(self) -> None:
        """Undo :meth:`_on_pause`."""
        self._on_start()

    def _metrics(self) -> dict[str, Any]:
        """Subclass metrics for :meth:`status` (override as needed)."""
        return {}

    # ------------------------------------------------------------------
    def _publish(self, event) -> None:
        """Publish to the attached bus (no-op with a warning if detached)."""
        bus = self._bus
        if bus is None:
            logger.warning("Module %s tried to publish before start", self.name)
            return
        bus.publish(event)

    def _fail(self, detail: str) -> None:
        with self._state_lock:
            self._state = ModuleState.FAILED
            self._detail = detail
        logger.error("Module %s FAILED: %s", self.name, detail)
