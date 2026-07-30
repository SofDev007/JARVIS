"""Synthetic hand-landmark fixtures for gesture tests.

Builds parametric 21-point hands in normalised image coordinates
(``x`` right, ``y`` down) so the full gesture pipeline can be tested
without a camera or MediaPipe. Fingers are generated as joint chains:
an *extended* finger is a straight chain, a *curled* finger folds each
successive segment by a fixed angle, and the thumb is placed either far
from the index MCP (extended) or tucked right next to it (curled) —
matching how :mod:`gesturesense.gesture.finger_state` measures thumbs.
"""

from __future__ import annotations

import numpy as np

from gesturesense.gesture import topology as topo
from gesturesense.utils import geometry as geo

# Screen-space axes (y grows downwards).
UP = np.array([0.0, -1.0, 0.0])
DOWN = np.array([0.0, 1.0, 0.0])
LEFT = np.array([-1.0, 0.0, 0.0])
RIGHT = np.array([1.0, 0.0, 0.0])

HAND_SIZE = 0.25
"""Wrist → middle-MCP distance used by every fixture."""

_SEGMENTS: dict[str, tuple[float, float, float]] = {
    "index": (0.075, 0.055, 0.040),
    "middle": (0.080, 0.060, 0.045),
    "ring": (0.075, 0.055, 0.040),
    "pinky": (0.060, 0.045, 0.035),
}
_THUMB_SEGMENTS: tuple[float, float, float] = (0.090, 0.080, 0.070)

# Lateral MCP offsets along the "across the palm" axis (index → pinky).
_MCP_OFFSETS: dict[str, float] = {
    "index": -0.05,
    "middle": 0.0,
    "ring": 0.05,
    "pinky": 0.10,
}

# Fold per joint (degrees). Extended = straight, curled folds well past the
# 120° extension threshold, half sits inside the ambiguous band.
FOLD_EXTENDED = 0.0
FOLD_HALF = 40.0
FOLD_CURLED = 100.0


def _chain(
    start: np.ndarray,
    direction: np.ndarray,
    segments: tuple[float, float, float],
    fold_degrees: float,
    fold_sign: float = 1.0,
) -> list[np.ndarray]:
    """Generate three chained joints from ``start`` along ``direction``.

    Each successive segment rotates by ``fold_degrees`` so the interior
    joint angles come out as ``180 - fold_degrees``.
    """
    points: list[np.ndarray] = []
    current = np.asarray(start, dtype=np.float64)
    heading = geo.normalize(np.asarray(direction, dtype=np.float64))
    for i, length in enumerate(segments):
        if i > 0:
            heading = geo.rotate_2d(heading, fold_sign * fold_degrees)
        current = current + heading * length
        points.append(current.copy())
    return points


def build_hand(
    palm_axis: np.ndarray = UP,
    finger_folds: dict[str, float] | None = None,
    finger_directions: dict[str, np.ndarray] | None = None,
    thumb: str = "tucked",
    thumb_direction: np.ndarray | None = None,
    thumb_tip_at: np.ndarray | None = None,
    wrist: np.ndarray | None = None,
) -> np.ndarray:
    """Build a synthetic ``(21, 3)`` landmark array.

    :param palm_axis: unit direction wrist → knuckles (screen coords).
    :param finger_folds: per-finger fold angle in degrees
        (defaults to :data:`FOLD_EXTENDED` for all four fingers).
    :param finger_directions: optional per-finger initial direction
        override (defaults to ``palm_axis``).
    :param thumb: ``"extended"`` (straight, far from the index MCP) or
        ``"tucked"`` (tip parked next to the index MCP).
    :param thumb_direction: direction of an extended thumb
        (defaults to pointing away from the palm laterally).
    :param thumb_tip_at: optional explicit thumb-tip position (used to
        pinch against another landmark, e.g. for the OK sign).
    :param wrist: wrist position (defaults to a sensible frame centre).
    """
    palm_axis = geo.normalize(np.asarray(palm_axis, dtype=np.float64))
    # Lateral "across the knuckles" axis: palm axis rotated +90°.
    lateral = geo.rotate_2d(palm_axis, 90.0)

    w = np.array([0.5, 0.75, 0.0]) if wrist is None else np.asarray(wrist, float)
    folds = finger_folds or {}
    directions = finger_directions or {}

    landmarks = np.zeros((topo.NUM_LANDMARKS, 3), dtype=np.float64)
    landmarks[topo.WRIST] = w

    # Four fingers -----------------------------------------------------
    for finger in ("index", "middle", "ring", "pinky"):
        mcp = w + HAND_SIZE * palm_axis + _MCP_OFFSETS[finger] * lateral
        fold = folds.get(finger, FOLD_EXTENDED)
        direction = directions.get(finger, palm_axis)
        joints = _chain(mcp, direction, _SEGMENTS[finger], fold)
        indices = topo.FINGER_JOINTS[finger]
        landmarks[indices[0]] = mcp
        for idx, point in zip(indices[1:], joints):
            landmarks[idx] = point

    index_mcp = landmarks[topo.INDEX_MCP]

    # Thumb --------------------------------------------------------------
    cmc = w + 0.10 * palm_axis - 0.09 * lateral
    if thumb_tip_at is not None:
        tip = np.asarray(thumb_tip_at, dtype=np.float64)
        mcp = cmc + 0.4 * (tip - cmc) + 0.02 * palm_axis
        ip = cmc + 0.75 * (tip - cmc) + 0.01 * palm_axis
        chain = [mcp, ip, tip]
    elif thumb == "extended":
        direction = thumb_direction if thumb_direction is not None else -lateral
        chain = _chain(cmc, direction, _THUMB_SEGMENTS, FOLD_EXTENDED)
    else:  # tucked: tip parked right beside the index MCP.
        tip = index_mcp - 0.03 * palm_axis + 0.02 * lateral
        mcp = cmc + 0.45 * (tip - cmc) - 0.03 * lateral
        ip = cmc + 0.80 * (tip - cmc) - 0.015 * lateral
        chain = [mcp, ip, tip]

    landmarks[topo.THUMB_CMC] = cmc
    for idx, point in zip(
        (topo.THUMB_MCP, topo.THUMB_IP, topo.THUMB_TIP), chain
    ):
        landmarks[idx] = point

    return landmarks


# ---------------------------------------------------------------------------
# Named poses — one per supported gesture.
# ---------------------------------------------------------------------------
def thumbs_up() -> np.ndarray:
    """Fist held sideways, thumb straight up."""
    return build_hand(
        palm_axis=RIGHT,
        finger_folds={f: FOLD_CURLED for f in ("index", "middle", "ring", "pinky")},
        thumb="extended",
        thumb_direction=UP,
    )


def thumbs_down() -> np.ndarray:
    """Fist held sideways, thumb straight down."""
    return build_hand(
        palm_axis=RIGHT,
        finger_folds={f: FOLD_CURLED for f in ("index", "middle", "ring", "pinky")},
        thumb="extended",
        thumb_direction=DOWN,
    )


def open_palm() -> np.ndarray:
    """All five fingers extended upwards, thumb fanned out."""
    return build_hand(
        palm_axis=UP,
        thumb="extended",
        thumb_direction=geo.normalize(np.array([-0.85, -0.55, 0.0])),
    )


def closed_fist() -> np.ndarray:
    """All fingers curled, thumb tucked across the palm."""
    return build_hand(
        palm_axis=UP,
        finger_folds={f: FOLD_CURLED for f in ("index", "middle", "ring", "pinky")},
        thumb="tucked",
    )


def peace_sign() -> np.ndarray:
    """Index + middle extended in a spread V, others curled."""
    return build_hand(
        palm_axis=UP,
        finger_folds={"ring": FOLD_CURLED, "pinky": FOLD_CURLED},
        finger_directions={
            "index": geo.rotate_2d(UP, -12.0),
            "middle": geo.rotate_2d(UP, 12.0),
        },
        thumb="tucked",
    )


def ok_sign() -> np.ndarray:
    """Thumb and index tips pinched, remaining fingers extended."""
    # Build once to find where the curled index tip lands, then pin the
    # thumb tip exactly onto it (pinch distance ≈ 0).
    prototype = build_hand(
        palm_axis=UP, finger_folds={"index": FOLD_CURLED}, thumb="tucked"
    )
    index_tip = prototype[topo.INDEX_TIP]
    return build_hand(
        palm_axis=UP,
        finger_folds={"index": FOLD_CURLED},
        thumb_tip_at=index_tip,
    )


def _pointing(axis: np.ndarray) -> np.ndarray:
    return build_hand(
        palm_axis=axis,
        finger_folds={f: FOLD_CURLED for f in ("middle", "ring", "pinky")},
        thumb="tucked",
    )


def pointing_up() -> np.ndarray:
    return _pointing(UP)


def pointing_down() -> np.ndarray:
    return _pointing(DOWN)


def pointing_left() -> np.ndarray:
    return _pointing(LEFT)


def pointing_right() -> np.ndarray:
    return _pointing(RIGHT)


def rock_sign() -> np.ndarray:
    """Index + pinky extended, middle + ring curled, thumb tucked."""
    return build_hand(
        palm_axis=UP,
        finger_folds={"middle": FOLD_CURLED, "ring": FOLD_CURLED},
        finger_directions={
            "index": geo.rotate_2d(UP, -8.0),
            "pinky": geo.rotate_2d(UP, 8.0),
        },
        thumb="tucked",
    )


def love_sign() -> np.ndarray:
    """ILY: thumb, index and pinky extended, middle + ring curled."""
    return build_hand(
        palm_axis=UP,
        finger_folds={"middle": FOLD_CURLED, "ring": FOLD_CURLED},
        thumb="extended",
        thumb_direction=geo.normalize(np.array([-0.85, -0.55, 0.0])),
    )


def call_me_sign() -> np.ndarray:
    """Shaka: thumb + pinky extended, other fingers curled."""
    return build_hand(
        palm_axis=UP,
        finger_folds={f: FOLD_CURLED for f in ("index", "middle", "ring")},
        thumb="extended",
        thumb_direction=geo.normalize(np.array([-0.85, -0.55, 0.0])),
    )


def finger_gun() -> np.ndarray:
    """Index pointing left, thumb up (≈90° apart), others curled."""
    return build_hand(
        palm_axis=LEFT,
        finger_folds={f: FOLD_CURLED for f in ("middle", "ring", "pinky")},
        thumb="extended",
        thumb_direction=UP,
    )


def relaxed_hand() -> np.ndarray:
    """Slightly bent open fingers with tucked thumb — matches no gesture."""
    return build_hand(
        palm_axis=UP,
        finger_folds={f: 20.0 for f in ("index", "middle", "ring", "pinky")},
        thumb="tucked",
    )


#: Mapping of expected gesture name → fixture builder, used to parametrise
#: the rule tests and to assert each rule wins on its own pose.
GESTURE_POSES: dict[str, callable] = {
    "Thumbs Up": thumbs_up,
    "Thumbs Down": thumbs_down,
    "Open Palm": open_palm,
    "Closed Fist": closed_fist,
    "Peace / Victory": peace_sign,
    "OK Sign": ok_sign,
    "Pointing Up": pointing_up,
    "Pointing Down": pointing_down,
    "Pointing Left": pointing_left,
    "Pointing Right": pointing_right,
    "Rock Sign": rock_sign,
    "Love Sign (ILY)": love_sign,
    "Call Me": call_me_sign,
    "Finger Gun": finger_gun,
}
