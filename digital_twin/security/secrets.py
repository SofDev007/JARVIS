"""Secret storage: values by name, never by value.

The one rule everything here serves: **a secret's value exists only
inside the handler that needs it, at the moment it needs it.** Actions,
events, plans, prompts, results and audit rows carry the *name*; the
value is resolved at the last moment and immediately forgotten. Nothing
in this module ever logs a value.

Two backends behind one interface:

* :class:`KeyringSecretStore` — the OS credential vault via the optional
  ``keyring`` package (values protected by the OS session). The OS
  keyring cannot enumerate entries, so a **names-only** index file makes
  ``names()`` work — names are metadata, the same deliberate policy as
  memory encryption (protect *what*, not *that*).
* :class:`EncryptedFileSecretStore` — a Fernet-encrypted JSON file with a
  ``0600`` key file, for machines without a keyring. Writes are atomic
  (temp file + replace).

**No silent downgrade**: asking for ``keyring`` without the package, or
``file`` without ``cryptography``, fails loudly with install guidance —
an assistant that quietly stores "secrets" insecurely is worse than one
that refuses.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path

from digital_twin.configuration.settings import SecretsConfig
from digital_twin.security.fsacl import ensure_private_dir, ensure_private_file

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_SERVICE = "digital-twin"


class SecretsError(RuntimeError):
    """Secret missing, backend unavailable, or storage failure."""


def check_secret_name(name: object) -> str:
    """Validate a secret name (raises ``ValueError`` — validator-safe)."""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(
            "secret names must be 1-100 chars of letters, digits, '_', "
            "'.', '-' (got %r)" % (name,)
        )
    return name


class SecretStore(ABC):
    """Named secret values. Implementations must never log values."""

    name: str = "abstract"

    @abstractmethod
    def set(self, key: str, value: str) -> None:
        """Store/overwrite one secret."""

    @abstractmethod
    def get(self, key: str) -> str:
        """Return the value or raise :class:`SecretsError` if missing."""

    @abstractmethod
    def delete(self, key: str) -> bool:
        """Remove one secret; ``True`` if it existed."""

    @abstractmethod
    def names(self) -> tuple[str, ...]:
        """Stored secret names (values are never enumerable)."""


class EncryptedFileSecretStore(SecretStore):
    """Fernet-encrypted JSON file; key file created ``0600`` lazily."""

    name = "file"

    def __init__(self, file_path: str | Path, key_path: str | Path):
        self._file = Path(file_path).expanduser()
        self._key_path = Path(key_path).expanduser()
        self._fernet = None  # created lazily: no key file until first use

    # -- internals ------------------------------------------------------
    def _cipher(self):
        if self._fernet is None:
            try:
                from cryptography.fernet import Fernet
            except ImportError as exc:
                raise SecretsError(
                    "The file secrets backend requires the 'cryptography' "
                    "package (pip install cryptography)"
                ) from exc
            self._fernet = Fernet(self._load_or_create_key())
        return self._fernet

    def _load_or_create_key(self) -> bytes:
        if self._key_path.exists():
            return self._key_path.read_bytes().strip()
        from cryptography.fernet import Fernet

        key = Fernet.generate_key()
        # Owner-only directory first: on NTFS this is what actually enforces
        # the guarantee — the 0o600 below only toggles the read-only attribute.
        ensure_private_dir(self._key_path.parent)
        descriptor = os.open(
            self._key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(key)
        ensure_private_file(self._key_path)  # per-file backstop
        logger.info("Created secrets key file %s (owner-only)", self._key_path)
        return key

    def _load(self) -> dict[str, str]:
        if not self._file.exists():
            return {}
        try:
            blob = self._cipher().decrypt(self._file.read_bytes())
            data = json.loads(blob.decode("utf-8"))
        except Exception as exc:  # wrong key, corrupt file
            raise SecretsError(f"cannot decrypt secrets file: {exc}") from exc
        return {str(k): str(v) for k, v in data.items()}

    def _save(self, data: dict[str, str]) -> None:
        blob = self._cipher().encrypt(
            json.dumps(data, ensure_ascii=False).encode("utf-8")
        )
        ensure_private_dir(self._file.parent)
        descriptor, tmp_name = tempfile.mkstemp(
            dir=str(self._file.parent), prefix=".secrets-"
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(blob)
            os.chmod(tmp_name, 0o600)
            ensure_private_file(tmp_name)  # per-file backstop before the swap
            os.replace(tmp_name, self._file)  # atomic
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # -- interface --------------------------------------------------------
    def set(self, key: str, value: str) -> None:
        check_secret_name(key)
        if not isinstance(value, str) or not value:
            raise ValueError("secret value must be a non-empty string")
        data = self._load()
        data[key] = value
        self._save(data)
        logger.info("Stored secret '%s' (file backend)", key)

    def get(self, key: str) -> str:
        check_secret_name(key)
        data = self._load()
        if key not in data:
            raise SecretsError(f"no such secret: '{key}'")
        return data[key]

    def delete(self, key: str) -> bool:
        check_secret_name(key)
        data = self._load()
        existed = data.pop(key, None) is not None
        if existed:
            self._save(data)
            logger.info("Deleted secret '%s' (file backend)", key)
        return existed

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._load()))


class KeyringSecretStore(SecretStore):
    """OS keyring values + a names-only local index (keyring can't list)."""

    name = "keyring"

    def __init__(self, index_path: str | Path):
        try:
            import keyring  # noqa: F401
        except ImportError as exc:
            raise SecretsError(
                "The keyring secrets backend requires the 'keyring' package "
                "(pip install keyring), or set secrets.backend: file"
            ) from exc
        self._keyring = __import__("keyring")
        self._index = Path(index_path).expanduser()

    def _load_index(self) -> set[str]:
        if not self._index.exists():
            return set()
        return {
            line.strip()
            for line in self._index.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }

    def _save_index(self, index: set[str]) -> None:
        self._index.parent.mkdir(parents=True, exist_ok=True)
        self._index.write_text(
            "\n".join(sorted(index)) + ("\n" if index else ""),
            encoding="utf-8",
        )

    def set(self, key: str, value: str) -> None:
        check_secret_name(key)
        if not isinstance(value, str) or not value:
            raise ValueError("secret value must be a non-empty string")
        self._keyring.set_password(_SERVICE, key, value)
        index = self._load_index()
        index.add(key)
        self._save_index(index)
        logger.info("Stored secret '%s' (keyring backend)", key)

    def get(self, key: str) -> str:
        check_secret_name(key)
        value = self._keyring.get_password(_SERVICE, key)
        if value is None:
            raise SecretsError(f"no such secret: '{key}'")
        return value

    def delete(self, key: str) -> bool:
        check_secret_name(key)
        index = self._load_index()
        existed = key in index
        try:
            self._keyring.delete_password(_SERVICE, key)
            existed = True
        except Exception:
            pass  # not in the keyring; index may still know it
        if key in index:
            index.discard(key)
            self._save_index(index)
        if existed:
            logger.info("Deleted secret '%s' (keyring backend)", key)
        return existed

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._load_index()))


def create_secret_store(config: SecretsConfig) -> SecretStore:
    """Build the configured backend; ``auto`` prefers the OS keyring."""
    if config.backend == "file":
        return EncryptedFileSecretStore(config.file_path, config.key_path)
    if config.backend == "keyring":
        return KeyringSecretStore(config.index_path)
    # auto
    try:
        store = KeyringSecretStore(config.index_path)
        logger.info("Secrets backend: keyring (auto)")
        return store
    except SecretsError:
        logger.info("Secrets backend: encrypted file (auto; keyring "
                    "package not installed)")
        return EncryptedFileSecretStore(config.file_path, config.key_path)
