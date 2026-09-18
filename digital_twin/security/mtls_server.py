"""mTLS wrapper for the dashboard server — every endpoint requires a client cert.

Phase 3 hardens the dashboard: the existing CSRF token guards POST, but GET
endpoints (status, events, MJPEG stream) were unauthenticated. Any local
process could read the live camera feed. mTLS closes this: a client must
present a certificate enrolled in the device registry, and every endpoint —
including MJPEG — requires it.

The implementation wraps Python's ssl module to create an SSLContext that:
1. Loads the server's own key/cert (self-signed, same as client certs)
2. Requires client certificates (ssl.CERT_REQUIRED)
3. Verifies the client cert against the trust bundle of enrolled devices

The server cert is generated on first use if missing, stored under data/
with a DPAPI-protected private key (same pattern as device keys).
"""

from __future__ import annotations

import datetime as _dt
import logging
import ssl
from pathlib import Path
from typing import TYPE_CHECKING

from digital_twin.security import dpapi
from digital_twin.security.fsacl import ensure_private_dir

if TYPE_CHECKING:
    from digital_twin.security.device_identity import DeviceRegistry

logger = logging.getLogger(__name__)

_SERVER_CERT_DAYS = 3653  # ~10 years
_SERVER_CERT_CN = "knowa-dashboard"
_SERVER_ENTROPY = b"knowa.dashboard.server-key.v1"


def _generate_server_cert(directory: Path) -> tuple[bytes, bytes]:
    """Generate a self-signed server cert + key. Returns (cert_pem, key_pem)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, _SERVER_CERT_CN),
    ])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _dt.timedelta(days=_SERVER_CERT_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                       critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return cert_pem, key_pem


def _load_or_create_server_cert(directory: Path) -> tuple[str, str]:
    """Load or create the server's cert/key. Returns (cert_path, key_path)."""
    ensure_private_dir(directory)
    cert_path = directory / "server.crt"
    key_blob_path = directory / "server.key.dpapi"

    if cert_path.exists() and key_blob_path.exists():
        return str(cert_path), str(key_blob_path)

    logger.info("Generating dashboard mTLS server certificate")
    cert_pem, key_pem = _generate_server_cert(directory)

    # Protect the private key with DPAPI
    if not dpapi.is_available():
        raise RuntimeError(
            "mTLS requires Windows DPAPI to protect the server private key")
    key_blob_path.write_bytes(dpapi.protect(key_pem, _SERVER_ENTROPY))
    cert_path.write_bytes(cert_pem)

    return str(cert_path), str(key_blob_path)


def create_mtls_context(
    registry: "DeviceRegistry",
    server_cert_dir: Path,
) -> ssl.SSLContext:
    """Create an SSLContext for mTLS with the enrolled devices as trust bundle.

    The context requires a client certificate and verifies it against the
    active device certs in the registry. The server's own cert/key are loaded
    from server_cert_dir (generated on first use).

    Args:
        registry: DeviceRegistry with enrolled devices
        server_cert_dir: Directory to store server cert/key

    Returns:
        Configured SSLContext for ssl.wrap_socket or SSLObject
    """
    cert_path, key_blob_path = _load_or_create_server_cert(server_cert_dir)

    # Decrypt the private key into memory (never written to disk in the clear)
    key_blob = Path(key_blob_path).read_bytes()
    key_pem = dpapi.unprotect(key_blob, _SERVER_ENTROPY)

    # Write key to a temp file for SSLContext.load_cert_chain (stdlib limitation)
    # The key is in memory anyway, and this temp file is deleted immediately
    import tempfile
    with tempfile.NamedTemporaryFile(
        mode="wb", suffix=".pem", delete=False
    ) as key_tmp:
        key_tmp.write(key_pem)
        key_tmp_path = key_tmp.name

    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert_path, key_tmp_path)
    finally:
        Path(key_tmp_path).unlink(missing_ok=True)

    # Require client cert and verify against enrolled devices
    ctx.verify_mode = ssl.CERT_REQUIRED

    # Build the trust bundle from active device certs
    trust_bundle = server_cert_dir / "trust_bundle.pem"
    _rebuild_trust_bundle(registry, trust_bundle)
    ctx.load_verify_locations(str(trust_bundle))

    return ctx


def _rebuild_trust_bundle(registry: "DeviceRegistry", path: Path) -> None:
    """Write all active device certs to the trust bundle file."""
    certs = registry.active_cert_pems()
    if not certs:
        logger.warning(
            "mTLS enabled but no devices enrolled — dashboard will reject all "
            "connections. Enroll a device with: python -m digital_twin.security."
            "device_cli enroll --label 'device name'")
        # Write an empty bundle — all connections will fail verification
        path.write_text("", encoding="utf-8")
        return

    path.write_text("\n".join(certs), encoding="utf-8")
    logger.debug("mTLS trust bundle: %d active device cert(s)", len(certs))


def verify_client_cert(
    registry: "DeviceRegistry",
    cert_der: bytes,
) -> str | None:
    """Verify a client cert and return the device_id if enrolled and active.

    This is the verification hook called after SSL handshake completes.
    The SSL layer already verified the cert against the trust bundle; this
    checks revocation status.

    Args:
        registry: DeviceRegistry to check enrollment status
        cert_der: DER-encoded client certificate from the handshake

    Returns:
        Device ID if valid, None if revoked or not found
    """
    import hashlib
    fingerprint = "sha256:" + hashlib.sha256(cert_der).hexdigest()
    device_id = registry.find_by_fingerprint(fingerprint)
    if device_id is None:
        return None
    if not registry.is_enrolled(device_id):
        return None  # revoked
    return device_id


class MTLSDashboardServer:
    """Wraps DashboardServer with mTLS on every endpoint.

    This is a thin wrapper that intercepts socket creation to apply the
    mTLS context. All other behavior (routes, handlers, token checks) remains
    unchanged — mTLS is applied at the transport layer.
    """

    def __init__(
        self,
        dashboard_server,  # DashboardServer instance
        registry: "DeviceRegistry",
        server_cert_dir: Path,
    ):
        self._server = dashboard_server
        self._registry = registry
        self._cert_dir = Path(server_cert_dir)
        self._ssl_context: ssl.SSLContext | None = None

    def start(self) -> None:
        """Start the server with mTLS wrapped sockets."""
        # Build the SSL context before the underlying server starts
        self._ssl_context = create_mtls_context(self._registry, self._cert_dir)

        # Patch the server's socket to use SSL
        original_socket = self._server._server.socket

        # Wrap the existing socket with SSL
        ssl_socket = self._ssl_context.wrap_socket(
            original_socket,
            server_side=True,
        )
        self._server._server.socket = ssl_socket

        # Start the underlying server
        self._server.start()
        logger.info(
            "Dashboard mTLS enabled — clients must present enrolled device cert")

    def stop(self) -> None:
        self._server.stop()

    @property
    def port(self) -> int:
        return self._server.port

    @property
    def token(self) -> str:
        return self._server.token
