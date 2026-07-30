"""Live debug visualization for the gesture perception module.

An optional OpenCV window showing the camera feed with hand skeletons,
bounding boxes and gesture labels (reusing the GestureSense renderer), plus
a small status line with inference FPS and hand count. Purely a developer/
calibration tool: it reads the same ``latest`` frame/result the module
polls and never participates in event flow.

Robustness contract: the view refuses to start without a usable display
(some GUI backends abort the process rather than raise), and runtime
rendering problems log one warning and self-disable — the perception
pipeline keeps publishing regardless. On macOS, OpenCV windows
must run on the main thread; there the standalone GestureSense app remains
the recommended visual debugger and this window disables itself.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Any

logger = logging.getLogger(__name__)


class DebugView:
    """Background render loop for the gesture debug window."""

    def __init__(self, window_title: str = "Digital Twin - Gesture Debug",
                 fps_limit: float = 30.0, frame_sink=None,
                 headless: bool = False):
        """``frame_sink``: optional callable receiving each annotated frame
        as JPEG bytes (the dashboard's FrameHub). ``headless``: annotate
        and sink without ever opening an OpenCV window — this is how the
        gesture view renders in the dashboard on machines with no display.
        """
        self._title = window_title
        self._interval = 1.0 / max(1.0, fps_limit)
        self._sink = frame_sink
        self._headless = headless and frame_sink is not None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._active = False

    @property
    def active(self) -> bool:
        """Whether the window is currently rendering."""
        return self._active

    # ------------------------------------------------------------------
    def start(self, camera: Any, worker: Any) -> None:
        """Begin rendering frames from ``camera`` annotated with ``worker`` results.

        Performs a pre-flight display check first: some OpenCV GUI
        backends (notably Qt) do not raise on a missing display — they
        ``abort()`` the whole process, which no ``except`` can catch. The
        debug view must therefore refuse to start rather than try-and-fail.
        """
        if not self._headless and not self._display_available():
            if self._sink is None:
                self._active = False
                return
            # No display but a dashboard is watching: render headless.
            self._headless = True
        self._stop.clear()
        self._active = True
        self._thread = threading.Thread(
            target=self._run, args=(camera, worker), name="gesture-debug", daemon=True
        )
        self._thread.start()

    @staticmethod
    def _display_available() -> bool:
        if sys.platform == "win32":
            return True
        if sys.platform == "darwin":
            # OpenCV windows must run on the main thread on macOS; the
            # standalone GestureSense app is the visual debugger there.
            logger.warning(
                "Debug window disabled on macOS (GUI must own the main "
                "thread); run the standalone GestureSense app instead"
            )
            return False
        if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
            return True
        logger.warning(
            "Debug window disabled: no display available "
            "(DISPLAY/WAYLAND_DISPLAY not set)"
        )
        return False

    def stop(self) -> None:
        """Close the window and join the render thread (idempotent)."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self._active = False

    # ------------------------------------------------------------------
    def _run(self, camera: Any, worker: Any) -> None:
        try:
            import cv2

            from gesturesense.config.settings import UiConfig
            from gesturesense.ui.renderer import HandRenderer
            from gesturesense.ui.theme import get_theme
        except Exception:
            logger.exception("Debug window unavailable (imports failed); disabled")
            self._active = False
            return

        renderer = HandRenderer(UiConfig(window_title=self._title))
        theme = get_theme("dark")
        font = cv2.FONT_HERSHEY_SIMPLEX
        last_seq = -1

        try:
            while not self._stop.wait(self._interval):
                frame = camera.latest()
                if frame is None or frame.seq == last_seq:
                    continue
                last_seq = frame.seq
                image = frame.image.copy()  # never draw on the shared frame

                output = worker.latest()
                hands = output.hands if output is not None else ()
                for hand in hands:
                    renderer.draw_hand(image, hand.observation, hand.gesture, theme)
                status = f"hands: {len(hands)}   inference fps: {worker.fps:.1f}"
                cv2.putText(image, status, (12, 24), font, 0.6, (0, 0, 0), 3, cv2.LINE_AA)
                cv2.putText(image, status, (12, 24), font, 0.6, (240, 240, 240), 1, cv2.LINE_AA)

                if self._sink is not None:
                    ok, encoded = cv2.imencode(
                        ".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 80])
                    if ok:
                        self._sink(encoded.tobytes())
                if not self._headless:
                    cv2.imshow(self._title, image)
                    if cv2.waitKey(1) & 0xFF in (ord("q"), 27):  # q / Esc
                        logger.info("Debug window closed by user")
                        break
        except Exception as exc:
            # Headless container, no GUI backend, or a backend hiccup:
            # the debug view is expendable, the perception pipeline is not.
            logger.warning("Debug window disabled: %s", exc)
        finally:
            self._active = False
            if not self._headless:
                try:
                    import cv2

                    cv2.destroyWindow(self._title)
                except Exception:
                    pass
