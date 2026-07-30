"""Browser drivers: one interface, Playwright reference, scripted double.

The driver is replaceable exactly like the LLM, the transcriber and the
OCR engine: actions talk to :class:`BrowserDriver`, and which browser
actually runs is a composition decision. Playwright is a **heavy,
optional** dependency, so it is imported lazily on first use — the
kernel starts without it, and a triggered browser action fails as an
audited ``failed`` result carrying install guidance (the M5/M10 lazy
resolver pattern).

:class:`DriverHolder` owns the driver lifecycle: created on first action,
shared across actions (one browsing session), closed on process exit or
via the SAFE ``browser_close`` action.
"""

from __future__ import annotations

import atexit
import logging
import threading
from abc import ABC, abstractmethod
from typing import Callable

from digital_twin.configuration.settings import BrowserConfig

logger = logging.getLogger(__name__)


class BrowserError(RuntimeError):
    """Navigation failed, driver unavailable, or the page misbehaved."""


class BrowserDriver(ABC):
    """One live browsing session."""

    name: str = "abstract"

    @abstractmethod
    def open(self, url: str, timeout_s: float) -> str:
        """Navigate to ``url``; return the page title (may be empty)."""

    @abstractmethod
    def extract_text(self, max_chars: int) -> str:
        """Visible text of the current page, bounded to ``max_chars``."""

    @abstractmethod
    def fill(self, selector: str, text: str, timeout_s: float) -> None:
        """Type ``text`` into the element matching ``selector``."""

    @abstractmethod
    def click(self, selector: str, timeout_s: float) -> None:
        """Click the element matching ``selector``."""

    @abstractmethod
    def current_url(self) -> str:
        """URL of the current page ('' before the first navigation)."""

    @abstractmethod
    def close(self) -> None:
        """Release the browser."""


class PlaywrightDriver(BrowserDriver):
    """Reference driver: Playwright + Chromium (sync API, lazy import)."""

    name = "playwright"

    def __init__(self, headless: bool = True):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserError(
                "Browser automation requires Playwright: pip install "
                "playwright && playwright install chromium. Alternatively "
                "disable the browser section in the configuration."
            ) from exc
        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.launch(headless=headless)
            self._page = self._browser.new_page()
        except Exception as exc:
            self._playwright.stop()
            raise BrowserError(
                f"could not launch Chromium: {exc} — run: playwright "
                "install chromium"
            ) from exc
        logger.info("Playwright Chromium launched (headless=%s)", headless)

    def open(self, url: str, timeout_s: float) -> str:
        try:
            self._page.goto(url, timeout=timeout_s * 1000)
        except Exception as exc:
            raise BrowserError(f"navigation to {url} failed: {exc}") from exc
        return self._page.title() or ""

    def extract_text(self, max_chars: int) -> str:
        try:
            text = self._page.inner_text("body")
        except Exception as exc:
            raise BrowserError(f"text extraction failed: {exc}") from exc
        return text[:max_chars]

    def fill(self, selector: str, text: str, timeout_s: float) -> None:
        try:
            self._page.fill(selector, text, timeout=timeout_s * 1000)
        except Exception as exc:
            raise BrowserError(f"fill {selector!r} failed: {exc}") from exc

    def click(self, selector: str, timeout_s: float) -> None:
        try:
            self._page.click(selector, timeout=timeout_s * 1000)
        except Exception as exc:
            raise BrowserError(f"click {selector!r} failed: {exc}") from exc

    def current_url(self) -> str:
        return self._page.url or ""

    def close(self) -> None:
        try:
            self._browser.close()
        finally:
            self._playwright.stop()
        logger.info("Playwright browser closed")


class ScriptedDriver(BrowserDriver):
    """Test double: canned pages, recorded interactions, injectable URL."""

    name = "scripted"

    def __init__(self, page_text: str = "", title: str = "Scripted Page"):
        self._page_text = page_text
        self._title = title
        self._url = ""
        self.opens: list[str] = []
        self.fills: list[tuple[str, str]] = []
        self.clicks: list[str] = []
        self.closed = False

    def open(self, url: str, timeout_s: float) -> str:
        self.opens.append(url)
        self._url = url
        return self._title

    def extract_text(self, max_chars: int) -> str:
        return self._page_text[:max_chars]

    def fill(self, selector: str, text: str, timeout_s: float) -> None:
        self.fills.append((selector, text))

    def click(self, selector: str, timeout_s: float) -> None:
        self.clicks.append(selector)

    def current_url(self) -> str:
        return self._url

    def close(self) -> None:
        self.closed = True


class DriverHolder:
    """Lazy, shared, self-closing driver lifecycle for the actions."""

    def __init__(self, factory: Callable[[], BrowserDriver]):
        self._factory = factory
        self._driver: BrowserDriver | None = None
        self._lock = threading.Lock()
        atexit.register(self.close)

    def driver(self) -> BrowserDriver:
        with self._lock:
            if self._driver is None:
                self._driver = self._factory()
            return self._driver

    @property
    def active(self) -> bool:
        return self._driver is not None

    def close(self) -> None:
        with self._lock:
            if self._driver is not None:
                try:
                    self._driver.close()
                except Exception as exc:  # closing must never raise at exit
                    logger.warning("Browser close failed: %s", exc)
                self._driver = None


def create_browser_driver(config: BrowserConfig) -> BrowserDriver:
    """Build the reference driver (raises with guidance if unavailable)."""
    return PlaywrightDriver(headless=config.headless)
