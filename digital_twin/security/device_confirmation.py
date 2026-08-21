"""Second-device confirmation for DANGEROUS actions.

M18 Phase 3: DANGEROUS actions (file delete, email send, browser click/type)
require confirmation from a *second* enrolled device — not just the one making
the request. This prevents a compromised phone from unilaterally approving
destructive actions.

The flow:
1. DANGEROUS action needs confirmation
2. System pushes confirmation request to all *other* active enrolled devices
3. Any one of them can approve (redundancy for availability)
4. Timeout without approval = deny (fail-closed)

This module provides the confirmation provider and the notification mechanism.
The actual push to devices (phone, second laptop) is via the dashboard API
and a future mobile endpoint (M22). For now, the second device confirms via
the web dashboard on a different enrolled machine.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping

from digital_twin.security.confirmation import ConfirmationProvider

if TYPE_CHECKING:
    from digital_twin.security.device_identity import DeviceRegistry

logger = logging.getLogger(__name__)


@dataclass
class PendingDeviceConfirmation:
    """A confirmation request awaiting response from a second device."""
    id: str
    action: str
    params: dict[str, Any]
    risk: str
    requesting_device: str | None
    created_at: float
    timeout_s: float
    resolved: bool = False
    approved: bool = False
    responding_device: str | None = None


class DeviceConfirmationProvider(ConfirmationProvider):
    """Confirmation provider that requires a second enrolled device to approve.

    Used for DANGEROUS actions. The requesting device cannot approve its own
    request — this is enforced by tracking the requesting_device and rejecting
    self-approvals.

    For M18, approval comes via the dashboard API. The dashboard client must
    present an mTLS client cert, which identifies the device. The approval
    endpoint verifies the device is enrolled and *different* from the requester.
    """

    name = "device"

    def __init__(
        self,
        registry: "DeviceRegistry",
        timeout_s: float = 60.0,
    ):
        self._registry = registry
        self._timeout_s = timeout_s
        self._pending: dict[str, PendingDeviceConfirmation] = {}
        self._lock = threading.Lock()

    def request(
        self,
        action: str,
        params: Mapping[str, Any],
        timeout_s: float,
        *,
        requesting_device: str | None = None,
        risk: str = "dangerous",
    ) -> bool:
        """Request confirmation from a second device.

        This blocks until either:
        - A different enrolled device approves
        - Timeout expires (deny)
        - Any device explicitly denies

        Args:
            action: Action name being confirmed
            params: Action parameters
            timeout_s: Seconds to wait for response
            requesting_device: Device ID of the requester (from mTLS cert)
            risk: Risk level (should be "dangerous")

        Returns:
            True only if a different device explicitly approved
        """
        if risk != "dangerous":
            # Non-dangerous actions use normal confirmation
            logger.warning(
                "DeviceConfirmation used for non-DANGEROUS action %s", action)
            return False

        confirm_id = secrets.token_urlsafe(16)
        pending = PendingDeviceConfirmation(
            id=confirm_id,
            action=action,
            params=dict(params),
            risk=risk,
            requesting_device=requesting_device,
            created_at=time.monotonic(),
            timeout_s=timeout_s,
        )

        with self._lock:
            self._pending[confirm_id] = pending

        logger.info(
            "DANGEROUS action %r awaiting second-device confirmation (id=%s, "
            "requester=%s)",
            action, confirm_id, requesting_device or "unknown"
        )

        # Wait for resolution or timeout
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with self._lock:
                if pending.resolved:
                    break
            time.sleep(0.1)

        # Cleanup
        with self._lock:
            self._pending.pop(confirm_id, None)

        if not pending.resolved:
            logger.warning(
                "DANGEROUS action %r denied — no second-device response within "
                "%.0fs", action, timeout_s)
            return False

        if not pending.approved:
            logger.info(
                "DANGEROUS action %r denied by device %s",
                action, pending.responding_device or "unknown")
            return False

        logger.info(
            "DANGEROUS action %r approved by second device %s",
            action, pending.responding_device)
        return True

    def resolve(
        self,
        confirm_id: str,
        approved: bool,
        responding_device: str,
    ) -> bool:
        """Resolve a pending confirmation from a second device.

        Called by the dashboard API when a device approves/denies.

        Args:
            confirm_id: The pending confirmation ID
            approved: True to approve, False to deny
            responding_device: Device ID of the approver (from mTLS cert)

        Returns:
            True if resolution was accepted, False if invalid
        """
        with self._lock:
            pending = self._pending.get(confirm_id)
            if pending is None:
                logger.warning("Unknown or expired confirmation id: %s", confirm_id)
                return False

            if pending.resolved:
                logger.debug("Confirmation %s already resolved", confirm_id)
                return False

            # Reject self-approval: requesting device cannot approve its own request
            if (pending.requesting_device is not None
                    and responding_device == pending.requesting_device):
                logger.warning(
                    "Device %s attempted to approve its own request for %r — denied",
                    responding_device, pending.action)
                return False

            # Verify the responding device is enrolled
            if not self._registry.is_enrolled(responding_device):
                logger.warning(
                    "Non-enrolled device %s attempted to confirm %r — denied",
                    responding_device, pending.action)
                return False

            pending.resolved = True
            pending.approved = approved
            pending.responding_device = responding_device

            if approved:
                logger.info(
                    "Device %s approved DANGEROUS action %r",
                    responding_device, pending.action)
            else:
                logger.info(
                    "Device %s denied DANGEROUS action %r",
                    responding_device, pending.action)

            return True

    def pending(self) -> list[dict[str, Any]]:
        """List pending confirmations for the dashboard API."""
        now = time.monotonic()
        with self._lock:
            result = []
            for p in list(self._pending.values()):
                expires_in = max(0, p.created_at + p.timeout_s - now)
                if expires_in <= 0:
                    continue  # expired, will be cleaned up
                result.append({
                    "id": p.id,
                    "action": p.action,
                    "params": p.params,
                    "risk": p.risk,
                    "requesting_device": p.requesting_device,
                    "expires_in_s": expires_in,
                })
            return result

    def is_enrolled(self, device_id: str) -> bool:
        """Check if a device is enrolled and active."""
        return self._registry.is_enrolled(device_id)


def build_device_confirmation_provider(
    registry: "DeviceRegistry",
    timeout_s: float = 60.0,
) -> DeviceConfirmationProvider:
    """Factory for the device confirmation provider."""
    return DeviceConfirmationProvider(registry, timeout_s=timeout_s)
