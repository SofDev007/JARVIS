"""Tail anchor: a DPAPI-protected copy of the audit chain's latest tail hash,
stored *outside* ``logs/``.

Phase B's hash chain cannot detect mutation of the *last* record (no following
``prev`` contradicts it) nor truncation of records from the end (a shorter
chain is still internally valid). The anchor closes that gap: after every audit
append we record the current tail hash here. Because the blob is DPAPI-bound to
the current user and sits in the owner-only ``data/`` tree, another local
account can neither read nor forge it — so if the on-disk tail no longer
matches (or has vanished through truncation), verification says so.

The anchor is best-effort by design: a missing or unreadable anchor degrades to
a warning, never a hard failure, so an install that predates this feature still
starts.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from digital_twin.security import dpapi
from digital_twin.security.fsacl import ensure_private_dir

logger = logging.getLogger(__name__)

# Application-specific secondary entropy: distinguishes this blob from any other
# DPAPI blob the same user might hold (e.g. device keys).
_ENTROPY = b"knowa.audit.tail-anchor.v1"


class TailAnchor:
    """Stores/reads the audit chain's latest tail hash under DPAPI."""

    def __init__(self, path: str | Path):
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def update(self, tail_hash: str) -> None:
        """Protect and write the current tail hash. Best-effort; logs on failure."""
        try:
            ensure_private_dir(self._path.parent)
            payload = json.dumps({"tail": tail_hash}, separators=(",", ":"))
            self._path.write_bytes(dpapi.protect(payload.encode("utf-8"),
                                                 _ENTROPY))
        except (OSError, dpapi.DpapiError):
            logger.warning("Could not update audit tail anchor at %s",
                           self._path, exc_info=True)

    def read(self) -> str | None:
        """Return the anchored tail hash, or ``None`` if absent/unreadable."""
        try:
            blob = self._path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            logger.warning("Could not read audit tail anchor at %s", self._path)
            return None
        try:
            payload = json.loads(dpapi.unprotect(blob, _ENTROPY).decode("utf-8"))
            return payload.get("tail")
        except (dpapi.DpapiError, json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("Audit tail anchor at %s is unreadable (wrong user "
                           "or corrupt)", self._path)
            return None
