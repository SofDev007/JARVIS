"""Web confirmation: approve or deny gated actions from the dashboard.

The dispatcher's calling convention is unchanged — ``request()`` blocks
the action worker until an answer or the timeout — but the *answering*
moves from stdin to HTTP: each request becomes a pending entry the
dashboard polls (``GET /api/confirmations``) and resolves
(``POST /api/confirmations/<id>``). Every fail-closed property of the
console provider is preserved:

* timeout with no click → **deny**;
* dashboard stopped / never opened → **deny** (nobody can answer);
* anything other than an explicit ``approve: true`` → **deny**.

This is what finally resolves the long-tracked trade-off of console chat
and console confirmations sharing stdin.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from digital_twin.security.confirmation import ConfirmationProvider

logger = logging.getLogger(__name__)


@dataclass
class PendingConfirmation:
    """One action waiting for a human click."""

    confirmation_id: str
    action: str
    params: dict[str, Any]
    expires_at: float
    _answered: threading.Event = field(default_factory=threading.Event,
                                       repr=False)
    _approved: bool = False

    def resolve(self, approved: bool) -> None:
        self._approved = bool(approved)
        self._answered.set()

    def wait(self, timeout_s: float) -> bool:
        answered = self._answered.wait(timeout_s)
        return self._approved if answered else False  # timeout = deny

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.confirmation_id,
            "action": self.action,
            "params": self.params,
            "expires_in_s": max(0.0, round(self.expires_at - time.time(), 1)),
        }


class WebConfirmation(ConfirmationProvider):
    """Queue-backed provider the dashboard answers over HTTP."""

    name = "web"

    def __init__(self) -> None:
        self._pending: dict[str, PendingConfirmation] = {}
        self._lock = threading.Lock()
        self._ids = itertools.count(1)

    # -- dispatcher side ---------------------------------------------------
    def request(self, action: str, params: Mapping[str, Any],
                timeout_s: float) -> bool:
        entry = PendingConfirmation(
            confirmation_id=f"c{next(self._ids)}",
            action=action,
            params=dict(params),
            expires_at=time.time() + timeout_s,
        )
        with self._lock:
            self._pending[entry.confirmation_id] = entry
        logger.info("Confirmation %s pending on the dashboard: %s",
                    entry.confirmation_id, action)
        try:
            approved = entry.wait(timeout_s)
        finally:
            with self._lock:
                self._pending.pop(entry.confirmation_id, None)
        if not approved:
            logger.info("Confirmation %s for %r: denied "
                        "(explicit deny or timeout)",
                        entry.confirmation_id, action)
        return approved

    # -- dashboard side -----------------------------------------------------
    def pending(self) -> list[dict[str, Any]]:
        """JSON-ready snapshot of everything awaiting an answer."""
        now = time.time()
        with self._lock:
            return [entry.to_json() for entry in self._pending.values()
                    if entry.expires_at > now]

    def resolve(self, confirmation_id: str, approved: bool) -> bool:
        """Answer one pending confirmation; ``False`` if unknown/expired."""
        with self._lock:
            entry = self._pending.get(confirmation_id)
        if entry is None:
            return False
        entry.resolve(approved)
        return True
