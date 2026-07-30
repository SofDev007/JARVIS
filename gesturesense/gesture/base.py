"""Gesture rule abstraction and plug-in registry.

Adding a new gesture requires exactly one new class decorated with
:func:`register_gesture` — no existing code changes (open/closed principle).
Rules return soft confidence scores in ``[0, 1]``; the engine picks the best
match and applies temporal stabilisation.

A future ML classifier can implement :class:`GestureRule` (or a batch variant
of it) and be registered the same way, coexisting with the geometric rules.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Iterable, Type

from gesturesense.gesture.features import HandFeatures


@dataclass(frozen=True)
class GestureMatch:
    """A scored gesture candidate for one hand."""

    name: str
    confidence: float


class GestureRule(ABC):
    """Base class for a single gesture detector."""

    #: Human-readable gesture name shown in the UI.
    name: str = "Unnamed"

    @abstractmethod
    def score(self, features: HandFeatures) -> float:
        """Return a confidence in ``[0, 1]`` that ``features`` match this gesture."""

    # ------------------------------------------------------------------
    # Scoring helpers shared by concrete rules
    # ------------------------------------------------------------------
    @staticmethod
    def all_of(*scores: float) -> float:
        """Combine hard requirements: the weakest constraint dominates.

        Geometric mean of the two smallest scores keeps confidence honest —
        a gesture is only as convincing as its least-satisfied constraint —
        while remaining smooth for near-misses.
        """
        if not scores:
            return 0.0
        ordered = sorted(scores)
        if len(ordered) == 1:
            return ordered[0]
        return (ordered[0] * ordered[1]) ** 0.5


_REGISTRY: dict[str, Type[GestureRule]] = {}


def register_gesture(cls: Type[GestureRule]) -> Type[GestureRule]:
    """Class decorator adding a :class:`GestureRule` to the global registry."""
    if not issubclass(cls, GestureRule):  # pragma: no cover - developer error
        raise TypeError(f"{cls.__name__} must subclass GestureRule")
    if cls.name in _REGISTRY:  # pragma: no cover - developer error
        raise ValueError(f"Duplicate gesture name: {cls.name!r}")
    _REGISTRY[cls.name] = cls
    return cls


def registered_rules() -> list[GestureRule]:
    """Instantiate one of every registered rule."""
    return [rule_cls() for rule_cls in _REGISTRY.values()]


def registered_names() -> Iterable[str]:
    """Names of all registered gestures."""
    return tuple(_REGISTRY.keys())
