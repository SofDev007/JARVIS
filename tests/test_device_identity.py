"""Tests for device identity: enrolment, revocation, and — the point — that
private key material never lands in the registry, the audit log, or on disk in
the clear.

DPAPI is a Windows-only facility, so these skip elsewhere (justified: there is
no cross-platform equivalent and the project targets Windows).
"""

from __future__ import annotations

import sys

import pytest

from digital_twin.security.audit import AuditLog
from digital_twin.security.device_identity import (
    DeviceIdentityError,
    DeviceRegistry,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="device identity uses Windows DPAPI to protect private keys",
)


def test_enroll_creates_active_device_with_dpapi_protected_key(tmp_path):
    registry = DeviceRegistry(tmp_path / "devices")
    device_id = registry.enroll("Arnav laptop")

    assert registry.is_enrolled(device_id)
    (listed,) = registry.list()
    assert listed["device_id"] == device_id
    assert listed["label"] == "Arnav laptop"
    assert listed["status"] == "active"

    # The key blob on disk is DPAPI ciphertext, not a PEM.
    blob = (tmp_path / "devices" / f"{device_id}.key.dpapi").read_bytes()
    assert b"PRIVATE KEY" not in blob
    assert b"-----BEGIN" not in blob
    # …but it round-trips back to a real private key in memory.
    key_pem = registry.private_key_pem(device_id)
    assert b"PRIVATE KEY" in key_pem


def test_no_key_material_in_registry_or_audit(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    registry = DeviceRegistry(tmp_path / "devices", audit=audit)
    device_id = registry.enroll("phone")

    # The private key, in the clear, for a direct-substring search.
    key_pem = registry.private_key_pem(device_id)
    key_body = key_pem.decode("ascii")

    registry_text = (tmp_path / "devices" / "registry.json").read_text(
        encoding="utf-8")
    audit_text = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    for haystack in (registry_text, audit_text):
        assert "PRIVATE KEY" not in haystack
        assert key_body not in haystack

    # The enrolment was audited (id + label only).
    (record,) = [e for e in audit.tail(10) if e.get("event") == "device_enrolled"]
    assert record["device_id"] == device_id
    assert record["label"] == "phone"


def test_revoke_marks_revoked_and_is_audited(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    registry = DeviceRegistry(tmp_path / "devices", audit=audit)
    device_id = registry.enroll("laptop")

    assert registry.revoke(device_id) is True
    assert registry.is_enrolled(device_id) is False
    (listed,) = registry.list()
    assert listed["status"] == "revoked" and listed["revoked_at"]
    assert registry.revoke(device_id) is False  # already revoked

    events = [e["event"] for e in audit.tail(10) if "event" in e]
    assert "device_revoked" in events
    # The audit chain stayed intact across both writes.
    assert audit.verify_chain() is None


def test_enroll_rejects_blank_label(tmp_path):
    registry = DeviceRegistry(tmp_path / "devices")
    with pytest.raises(DeviceIdentityError):
        registry.enroll("   ")


def test_fingerprint_lookup_and_active_bundle(tmp_path):
    registry = DeviceRegistry(tmp_path / "devices")
    keep = registry.enroll("keep")
    drop = registry.enroll("drop")
    registry.revoke(drop)

    (kept,) = registry.active_devices()
    assert kept["device_id"] == keep
    assert registry.find_by_fingerprint(kept["fingerprint"]) == keep
    # Only the active device's cert is in the mTLS trust bundle (3B prep).
    bundle = registry.active_cert_pems()
    assert len(bundle) == 1 and "BEGIN CERTIFICATE" in bundle[0]
