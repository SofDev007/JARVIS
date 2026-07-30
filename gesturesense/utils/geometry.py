"""Vector geometry helpers shared across the vision and gesture pipeline.

All functions operate on plain ``numpy`` arrays so that the gesture engine
stays fully decoupled from MediaPipe and can be unit-tested in isolation.
Coordinates follow image conventions: ``x`` grows rightwards, ``y`` grows
*downwards* (so "up" on screen is the negative-y direction).
"""

from __future__ import annotations

from enum import Enum

import numpy as np

_EPS: float = 1e-9


class Direction(Enum):
    """Dominant screen-space direction of a vector."""

    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"
    NONE = "none"


def norm(vector: np.ndarray) -> float:
    """Return the Euclidean length of ``vector``."""
    return float(np.linalg.norm(vector))


def normalize(vector: np.ndarray) -> np.ndarray:
    """Return ``vector`` scaled to unit length (zero vector stays zero)."""
    length = norm(vector)
    if length < _EPS:
        return np.zeros_like(vector)
    return vector / length


def distance(a: np.ndarray, b: np.ndarray) -> float:
    """Euclidean distance between points ``a`` and ``b``."""
    return norm(np.asarray(b, dtype=np.float64) - np.asarray(a, dtype=np.float64))


def angle_between(v1: np.ndarray, v2: np.ndarray) -> float:
    """Angle between two vectors in degrees, in ``[0, 180]``."""
    n1, n2 = norm(v1), norm(v2)
    if n1 < _EPS or n2 < _EPS:
        return 0.0
    cosine = float(np.dot(v1, v2) / (n1 * n2))
    cosine = max(-1.0, min(1.0, cosine))
    return float(np.degrees(np.arccos(cosine)))


def joint_angle(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> float:
    """Interior angle at joint ``b`` formed by segments ``b→a`` and ``b→c``.

    A perfectly straight chain (``a-b-c`` collinear) yields ``180``; a fully
    folded joint approaches ``0``.
    """
    return angle_between(np.asarray(a) - np.asarray(b), np.asarray(c) - np.asarray(b))


def smoothstep(value: float, low: float, high: float) -> float:
    """Map ``value`` to ``[0, 1]`` with a smooth Hermite ramp between bounds.

    Returns ``0`` for ``value <= low`` and ``1`` for ``value >= high``.
    Used to turn raw geometric measurements into soft confidence scores
    instead of brittle boolean thresholds.
    """
    if high <= low:
        return 1.0 if value >= high else 0.0
    t = (value - low) / (high - low)
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def inverse_smoothstep(value: float, low: float, high: float) -> float:
    """Complement of :func:`smoothstep`: ``1`` below ``low``, ``0`` above ``high``."""
    return 1.0 - smoothstep(value, low, high)


def principal_direction(vector: np.ndarray, dominance: float = 0.75) -> Direction:
    """Classify a 2D/3D vector into a dominant screen direction.

    The dominant axis component must account for at least ``dominance`` of
    the planar magnitude; otherwise :attr:`Direction.NONE` is returned so
    diagonal, ambiguous poses are not force-classified. The default sits
    above ``1/√2 ≈ 0.707`` so a perfect 45° diagonal maps to ``NONE``.
    """
    v = np.asarray(vector, dtype=np.float64)
    x, y = float(v[0]), float(v[1])
    planar = float(np.hypot(x, y))
    if planar < _EPS:
        return Direction.NONE
    if abs(x) >= abs(y):
        if abs(x) / planar < dominance:
            return Direction.NONE
        return Direction.RIGHT if x > 0 else Direction.LEFT
    if abs(y) / planar < dominance:
        return Direction.NONE
    return Direction.DOWN if y > 0 else Direction.UP


def rotate_2d(vector: np.ndarray, degrees: float) -> np.ndarray:
    """Rotate the ``xy`` components of a vector by ``degrees`` (z untouched)."""
    v = np.asarray(vector, dtype=np.float64).copy()
    rad = np.radians(degrees)
    cos_a, sin_a = np.cos(rad), np.sin(rad)
    x, y = v[0], v[1]
    v[0] = cos_a * x - sin_a * y
    v[1] = sin_a * x + cos_a * y
    return v
