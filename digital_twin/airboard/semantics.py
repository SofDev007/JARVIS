"""Gesture id aliases.

The Airboard page emits stable ``snake_case`` gesture ids (``thumbs_up``,
``peace``, ... see ``static/gestures.js``). Intent tables may name a few
gestures by an alternate word; this maps those onto the canonical id.
"""

from __future__ import annotations

#: Alternate names that resolve to an existing identifier. "High Five" is
#: physically an open palm; "victory" is the same hand shape as "peace".
ALIASES: dict[str, str] = {
    "high_five": "open_palm",
    "victory": "peace",
}


def resolve(identifier: str) -> str:
    """Resolve aliases (``high_five`` → ``open_palm``); identity otherwise."""
    return ALIASES.get(identifier, identifier)
