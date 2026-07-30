"""Gesture perception module: GestureSense as a first-class plugin.

Wraps the GestureSense library (camera capture, MediaPipe hand tracking,
landmark smoothing, rule-based gesture engine) as an assistant module that
publishes **semantic events** and nothing else:

* ``perception.gesture`` — ``{gesture, confidence, hand, repeat}``,
  edge-triggered on gesture changes, optionally repeated while held.
* ``perception.hand`` — ``{hand, present}`` when hands enter/leave view.

The module never interprets gestures — a thumbs-up means "next slide" in a
presentation and "like" on YouTube, and that decision belongs to the
reasoning layer. Application-specific behaviour is therefore *impossible*
to express here by construction: the payload contains only what was seen.

Runtime disable (``pause``) fully releases the camera and the MediaPipe
graph; resume rebuilds them. Camera and tracker construction are injectable
for tests, so the whole event pipeline is verifiable without hardware.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from digital_twin.configuration.settings import GestureModuleConfig
from digital_twin.core.events import Event, Topics
from digital_twin.core.module import BaseModule, ModuleState
from digital_twin.perception.gesture.semantics import to_semantic
from digital_twin.paths import resolve_asset

logger = logging.getLogger(__name__)


class GesturePerceptionModule(BaseModule):
    """Publishes semantic hand-gesture events from a webcam."""

    name = "gesture"
    topics = (Topics.GESTURE, Topics.HAND)

    def __init__(
        self,
        config: GestureModuleConfig,
        camera_factory: Callable[[], Any] | None = None,
        tracker_factory: Callable[[], Any] | None = None,
        frame_sink: Callable[[bytes], None] | None = None,
    ):
        super().__init__()
        self._config = config
        self._frame_sink = frame_sink
        self._camera_factory = camera_factory or self._default_camera
        self._tracker_factory = tracker_factory or self._default_tracker

        self._camera: Any = None
        self._tracker: Any = None
        self._worker: Any = None
        self._poll_thread: threading.Thread | None = None
        self._poll_stop = threading.Event()

        # Calibration filters (per-user tuning via config/profiles).
        self._disabled = frozenset(config.disabled_gestures)
        self._thresholds = dict(config.gesture_thresholds)

        # Tooling state.
        self._debug_view: Any = None
        self._custom_report: Any = None

        # Per-hand event state, touched only by the poll thread (or by
        # tests calling _process_output directly).
        self._last_seq: int = -1
        self._present: set[str] = set()
        self._last_gesture: dict[str, tuple[str, float]] = {}

    # ------------------------------------------------------------------
    # Default factories (lazy imports keep tests hardware-free)
    # ------------------------------------------------------------------
    def _default_camera(self) -> Any:
        from gesturesense.camera.capture import ThreadedCamera
        from gesturesense.config.settings import CameraConfig

        return ThreadedCamera(
            CameraConfig(
                index=self._config.camera_index,
                width=self._config.frame_width,
                height=self._config.frame_height,
                fps=self._config.camera_fps,
                mirror=self._config.mirror,
            )
        )

    def _default_tracker(self) -> Any:
        from gesturesense.config.settings import TrackingConfig
        from gesturesense.vision.hand_tracker import MediaPipeHandTracker

        return MediaPipeHandTracker(
            TrackingConfig(
                max_hands=self._config.max_hands,
                model_complexity=self._config.model_complexity,
                detection_confidence=self._config.detection_confidence,
                tracking_confidence=self._config.tracking_confidence,
                landmark_smoothing=self._config.landmark_smoothing,
                model_path=str(resolve_asset(self._config.model_path)),
            ),
            input_is_mirrored=self._config.mirror,
        )

    # ------------------------------------------------------------------
    # Lifecycle hooks
    # ------------------------------------------------------------------
    def _on_start(self) -> None:
        from gesturesense.core.pipeline import InferenceWorker
        from gesturesense.gesture.engine import GestureEngine
        from gesturesense.tracking.smoother import LandmarkSmoother

        # Custom rules must be registered before the engine instantiates
        # the rule set. Failures degrade gracefully (logged + in metrics).
        if self._config.custom_gesture_modules:
            from digital_twin.perception.gesture.custom import (
                load_custom_gesture_modules,
            )

            self._custom_report = load_custom_gesture_modules(
                self._config.custom_gesture_modules
            )

        self._camera = self._camera_factory()
        self._tracker = self._tracker_factory()
        engine = GestureEngine(
            min_confidence=self._config.min_confidence,
            history=self._config.history,
            min_votes=self._config.min_votes,
        )
        smoother = LandmarkSmoother(alpha=self._config.landmark_smoothing)

        self._camera.start()
        self._worker = InferenceWorker(self._camera, self._tracker, engine, smoother)
        self._worker.start()

        if self._config.debug_window or self._frame_sink is not None:
            from digital_twin.perception.gesture.debug_view import DebugView

            self._debug_view = DebugView(
                frame_sink=self._frame_sink,
                headless=not self._config.debug_window,
            )
            self._debug_view.start(self._camera, self._worker)

        self._last_seq = -1
        self._present = set()
        self._last_gesture = {}
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="gesture-poll", daemon=True
        )
        self._poll_thread.start()

    def _on_stop(self) -> None:
        self._poll_stop.set()
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=3.0)
            self._poll_thread = None
        if self._debug_view is not None:
            self._debug_view.stop()
            self._debug_view = None
        if self._worker is not None:
            self._worker.stop()  # also closes the tracker
            self._worker = None
        self._tracker = None
        if self._camera is not None:
            self._camera.stop()
            self._camera = None

    # pause/resume inherit the BaseModule defaults: a paused gesture module
    # releases the camera and the MediaPipe graph entirely (privacy + CPU),
    # and resume rebuilds them from the injected factories.

    # ------------------------------------------------------------------
    # Polling and event derivation
    # ------------------------------------------------------------------
    def _poll_loop(self) -> None:
        # _on_start launches this thread before the state machine flips to
        # RUNNING; wait out the STARTING window so no early result is
        # dropped by the state guard in _process_output.
        while self.state is ModuleState.STARTING and not self._poll_stop.wait(0.001):
            pass
        while not self._poll_stop.is_set():
            try:
                self._process_output(self._worker.latest())
            except Exception:  # one bad result must not kill perception
                logger.exception("Gesture module failed to process output")
            self._poll_stop.wait(self._config.poll_interval_s)

    def _process_output(self, output: Any, now: float | None = None) -> None:
        """Diff one inference result against event state and publish changes.

        Pure event derivation — separated from the polling thread so it can
        be unit-tested deterministically with hand-built outputs.
        """
        if output is None or output.frame_seq == self._last_seq:
            return
        if self.state is not ModuleState.RUNNING:
            return
        self._last_seq = output.frame_seq
        now = time.time() if now is None else now

        current: dict[str, tuple[str, float]] = {}
        hands_present: set[str] = set()
        for hand in output.hands:
            label = hand.observation.handedness.lower()
            hands_present.add(label)
            if hand.gesture is None:
                continue
            gesture = to_semantic(hand.gesture.name)
            confidence = float(hand.gesture.confidence)
            # Calibration filters: user-disabled gestures and per-gesture
            # confidence floors are treated as "no stable gesture", which
            # also re-arms the edge exactly like an unstable frame would.
            if gesture in self._disabled:
                continue
            if confidence < self._thresholds.get(gesture, 0.0):
                continue
            current[label] = (gesture, confidence)

        self._diff_presence(hands_present)
        self._diff_gestures(current, hands_present, now)

    def _diff_presence(self, hands_present: set[str]) -> None:
        for hand in sorted(hands_present - self._present):
            self._publish_hand(hand, present=True)
        for hand in sorted(self._present - hands_present):
            self._publish_hand(hand, present=False)
            self._last_gesture.pop(hand, None)
        self._present = hands_present

    def _diff_gestures(
        self,
        current: dict[str, tuple[str, float]],
        hands_present: set[str],
        now: float,
    ) -> None:
        repeat_every = self._config.repeat_interval_s
        for hand, (gesture, confidence) in current.items():
            previous = self._last_gesture.get(hand)
            if previous is None or previous[0] != gesture:
                self._publish_gesture(hand, gesture, confidence, repeat=False)
                self._last_gesture[hand] = (gesture, now)
            elif repeat_every > 0 and now - previous[1] >= repeat_every:
                self._publish_gesture(hand, gesture, confidence, repeat=True)
                self._last_gesture[hand] = (gesture, now)
        # A hand that is still visible but no longer holds a stable gesture
        # resets its edge state so the next gesture fires a fresh event.
        for hand in list(self._last_gesture):
            if hand in hands_present and hand not in current:
                del self._last_gesture[hand]

    # ------------------------------------------------------------------
    def _publish_gesture(
        self, hand: str, gesture: str, confidence: float, repeat: bool
    ) -> None:
        self._publish(
            Event(
                topic=Topics.GESTURE,
                source=self.name,
                payload={
                    "gesture": gesture,
                    "confidence": round(confidence, 3),
                    "hand": hand,
                    "repeat": repeat,
                },
            )
        )

    def _publish_hand(self, hand: str, present: bool) -> None:
        self._publish(
            Event(
                topic=Topics.HAND,
                source=self.name,
                payload={"hand": hand, "present": present},
            )
        )

    # ------------------------------------------------------------------
    def _metrics(self) -> dict[str, Any]:
        metrics: dict[str, Any] = {
            "hands_visible": len(self._present),
        }
        worker = self._worker
        if worker is not None:
            metrics["inference_fps"] = round(worker.fps, 1)
        camera = self._camera
        status = getattr(camera, "status", None)
        if status is not None:
            metrics["camera_status"] = getattr(status, "value", str(status))
        if self._custom_report is not None:
            metrics["custom_gestures_loaded"] = len(self._custom_report.loaded)
            if self._custom_report.failed:
                metrics["custom_gestures_failed"] = list(self._custom_report.failed)
        if self._config.debug_window:
            view = self._debug_view
            metrics["debug_window"] = bool(view is not None and view.active)
        return metrics
