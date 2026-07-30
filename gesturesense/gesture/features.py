"""Feature extraction: raw landmarks → orientation-aware hand features.

This is the single place where geometry is derived from landmarks. Gesture
rules consume :class:`HandFeatures` only, never raw landmarks, which keeps
every rule short, testable and independent of the tracking backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from gesturesense.gesture import topology as topo
from gesturesense.gesture.finger_state import FingerReading, FingerState, read_fingers
from gesturesense.utils import geometry as geo


@dataclass(frozen=True)
class HandFeatures:
    """Derived, scale-normalised features for a single detected hand."""

    landmarks: np.ndarray
    """Raw ``(21, 3)`` landmark array (image-normalised coordinates)."""

    handedness: str
    """``"Left"`` or ``"Right"`` as seen by the user."""

    hand_size: float
    """Wrist → middle-MCP distance; the scale reference for all ratios."""

    fingers: dict[str, FingerReading]
    """Per-finger extension readings keyed by finger name."""

    palm_direction: np.ndarray
    """Unit vector wrist → middle MCP (the 'hand is pointing' axis)."""

    finger_directions: dict[str, np.ndarray] = field(default_factory=dict)
    """Unit direction of each finger's distal segment (PIP/IP → TIP)."""

    pinch_distance: float = 1.0
    """Thumb-tip ↔ index-tip distance divided by hand size."""

    index_middle_spread: float = 0.0
    """Angle in degrees between index and middle finger directions."""

    # ------------------------------------------------------------------
    # Convenience accessors used heavily by rules
    # ------------------------------------------------------------------
    def extension(self, finger: str) -> float:
        """Soft extension score of ``finger`` in ``[0, 1]``."""
        return self.fingers[finger].extension

    def curl(self, finger: str) -> float:
        """Soft curl score of ``finger`` (``1 - extension``)."""
        return 1.0 - self.fingers[finger].extension

    def state(self, finger: str) -> FingerState:
        """Discrete :class:`FingerState` of ``finger``."""
        return self.fingers[finger].state

    def direction(self, finger: str) -> np.ndarray:
        """Unit direction of the finger's distal segment."""
        return self.finger_directions[finger]

    def screen_direction(self, finger: str) -> geo.Direction:
        """Dominant screen direction the finger is pointing towards."""
        return geo.principal_direction(self.finger_directions[finger])


def extract_features(landmarks: np.ndarray, handedness: str) -> HandFeatures:
    """Build :class:`HandFeatures` from a ``(21, 3)`` landmark array."""
    landmarks = np.asarray(landmarks, dtype=np.float64)
    wrist = landmarks[topo.WRIST]
    middle_mcp = landmarks[topo.MIDDLE_MCP]

    hand_size = geo.distance(wrist, middle_mcp)
    hand_size = max(hand_size, 1e-6)

    fingers = read_fingers(landmarks, hand_size)

    directions: dict[str, np.ndarray] = {}
    for finger, joints in topo.FINGER_JOINTS.items():
        # Distal segment (second-to-last joint → tip) tracks where the finger
        # actually points even when the base of the finger is bent.
        directions[finger] = geo.normalize(landmarks[joints[3]] - landmarks[joints[1]])

    pinch = geo.distance(landmarks[topo.THUMB_TIP], landmarks[topo.INDEX_TIP]) / hand_size
    spread = geo.angle_between(directions["index"], directions["middle"])

    return HandFeatures(
        landmarks=landmarks,
        handedness=handedness,
        hand_size=hand_size,
        fingers=fingers,
        palm_direction=geo.normalize(middle_mcp - wrist),
        finger_directions=directions,
        pinch_distance=pinch,
        index_middle_spread=spread,
    )
