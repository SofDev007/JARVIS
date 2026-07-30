"""Hand landmark topology (MediaPipe 21-point model).

Defined locally so that the gesture engine and renderer never import
MediaPipe directly — the tracking backend is swappable as long as it emits
landmarks in this layout.
"""

from __future__ import annotations

from typing import Final

# Landmark indices ---------------------------------------------------------
WRIST: Final[int] = 0

THUMB_CMC: Final[int] = 1
THUMB_MCP: Final[int] = 2
THUMB_IP: Final[int] = 3
THUMB_TIP: Final[int] = 4

INDEX_MCP: Final[int] = 5
INDEX_PIP: Final[int] = 6
INDEX_DIP: Final[int] = 7
INDEX_TIP: Final[int] = 8

MIDDLE_MCP: Final[int] = 9
MIDDLE_PIP: Final[int] = 10
MIDDLE_DIP: Final[int] = 11
MIDDLE_TIP: Final[int] = 12

RING_MCP: Final[int] = 13
RING_PIP: Final[int] = 14
RING_DIP: Final[int] = 15
RING_TIP: Final[int] = 16

PINKY_MCP: Final[int] = 17
PINKY_PIP: Final[int] = 18
PINKY_DIP: Final[int] = 19
PINKY_TIP: Final[int] = 20

NUM_LANDMARKS: Final[int] = 21

# Finger names in canonical order ------------------------------------------
FINGER_NAMES: Final[tuple[str, ...]] = ("thumb", "index", "middle", "ring", "pinky")

# Joint chains per finger (proximal → distal) -------------------------------
FINGER_JOINTS: Final[dict[str, tuple[int, int, int, int]]] = {
    "thumb": (THUMB_CMC, THUMB_MCP, THUMB_IP, THUMB_TIP),
    "index": (INDEX_MCP, INDEX_PIP, INDEX_DIP, INDEX_TIP),
    "middle": (MIDDLE_MCP, MIDDLE_PIP, MIDDLE_DIP, MIDDLE_TIP),
    "ring": (RING_MCP, RING_PIP, RING_DIP, RING_TIP),
    "pinky": (PINKY_MCP, PINKY_PIP, PINKY_DIP, PINKY_TIP),
}

FINGER_TIPS: Final[dict[str, int]] = {
    "thumb": THUMB_TIP,
    "index": INDEX_TIP,
    "middle": MIDDLE_TIP,
    "ring": RING_TIP,
    "pinky": PINKY_TIP,
}

# Skeleton connections used by the renderer ---------------------------------
HAND_CONNECTIONS: Final[tuple[tuple[int, int], ...]] = (
    # Palm
    (WRIST, THUMB_CMC),
    (WRIST, INDEX_MCP),
    (WRIST, PINKY_MCP),
    (INDEX_MCP, MIDDLE_MCP),
    (MIDDLE_MCP, RING_MCP),
    (RING_MCP, PINKY_MCP),
    # Thumb
    (THUMB_CMC, THUMB_MCP),
    (THUMB_MCP, THUMB_IP),
    (THUMB_IP, THUMB_TIP),
    # Index
    (INDEX_MCP, INDEX_PIP),
    (INDEX_PIP, INDEX_DIP),
    (INDEX_DIP, INDEX_TIP),
    # Middle
    (MIDDLE_MCP, MIDDLE_PIP),
    (MIDDLE_PIP, MIDDLE_DIP),
    (MIDDLE_DIP, MIDDLE_TIP),
    # Ring
    (RING_MCP, RING_PIP),
    (RING_PIP, RING_DIP),
    (RING_DIP, RING_TIP),
    # Pinky
    (PINKY_MCP, PINKY_PIP),
    (PINKY_PIP, PINKY_DIP),
    (PINKY_DIP, PINKY_TIP),
)

# Which finger a landmark index belongs to (for per-finger colouring) --------
LANDMARK_FINGER: Final[dict[int, str]] = {
    idx: finger
    for finger, joints in FINGER_JOINTS.items()
    for idx in joints
}
LANDMARK_FINGER[WRIST] = "palm"
