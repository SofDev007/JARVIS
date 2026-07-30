"""Active-window probes: who has focus right now, cross-platform.

A probe answers one question — the title and process of the focused
window — behind one tiny interface, so the context module never contains a
line of platform code. Backends:

* **Linux/X11** — ``xdotool`` (title + pid, process from ``/proc``).
  Wayland compositors generally do not expose the active window to
  arbitrary processes by design; unsupported for now.
* **Windows** — ``ctypes`` against ``user32``/``kernel32``; process name
  via ``psutil`` when present, else empty.
* **macOS** — ``osascript`` asking System Events for the frontmost app.

``create_window_probe`` picks the first available backend and raises a
``RuntimeError`` with actionable guidance when none is usable — the module
then FAILS visibly at start (and the rest of the assistant keeps running),
rather than silently publishing nothing.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_SUBPROCESS_TIMEOUT_S = 1.0


@dataclass(frozen=True)
class WindowInfo:
    """The focused window at one instant."""

    title: str
    process: str

    @property
    def haystack(self) -> str:
        """Lower-cased searchable text for rule matching."""
        return f"{self.title} {self.process}".lower()


class WindowProbe(ABC):
    """Backend interface: report the currently focused window."""

    name: str = "abstract"

    @classmethod
    @abstractmethod
    def available(cls) -> bool:
        """Whether this backend can work in the current environment."""

    @abstractmethod
    def active_window(self) -> WindowInfo | None:
        """Focused window, or ``None`` if it cannot be determined right now
        (transient failures must return ``None``, never raise)."""


def _run(command: list[str]) -> str | None:
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_S
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


class LinuxXdotoolProbe(WindowProbe):
    """X11 active window via ``xdotool``; process name from ``/proc``."""

    name = "xdotool"

    @classmethod
    def available(cls) -> bool:
        return (
            sys.platform.startswith("linux")
            and bool(os.environ.get("DISPLAY"))
            and shutil.which("xdotool") is not None
        )

    def active_window(self) -> WindowInfo | None:
        window_id = _run(["xdotool", "getactivewindow"])
        if not window_id:
            return None
        title = _run(["xdotool", "getwindowname", window_id]) or ""
        process = ""
        pid = _run(["xdotool", "getwindowpid", window_id])
        if pid and pid.isdigit():
            try:
                process = Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
            except OSError:
                process = ""
        if not title and not process:
            return None
        return WindowInfo(title=title, process=process)


class WindowsProbe(WindowProbe):
    """Win32 foreground window via ``ctypes``."""

    name = "win32"

    @classmethod
    def available(cls) -> bool:
        return sys.platform == "win32"

    def active_window(self) -> WindowInfo | None:
        try:
            import ctypes

            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            handle = user32.GetForegroundWindow()
            if not handle:
                return None
            length = user32.GetWindowTextLengthW(handle)
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(handle, buffer, length + 1)
            title = buffer.value

            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
            process = ""
            try:
                import psutil

                process = psutil.Process(pid.value).name()
            except Exception:
                process = ""
            if not title and not process:
                return None
            return WindowInfo(title=title, process=process)
        except Exception:
            return None


class MacProbe(WindowProbe):
    """Frontmost application via ``osascript`` (System Events)."""

    name = "osascript"

    _SCRIPT = (
        'tell application "System Events" to get name of first application '
        "process whose frontmost is true"
    )

    @classmethod
    def available(cls) -> bool:
        return sys.platform == "darwin" and shutil.which("osascript") is not None

    def active_window(self) -> WindowInfo | None:
        app = _run(["osascript", "-e", self._SCRIPT])
        if not app:
            return None
        return WindowInfo(title=app, process=app)


_BACKENDS: tuple[type[WindowProbe], ...] = (
    LinuxXdotoolProbe,
    WindowsProbe,
    MacProbe,
)


def create_window_probe() -> WindowProbe:
    """Return the first usable backend or raise with actionable guidance."""
    for backend in _BACKENDS:
        try:
            usable = backend.available()
        except Exception:
            usable = False
        if usable:
            logger.info("Screen-context probe: %s", backend.name)
            return backend()
    raise RuntimeError(
        "No active-window probe available. On Linux/X11 install xdotool "
        "(sudo apt install xdotool) and ensure DISPLAY is set; Wayland is "
        "not supported yet. On macOS ensure osascript is present. "
        "Alternatively disable context_perception or use --context."
    )
