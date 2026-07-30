"""Threaded webcam capture with latest-frame semantics and auto-reconnect.

Design notes
------------
* A dedicated capture thread keeps ``cv2.VideoCapture.read()`` — which blocks
  for up to a full frame interval — off the inference and render paths.
* Only the *latest* frame is retained (no queue): consumers always see the
  freshest image, and backlog latency cannot build up under load.
* Preprocessing that belongs to the camera (mirroring) happens here once, so
  every consumer sees identical pixels.
* If the device disappears mid-session the thread keeps retrying at a
  configurable interval and reports its status, so the app degrades to a
  "camera lost" screen instead of crashing.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum

import cv2
import numpy as np

from gesturesense.config.settings import CameraConfig

logger = logging.getLogger(__name__)


class CameraStatus(Enum):
    """Lifecycle status reported by the capture thread."""

    STARTING = "starting"
    RUNNING = "running"
    RECONNECTING = "reconnecting"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True)
class Frame:
    """A single captured frame with identity and timing."""

    image: np.ndarray
    """BGR image, already mirrored if configured."""
    seq: int
    timestamp: float


class ThreadedCamera:
    """Background webcam reader exposing the most recent :class:`Frame`.

    Thread-safe: :meth:`latest` may be called from any thread. Designed so a
    future multi-camera setup simply instantiates one ``ThreadedCamera`` per
    device.
    """

    def __init__(self, config: CameraConfig):
        self._config = config
        self._capture: cv2.VideoCapture | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._frame: Frame | None = None
        self._status = CameraStatus.STARTING
        self._seq = 0
        self._mirror = config.mirror

    @property
    def mirror(self) -> bool:
        """Whether frames are mirrored before publishing."""
        return self._mirror

    def set_mirror(self, enabled: bool) -> None:
        """Toggle selfie mirroring at runtime (bool writes are atomic)."""
        self._mirror = enabled

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Start the capture thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="camera-capture", daemon=True
        )
        self._thread.start()
        logger.info(
            "Camera thread started (device=%d, %dx%d@%d, mirror=%s)",
            self._config.index,
            self._config.width,
            self._config.height,
            self._config.fps,
            self._config.mirror,
        )

    def stop(self) -> None:
        """Signal the thread to stop and release the device."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self._release()
        with self._lock:
            self._status = CameraStatus.STOPPED
        logger.info("Camera stopped")

    def latest(self, newer_than: int = -1) -> Frame | None:
        """Return the newest frame, or ``None`` if not newer than ``newer_than``."""
        with self._lock:
            if self._frame is None or self._frame.seq <= newer_than:
                return None
            return self._frame

    @property
    def status(self) -> CameraStatus:
        """Current capture status (thread-safe)."""
        with self._lock:
            return self._status

    # ------------------------------------------------------------------
    # Capture thread
    # ------------------------------------------------------------------
    def _run(self) -> None:
        try:
            while not self._stop_event.is_set():
                if self._capture is None or not self._capture.isOpened():
                    if not self._open():
                        self._set_status(CameraStatus.RECONNECTING)
                        if self._stop_event.wait(self._config.reconnect_delay_s):
                            break
                        continue
                    self._set_status(CameraStatus.RUNNING)

                ok, image = self._capture.read()
                if not ok or image is None:
                    logger.warning("Camera read failed — attempting reconnect")
                    self._release()
                    self._set_status(CameraStatus.RECONNECTING)
                    continue

                if self._mirror:
                    # Mirror once, at the source, so overlays, gestures and
                    # MediaPipe handedness all agree on orientation.
                    image = cv2.flip(image, 1)

                self._publish(image)
        except Exception:  # pragma: no cover - defensive: thread must not die silently
            logger.exception("Camera thread crashed")
            self._set_status(CameraStatus.FAILED)
        finally:
            self._release()

    def _open(self) -> bool:
        logger.info("Opening camera %d", self._config.index)
        capture = cv2.VideoCapture(self._config.index)
        if not capture.isOpened():
            capture.release()
            logger.error("Camera %d unavailable", self._config.index)
            return False
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, self._config.width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self._config.height)
        capture.set(cv2.CAP_PROP_FPS, self._config.fps)
        # Keep the driver-side queue minimal for latency (ignored by some backends).
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        actual_w = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        logger.info("Camera negotiated resolution: %dx%d", actual_w, actual_h)
        self._capture = capture
        return True

    def _publish(self, image: np.ndarray) -> None:
        self._seq += 1
        frame = Frame(image=image, seq=self._seq, timestamp=time.perf_counter())
        with self._lock:
            self._frame = frame

    def _set_status(self, status: CameraStatus) -> None:
        with self._lock:
            if self._status is not status:
                self._status = status

    def _release(self) -> None:
        if self._capture is not None:
            try:
                self._capture.release()
            except Exception:  # pragma: no cover
                logger.exception("Error releasing camera")
            self._capture = None
