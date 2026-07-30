"""Module registry: owns every module's lifecycle with fault isolation.

The registry is the kernel's view of the system. It starts modules in
registration order, stops them in reverse, exposes runtime enable/disable,
and — crucially — isolates failures: one module failing to start or stop
never prevents the others from running. Every transition is announced on
the bus (``system.module``) so dashboards, logs and future watchdogs all
observe module health the same way everything else is observed: as events.
"""

from __future__ import annotations

import logging
from functools import partial

from digital_twin.core.bus import EventBus
from digital_twin.core.events import Event, Topics
from digital_twin.core.module import BaseModule, ModuleStatus

logger = logging.getLogger(__name__)


class ModuleRegistry:
    """Ordered collection of modules with uniform, fault-isolated control."""

    def __init__(self, bus: EventBus):
        self._bus = bus
        self._modules: dict[str, BaseModule] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------
    def register(self, module: BaseModule) -> None:
        """Add a module; names must be unique."""
        if module.name in self._modules:
            raise ValueError(f"Module name already registered: {module.name!r}")
        self._modules[module.name] = module
        logger.info(
            "Registered module %s (topics: %s)",
            module.name,
            ", ".join(module.topics) or "none",
        )

    def get(self, name: str) -> BaseModule:
        """Look up a module by name (raises ``KeyError`` if unknown)."""
        return self._modules[name]

    @property
    def names(self) -> tuple[str, ...]:
        """Registered module names in registration order."""
        return tuple(self._modules)

    # ------------------------------------------------------------------
    # Bulk lifecycle
    # ------------------------------------------------------------------
    def start_all(self) -> None:
        """Start every module; failures are isolated and announced."""
        for module in self._modules.values():
            self._transition(module, "start", partial(module.start, self._bus))

    def stop_all(self) -> None:
        """Stop every module in reverse registration order."""
        for module in reversed(list(self._modules.values())):
            self._transition(module, "stop", module.stop)

    # ------------------------------------------------------------------
    # Runtime enable/disable
    # ------------------------------------------------------------------
    def disable(self, name: str) -> None:
        """Pause a module at runtime (releases resources where it can)."""
        module = self.get(name)
        self._transition(module, "disable", module.pause, reraise=True)

    def enable(self, name: str) -> None:
        """Resume a previously disabled module."""
        module = self.get(name)
        self._transition(module, "enable", module.resume, reraise=True)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    def statuses(self) -> list[ModuleStatus]:
        """Health snapshot of every registered module."""
        return [module.status() for module in self._modules.values()]

    # ------------------------------------------------------------------
    def _transition(
        self, module: BaseModule, action: str, operation, reraise: bool = False
    ) -> None:
        detail = ""
        try:
            operation()
        except Exception as exc:
            detail = str(exc)
            logger.exception("Module %s: %s failed", module.name, action)
            if reraise:
                self._announce(module, action, detail)
                raise
        self._announce(module, action, detail)

    def _announce(self, module: BaseModule, action: str, detail: str) -> None:
        self._bus.publish(
            Event(
                topic=Topics.MODULE,
                source="registry",
                payload={
                    "name": module.name,
                    "action": action,
                    "state": module.state.value,
                    "detail": detail,
                },
            )
        )
