"""Hand overlay rendering: skeleton, landmarks, bounding box and labels.

Drawing happens directly on the frame owned by the render loop (the capture
thread hands over a fresh array every frame), so no defensive copies are
needed and per-frame allocations stay minimal.
"""

from __future__ import annotations

import cv2
import numpy as np

from gesturesense.config.settings import UiConfig
from gesturesense.gesture import topology as topo
from gesturesense.gesture.engine import RecognizedGesture
from gesturesense.ui.theme import Theme
from gesturesense.vision.hand_tracker import HandObservation

_FONT = cv2.FONT_HERSHEY_SIMPLEX


class HandRenderer:
    """Draws per-hand overlays onto BGR frames."""

    def __init__(self, config: UiConfig):
        self._config = config

    def draw_hand(
        self,
        frame: np.ndarray,
        hand: HandObservation,
        gesture: RecognizedGesture | None,
        theme: Theme,
    ) -> None:
        """Render one hand's skeleton, box and label chip onto ``frame``."""
        height, width = frame.shape[:2]
        points = np.empty((topo.NUM_LANDMARKS, 2), dtype=np.int32)
        points[:, 0] = np.clip(hand.landmarks[:, 0] * width, 0, width - 1)
        points[:, 1] = np.clip(hand.landmarks[:, 1] * height, 0, height - 1)

        if self._config.show_landmarks:
            self._draw_skeleton(frame, points, theme)
        if self._config.show_bounding_box:
            self._draw_bbox_and_label(frame, points, hand, gesture, theme)

    # ------------------------------------------------------------------
    def _draw_skeleton(
        self, frame: np.ndarray, points: np.ndarray, theme: Theme
    ) -> None:
        for start, end in topo.HAND_CONNECTIONS:
            cv2.line(
                frame,
                tuple(points[start]),
                tuple(points[end]),
                theme.connection,
                2,
                cv2.LINE_AA,
            )
        for index in range(topo.NUM_LANDMARKS):
            color = theme.finger(topo.LANDMARK_FINGER[index])
            radius = 6 if index in (topo.WRIST,) else 4
            cv2.circle(frame, tuple(points[index]), radius, color, -1, cv2.LINE_AA)
            cv2.circle(frame, tuple(points[index]), radius, (30, 30, 30), 1, cv2.LINE_AA)

    def _draw_bbox_and_label(
        self,
        frame: np.ndarray,
        points: np.ndarray,
        hand: HandObservation,
        gesture: RecognizedGesture | None,
        theme: Theme,
    ) -> None:
        margin = 14
        x_min = max(int(points[:, 0].min()) - margin, 0)
        y_min = max(int(points[:, 1].min()) - margin, 0)
        x_max = min(int(points[:, 0].max()) + margin, frame.shape[1] - 1)
        y_max = min(int(points[:, 1].max()) + margin, frame.shape[0] - 1)

        color = theme.success if gesture else theme.bbox
        self._corner_box(frame, (x_min, y_min), (x_max, y_max), color)

        if gesture:
            label = f"{hand.handedness} | {gesture.name}  {gesture.confidence:.0%}"
        else:
            label = f"{hand.handedness} hand"
        self._label_chip(frame, label, x_min, y_min, color, theme)

    @staticmethod
    def _corner_box(
        frame: np.ndarray,
        top_left: tuple[int, int],
        bottom_right: tuple[int, int],
        color: tuple[int, int, int],
    ) -> None:
        """Corner-bracket bounding box — lighter visual weight than a full rect."""
        x1, y1 = top_left
        x2, y2 = bottom_right
        length = max(12, min(x2 - x1, y2 - y1) // 5)
        thickness = 2
        for cx, cy, dx, dy in (
            (x1, y1, 1, 1),
            (x2, y1, -1, 1),
            (x1, y2, 1, -1),
            (x2, y2, -1, -1),
        ):
            cv2.line(frame, (cx, cy), (cx + dx * length, cy), color, thickness, cv2.LINE_AA)
            cv2.line(frame, (cx, cy), (cx, cy + dy * length), color, thickness, cv2.LINE_AA)

    def _label_chip(
        self,
        frame: np.ndarray,
        text: str,
        x: int,
        y: int,
        color: tuple[int, int, int],
        theme: Theme,
    ) -> None:
        scale = self._config.font_scale
        (text_w, text_h), baseline = cv2.getTextSize(text, _FONT, scale, 1)
        pad = 6
        chip_y2 = max(y - 8, text_h + 2 * pad)
        chip_y1 = chip_y2 - text_h - 2 * pad
        chip_x2 = min(x + text_w + 2 * pad, frame.shape[1] - 1)

        roi = frame[chip_y1:chip_y2, x:chip_x2]
        if roi.size:
            overlay = np.full_like(roi, theme.panel)
            cv2.addWeighted(overlay, 0.8, roi, 0.2, 0, dst=roi)
        cv2.putText(
            frame,
            text,
            (x + pad, chip_y2 - pad - baseline // 2),
            _FONT,
            scale,
            color,
            1,
            cv2.LINE_AA,
        )
