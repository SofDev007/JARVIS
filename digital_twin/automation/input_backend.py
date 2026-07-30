"""Input-synthesis backends: pressing keys, typing, clipboard, window focus.

One small interface (:class:`InputBackend`) hides the platform mess from
the action layer, exactly like the window probes in M4:

* **xdotool** (Linux/X11) — primary backend; no extra Python deps, pairs
  with the M4 context probe.
* **pynput** (cross-platform, optional dependency) — keyboard synthesis on
  Windows/macOS; clipboard via platform commands (``pbcopy``/``clip``),
  window focus via ``osascript`` (macOS) or Win32 ``EnumWindows``.

The action layer speaks only **canonical key names** (lower-case:
``ctrl``, ``right``, ``play_pause``, single alphanumerics …) which each
backend maps to its native vocabulary. Anything outside the canonical set
is rejected *before* the permission gate — there is no raw-keysym
injection path.

Backends raise :class:`InputBackendError` on failure; the dispatcher turns
that into an audited ``failed`` result with the message as detail.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod
from importlib.util import find_spec

logger = logging.getLogger(__name__)

_SUBPROCESS_TIMEOUT_S = 3.0


class InputBackendError(RuntimeError):
    """An input operation could not be performed."""


# ---------------------------------------------------------------------------
# Canonical key model
# ---------------------------------------------------------------------------
MODIFIER_KEYS = frozenset({"ctrl", "alt", "shift", "super"})

NAVIGATION_KEYS = frozenset({"left", "right", "up", "down"})

NAMED_KEYS = frozenset(
    {
        *NAVIGATION_KEYS,
        "enter", "escape", "space", "tab", "backspace", "delete",
        "home", "end", "page_up", "page_down",
        *(f"f{i}" for i in range(1, 13)),
    }
)

MEDIA_KEYS = frozenset(
    {"play_pause", "next_track", "prev_track", "volume_up", "volume_down", "mute"}
)


def is_valid_key(key: str) -> bool:
    """Whether ``key`` belongs to the canonical vocabulary."""
    if not isinstance(key, str):
        return False
    return (
        key in MODIFIER_KEYS
        or key in NAMED_KEYS
        or key in MEDIA_KEYS
        or (len(key) == 1 and key.isalnum())
    )


class InputBackend(ABC):
    """Backend interface for synthesising user input."""

    name: str = "abstract"

    @classmethod
    @abstractmethod
    def available(cls) -> bool:
        """Whether this backend can work in the current environment."""

    @abstractmethod
    def press_keys(self, keys: list[str]) -> None:
        """Press one chord of canonical keys (modifiers first)."""

    @abstractmethod
    def type_text(self, text: str) -> None:
        """Type literal text into the focused window."""

    @abstractmethod
    def set_clipboard(self, text: str) -> None:
        """Replace the clipboard contents with ``text``."""

    @abstractmethod
    def focus_window(self, title_substring: str) -> bool:
        """Focus the first window whose title contains the substring
        (case-insensitive); ``False`` if none matched."""


# ---------------------------------------------------------------------------
# xdotool backend (Linux/X11)
# ---------------------------------------------------------------------------
_XDOTOOL_KEYS = {
    "ctrl": "ctrl", "alt": "alt", "shift": "shift", "super": "super",
    "left": "Left", "right": "Right", "up": "Up", "down": "Down",
    "enter": "Return", "escape": "Escape", "space": "space", "tab": "Tab",
    "backspace": "BackSpace", "delete": "Delete",
    "home": "Home", "end": "End", "page_up": "Page_Up", "page_down": "Page_Down",
    "play_pause": "XF86AudioPlay", "next_track": "XF86AudioNext",
    "prev_track": "XF86AudioPrev", "volume_up": "XF86AudioRaiseVolume",
    "volume_down": "XF86AudioLowerVolume", "mute": "XF86AudioMute",
    **{f"f{i}": f"F{i}" for i in range(1, 13)},
}


def _run(command: list[str], input_text: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InputBackendError(f"{command[0]} failed: {exc}") from exc


class XdotoolBackend(InputBackend):
    """X11 input synthesis via xdotool; clipboard via xclip/xsel."""

    name = "xdotool"

    @classmethod
    def available(cls) -> bool:
        return (
            sys.platform.startswith("linux")
            and bool(os.environ.get("DISPLAY"))
            and shutil.which("xdotool") is not None
        )

    # ------------------------------------------------------------------
    def press_keys(self, keys: list[str]) -> None:
        chord = "+".join(_XDOTOOL_KEYS.get(key, key) for key in keys)
        result = _run(["xdotool", "key", "--clearmodifiers", chord])
        if result.returncode != 0:
            raise InputBackendError(
                f"xdotool key {chord!r} failed: {result.stderr.strip()}"
            )

    def type_text(self, text: str) -> None:
        # "--" guards against text starting with a dash; a small delay
        # keeps slow applications from dropping characters.
        result = _run(["xdotool", "type", "--delay", "12", "--", text])
        if result.returncode != 0:
            raise InputBackendError(f"xdotool type failed: {result.stderr.strip()}")

    def set_clipboard(self, text: str) -> None:
        if shutil.which("xclip"):
            command = ["xclip", "-selection", "clipboard"]
        elif shutil.which("xsel"):
            command = ["xsel", "--clipboard", "--input"]
        else:
            raise InputBackendError(
                "Clipboard needs xclip or xsel (sudo apt install xclip)"
            )
        result = _run(command, input_text=text)
        if result.returncode != 0:
            raise InputBackendError(
                f"clipboard write failed: {result.stderr.strip()}"
            )

    def focus_window(self, title_substring: str) -> bool:
        search = _run(["xdotool", "search", "--name", title_substring])
        window_ids = [line for line in search.stdout.split() if line.strip()]
        if search.returncode != 0 or not window_ids:
            return False
        result = _run(["xdotool", "windowactivate", "--sync", window_ids[0]])
        return result.returncode == 0


# ---------------------------------------------------------------------------
# pynput backend (cross-platform keyboard; optional dependency)
# ---------------------------------------------------------------------------
class PynputBackend(InputBackend):
    """Keyboard via pynput; clipboard/focus via platform helpers."""

    name = "pynput"

    def __init__(self) -> None:
        self._controller = None  # created lazily on first use

    @classmethod
    def available(cls) -> bool:
        if find_spec("pynput") is None:
            return False
        if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
            return False  # pynput's X backend needs a display
        return True

    # ------------------------------------------------------------------
    def _keyboard(self):
        if self._controller is None:
            try:
                from pynput.keyboard import Controller

                self._controller = Controller()
            except Exception as exc:  # e.g. no display after all
                raise InputBackendError(f"pynput unavailable: {exc}") from exc
        return self._controller

    def _to_pynput(self, key: str):
        from pynput.keyboard import Key

        mapping = {
            "ctrl": Key.ctrl, "alt": Key.alt, "shift": Key.shift,
            "super": Key.cmd, "left": Key.left, "right": Key.right,
            "up": Key.up, "down": Key.down, "enter": Key.enter,
            "escape": Key.esc, "space": Key.space, "tab": Key.tab,
            "backspace": Key.backspace, "delete": Key.delete,
            "home": Key.home, "end": Key.end,
            "page_up": Key.page_up, "page_down": Key.page_down,
            "play_pause": getattr(Key, "media_play_pause", None),
            "next_track": getattr(Key, "media_next", None),
            "prev_track": getattr(Key, "media_previous", None),
            "volume_up": getattr(Key, "media_volume_up", None),
            "volume_down": getattr(Key, "media_volume_down", None),
            "mute": getattr(Key, "media_volume_mute", None),
            **{f"f{i}": getattr(Key, f"f{i}") for i in range(1, 13)},
        }
        if key in mapping:
            resolved = mapping[key]
            if resolved is None:
                raise InputBackendError(f"{key!r} unsupported by pynput here")
            return resolved
        return key  # single character

    def press_keys(self, keys: list[str]) -> None:
        keyboard = self._keyboard()
        resolved = [self._to_pynput(key) for key in keys]
        modifiers, final = resolved[:-1], resolved[-1]
        try:
            for modifier in modifiers:
                keyboard.press(modifier)
            keyboard.press(final)
            keyboard.release(final)
        finally:
            for modifier in reversed(modifiers):
                keyboard.release(modifier)

    def type_text(self, text: str) -> None:
        self._keyboard().type(text)

    def set_clipboard(self, text: str) -> None:
        if sys.platform == "darwin" and shutil.which("pbcopy"):
            command = ["pbcopy"]
        elif sys.platform == "win32":
            command = ["clip"]
        elif shutil.which("xclip"):
            command = ["xclip", "-selection", "clipboard"]
        else:
            raise InputBackendError("No clipboard helper available")
        result = _run(command, input_text=text)
        if result.returncode != 0:
            raise InputBackendError(f"clipboard write failed: {result.stderr.strip()}")

    def focus_window(self, title_substring: str) -> bool:
        if sys.platform == "darwin":
            script = (
                'tell application "System Events" to set frontmost of first '
                f'application process whose name contains "{title_substring}" to true'
            )
            return _run(["osascript", "-e", script]).returncode == 0
        if sys.platform == "win32":
            return self._focus_window_win32(title_substring)
        raise InputBackendError(
            "focus_window on Linux needs the xdotool backend"
        )

    @staticmethod
    def _focus_window_win32(title_substring: str) -> bool:
        import ctypes
        import ctypes.wintypes as wintypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        needle = title_substring.lower()
        matches: list[int] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def visit(handle, _lparam):
            if user32.IsWindowVisible(handle):
                length = user32.GetWindowTextLengthW(handle)
                if length:
                    buffer = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(handle, buffer, length + 1)
                    if needle in buffer.value.lower():
                        matches.append(handle)
                        return False  # stop enumeration
            return True

        user32.EnumWindows(visit, 0)
        if not matches:
            return False
        return bool(user32.SetForegroundWindow(matches[0]))


# ---------------------------------------------------------------------------
_BACKENDS: tuple[type[InputBackend], ...] = (XdotoolBackend, PynputBackend)


def create_input_backend(preference: str = "auto") -> InputBackend:
    """Instantiate the configured backend or raise with install guidance."""
    if preference != "auto":
        for backend in _BACKENDS:
            if backend.name == preference:
                if backend.available():
                    logger.info("Input backend: %s", backend.name)
                    return backend()
                raise InputBackendError(
                    f"Configured input backend {preference!r} is not available "
                    "in this environment"
                )
        raise InputBackendError(f"Unknown input backend {preference!r}")

    for backend in _BACKENDS:
        try:
            usable = backend.available()
        except Exception:
            usable = False
        if usable:
            logger.info("Input backend: %s", backend.name)
            return backend()
    raise InputBackendError(
        "No input backend available. On Linux/X11 install xdotool "
        "(sudo apt install xdotool); on Windows/macOS install pynput "
        "(pip install pynput)."
    )
