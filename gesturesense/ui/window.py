"""Window lifecycle and keyboard handling for the OpenCV display surface."""

from __future__ import annotations

import logging
from enum import Enum

import cv2
import numpy as np

logger = logging.getLogger(__name__)


class KeyAction(Enum):
    """Semantic actions mapped from raw key codes."""

    NONE = "none"
    QUIT = "quit"
    TOGGLE_FULLSCREEN = "fullscreen"
    TOGGLE_DEBUG = "debug"
    TOGGLE_MIRROR = "mirror"
    TOGGLE_THEME = "theme"
    SCREENSHOT = "screenshot"


_KEYMAP: dict[int, KeyAction] = {
    ord("q"): KeyAction.QUIT,
    27: KeyAction.QUIT,  # ESC
    ord("f"): KeyAction.TOGGLE_FULLSCREEN,
    ord("d"): KeyAction.TOGGLE_DEBUG,
    ord("m"): KeyAction.TOGGLE_MIRROR,
    ord("t"): KeyAction.TOGGLE_THEME,
    ord("s"): KeyAction.SCREENSHOT,
}


class AppWindow:
    """Thin wrapper around the OpenCV HighGUI window."""

    def __init__(self, title: str, fullscreen: bool = False):
        self._title = title
        self._fullscreen = fullscreen
        cv2.namedWindow(self._title, cv2.WINDOW_NORMAL)
        if fullscreen:
            self._apply_fullscreen(True)

    def show(self, frame: np.ndarray) -> KeyAction:
        """Display ``frame`` and return the decoded key action.

        ``cv2.waitKey(1)`` doubles as the HighGUI event pump; without it the
        window never repaints.
        """
        cv2.imshow(self._title, frame)
        key = cv2.waitKey(1) & 0xFF
        if key == 0xFF:
            if self._was_closed():
                return KeyAction.QUIT
            return KeyAction.NONE
        return _KEYMAP.get(key, KeyAction.NONE)

    def toggle_fullscreen(self) -> bool:
        """Flip fullscreen state; returns the new state."""
        self._fullscreen = not self._fullscreen
        self._apply_fullscreen(self._fullscreen)
        return self._fullscreen

    def _apply_fullscreen(self, enabled: bool) -> None:
        cv2.setWindowProperty(
            self._title,
            cv2.WND_PROP_FULLSCREEN,
            cv2.WINDOW_FULLSCREEN if enabled else cv2.WINDOW_NORMAL,
        )

    def _was_closed(self) -> bool:
        try:
            return cv2.getWindowProperty(self._title, cv2.WND_PROP_VISIBLE) < 1
        except cv2.error:  # pragma: no cover - window already destroyed
            return True

    def close(self) -> None:
        """Destroy the window (safe to call twice)."""
        try:
            cv2.destroyWindow(self._title)
        except cv2.error:  # pragma: no cover
            pass
        logger.info("Window closed")
