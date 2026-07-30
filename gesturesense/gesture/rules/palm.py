"""Whole-hand gestures: Open Palm (high five) and Closed Fist."""

from __future__ import annotations

from gesturesense.gesture.base import GestureRule, register_gesture
from gesturesense.gesture.features import HandFeatures
from gesturesense.gesture.rules import scoring as sc


@register_gesture
class OpenPalm(GestureRule):
    """All five fingers extended — a high five."""

    name = "Open Palm"

    def score(self, features: HandFeatures) -> float:
        return self.all_of(
            sc.extended(features, "thumb"),
            sc.extended(features, "index"),
            sc.extended(features, "middle"),
            sc.extended(features, "ring"),
            sc.extended(features, "pinky"),
        )


@register_gesture
class ClosedFist(GestureRule):
    """All five fingers curled into the palm."""

    name = "Closed Fist"

    def score(self, features: HandFeatures) -> float:
        return self.all_of(
            sc.not_extended(features, "thumb"),
            sc.curled(features, "index"),
            sc.curled(features, "middle"),
            sc.curled(features, "ring"),
            sc.curled(features, "pinky"),
        )
