"""Symbolic gestures: Peace/Victory, OK, Rock, Call Me, Love (ILY), Finger Gun."""

from __future__ import annotations

from gesturesense.gesture.base import GestureRule, register_gesture
from gesturesense.gesture.features import HandFeatures
from gesturesense.gesture.rules import scoring as sc
from gesturesense.utils import geometry as geo


@register_gesture
class PeaceSign(GestureRule):
    """Index and middle fingers extended in a spread V.

    The victory sign is the same hand shape, so both labels map here —
    duplicating the rule would only create a coin-flip between two
    identical detections.
    """

    name = "Peace / Victory"

    def score(self, features: HandFeatures) -> float:
        spread = geo.smoothstep(features.index_middle_spread, 6.0, 16.0)
        return self.all_of(
            sc.extended(features, "index"),
            sc.extended(features, "middle"),
            sc.curled(features, "ring"),
            sc.curled(features, "pinky"),
            sc.not_extended(features, "thumb"),
            spread,
        )


@register_gesture
class OkSign(GestureRule):
    """Thumb and index tips pinched into a ring, other fingers extended."""

    name = "OK Sign"

    def score(self, features: HandFeatures) -> float:
        pinch = geo.inverse_smoothstep(features.pinch_distance, 0.25, 0.50)
        return self.all_of(
            pinch,
            sc.extended(features, "middle"),
            sc.extended(features, "ring"),
            sc.extended(features, "pinky"),
            sc.not_extended(features, "index"),
        )


@register_gesture
class RockSign(GestureRule):
    """Index and pinky extended, middle and ring curled, thumb tucked."""

    name = "Rock Sign"

    def score(self, features: HandFeatures) -> float:
        return self.all_of(
            sc.extended(features, "index"),
            sc.extended(features, "pinky"),
            sc.curled(features, "middle"),
            sc.curled(features, "ring"),
            sc.not_extended(features, "thumb"),
        )


@register_gesture
class LoveSign(GestureRule):
    """ILY: thumb, index and pinky extended, middle and ring curled."""

    name = "Love Sign (ILY)"

    def score(self, features: HandFeatures) -> float:
        return self.all_of(
            sc.extended(features, "thumb"),
            sc.extended(features, "index"),
            sc.extended(features, "pinky"),
            sc.curled(features, "middle"),
            sc.curled(features, "ring"),
        )


@register_gesture
class CallMeSign(GestureRule):
    """Shaka: thumb and pinky extended, remaining fingers curled."""

    name = "Call Me"

    def score(self, features: HandFeatures) -> float:
        return self.all_of(
            sc.extended(features, "thumb"),
            sc.extended(features, "pinky"),
            sc.curled(features, "index"),
            sc.curled(features, "middle"),
            sc.curled(features, "ring"),
        )


@register_gesture
class FingerGun(GestureRule):
    """Thumb and index extended, roughly perpendicular; others curled."""

    name = "Finger Gun"

    def score(self, features: HandFeatures) -> float:
        angle = geo.angle_between(
            features.direction("thumb"), features.direction("index")
        )
        perpendicular = self.all_of(
            geo.smoothstep(angle, 30.0, 55.0),
            geo.inverse_smoothstep(angle, 105.0, 135.0),
        )
        return self.all_of(
            sc.extended(features, "thumb"),
            sc.extended(features, "index"),
            sc.curled(features, "middle"),
            sc.curled(features, "ring"),
            sc.curled(features, "pinky"),
            perpendicular,
        )
