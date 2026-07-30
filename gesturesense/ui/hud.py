"""Heads-up display: status bars, notifications and the debug panel.

Panels are alpha-blended only on their own regions of interest (never the
whole frame) to keep per-frame rendering cost low and FPS stable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import cv2
import numpy as np

from gesturesense.camera.capture import CameraStatus
from gesturesense.config.settings import UiConfig
from gesturesense.gesture.engine import RecognizedGesture
from gesturesense.gesture.features import HandFeatures
from gesturesense.ui.theme import Theme

_FONT = cv2.FONT_HERSHEY_SIMPLEX


@dataclass
class HudState:
    """Everything the HUD needs for one rendered frame."""

    render_fps: float = 0.0
    camera_fps: float = 0.0
    inference_ms: float = 0.0
    camera_status: CameraStatus = CameraStatus.STARTING
    hands: list[tuple[HandFeatures, RecognizedGesture | None]] = field(
        default_factory=list
    )
    mirror: bool = True
    debug: bool = False
    timings: dict[str, float] = field(default_factory=dict)
    cpu_percent: float | None = None
    memory_mb: float | None = None


class NotificationCenter:
    """Transient toast messages with a time-to-live."""

    def __init__(self, ttl_s: float = 2.5, max_visible: int = 4):
        self._ttl = ttl_s
        self._max_visible = max_visible
        self._items: list[tuple[float, str]] = []

    def push(self, message: str) -> None:
        """Show ``message`` for the configured TTL."""
        self._items.append((time.perf_counter(), message))

    def active(self) -> list[str]:
        """Currently visible messages, oldest first."""
        now = time.perf_counter()
        self._items = [(ts, msg) for ts, msg in self._items if now - ts < self._ttl]
        return [msg for _, msg in self._items[-self._max_visible :]]


class Hud:
    """Draws the persistent interface chrome onto each frame."""

    _BAR_HEIGHT = 58
    _STATUS_HEIGHT = 30

    def __init__(self, config: UiConfig, notifications: NotificationCenter):
        self._config = config
        self._notifications = notifications

    # ------------------------------------------------------------------
    def draw(self, frame: np.ndarray, state: HudState, theme: Theme) -> None:
        """Render all HUD elements for this frame."""
        self._top_bar(frame, state, theme)
        self._bottom_bar(frame, state, theme)
        self._toasts(frame, theme)
        if state.debug:
            self._debug_panel(frame, state, theme)
        if state.camera_status is not CameraStatus.RUNNING:
            self._camera_banner(frame, state, theme)

    # ------------------------------------------------------------------
    def _blend_panel(
        self, frame: np.ndarray, x1: int, y1: int, x2: int, y2: int, theme: Theme
    ) -> None:
        roi = frame[y1:y2, x1:x2]
        if not roi.size:
            return
        overlay = np.full_like(roi, theme.panel)
        cv2.addWeighted(overlay, theme.panel_alpha, roi, 1 - theme.panel_alpha, 0, dst=roi)

    def _text(
        self,
        frame: np.ndarray,
        text: str,
        origin: tuple[int, int],
        color: tuple[int, int, int],
        scale_mul: float = 1.0,
        bold: bool = False,
    ) -> int:
        scale = self._config.font_scale * scale_mul
        thickness = 2 if bold else 1
        cv2.putText(frame, text, origin, _FONT, scale, color, thickness, cv2.LINE_AA)
        (width, _), _ = cv2.getTextSize(text, _FONT, scale, thickness)
        return width

    # ------------------------------------------------------------------
    def _top_bar(self, frame: np.ndarray, state: HudState, theme: Theme) -> None:
        width = frame.shape[1]
        self._blend_panel(frame, 0, 0, width, self._BAR_HEIGHT, theme)

        x = 16
        x += self._text(frame, "GestureSense", (x, 24), theme.accent, 1.1, bold=True) + 24

        if self._config.show_fps:
            fps_color = (
                theme.success
                if state.render_fps >= 25
                else theme.warning
                if state.render_fps >= 15
                else theme.danger
            )
            x += self._text(frame, f"{state.render_fps:5.1f} FPS", (x, 24), fps_color) + 18
            x += self._text(
                frame,
                f"cam {state.camera_fps:4.1f} | inf {state.inference_ms:5.1f} ms",
                (x, 24),
                theme.text_secondary,
            ) + 18

        tracking = "TRACKING" if state.hands else "NO HAND"
        tracking_color = theme.success if state.hands else theme.text_secondary
        self._text(frame, tracking, (width - 130, 24), tracking_color, bold=True)

        # Second row: one gesture readout per hand with a confidence bar.
        x = 16
        if not state.hands:
            self._text(
                frame, "Show a hand to the camera", (x, 46), theme.text_secondary
            )
            return
        for features, gesture in state.hands:
            # Each entry needs roughly label + name + bar; stop cleanly if
            # the window is too narrow rather than drawing off-frame.
            if x > width - 240:
                self._text(frame, "...", (x, 46), theme.text_secondary)
                break
            label = gesture.name if gesture else "-"
            confidence = gesture.confidence if gesture else 0.0
            x += self._text(
                frame, f"{features.handedness}:", (x, 46), theme.text_secondary
            ) + 8
            x += self._text(frame, label, (x, 46), theme.text_primary, bold=True) + 12
            x = self._confidence_bar(frame, x, 38, confidence, theme) + 28

    def _confidence_bar(
        self, frame: np.ndarray, x: int, y: int, value: float, theme: Theme
    ) -> int:
        bar_w, bar_h = 70, 8
        cv2.rectangle(frame, (x, y), (x + bar_w, y + bar_h), theme.text_secondary, 1)
        fill = int(bar_w * max(0.0, min(1.0, value)))
        if fill > 0:
            cv2.rectangle(
                frame, (x + 1, y + 1), (x + fill - 1, y + bar_h - 1), theme.accent, -1
            )
        return x + bar_w

    # ------------------------------------------------------------------
    def _bottom_bar(self, frame: np.ndarray, state: HudState, theme: Theme) -> None:
        height, width = frame.shape[:2]
        y1 = height - self._STATUS_HEIGHT
        self._blend_panel(frame, 0, y1, width, height, theme)

        mode = f"mode: {'debug' if state.debug else 'normal'} | mirror: {'on' if state.mirror else 'off'} | theme: {theme.name}"
        self._text(frame, mode, (16, height - 10), theme.text_secondary, 0.9)

        hints = "Q quit  F fullscreen  D debug  M mirror  T theme  S screenshot"
        (hint_w, _), _ = cv2.getTextSize(hints, _FONT, self._config.font_scale * 0.9, 1)
        self._text(
            frame, hints, (width - hint_w - 16, height - 10), theme.text_secondary, 0.9
        )

    # ------------------------------------------------------------------
    def _toasts(self, frame: np.ndarray, theme: Theme) -> None:
        messages = self._notifications.active()
        if not messages:
            return
        width = frame.shape[1]
        y = self._BAR_HEIGHT + 16
        for message in messages:
            scale = self._config.font_scale * 0.95
            (text_w, text_h), baseline = cv2.getTextSize(message, _FONT, scale, 1)
            pad = 8
            x1 = width - text_w - 2 * pad - 16
            self._blend_panel(frame, x1, y, width - 16, y + text_h + 2 * pad, theme)
            cv2.putText(
                frame,
                message,
                (x1 + pad, y + text_h + pad - baseline // 2),
                _FONT,
                scale,
                theme.text_primary,
                1,
                cv2.LINE_AA,
            )
            y += text_h + 2 * pad + 8

    # ------------------------------------------------------------------
    def _debug_panel(self, frame: np.ndarray, state: HudState, theme: Theme) -> None:
        lines: list[str] = ["DEBUG"]
        for stage, elapsed in sorted(state.timings.items()):
            lines.append(f"{stage:<10s} {elapsed:6.2f} ms")
        if state.cpu_percent is not None and state.memory_mb is not None:
            lines.append(f"cpu {state.cpu_percent:5.1f} %  mem {state.memory_mb:6.1f} MB")
        for features, _ in state.hands:
            lines.append(f"[{features.handedness}] size {features.hand_size:.3f}")
            for name, reading in features.fingers.items():
                lines.append(
                    f"  {name:<6s} {reading.state.value:<8s} "
                    f"ext {reading.extension:4.2f}  ang {reading.avg_joint_angle:5.1f}"
                )
            lines.append(f"  pinch {features.pinch_distance:4.2f}  spread {features.index_middle_spread:4.1f}")

        scale = self._config.font_scale * 0.85
        line_h = int(22 * scale / 0.55)
        y1 = self._BAR_HEIGHT + 8
        max_y2 = frame.shape[0] - self._STATUS_HEIGHT - 8

        # Truncate to the lines that actually fit between the bars instead
        # of drawing over the video and the status bar.
        max_lines = max(1, (max_y2 - y1 - 16) // line_h)
        if len(lines) > max_lines:
            lines = lines[: max_lines - 1] + ["..."]

        panel_h = line_h * len(lines) + 16
        self._blend_panel(frame, 8, y1, 320, y1 + panel_h, theme)
        y = y1 + line_h
        for line in lines:
            cv2.putText(frame, line, (16, y), _FONT, scale, theme.text_primary, 1, cv2.LINE_AA)
            y += line_h

    # ------------------------------------------------------------------
    def _camera_banner(self, frame: np.ndarray, state: HudState, theme: Theme) -> None:
        height, width = frame.shape[:2]
        message = {
            CameraStatus.STARTING: "Starting camera...",
            CameraStatus.RECONNECTING: "Camera lost - reconnecting...",
            CameraStatus.FAILED: "Camera failed - check device and permissions",
            CameraStatus.STOPPED: "Camera stopped",
        }.get(state.camera_status, "Camera unavailable")
        scale = self._config.font_scale * 1.4
        (text_w, text_h), _ = cv2.getTextSize(message, _FONT, scale, 2)
        x = (width - text_w) // 2
        y = height // 2
        self._blend_panel(frame, x - 20, y - text_h - 16, x + text_w + 20, y + 16, theme)
        cv2.putText(frame, message, (x, y), _FONT, scale, theme.warning, 2, cv2.LINE_AA)
