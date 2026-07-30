"""Shared soft-scoring helpers used by the concrete gesture rules."""

from __future__ import annotations

import numpy as np

from gesturesense.gesture.features import HandFeatures
from gesturesense.utils import geometry as geo

# Screen-space axes (image coordinates: y grows downwards).
UP = np.array([0.0, -1.0, 0.0])
DOWN = np.array([0.0, 1.0, 0.0])
LEFT = np.array([-1.0, 0.0, 0.0])
RIGHT = np.array([1.0, 0.0, 0.0])


def direction_score(direction: np.ndarray, target: np.ndarray) -> float:
    """Score how well ``direction`` aligns with the ``target`` axis.

    Full credit above ~30° cone half-angle mismatch, fading to zero as the
    vectors approach 60° apart.
    """
    cosine = float(np.dot(geo.normalize(direction), geo.normalize(target)))
    return geo.smoothstep(cosine, 0.50, 0.87)


def extended(features: HandFeatures, finger: str) -> float:
    """Soft requirement that ``finger`` is extended."""
    return geo.smoothstep(features.extension(finger), 0.35, 0.75)


def curled(features: HandFeatures, finger: str) -> float:
    """Soft requirement that ``finger`` is curled."""
    return geo.smoothstep(features.curl(finger), 0.35, 0.75)


def not_extended(features: HandFeatures, finger: str) -> float:
    """Soft requirement that ``finger`` is anything but fully extended."""
    return geo.inverse_smoothstep(features.extension(finger), 0.45, 0.80)
