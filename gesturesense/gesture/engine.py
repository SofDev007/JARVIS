"""Gesture engine: features → rule scores → stabilised final gesture.

Pipeline per hand and per frame::

    landmarks → extract_features → [rule.score() ...] → best match
             → GestureStabilizer (temporal majority + confidence EMA)
             → RecognizedGesture | None
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass

import numpy as np

import gesturesense.gesture.rules  # noqa: F401  (loads and registers all rules)
from gesturesense.gesture.base import GestureMatch, registered_rules
from gesturesense.gesture.features import HandFeatures, extract_features


@dataclass(frozen=True)
class RecognizedGesture:
    """Stable gesture decision for one hand."""

    name: str
    confidence: float
    handedness: str


class GestureStabilizer:
    """Temporal majority vote with confidence smoothing for one hand.

    Raw per-frame winners flicker during transitions; requiring a majority
    over a short history removes flicker while keeping latency at a few
    frames (~100 ms at 30 FPS with default settings).
    """

    def __init__(self, history: int = 7, min_votes: int = 4, ema_alpha: float = 0.35):
        self._history: deque[str | None] = deque(maxlen=max(2, history))
        self._min_votes = max(1, min_votes)
        self._alpha = ema_alpha
        self._confidence: float = 0.0
        self._current: str | None = None

    def update(self, match: GestureMatch | None) -> tuple[str | None, float]:
        """Feed this frame's best raw match; get the stabilised decision."""
        self._history.append(match.name if match else None)

        counts = Counter(self._history)
        winner, votes = counts.most_common(1)[0]
        if votes >= self._min_votes:
            if winner != self._current:
                self._current = winner
                self._confidence = match.confidence if match and match.name == winner else 0.0

        if self._current is None:
            return None, 0.0

        target = match.confidence if match and match.name == self._current else 0.0
        self._confidence += self._alpha * (target - self._confidence)
        return self._current, self._confidence

    def reset(self) -> None:
        """Forget history (call when the hand is lost)."""
        self._history.clear()
        self._current = None
        self._confidence = 0.0


class GestureEngine:
    """Runs all registered rules and stabilises results per hand."""

    def __init__(
        self,
        min_confidence: float = 0.55,
        history: int = 7,
        min_votes: int = 4,
    ):
        self._rules = registered_rules()
        self._min_confidence = min_confidence
        self._history = history
        self._min_votes = min_votes
        self._stabilizers: dict[str, GestureStabilizer] = {}

    # ------------------------------------------------------------------
    def classify(self, features: HandFeatures) -> GestureMatch | None:
        """Best raw (unstabilised) gesture for the given features."""
        best: GestureMatch | None = None
        for rule in self._rules:
            confidence = rule.score(features)
            if confidence >= self._min_confidence and (
                best is None or confidence > best.confidence
            ):
                best = GestureMatch(name=rule.name, confidence=confidence)
        return best

    def process_hand(
        self, landmarks: np.ndarray, handedness: str
    ) -> tuple[HandFeatures, RecognizedGesture | None]:
        """Full per-hand pipeline: features, classification, stabilisation."""
        features = extract_features(landmarks, handedness)
        raw = self.classify(features)

        stabilizer = self._stabilizers.get(handedness)
        if stabilizer is None:
            stabilizer = GestureStabilizer(self._history, self._min_votes)
            self._stabilizers[handedness] = stabilizer

        name, confidence = stabilizer.update(raw)
        if name is None:
            return features, None
        return features, RecognizedGesture(
            name=name, confidence=confidence, handedness=handedness
        )

    def hand_lost(self, handedness: str) -> None:
        """Reset temporal state for a hand that left the frame."""
        stabilizer = self._stabilizers.get(handedness)
        if stabilizer is not None:
            stabilizer.reset()
