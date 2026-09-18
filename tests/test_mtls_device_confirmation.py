"""Tests for M18 Phase 3: mTLS dashboard and device confirmation.

Tests verify:
1. mTLS context creation with enrolled devices
2. Device confirmation requires second device
3. Self-approval is rejected
4. Non-enrolled devices cannot confirm
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="device identity uses Windows DPAPI to protect private keys",
)


def test_mtls_context_with_enrolled_device():
    """mTLS context loads server cert and client trust bundle."""
    from digital_twin.security.device_identity import DeviceRegistry
    from digital_twin.security.mtls_server import create_mtls_context

    with tempfile.TemporaryDirectory() as tmpdir:
        registry = DeviceRegistry(Path(tmpdir) / "devices")
        device_id = registry.enroll("laptop")

        # Create mTLS context
        ctx = create_mtls_context(registry, Path(tmpdir) / "server")

        # Verify context is configured for mTLS
        import ssl
        assert ctx.verify_mode == ssl.CERT_REQUIRED

        # Trust bundle should contain the enrolled device's cert
        trust_bundle = Path(tmpdir) / "server" / "trust_bundle.pem"
        assert trust_bundle.exists()
        bundle_content = trust_bundle.read_text(encoding="utf-8")
        assert "BEGIN CERTIFICATE" in bundle_content


def test_mtls_context_no_devices_warns():
    """mTLS with no enrolled devices creates empty trust bundle."""
    from digital_twin.security.device_identity import DeviceRegistry
    from digital_twin.security.mtls_server import create_mtls_context
    import ssl

    with tempfile.TemporaryDirectory() as tmpdir:
        registry = DeviceRegistry(Path(tmpdir) / "devices")
        # No devices enrolled

        # This should raise because ssl.load_verify_locations fails on empty bundle
        # In practice, the dashboard won't start mTLS without enrolled devices
        with pytest.raises(ssl.SSLError):
            ctx = create_mtls_context(registry, Path(tmpdir) / "server")


def test_device_confirmation_requires_second_device():
    """DANGEROUS action cannot be approved by requesting device."""
    from digital_twin.security.device_confirmation import DeviceConfirmationProvider
    from digital_twin.security.device_identity import DeviceRegistry

    with tempfile.TemporaryDirectory() as tmpdir:
        registry = DeviceRegistry(Path(tmpdir) / "devices")
        laptop_id = registry.enroll("laptop")
        phone_id = registry.enroll("phone")

        provider = DeviceConfirmationProvider(registry, timeout_s=5.0)

        # Start a confirmation request from laptop
        import threading
        result = {"approved": None}

        def request_confirmation():
            result["approved"] = provider.request(
                "delete_file",
                {"path": "/important/data"},
                timeout_s=2.0,
                requesting_device=laptop_id,
                risk="dangerous",
            )

        thread = threading.Thread(target=request_confirmation, daemon=True)
        thread.start()

        # Get pending confirmations
        pending = provider.pending()
        assert len(pending) == 1
        confirm_id = pending[0]["id"]

        # Laptop tries to approve its own request - should fail
        assert provider.resolve(confirm_id, True, laptop_id) is False

        # Phone approves - should succeed
        assert provider.resolve(confirm_id, True, phone_id) is True

        thread.join(timeout=3.0)
        assert result["approved"] is True


def test_device_confirmation_rejects_non_enrolled():
    """Non-enrolled device cannot approve."""
    from digital_twin.security.device_confirmation import DeviceConfirmationProvider
    from digital_twin.security.device_identity import DeviceRegistry

    with tempfile.TemporaryDirectory() as tmpdir:
        registry = DeviceRegistry(Path(tmpdir) / "devices")
        laptop_id = registry.enroll("laptop")

        provider = DeviceConfirmationProvider(registry, timeout_s=5.0)

        import threading
        result = {"approved": None}

        def request_confirmation():
            result["approved"] = provider.request(
                "email.send_email",
                {"to": "test@example.com"},
                timeout_s=2.0,
                requesting_device=laptop_id,
                risk="dangerous",
            )

        thread = threading.Thread(target=request_confirmation, daemon=True)
        thread.start()

        pending = provider.pending()
        confirm_id = pending[0]["id"]

        # Non-enrolled device tries to approve - should fail
        assert provider.resolve(confirm_id, True, "non-enrolled-id") is False

        thread.join(timeout=3.0)
        # Timeout - no valid approver
        assert result["approved"] is False


def test_device_confirmation_revoked_device_cannot_approve():
    """Revoked device cannot approve."""
    from digital_twin.security.device_confirmation import DeviceConfirmationProvider
    from digital_twin.security.device_identity import DeviceRegistry

    with tempfile.TemporaryDirectory() as tmpdir:
        registry = DeviceRegistry(Path(tmpdir) / "devices")
        laptop_id = registry.enroll("laptop")
        phone_id = registry.enroll("phone")

        # Revoke the phone
        registry.revoke(phone_id)

        provider = DeviceConfirmationProvider(registry, timeout_s=5.0)

        import threading
        result = {"approved": None}

        def request_confirmation():
            result["approved"] = provider.request(
                "browser_click",
                {"selector": "#submit"},
                timeout_s=2.0,
                requesting_device=laptop_id,
                risk="dangerous",
            )

        thread = threading.Thread(target=request_confirmation, daemon=True)
        thread.start()

        pending = provider.pending()
        confirm_id = pending[0]["id"]

        # Revoked phone tries to approve - should fail
        assert provider.resolve(confirm_id, True, phone_id) is False

        thread.join(timeout=3.0)
        assert result["approved"] is False


def test_device_confirmation_timeout_denies():
    """Timeout without approval denies the action."""
    from digital_twin.security.device_confirmation import DeviceConfirmationProvider
    from digital_twin.security.device_identity import DeviceRegistry

    with tempfile.TemporaryDirectory() as tmpdir:
        registry = DeviceRegistry(Path(tmpdir) / "devices")
        laptop_id = registry.enroll("laptop")
        phone_id = registry.enroll("phone")

        provider = DeviceConfirmationProvider(registry, timeout_s=1.0)

        # Request with no resolution
        approved = provider.request(
            "delete_file",
            {"path": "/data"},
            timeout_s=0.5,
            requesting_device=laptop_id,
            risk="dangerous",
        )

        assert approved is False
