"""Colour themes for the renderer and HUD.

All colours are BGR tuples (OpenCV convention). Two curated palettes ship by
default; adding a theme means adding one :class:`Theme` instance to
:data:`THEMES`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

Color = tuple[int, int, int]


@dataclass(frozen=True)
class Theme:
    """A complete colour palette for the interface."""

    name: str
    panel: Color
    panel_alpha: float
    text_primary: Color
    text_secondary: Color
    accent: Color
    success: Color
    warning: Color
    danger: Color
    bbox: Color
    connection: Color
    finger_colors: dict[str, Color] = field(default_factory=dict)

    def finger(self, finger: str) -> Color:
        """Colour for a finger's landmarks (falls back to accent)."""
        return self.finger_colors.get(finger, self.accent)


_DARK = Theme(
    name="dark",
    panel=(24, 20, 16),
    panel_alpha=0.72,
    text_primary=(245, 245, 245),
    text_secondary=(170, 170, 170),
    accent=(255, 179, 0),      # vivid azure
    success=(96, 200, 80),
    warning=(64, 180, 255),
    danger=(70, 70, 235),
    bbox=(255, 179, 0),
    connection=(200, 200, 200),
    finger_colors={
        "palm": (200, 200, 200),
        "thumb": (66, 132, 244),   # warm red-orange
        "index": (80, 200, 255),   # amber
        "middle": (96, 200, 80),   # green
        "ring": (255, 179, 0),     # azure
        "pinky": (240, 130, 200),  # violet-pink
    },
)

_LIGHT = Theme(
    name="light",
    panel=(246, 244, 240),
    panel_alpha=0.85,
    text_primary=(30, 30, 30),
    text_secondary=(110, 110, 110),
    accent=(200, 130, 0),
    success=(60, 160, 50),
    warning=(0, 140, 230),
    danger=(50, 50, 210),
    bbox=(200, 130, 0),
    connection=(90, 90, 90),
    finger_colors={
        "palm": (90, 90, 90),
        "thumb": (40, 100, 210),
        "index": (30, 160, 230),
        "middle": (60, 160, 50),
        "ring": (200, 130, 0),
        "pinky": (190, 90, 160),
    },
)

THEMES: dict[str, Theme] = {theme.name: theme for theme in (_DARK, _LIGHT)}


def get_theme(name: str) -> Theme:
    """Look up a theme, defaulting to dark for unknown names."""
    return THEMES.get(name, _DARK)


def next_theme(name: str) -> Theme:
    """Cycle to the next theme (used by the runtime toggle key)."""
    names = list(THEMES.keys())
    try:
        index = names.index(name)
    except ValueError:
        index = -1
    return THEMES[names[(index + 1) % len(names)]]
