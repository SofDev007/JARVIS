"""Directional pointing gestures: index finger extended along a screen axis."""

from __future__ import annotations

import numpy as np

from gesturesense.gesture.base import GestureRule, register_gesture
from gesturesense.gesture.features import HandFeatures
from gesturesense.gesture.rules import scoring as sc


class _PointingRule(GestureRule):
    """Only the index finger extended, aimed along ``axis``.

    Requires the thumb *not* extended so that Finger Gun (thumb + index)
    remains unambiguous.
    """

    axis: np.ndarray = sc.UP

    def score(self, features: HandFeatures) -> float:
        return self.all_of(
            sc.extended(features, "index"),
            sc.curled(features, "middle"),
            sc.curled(features, "ring"),
            sc.curled(features, "pinky"),
            sc.not_extended(features, "thumb"),
            sc.direction_score(features.direction("index"), self.axis),
        )


@register_gesture
class PointingUp(_PointingRule):
    name = "Pointing Up"
    axis = sc.UP


@register_gesture
class PointingDown(_PointingRule):
    name = "Pointing Down"
    axis = sc.DOWN


@register_gesture
class PointingLeft(_PointingRule):
    name = "Pointing Left"
    axis = sc.LEFT


@register_gesture
class PointingRight(_PointingRule):
    name = "Pointing Right"
    axis = sc.RIGHT
