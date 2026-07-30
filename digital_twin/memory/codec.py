"""Memory codecs: how memory content is stored at rest.

Two implementations behind one two-method interface:

* :class:`PlainCodec` — UTF-8, the default.
* :class:`FernetCodec` — authenticated symmetric encryption (Fernet /
  AES-128-CBC + HMAC via the ``cryptography`` package). The key lives in a
  local file created with ``0600`` permissions on first use.

Policy decisions worth stating explicitly:

* **No silent downgrade.** If encryption is enabled but ``cryptography``
  is missing, startup fails with install guidance. An assistant that
  quietly stores "encrypted" memories in plaintext is worse than one that
  refuses to start.
* **Content is encrypted; metadata is not.** Record kinds, sources, tags,
  timestamps and importance stay queryable plaintext — the threat model is
  "someone reads the DB file", protecting *what was remembered*, not *that
  something was remembered at time T*. Documented, deliberate.
* **Encryption disables nothing functionally** — search decrypts and
  scans instead of indexing (see the store), trading speed for privacy.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path

logger = logging.getLogger(__name__)


class CodecError(RuntimeError):
    """Encoding/decoding failed (wrong key, corrupt data, missing dep)."""


class MemoryCodec(ABC):
    """Encodes memory content for storage and decodes it back."""

    name: str = "abstract"

    @abstractmethod
    def encode(self, text: str) -> bytes:
        """Text → stored bytes."""

    @abstractmethod
    def decode(self, blob: bytes) -> str:
        """Stored bytes → text (raises :class:`CodecError` on failure)."""


class PlainCodec(MemoryCodec):
    """UTF-8 passthrough."""

    name = "plain"

    def encode(self, text: str) -> bytes:
        return text.encode("utf-8")

    def decode(self, blob: bytes) -> str:
        try:
            return bytes(blob).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CodecError(f"corrupt plaintext memory: {exc}") from exc


class FernetCodec(MemoryCodec):
    """Authenticated encryption with a locally stored key file."""

    name = "fernet"

    def __init__(self, key_path: str | Path):
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:
            raise CodecError(
                "Memory encryption requires the 'cryptography' package "
                "(pip install cryptography) or set memory.encryption: false"
            ) from exc
        self._fernet = Fernet(self._load_or_create_key(Path(key_path)))

    @staticmethod
    def _load_or_create_key(path: Path) -> bytes:
        from cryptography.fernet import Fernet

        if path.exists():
            key = path.read_bytes().strip()
            if not key:
                raise CodecError(f"Memory key file is empty: {path}")
            return key
        path.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        # Owner-only before content is written.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(key)
        logger.info("Generated new memory encryption key at %s", path)
        return key

    def encode(self, text: str) -> bytes:
        return self._fernet.encrypt(text.encode("utf-8"))

    def decode(self, blob: bytes) -> str:
        from cryptography.fernet import InvalidToken

        try:
            return self._fernet.decrypt(bytes(blob)).decode("utf-8")
        except InvalidToken as exc:
            raise CodecError(
                "Cannot decrypt memory (wrong key or corrupt record)"
            ) from exc


def create_codec(encryption: bool, key_path: str | Path) -> MemoryCodec:
    """Build the configured codec; encryption failures are fail-fast."""
    if not encryption:
        return PlainCodec()
    codec = FernetCodec(key_path)
    logger.info("Memory encryption enabled (key: %s)", key_path)
    return codec
