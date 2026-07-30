"""Semantic gesture identifiers.

The GestureSense library reports human-readable display names ("Thumbs Up",
"Peace / Victory"). Events on the bus carry stable, machine-friendly
``snake_case`` identifiers instead, so downstream consumers (intent tables,
plugins, persisted user mappings) never break when a display name is
reworded. This mapping is the *only* place the two vocabularies meet.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

#: GestureSense display name → semantic identifier.
SEMANTIC_IDS: dict[str, str] = {
    "Thumbs Up": "thumbs_up",
    "Thumbs Down": "thumbs_down",
    "Open Palm": "open_palm",
    "Closed Fist": "closed_fist",
    "Peace / Victory": "peace",
    "OK Sign": "ok",
    "Pointing Up": "pointing_up",
    "Pointing Down": "pointing_down",
    "Pointing Left": "pointing_left",
    "Pointing Right": "pointing_right",
    "Rock Sign": "rock",
    "Love Sign (ILY)": "i_love_you",
    "Call Me": "call_me",
    "Finger Gun": "finger_gun",
}

#: Alternate names that resolve to an existing identifier. "High Five" is
#: physically an open palm; "victory" is the same hand shape as "peace".
ALIASES: dict[str, str] = {
    "high_five": "open_palm",
    "victory": "peace",
}

_unmapped_warned: set[str] = set()


def to_semantic(display_name: str) -> str:
    """Return the stable identifier for a GestureSense display name.

    Unknown names (e.g. a user-registered custom gesture the mapping does
    not know yet) are slugified deterministically and logged once, so new
    gestures flow through the system immediately instead of being dropped.
    """
    identifier = SEMANTIC_IDS.get(display_name)
    if identifier is not None:
        return identifier
    slug = re.sub(r"[^a-z0-9]+", "_", display_name.lower()).strip("_")
    if display_name not in _unmapped_warned:
        _unmapped_warned.add(display_name)
        logger.warning(
            "No semantic id registered for gesture %r; using slug %r",
            display_name,
            slug,
        )
    return slug


def resolve(identifier: str) -> str:
    """Resolve aliases (``high_five`` → ``open_palm``); identity otherwise."""
    return ALIASES.get(identifier, identifier)
