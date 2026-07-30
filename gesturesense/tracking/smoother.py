"""Temporal landmark smoothing.

MediaPipe landmarks jitter by a couple of pixels even for a perfectly still
hand, which makes overlays shimmer. A light exponential moving average per
hand removes the shimmer while keeping latency well under a frame at the
default responsiveness.
"""

from __future__ import annotations

import numpy as np


class LandmarkSmoother:
    """Per-hand EMA smoothing keyed by handedness label.

    ``alpha`` is responsiveness: ``1.0`` passes landmarks through untouched,
    smaller values smooth harder. State resets automatically when a hand
    disappears (via :meth:`mark_all_stale` / :meth:`prune`) or when it jumps
    far enough that it is clearly a re-detection rather than motion.
    """

    #: Jump threshold (fraction of image diagonal in normalised coords) above
    #: which smoothing restarts rather than dragging the skeleton across the
    #: frame.
    _TELEPORT_THRESHOLD: float = 0.25

    def __init__(self, alpha: float = 0.55):
        if not 0.0 < alpha <= 1.0:
            raise ValueError("alpha must be in (0, 1]")
        self._alpha = alpha
        self._state: dict[str, np.ndarray] = {}
        self._seen: set[str] = set()

    def smooth(self, handedness: str, landmarks: np.ndarray) -> np.ndarray:
        """Return smoothed landmarks for the hand labelled ``handedness``."""
        self._seen.add(handedness)
        if self._alpha >= 1.0:
            return landmarks

        landmarks = np.asarray(landmarks, dtype=np.float64)
        previous = self._state.get(handedness)
        if previous is None or self._teleported(previous, landmarks):
            self._state[handedness] = landmarks.copy()
            return landmarks

        smoothed = previous + self._alpha * (landmarks - previous)
        self._state[handedness] = smoothed
        return smoothed

    def _teleported(self, previous: np.ndarray, current: np.ndarray) -> bool:
        wrist_jump = float(np.linalg.norm(current[0, :2] - previous[0, :2]))
        return wrist_jump > self._TELEPORT_THRESHOLD

    # ------------------------------------------------------------------
    # Lifecycle: call once per frame to drop hands that vanished.
    # ------------------------------------------------------------------
    def mark_all_stale(self) -> None:
        """Begin a frame: forget which hands have been seen."""
        self._seen.clear()

    def prune(self) -> list[str]:
        """End a frame: drop state for unseen hands; returns their labels."""
        lost = [label for label in self._state if label not in self._seen]
        for label in lost:
            del self._state[label]
        return lost
