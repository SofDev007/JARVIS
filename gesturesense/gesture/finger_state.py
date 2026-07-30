"""Per-finger state detection from raw hand landmarks.

Turns the 21-point landmark array into soft *extension scores* and discrete
:class:`FingerState` values per finger. Soft scores (``0.0`` = fully curled,
``1.0`` = fully extended) feed the gesture rule engine so that borderline
poses degrade confidence gracefully instead of flipping detections.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np

from gesturesense.gesture import topology as topo
from gesturesense.utils import geometry as geo


class FingerState(Enum):
    """Discrete extension state of a single finger."""

    EXTENDED = "extended"
    HALF = "half"
    CURLED = "curled"


# Score thresholds for discretising the soft extension score.
_EXTENDED_MIN_SCORE: float = 0.70
_CURLED_MAX_SCORE: float = 0.35

# Joint-angle ramp (degrees) for non-thumb fingers: fully curled fingers sit
# well below 120°, straight fingers approach 180°.
_CURL_ANGLE_LOW: float = 120.0
_CURL_ANGLE_HIGH: float = 165.0

# Thumb straightness ramp — the thumb never reaches the same straightness as
# the other fingers, so the ramp is more forgiving.
_THUMB_ANGLE_LOW: float = 120.0
_THUMB_ANGLE_HIGH: float = 155.0

# Thumb separation ramp: distance from thumb tip to the index MCP relative to
# hand size. A thumb tucked across the palm sits near ~0.3–0.5; an extended
# thumb clears ~0.8.
_THUMB_SEP_LOW: float = 0.45
_THUMB_SEP_HIGH: float = 0.80


@dataclass(frozen=True)
class FingerReading:
    """Measurements for a single finger."""

    name: str
    state: FingerState
    extension: float
    """Soft extension score in ``[0, 1]``."""
    avg_joint_angle: float
    """Mean interior joint angle in degrees (diagnostic / debug HUD)."""


def _state_from_score(score: float) -> FingerState:
    if score >= _EXTENDED_MIN_SCORE:
        return FingerState.EXTENDED
    if score <= _CURLED_MAX_SCORE:
        return FingerState.CURLED
    return FingerState.HALF


def _finger_extension(landmarks: np.ndarray, finger: str) -> tuple[float, float]:
    """Extension score and mean joint angle for a non-thumb finger."""
    mcp, pip, dip, tip = (landmarks[i] for i in topo.FINGER_JOINTS[finger])
    pip_angle = geo.joint_angle(mcp, pip, dip)
    dip_angle = geo.joint_angle(pip, dip, tip)
    avg_angle = (pip_angle + dip_angle) / 2.0
    return geo.smoothstep(avg_angle, _CURL_ANGLE_LOW, _CURL_ANGLE_HIGH), avg_angle


def _thumb_extension(landmarks: np.ndarray, hand_size: float) -> tuple[float, float]:
    """Extension score and mean joint angle for the thumb.

    Straightness alone is unreliable for thumbs (a tucked thumb can still be
    fairly straight), so it is combined with how far the thumb tip travels
    away from the index MCP relative to hand size.
    """
    cmc, mcp, ip, tip = (landmarks[i] for i in topo.FINGER_JOINTS["thumb"])
    mcp_angle = geo.joint_angle(cmc, mcp, ip)
    ip_angle = geo.joint_angle(mcp, ip, tip)
    avg_angle = (mcp_angle + ip_angle) / 2.0

    straightness = geo.smoothstep(avg_angle, _THUMB_ANGLE_LOW, _THUMB_ANGLE_HIGH)
    separation = geo.distance(tip, landmarks[topo.INDEX_MCP]) / max(hand_size, 1e-6)
    separation_score = geo.smoothstep(separation, _THUMB_SEP_LOW, _THUMB_SEP_HIGH)

    # Separation is the primary signal; straightness refines it.
    score = separation_score * (0.5 + 0.5 * straightness)
    return score, avg_angle


def read_fingers(landmarks: np.ndarray, hand_size: float) -> dict[str, FingerReading]:
    """Compute :class:`FingerReading` for all five fingers.

    :param landmarks: ``(21, 3)`` array of hand landmarks.
    :param hand_size: normalisation reference (wrist → middle MCP distance).
    """
    readings: dict[str, FingerReading] = {}
    for finger in topo.FINGER_NAMES:
        if finger == "thumb":
            score, avg_angle = _thumb_extension(landmarks, hand_size)
        else:
            score, avg_angle = _finger_extension(landmarks, finger)
        readings[finger] = FingerReading(
            name=finger,
            state=_state_from_score(score),
            extension=score,
            avg_joint_angle=avg_angle,
        )
    return readings
