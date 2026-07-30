"""Hand tracking backends.

The rest of the application depends only on :class:`HandTrackerBackend` and
:class:`HandObservation`; MediaPipe is an implementation detail confined to
this module.

MediaPipe removed its legacy ``mp.solutions`` API in recent releases
(≥ 0.10.15) in favour of the *Tasks* API, so :class:`MediaPipeHandTracker`
supports **both**: it uses the legacy Solutions backend when the installed
wheel still ships it (models bundled, zero setup) and otherwise falls back
to the Tasks ``HandLandmarker`` (downloading its ``.task`` model file on
first run). Both produce identical :class:`HandObservation` output.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from gesturesense.config.settings import TrackingConfig

logger = logging.getLogger(__name__)

#: Official model file for the Tasks backend (downloaded on first run).
_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
    "hand_landmarker/float16/latest/hand_landmarker.task"
)

#: Project root (…/gesturesense/vision/hand_tracker.py → two levels up).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class HandObservation:
    """One detected hand in image-normalised coordinates."""

    landmarks: np.ndarray
    """``(21, 3)`` array; x/y normalised to ``[0, 1]``, z is relative depth."""
    handedness: str
    """``"Left"`` or ``"Right"`` from the user's point of view."""
    score: float
    """Handedness classification confidence."""


class HandTrackerBackend(ABC):
    """Abstract hand-landmark tracker."""

    @abstractmethod
    def process(self, bgr_image: np.ndarray) -> list[HandObservation]:
        """Detect hands in a BGR frame."""

    @abstractmethod
    def close(self) -> None:
        """Release model resources."""


# ---------------------------------------------------------------------------
# Raw per-hand tuple emitted by the internal backends before the shared
# handedness-mirroring logic is applied by the facade.
# ---------------------------------------------------------------------------
_RawHand = tuple[np.ndarray, str, float]


class _LegacySolutionsBackend:
    """``mp.solutions.hands`` wrapper (mediapipe < 0.10.15, models bundled)."""

    def __init__(self, config: TrackingConfig):
        import mediapipe as mp

        self._hands = mp.solutions.hands.Hands(
            static_image_mode=False,
            max_num_hands=config.max_hands,
            model_complexity=config.model_complexity,
            min_detection_confidence=config.detection_confidence,
            min_tracking_confidence=config.tracking_confidence,
        )
        logger.info(
            "MediaPipe legacy Solutions backend initialised "
            "(max_hands=%d, complexity=%d, det=%.2f, trk=%.2f)",
            config.max_hands,
            config.model_complexity,
            config.detection_confidence,
            config.tracking_confidence,
        )

    def detect(self, rgb: np.ndarray) -> list[_RawHand]:
        results = self._hands.process(rgb)
        if not results.multi_hand_landmarks:
            return []
        hands: list[_RawHand] = []
        for hand_landmarks, handedness in zip(
            results.multi_hand_landmarks, results.multi_handedness or []
        ):
            landmarks = np.array(
                [(lm.x, lm.y, lm.z) for lm in hand_landmarks.landmark],
                dtype=np.float64,
            )
            classification = handedness.classification[0]
            hands.append(
                (landmarks, classification.label, float(classification.score))
            )
        return hands

    def close(self) -> None:
        self._hands.close()


class _TasksBackend:
    """``mediapipe.tasks`` HandLandmarker wrapper (mediapipe ≥ 0.10.15).

    The Tasks API has a single hand model, so ``model_complexity`` from the
    configuration does not apply here.
    """

    def __init__(self, config: TrackingConfig):
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions
        from mediapipe.tasks.python import vision as mp_vision

        self._mp = mp
        model_path = _ensure_model(config.model_path)

        options = mp_vision.HandLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(model_path)),
            running_mode=mp_vision.RunningMode.VIDEO,
            num_hands=config.max_hands,
            min_hand_detection_confidence=config.detection_confidence,
            min_hand_presence_confidence=config.tracking_confidence,
            min_tracking_confidence=config.tracking_confidence,
        )
        self._landmarker = mp_vision.HandLandmarker.create_from_options(options)
        self._last_timestamp_ms = -1
        logger.info(
            "MediaPipe Tasks backend initialised (model=%s, max_hands=%d, "
            "det=%.2f, trk=%.2f); model_complexity is not used by this API",
            model_path,
            config.max_hands,
            config.detection_confidence,
            config.tracking_confidence,
        )

    def detect(self, rgb: np.ndarray) -> list[_RawHand]:
        image = self._mp.Image(
            image_format=self._mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb)
        )
        # VIDEO mode requires strictly increasing timestamps.
        timestamp_ms = max(int(time.monotonic() * 1000), self._last_timestamp_ms + 1)
        self._last_timestamp_ms = timestamp_ms
        result = self._landmarker.detect_for_video(image, timestamp_ms)

        hands: list[_RawHand] = []
        for hand_landmarks, categories in zip(
            result.hand_landmarks, result.handedness
        ):
            landmarks = np.array(
                [(lm.x, lm.y, lm.z) for lm in hand_landmarks], dtype=np.float64
            )
            top = categories[0]
            hands.append((landmarks, top.category_name, float(top.score)))
        return hands

    def close(self) -> None:
        self._landmarker.close()


def _ensure_model(configured_path: str) -> Path:
    """Resolve the Tasks model path, downloading the file if missing."""
    model_path = Path(configured_path)
    if not model_path.is_absolute():
        model_path = _PROJECT_ROOT / model_path
    if model_path.exists():
        return model_path

    logger.info("Hand landmarker model not found, downloading to %s", model_path)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    partial = model_path.with_suffix(".task.partial")
    try:
        import urllib.request

        with urllib.request.urlopen(_MODEL_URL, timeout=60) as response:
            partial.write_bytes(response.read())
        partial.rename(model_path)
        logger.info(
            "Downloaded hand landmarker model (%.1f MB)",
            model_path.stat().st_size / 1e6,
        )
        return model_path
    except Exception as exc:  # noqa: BLE001 — any failure gets one clear message
        partial.unlink(missing_ok=True)
        raise RuntimeError(
            f"Could not download the hand landmarker model to {model_path}. "
            f"Download it manually from {_MODEL_URL} and place it there, or "
            f"set tracking.model_path in the configuration. ({exc})"
        ) from exc


class MediaPipeHandTracker(HandTrackerBackend):
    """MediaPipe hand tracker with automatic backend selection.

    Note on handedness: MediaPipe labels hands assuming a mirrored (selfie)
    image. The camera module mirrors frames when ``camera.mirror`` is true,
    in which case labels are already correct from the user's perspective;
    for un-mirrored input we swap them.
    """

    def __init__(self, config: TrackingConfig, input_is_mirrored: bool = True):
        # Imported lazily so the gesture engine and tests never require
        # MediaPipe to be installed.
        import mediapipe as mp

        if hasattr(mp, "solutions"):
            self._backend = _LegacySolutionsBackend(config)
        else:
            self._backend = _TasksBackend(config)
        self._input_is_mirrored = input_is_mirrored

    def set_input_mirrored(self, mirrored: bool) -> None:
        """Keep handedness labelling correct when mirroring is toggled."""
        self._input_is_mirrored = mirrored

    def process(self, bgr_image: np.ndarray) -> list[HandObservation]:
        """Run hand detection on a BGR frame."""
        import cv2

        rgb = cv2.cvtColor(bgr_image, cv2.COLOR_BGR2RGB)
        # Mark read-only: lets MediaPipe skip an internal copy.
        rgb.flags.writeable = False

        observations: list[HandObservation] = []
        for landmarks, label, score in self._backend.detect(rgb):
            if not self._input_is_mirrored:
                label = "Left" if label == "Right" else "Right"
            observations.append(
                HandObservation(landmarks=landmarks, handedness=label, score=score)
            )
        return observations

    def close(self) -> None:
        """Release model resources."""
        self._backend.close()
