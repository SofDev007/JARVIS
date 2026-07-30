"""Action registry: the catalog of everything the assistant *can* do.

An :class:`ActionSpec` bundles a handler with its risk level and a
parameter validator, so the dispatcher can make security decisions about
an action without knowing anything about its implementation. Registries
are instances (not module-global) — each dispatcher owns its catalog,
which keeps tests isolated and future plugin sandboxing tractable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from digital_twin.security.permissions import RiskLevel


def _no_validation(params: Mapping[str, Any]) -> None:
    """Default validator: accept anything."""


@dataclass(frozen=True)
class ActionSpec:
    """One executable capability with its safety metadata."""

    name: str
    description: str
    risk: RiskLevel
    handler: Callable[[Mapping[str, Any]], str | None]
    """Runs the action; may return a short human-readable detail string."""
    validate: Callable[[Mapping[str, Any]], None] = field(default=_no_validation)
    """Raises ``ValueError`` for unacceptable parameters (checked *before*
    the permission gate, so a user is never asked to confirm garbage)."""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("ActionSpec.name must be non-empty")


class ActionRegistry:
    """Name-unique collection of :class:`ActionSpec`."""

    def __init__(self) -> None:
        self._actions: dict[str, ActionSpec] = {}

    def register(self, spec: ActionSpec) -> None:
        """Add an action; duplicate names are a programming error."""
        if spec.name in self._actions:
            raise ValueError(f"Action already registered: {spec.name!r}")
        self._actions[spec.name] = spec

    def get(self, name: str) -> ActionSpec | None:
        """Look up an action, ``None`` if unknown."""
        return self._actions.get(name)

    @property
    def names(self) -> tuple[str, ...]:
        """Registered action names."""
        return tuple(self._actions)
