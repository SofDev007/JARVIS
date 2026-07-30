"""Screenshot capture backends: one frame, on demand, dependency-light.

Follows the M4 window-probe pattern exactly: each backend is a tiny class
behind one interface, ``create_screen_capturer`` picks the first usable one
(or honours an explicit preference) and raises with actionable guidance
when none is. All backends are subprocess-based — no new Python
dependencies — and write a PNG to a caller-provided path. The caller (the
screen module) owns that file's lifetime and deletes it immediately after
OCR: screenshots never linger on disk.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod
from pathlib import Path

logger = logging.getLogger(__name__)


class ScreenCaptureError(RuntimeError):
    """A capture attempt failed (backend missing output, timeout, …)."""


class ScreenCapturer(ABC):
    """Backend interface: write one full-screen PNG to ``destination``."""

    name: str = "abstract"

    @classmethod
    @abstractmethod
    def available(cls) -> bool:
        """Whether this backend can work in the current environment."""

    @abstractmethod
    def capture(self, destination: Path, timeout_s: float) -> None:
        """Capture the screen to ``destination`` or raise
        :class:`ScreenCaptureError`."""


def _run_capture(command: list[str], destination: Path, timeout_s: float) -> None:
    """Run one capture command and verify it actually produced a file."""
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout_s
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ScreenCaptureError(f"{command[0]} failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:200]
        raise ScreenCaptureError(
            f"{command[0]} exited {result.returncode}: {detail or '(no output)'}"
        )
    if not destination.exists() or destination.stat().st_size == 0:
        raise ScreenCaptureError(f"{command[0]} produced no screenshot file")


def _linux_display() -> bool:
    return sys.platform.startswith("linux") and bool(os.environ.get("DISPLAY"))


class ScrotCapture(ScreenCapturer):
    """Linux/X11 via ``scrot`` (``-z`` silent, ``-o`` overwrite)."""

    name = "scrot"

    @classmethod
    def available(cls) -> bool:
        return _linux_display() and shutil.which("scrot") is not None

    def capture(self, destination: Path, timeout_s: float) -> None:
        _run_capture(["scrot", "-z", "-o", str(destination)],
                     destination, timeout_s)


class ImageMagickCapture(ScreenCapturer):
    """Linux/X11 via ImageMagick ``import -window root``."""

    name = "imagemagick"

    @classmethod
    def available(cls) -> bool:
        return _linux_display() and shutil.which("import") is not None

    def capture(self, destination: Path, timeout_s: float) -> None:
        _run_capture(["import", "-window", "root", str(destination)],
                     destination, timeout_s)


class GnomeScreenshotCapture(ScreenCapturer):
    """Linux via ``gnome-screenshot`` (works on some Wayland sessions too)."""

    name = "gnome-screenshot"

    @classmethod
    def available(cls) -> bool:
        return (sys.platform.startswith("linux")
                and shutil.which("gnome-screenshot") is not None)

    def capture(self, destination: Path, timeout_s: float) -> None:
        _run_capture(["gnome-screenshot", "-f", str(destination)],
                     destination, timeout_s)


class MacScreencapture(ScreenCapturer):
    """macOS built-in ``screencapture`` (``-x``: no shutter sound)."""

    name = "screencapture"

    @classmethod
    def available(cls) -> bool:
        return sys.platform == "darwin" and shutil.which("screencapture") is not None

    def capture(self, destination: Path, timeout_s: float) -> None:
        _run_capture(["screencapture", "-x", str(destination)],
                     destination, timeout_s)


class WindowsPowerShellCapture(ScreenCapturer):
    """Windows via a PowerShell ``CopyFromScreen`` snippet (no extra deps)."""

    name = "powershell"

    _SCRIPT = (
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
        "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen; "
        "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height; "
        "$g=[System.Drawing.Graphics]::FromImage($bmp); "
        "$g.CopyFromScreen($b.Left,$b.Top,0,0,$bmp.Size); "
        "$bmp.Save('{dest}'); $g.Dispose(); $bmp.Dispose()"
    )

    @classmethod
    def available(cls) -> bool:
        return sys.platform == "win32"

    def capture(self, destination: Path, timeout_s: float) -> None:
        script = self._SCRIPT.format(dest=str(destination).replace("'", "''"))
        _run_capture(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            destination, timeout_s,
        )


class ScriptedCapturer(ScreenCapturer):
    """Test double: 'captures' by writing canned bytes; records calls."""

    name = "scripted"

    def __init__(self, payload: bytes = b"fake-png", fail: bool = False):
        self._payload = payload
        self._fail = fail
        self.captures: list[Path] = []

    @classmethod
    def available(cls) -> bool:
        return True

    def capture(self, destination: Path, timeout_s: float) -> None:
        self.captures.append(destination)
        if self._fail:
            raise ScreenCaptureError("scripted failure")
        destination.write_bytes(self._payload)


_BACKENDS: tuple[type[ScreenCapturer], ...] = (
    ScrotCapture,
    ImageMagickCapture,
    GnomeScreenshotCapture,
    MacScreencapture,
    WindowsPowerShellCapture,
)

_BY_NAME = {backend.name: backend for backend in _BACKENDS}


def create_screen_capturer(preference: str = "auto") -> ScreenCapturer:
    """Return the preferred/first usable backend or raise with guidance."""
    candidates: tuple[type[ScreenCapturer], ...]
    if preference != "auto":
        backend = _BY_NAME.get(preference)
        if backend is None:
            raise ScreenCaptureError(
                f"Unknown capture backend {preference!r}; "
                f"choose one of {sorted(_BY_NAME)} or 'auto'"
            )
        candidates = (backend,)
    else:
        candidates = _BACKENDS
    for backend in candidates:
        try:
            usable = backend.available()
        except Exception:
            usable = False
        if usable:
            logger.info("Screen capture backend: %s", backend.name)
            return backend()
    raise ScreenCaptureError(
        "No screenshot backend available. On Linux/X11 install scrot "
        "(sudo apt install scrot) or ImageMagick and ensure DISPLAY is set; "
        "on GNOME, gnome-screenshot also works. macOS uses the built-in "
        "screencapture; Windows uses PowerShell. Alternatively disable "
        "screen_reading in the configuration."
    )
