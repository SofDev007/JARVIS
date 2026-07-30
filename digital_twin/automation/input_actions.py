"""Input actions: keyboard, typing, clipboard and window focus — risk-stratified.

The risk split is the design here, because it decides the UX:

* ``nav_key`` (arrow keys only) and ``media_key`` are **SAFE** — a stray
  Right-arrow or play/pause is provably benign, so a thumbs-up can advance
  a slide *without a confirmation prompt every three seconds*.
* ``press_keys`` (arbitrary chords), ``type_text``, ``set_clipboard`` and
  ``focus_window`` are **SENSITIVE** — confirmed by default, allow-able
  per action via ``security.permissions`` for users who accept the risk.

Hard limits enforced *before* the permission gate:

* Only canonical key names; chords are ``modifier* + one key``; max 4 keys;
  media keys are not expressible through ``press_keys`` (use ``media_key``).
* ``type_text`` rejects newlines and control characters outright — typed
  text can never carry its own Enter, so it cannot execute anything in a
  focused terminal. Pressing Enter is a separate, separately-gated action.
* Repeat counts are capped at 10 per event.

The backend resolves lazily on first use: the kernel starts everywhere,
and on machines without xdotool/pynput these actions fail as audited
``failed`` results with install guidance — never at startup.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Mapping

from digital_twin.automation.registry import ActionRegistry, ActionSpec
from digital_twin.automation.input_backend import (
    InputBackend,
    InputBackendError,
    MEDIA_KEYS,
    MODIFIER_KEYS,
    NAVIGATION_KEYS,
    create_input_backend,
    is_valid_key,
)
from digital_twin.security.permissions import RiskLevel

logger = logging.getLogger(__name__)

MAX_CHORD_KEYS = 4
MAX_REPEAT = 10


class _BackendResolver:
    """Creates the backend once, on first use, thread-safely."""

    def __init__(self, factory: Callable[[], InputBackend]):
        self._factory = factory
        self._backend: InputBackend | None = None
        self._lock = threading.Lock()

    def get(self) -> InputBackend:
        with self._lock:
            if self._backend is None:
                self._backend = self._factory()
            return self._backend


def _repeat_of(params: Mapping[str, Any]) -> int:
    return int(params.get("repeat", 1))


def _validate_repeat(params: Mapping[str, Any]) -> None:
    repeat = params.get("repeat", 1)
    if not isinstance(repeat, int) or isinstance(repeat, bool) \
            or not 1 <= repeat <= MAX_REPEAT:
        raise ValueError(f"'repeat' must be an integer in 1..{MAX_REPEAT}")


# ---------------------------------------------------------------------------
def register_input_actions(
    registry: ActionRegistry,
    backend_factory: Callable[[], InputBackend] | None = None,
    max_type_text_chars: int = 500,
) -> None:
    """Register the input-synthesis actions onto ``registry``."""
    resolver = _BackendResolver(backend_factory or create_input_backend)

    # -- nav_key (SAFE) -------------------------------------------------
    def validate_nav_key(params: Mapping[str, Any]) -> None:
        if params.get("key") not in NAVIGATION_KEYS:
            raise ValueError(
                f"nav_key: 'key' must be one of {sorted(NAVIGATION_KEYS)}"
            )
        _validate_repeat(params)

    def handle_nav_key(params: Mapping[str, Any]) -> str:
        key, repeat = str(params["key"]), _repeat_of(params)
        backend = resolver.get()
        for _ in range(repeat):
            backend.press_keys([key])
        return f"pressed {key} x{repeat}"

    registry.register(ActionSpec(
        name="nav_key",
        description="Press an arrow key (bounded, non-destructive).",
        risk=RiskLevel.SAFE,
        handler=handle_nav_key,
        validate=validate_nav_key,
    ))

    # -- media_key (SAFE) -----------------------------------------------
    def validate_media_key(params: Mapping[str, Any]) -> None:
        if params.get("key") not in MEDIA_KEYS:
            raise ValueError(f"media_key: 'key' must be one of {sorted(MEDIA_KEYS)}")
        _validate_repeat(params)

    def handle_media_key(params: Mapping[str, Any]) -> str:
        key, repeat = str(params["key"]), _repeat_of(params)
        backend = resolver.get()
        for _ in range(repeat):
            backend.press_keys([key])
        return f"pressed {key} x{repeat}"

    registry.register(ActionSpec(
        name="media_key",
        description="Press a media key (play/pause, tracks, volume, mute).",
        risk=RiskLevel.SAFE,
        handler=handle_media_key,
        validate=validate_media_key,
    ))

    # -- press_keys (SENSITIVE) ------------------------------------------
    def validate_press_keys(params: Mapping[str, Any]) -> None:
        keys = params.get("keys")
        if not isinstance(keys, list) or not 1 <= len(keys) <= MAX_CHORD_KEYS:
            raise ValueError(
                f"press_keys: 'keys' must be a list of 1..{MAX_CHORD_KEYS} keys"
            )
        for key in keys:
            if not is_valid_key(key):
                raise ValueError(f"press_keys: invalid key {key!r}")
            if key in MEDIA_KEYS:
                raise ValueError("press_keys: use the media_key action for media keys")
        for key in keys[:-1]:
            if key not in MODIFIER_KEYS:
                raise ValueError(
                    "press_keys: chords must be modifiers followed by one key"
                )
        if keys[-1] in MODIFIER_KEYS:
            raise ValueError("press_keys: a chord cannot end on a modifier")
        _validate_repeat(params)

    def handle_press_keys(params: Mapping[str, Any]) -> str:
        keys = [str(key) for key in params["keys"]]
        repeat = _repeat_of(params)
        backend = resolver.get()
        for _ in range(repeat):
            backend.press_keys(keys)
        return f"pressed {'+'.join(keys)} x{repeat}"

    registry.register(ActionSpec(
        name="press_keys",
        description="Press a keyboard shortcut (modifier chord + key).",
        risk=RiskLevel.SENSITIVE,
        handler=handle_press_keys,
        validate=validate_press_keys,
    ))

    # -- type_text (SENSITIVE) --------------------------------------------
    def validate_type_text(params: Mapping[str, Any]) -> None:
        text = params.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError("type_text requires a non-empty 'text' string")
        if len(text) > max_type_text_chars:
            raise ValueError(
                f"type_text: text exceeds {max_type_text_chars} characters"
            )
        if any(ch in "\r\n" for ch in text):
            raise ValueError(
                "type_text: newlines are not allowed (press enter is a "
                "separate, separately-confirmed action)"
            )
        if any(ord(ch) < 32 and ch != "\t" for ch in text):
            raise ValueError("type_text: control characters are not allowed")

    def handle_type_text(params: Mapping[str, Any]) -> str:
        text = str(params["text"])
        resolver.get().type_text(text)
        return f"typed {len(text)} characters"

    registry.register(ActionSpec(
        name="type_text",
        description="Type literal text into the focused window (no newlines).",
        risk=RiskLevel.SENSITIVE,
        handler=handle_type_text,
        validate=validate_type_text,
    ))

    # -- set_clipboard (SENSITIVE) ------------------------------------------
    def validate_set_clipboard(params: Mapping[str, Any]) -> None:
        text = params.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError("set_clipboard requires a non-empty 'text' string")
        if len(text) > 10_000:
            raise ValueError("set_clipboard: text exceeds 10000 characters")

    def handle_set_clipboard(params: Mapping[str, Any]) -> str:
        text = str(params["text"])
        resolver.get().set_clipboard(text)
        return f"clipboard set ({len(text)} characters)"

    registry.register(ActionSpec(
        name="set_clipboard",
        description="Replace the clipboard contents with the given text.",
        risk=RiskLevel.SENSITIVE,
        handler=handle_set_clipboard,
        validate=validate_set_clipboard,
    ))

    # -- focus_window (SENSITIVE) --------------------------------------------
    def validate_focus_window(params: Mapping[str, Any]) -> None:
        title = params.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("focus_window requires a non-empty 'title' substring")
        if len(title) > 200:
            raise ValueError("focus_window: title substring too long")

    def handle_focus_window(params: Mapping[str, Any]) -> str:
        title = str(params["title"])
        if not resolver.get().focus_window(title):
            raise InputBackendError(f"no window matching {title!r}")
        return f"focused window matching {title!r}"

    registry.register(ActionSpec(
        name="focus_window",
        description="Focus the first window whose title contains the substring.",
        risk=RiskLevel.SENSITIVE,
        handler=handle_focus_window,
        validate=validate_focus_window,
    ))
