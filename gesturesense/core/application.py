"""Application orchestration: wiring, render loop and graceful shutdown.

The render loop runs on the main thread (a HighGUI requirement on most
platforms); capture and inference run on their own threads and communicate
through latest-value slots (see :mod:`gesturesense.core.pipeline`).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from gesturesense.camera.capture import CameraStatus, ThreadedCamera
from gesturesense.config.settings import AppConfig
from gesturesense.core.pipeline import InferenceOutput, InferenceWorker
from gesturesense.gesture.engine import GestureEngine
from gesturesense.tracking.smoother import LandmarkSmoother
from gesturesense.ui.hud import Hud, HudState, NotificationCenter
from gesturesense.ui.renderer import HandRenderer
from gesturesense.ui.theme import Theme, get_theme, next_theme
from gesturesense.ui.window import AppWindow, KeyAction
from gesturesense.utils.perf import StageTimings, system_usage
from gesturesense.utils.timing import FpsCounter, RateLimiter
from gesturesense.vision.hand_tracker import MediaPipeHandTracker

logger = logging.getLogger(__name__)


class Application:
    """Owns all subsystems and runs the main loop."""

    def __init__(self, config: AppConfig):
        self._config = config
        self._camera = ThreadedCamera(config.camera)
        self._tracker = MediaPipeHandTracker(
            config.tracking, input_is_mirrored=config.camera.mirror
        )
        self._engine = GestureEngine(
            min_confidence=config.gestures.min_confidence,
            history=config.gestures.history,
            min_votes=config.gestures.min_votes,
        )
        self._smoother = LandmarkSmoother(alpha=config.tracking.landmark_smoothing)
        self._worker = InferenceWorker(
            self._camera, self._tracker, self._engine, self._smoother
        )

        self._notifications = NotificationCenter(ttl_s=config.ui.notification_ttl_s)
        self._renderer = HandRenderer(config.ui)
        self._hud = Hud(config.ui, self._notifications)
        self._theme: Theme = get_theme(config.ui.theme)
        self._window: AppWindow | None = None

        self._debug = config.debug.enabled
        self._render_fps = FpsCounter()
        self._camera_fps = FpsCounter()
        self._timings = StageTimings()
        self._placeholder = np.zeros(
            (config.camera.height, config.camera.width, 3), dtype=np.uint8
        )
        self._last_fps_log = time.perf_counter()
        self._last_camera_seq = -1

    # ------------------------------------------------------------------
    def run(self) -> int:
        """Run until quit; returns a process exit code."""
        logger.info("GestureSense starting up")
        exit_code = 0
        try:
            self._camera.start()
            self._worker.start()
            self._window = AppWindow(
                self._config.ui.window_title, self._config.ui.fullscreen
            )
            self._render_loop()
        except KeyboardInterrupt:
            logger.info("Interrupted by user")
        except Exception:
            logger.exception("Fatal error in main loop")
            exit_code = 1
        finally:
            self._shutdown()
        return exit_code

    # ------------------------------------------------------------------
    def _render_loop(self) -> None:
        assert self._window is not None
        limiter = RateLimiter(self._config.ui.render_fps_limit)

        while True:
            limiter.wait()

            frame = self._camera.latest()
            output = self._worker.latest()

            with self._timings.measure("render"):
                if frame is None:
                    canvas = self._placeholder.copy()
                else:
                    if frame.seq != self._last_camera_seq:
                        self._camera_fps.tick(frame.timestamp)
                        self._last_camera_seq = frame.seq
                    # Copy before drawing: the inference thread may still be
                    # reading this exact buffer.
                    canvas = frame.image.copy()
                    self._draw_hands(canvas, output)

                self._hud.draw(canvas, self._hud_state(output), self._theme)

            self._render_fps.tick()
            self._log_fps_periodically()

            action = self._window.show(canvas)
            if not self._handle_key(action, canvas):
                break

    def _draw_hands(self, canvas: np.ndarray, output: InferenceOutput | None) -> None:
        if output is None:
            return
        for hand in output.hands:
            self._renderer.draw_hand(canvas, hand.observation, hand.gesture, self._theme)

    def _hud_state(self, output: InferenceOutput | None) -> HudState:
        cpu, mem = (
            system_usage()
            if self._debug and self._config.debug.show_system_usage
            else (None, None)
        )
        timings = self._timings.snapshot()
        timings["inference"] = output.inference_ms if output else 0.0
        return HudState(
            render_fps=self._render_fps.fps,
            camera_fps=self._camera_fps.fps,
            inference_ms=output.inference_ms if output else 0.0,
            camera_status=self._camera.status,
            hands=[(hand.features, hand.gesture) for hand in output.hands]
            if output
            else [],
            mirror=self._camera.mirror,
            debug=self._debug,
            timings=timings if self._config.debug.show_timings else {},
            cpu_percent=cpu,
            memory_mb=mem,
        )

    # ------------------------------------------------------------------
    def _handle_key(self, action: KeyAction, canvas: np.ndarray) -> bool:
        """Apply a key action; returns ``False`` when the app should exit."""
        assert self._window is not None
        if action is KeyAction.QUIT:
            logger.info("Quit requested")
            return False
        if action is KeyAction.TOGGLE_FULLSCREEN:
            state = self._window.toggle_fullscreen()
            self._notifications.push(f"Fullscreen {'on' if state else 'off'}")
        elif action is KeyAction.TOGGLE_DEBUG:
            self._debug = not self._debug
            self._notifications.push(f"Debug {'on' if self._debug else 'off'}")
        elif action is KeyAction.TOGGLE_MIRROR:
            mirrored = not self._camera.mirror
            self._camera.set_mirror(mirrored)
            self._tracker.set_input_mirrored(mirrored)
            self._notifications.push(f"Mirror {'on' if mirrored else 'off'}")
        elif action is KeyAction.TOGGLE_THEME:
            self._theme = next_theme(self._theme.name)
            self._notifications.push(f"Theme: {self._theme.name}")
        elif action is KeyAction.SCREENSHOT:
            self._save_screenshot(canvas)
        return True

    def _save_screenshot(self, canvas: np.ndarray) -> None:
        captures = Path(self._config.logging.directory) / "captures"
        try:
            captures.mkdir(parents=True, exist_ok=True)
            filename = captures / f"capture_{datetime.now():%Y%m%d_%H%M%S}.png"
            cv2.imwrite(str(filename), canvas)
            self._notifications.push(f"Saved {filename.name}")
            logger.info("Screenshot saved: %s", filename)
        except OSError:
            logger.exception("Screenshot failed")
            self._notifications.push("Screenshot failed")

    def _log_fps_periodically(self) -> None:
        interval = self._config.logging.fps_report_interval_s
        now = time.perf_counter()
        if now - self._last_fps_log >= interval:
            self._last_fps_log = now
            logger.info(
                "FPS report — render: %.1f, camera: %.1f, inference: %.1f (%.1f ms)",
                self._render_fps.fps,
                self._camera_fps.fps,
                self._worker.fps,
                self._timings.snapshot().get("inference", 0.0),
            )

    # ------------------------------------------------------------------
    def _shutdown(self) -> None:
        logger.info("Shutting down…")
        self._worker.stop()
        self._camera.stop()
        if self._window is not None:
            self._window.close()
        cv2.destroyAllWindows()
        logger.info("Shutdown complete")
