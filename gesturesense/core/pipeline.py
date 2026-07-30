"""Inference pipeline: background worker consuming frames, producing results.

Threading model
---------------
::

    camera thread  →  latest Frame slot  →  inference thread  →  latest
    (blocking I/O)                          (MediaPipe + rules)   InferenceOutput
                                                                  slot → render loop

Each hand-off keeps only the *latest* item, so a slow stage drops stale work
instead of queueing latency. All shared state is lock-protected and
immutable once published.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field, replace

from gesturesense.camera.capture import ThreadedCamera
from gesturesense.gesture.engine import GestureEngine, RecognizedGesture
from gesturesense.gesture.features import HandFeatures
from gesturesense.tracking.smoother import LandmarkSmoother
from gesturesense.utils.timing import FpsCounter
from gesturesense.vision.hand_tracker import HandObservation, HandTrackerBackend

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HandResult:
    """Everything known about one hand in one frame."""

    observation: HandObservation
    features: HandFeatures
    gesture: RecognizedGesture | None


@dataclass(frozen=True)
class InferenceOutput:
    """Published result of processing one camera frame."""

    frame_seq: int
    hands: tuple[HandResult, ...] = field(default_factory=tuple)
    inference_ms: float = 0.0


class InferenceWorker:
    """Background thread running tracking + gesture recognition.

    Owns the tracker, the landmark smoother and the gesture engine so that
    all model state is confined to a single thread — no locking inside the
    hot path.
    """

    def __init__(
        self,
        camera: ThreadedCamera,
        tracker: HandTrackerBackend,
        engine: GestureEngine,
        smoother: LandmarkSmoother,
    ):
        self._camera = camera
        self._tracker = tracker
        self._engine = engine
        self._smoother = smoother
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._output: InferenceOutput | None = None
        self._fps = FpsCounter()

    # ------------------------------------------------------------------
    def start(self) -> None:
        """Start the worker thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="inference", daemon=True
        )
        self._thread.start()
        logger.info("Inference thread started")

    def stop(self) -> None:
        """Stop the worker and release tracker resources."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        try:
            self._tracker.close()
        except Exception:  # pragma: no cover - backend specific
            logger.exception("Error closing tracker")
        logger.info("Inference thread stopped")

    def latest(self) -> InferenceOutput | None:
        """Most recent published output (thread-safe)."""
        with self._lock:
            return self._output

    @property
    def fps(self) -> float:
        """Inference throughput estimate."""
        return self._fps.fps

    # ------------------------------------------------------------------
    def _run(self) -> None:
        last_seq = -1
        while not self._stop_event.is_set():
            frame = self._camera.latest(newer_than=last_seq)
            if frame is None:
                # No new frame yet — yield briefly instead of spinning.
                time.sleep(0.001)
                continue
            last_seq = frame.seq

            try:
                started = time.perf_counter()
                hands = self._process(frame.image)
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                self._fps.tick()
                output = InferenceOutput(
                    frame_seq=frame.seq, hands=tuple(hands), inference_ms=elapsed_ms
                )
                with self._lock:
                    self._output = output
            except Exception:
                # One bad frame must never take the pipeline down.
                logger.exception("Inference failed on frame %d", frame.seq)

    def _process(self, image) -> list[HandResult]:
        observations = self._tracker.process(image)

        self._smoother.mark_all_stale()
        results: list[HandResult] = []
        used_keys: set[str] = set()
        for observation in observations:
            # MediaPipe can emit two hands with the same handedness label
            # (two people, or a misclassified flip). Temporal state — the
            # smoother and the gesture stabilizer — must be keyed uniquely
            # per hand or the two hands corrupt each other's history. The
            # display label on the observation stays untouched.
            key = observation.handedness
            while key in used_keys:
                key += "'"
            used_keys.add(key)

            smoothed = self._smoother.smooth(key, observation.landmarks)
            stabilized = HandObservation(
                landmarks=smoothed,
                handedness=observation.handedness,
                score=observation.score,
            )
            features, gesture = self._engine.process_hand(smoothed, key)
            if gesture is not None and gesture.handedness != observation.handedness:
                gesture = replace(gesture, handedness=observation.handedness)
            results.append(
                HandResult(observation=stabilized, features=features, gesture=gesture)
            )

        for lost_label in self._smoother.prune():
            self._engine.hand_lost(lost_label)
        return results
