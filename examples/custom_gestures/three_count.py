"""Example custom gesture: "Three Count" (index + middle + ring extended).

Enable it without touching any core code — add to your config:

    gesture:
      custom_gesture_modules:
        - examples/custom_gestures/three_count.py

The gesture then flows through the whole platform automatically: it is
published as ``perception.gesture`` events with the auto-slugified semantic
id ``three_count``, can be tuned per user (``gesture_thresholds:
{three_count: 0.7}``), disabled (``disabled_gestures: [three_count]``), and
mapped to intents per context::

    intent:
      mappings:
        media:
          three_count: volume_up

Use this file as the template for your own gestures: subclass
``GestureRule``, give it a unique ``name``, implement ``score`` returning a
confidence in ``[0, 1]`` from the provided hand features, and decorate with
``@register_gesture``.
"""

from __future__ import annotations

from gesturesense.gesture.base import GestureRule, register_gesture
from gesturesense.gesture.features import HandFeatures
from gesturesense.gesture.rules import scoring as sc


@register_gesture
class ThreeCount(GestureRule):
    """Index, middle and ring extended; thumb and pinky folded."""

    name = "Three Count"

    def score(self, features: HandFeatures) -> float:
        return self.all_of(
            sc.extended(features, "index"),
            sc.extended(features, "middle"),
            sc.extended(features, "ring"),
            sc.not_extended(features, "pinky"),
            sc.not_extended(features, "thumb"),
        )
