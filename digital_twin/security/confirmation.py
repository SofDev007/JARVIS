"""Confirmation gates: explicit user approval for guarded actions.

The dispatcher asks a :class:`ConfirmationProvider` before running any
action whose permission decision is ``CONFIRM``. Providers are replaceable
(console today, dashboard UI later) and every provider is **fail-closed**:
no answer, no TTY, no time — no action.
"""

from __future__ import annotations

import logging
import sys
import threading
from abc import ABC, abstractmethod
from typing import Any, Mapping

logger = logging.getLogger(__name__)


class ConfirmationProvider(ABC):
    """Interface for asking the user to approve one action."""

    name: str = "abstract"

    @abstractmethod
    def request(self, action: str, params: Mapping[str, Any], timeout_s: float) -> bool:
        """Return ``True`` only on explicit approval within ``timeout_s``."""


class AutoDenyConfirmation(ConfirmationProvider):
    """Denies everything — the safe default for unattended/headless runs."""

    name = "auto_deny"

    def request(self, action: str, params: Mapping[str, Any], timeout_s: float) -> bool:
        logger.info(
            "Auto-denied action %r (no interactive confirmation configured)", action
        )
        return False


class ConsoleConfirmation(ConfirmationProvider):
    """Interactive y/N prompt on the terminal with a hard timeout.

    Fail-closed behaviours: not a TTY → deny; timeout → deny; anything but
    an explicit ``y``/``yes`` → deny. The prompt runs in a helper thread so
    a walked-away user cannot stall the action pipeline forever (the
    abandoned ``input`` thread is a daemon and dies with the process).
    """

    name = "console"

    def request(self, action: str, params: Mapping[str, Any], timeout_s: float) -> bool:
        stdin = getattr(sys, "stdin", None)
        if stdin is None or not stdin.isatty():
            logger.warning(
                "Confirmation for %r denied: no interactive terminal", action
            )
            return False

        answer: list[str] = []
        done = threading.Event()

        def ask() -> None:
            try:
                answer.append(
                    input(f"\n[confirm] Run action {action!r} with {dict(params)}? [y/N] ")
                )
            except (EOFError, KeyboardInterrupt):
                answer.append("")
            finally:
                done.set()

        threading.Thread(target=ask, name="confirm-prompt", daemon=True).start()
        if not done.wait(timeout=timeout_s):
            print(f"\n[confirm] Timed out after {timeout_s:.0f}s — denied.")
            logger.info("Confirmation for %r timed out; denied", action)
            return False
        approved = bool(answer) and answer[0].strip().lower() in ("y", "yes")
        logger.info("Confirmation for %r: %s", action, "approved" if approved else "denied")
        return approved


class ScriptedConfirmation(ConfirmationProvider):
    """Deterministic provider for tests and demos: pops pre-loaded answers.

    Records every request it receives; an exhausted script denies
    (fail-closed, like everything else).
    """

    name = "scripted"

    def __init__(self, answers: list[bool] | None = None):
        self._answers = list(answers or [])
        self.requests: list[tuple[str, dict]] = []

    def request(self, action: str, params: Mapping[str, Any], timeout_s: float) -> bool:
        self.requests.append((action, dict(params)))
        return self._answers.pop(0) if self._answers else False


_PROVIDERS = {
    AutoDenyConfirmation.name: AutoDenyConfirmation,
    ConsoleConfirmation.name: ConsoleConfirmation,
}


def create_confirmation_provider(name: str) -> ConfirmationProvider:
    """Instantiate a provider by configured name (validated at config load)."""
    try:
        return _PROVIDERS[name]()
    except KeyError:
        logger.error("Unknown confirmation provider %r; using auto_deny", name)
        return AutoDenyConfirmation()
