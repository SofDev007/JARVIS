"""Device identity: enrolment as possession of a keypair, not a password.

Identity in KNOWA is *possession of an enrolled device*. Each device gets an
EC P-256 keypair and a self-signed X.509 certificate (client+server auth, so it
serves the mTLS work in Phase 3B). The **private key never touches disk in the
clear**: it is DPAPI-protected (CurrentUser) and written as an opaque ``.dpapi``
blob. The registry stores only public material — the certificate, its
fingerprint, a stable device id, and a human label.

Nothing here logs or audits key material. Enrol and revoke write to the
hash-chained audit log (tamper-evident since Phase B), carrying the device id
and label only.

The registry and the key blobs live under ``data/`` (owner-only since Phase A);
this module also calls :func:`ensure_private_dir` on the devices directory it
creates.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import uuid
from pathlib import Path
from typing import Any

from digital_twin.security import dpapi
from digital_twin.security.fsacl import ensure_private_dir

logger = logging.getLogger(__name__)

# Application-specific DPAPI entropy for device private keys (distinct from the
# audit anchor's entropy).
_KEY_ENTROPY = b"knowa.device.private-key.v1"
_CERT_VALIDITY_DAYS = 3653  # ~10 years; devices are revoked, not expired


class DeviceIdentityError(RuntimeError):
    """Enrolment/registry failure (bad id, missing key, DPAPI unavailable)."""


def _fingerprint(cert_der: bytes) -> str:
    return "sha256:" + hashlib.sha256(cert_der).hexdigest()


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


class DeviceRegistry:
    """Enrol, list and revoke devices; resolve certs for mTLS."""

    def __init__(self, directory: str | Path, audit=None):
        self._dir = Path(directory)
        self._audit = audit
        ensure_private_dir(self._dir)
        self._registry_path = self._dir / "registry.json"

    # -- storage --------------------------------------------------------
    def _load(self) -> dict[str, Any]:
        if not self._registry_path.exists():
            return {"devices": {}}
        try:
            data = json.loads(self._registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DeviceIdentityError(
                f"cannot read device registry: {exc}") from exc
        data.setdefault("devices", {})
        return data

    def _save(self, data: dict[str, Any]) -> None:
        ensure_private_dir(self._dir)
        self._registry_path.write_text(
            json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

    def _key_path(self, device_id: str) -> Path:
        return self._dir / f"{device_id}.key.dpapi"

    # -- operations -----------------------------------------------------
    def enroll(self, label: str) -> str:
        """Generate a keypair + cert for *label*, store them, return device id."""
        if not isinstance(label, str) or not label.strip():
            raise DeviceIdentityError("a non-empty device label is required")
        if not dpapi.is_available():
            raise DeviceIdentityError(
                "device enrolment requires Windows DPAPI to protect the "
                "private key")
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

        device_id = uuid.uuid4().hex
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, label.strip()),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, device_id),
        ])
        now = _dt.datetime.now(_dt.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _dt.timedelta(minutes=5))
            .not_valid_after(now + _dt.timedelta(days=_CERT_VALIDITY_DAYS))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                           critical=True)
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH,
                                       ExtendedKeyUsageOID.SERVER_AUTH]),
                critical=False)
            .sign(key, hashes.SHA256())
        )
        cert_pem = cert.public_bytes(serialization.Encoding.PEM)
        # PKCS#8, unencrypted at the PEM layer — DPAPI is the protection, and it
        # is applied before anything reaches disk.
        key_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        self._key_path(device_id).write_bytes(
            dpapi.protect(key_pem, _KEY_ENTROPY))
        del key_pem, key  # do not keep plaintext key material around

        data = self._load()
        data["devices"][device_id] = {
            "label": label.strip(),
            "cert_pem": cert_pem.decode("ascii"),
            "fingerprint": _fingerprint(cert.public_bytes(
                serialization.Encoding.DER)),
            "enrolled_at": _now_iso(),
            "status": "active",
            "revoked_at": None,
        }
        self._save(data)
        logger.info("Enrolled device %s (%s)", device_id, label.strip())
        if self._audit is not None:
            self._audit.record(event="device_enrolled", device_id=device_id,
                               label=label.strip())
        return device_id

    def revoke(self, device_id: str) -> bool:
        """Mark a device revoked (its key blob is left on disk, inert)."""
        data = self._load()
        record = data["devices"].get(device_id)
        if record is None or record.get("status") == "revoked":
            return False
        record["status"] = "revoked"
        record["revoked_at"] = _now_iso()
        self._save(data)
        logger.info("Revoked device %s (%s)", device_id, record.get("label"))
        if self._audit is not None:
            self._audit.record(event="device_revoked", device_id=device_id,
                               label=record.get("label"))
        return True

    def list(self) -> list[dict[str, Any]]:
        """Public metadata for every device (never any key material)."""
        data = self._load()
        out = []
        for device_id, record in sorted(data["devices"].items()):
            out.append({
                "device_id": device_id,
                "label": record.get("label"),
                "status": record.get("status"),
                "fingerprint": record.get("fingerprint"),
                "enrolled_at": record.get("enrolled_at"),
                "revoked_at": record.get("revoked_at"),
            })
        return out

    def is_enrolled(self, device_id: str) -> bool:
        """True iff the device exists and is active (not revoked)."""
        record = self._load()["devices"].get(device_id)
        return bool(record) and record.get("status") == "active"

    def active_devices(self) -> list[dict[str, Any]]:
        return [d for d in self.list() if d["status"] == "active"]

    def find_by_fingerprint(self, fingerprint: str) -> str | None:
        """Device id for a cert fingerprint, or ``None``. Used by mTLS (3B)."""
        for device_id, record in self._load()["devices"].items():
            if record.get("fingerprint") == fingerprint:
                return device_id
        return None

    def active_cert_pems(self) -> list[str]:
        """PEM certs of active devices — the trust bundle for mTLS (3B)."""
        return [r["cert_pem"] for r in self._load()["devices"].values()
                if r.get("status") == "active"]

    def private_key_pem(self, device_id: str) -> bytes:
        """DPAPI-unprotect and return a device's private key (in-memory only)."""
        try:
            blob = self._key_path(device_id).read_bytes()
        except FileNotFoundError as exc:
            raise DeviceIdentityError(
                f"no private key stored for device {device_id}") from exc
        return dpapi.unprotect(blob, _KEY_ENTROPY)

    def cert_pem(self, device_id: str) -> str:
        record = self._load()["devices"].get(device_id)
        if record is None:
            raise DeviceIdentityError(f"no such device: {device_id}")
        return record["cert_pem"]
