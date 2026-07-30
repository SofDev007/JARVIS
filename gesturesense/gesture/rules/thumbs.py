"""Thumb-orientation gestures: Thumbs Up and Thumbs Down."""

from __future__ import annotations

import numpy as np

from gesturesense.gesture.base import GestureRule, register_gesture
from gesturesense.gesture.features import HandFeatures
from gesturesense.gesture.rules import scoring as sc


class _ThumbOrientationRule(GestureRule):
    """Shared logic: fist with an extended thumb pointing along an axis."""

    axis: np.ndarray = sc.UP

    def score(self, features: HandFeatures) -> float:
        return self.all_of(
            sc.extended(features, "thumb"),
            sc.curled(features, "index"),
            sc.curled(features, "middle"),
            sc.curled(features, "ring"),
            sc.curled(features, "pinky"),
            sc.direction_score(features.direction("thumb"), self.axis),
        )


@register_gesture
class ThumbsUp(_ThumbOrientationRule):
    """Fist with the thumb pointing up."""

    name = "Thumbs Up"
    axis = sc.UP


@register_gesture
class ThumbsDown(_ThumbOrientationRule):
    """Fist with the thumb pointing down."""

    name = "Thumbs Down"
    axis = sc.DOWN
