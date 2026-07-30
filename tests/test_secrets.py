"""Tests for the secrets manager: file/keyring backends, at-rest
encryption, name discipline, and the CLI."""

from __future__ import annotations

import os
import stat
import sys
import types

import pytest

from digital_twin.configuration.settings import SecretsConfig
from digital_twin.security.secrets import (
    EncryptedFileSecretStore,
    KeyringSecretStore,
    SecretsError,
    check_secret_name,
    create_secret_store,
)
from digital_twin.security import secrets_cli
from tests._keyfile import assert_key_owner_only


# ---------------------------------------------------------------------------
# Name discipline
# ---------------------------------------------------------------------------
def test_secret_names_are_validated():
    assert check_secret_name("github_token") == "github_token"
    assert check_secret_name("a.b-c_1") == "a.b-c_1"
    for bad in ("", "with space", "no/slash", "-leading", None, 42, "x" * 101):
        with pytest.raises(ValueError):
            check_secret_name(bad)


# ---------------------------------------------------------------------------
# Encrypted file backend
# ---------------------------------------------------------------------------
@pytest.fixture()
def file_store(tmp_path):
    return EncryptedFileSecretStore(
        tmp_path / "secrets.enc", tmp_path / "secrets.key"
    )


def test_file_store_roundtrip_delete_list(file_store):
    file_store.set("api_key", "hunter2")
    file_store.set("db_pass", "s3cret")
    assert file_store.get("api_key") == "hunter2"
    assert file_store.names() == ("api_key", "db_pass")
    assert file_store.delete("api_key") is True
    assert file_store.delete("api_key") is False
    with pytest.raises(SecretsError, match="no such secret"):
        file_store.get("api_key")


def test_file_store_encrypts_at_rest(file_store, tmp_path):
    file_store.set("token", "PLAINTEXT-CANARY-VALUE")
    blob = (tmp_path / "secrets.enc").read_bytes()
    assert b"PLAINTEXT-CANARY-VALUE" not in blob
    assert b"token" not in blob  # names are inside the ciphertext too


def test_file_store_key_file_permissions_and_lazy_creation(file_store,
                                                           tmp_path):
    key_path = tmp_path / "secrets.key"
    assert not key_path.exists()  # nothing until first use
    file_store.set("k", "v")
    assert key_path.exists()
    assert_key_owner_only(key_path)


def test_file_store_wrong_key_fails_loudly(tmp_path):
    store = EncryptedFileSecretStore(tmp_path / "s.enc", tmp_path / "k1.key")
    store.set("a", "b")
    (tmp_path / "k1.key").unlink()  # a new key will be generated
    fresh = EncryptedFileSecretStore(tmp_path / "s.enc", tmp_path / "k1.key")
    with pytest.raises(SecretsError, match="cannot decrypt"):
        fresh.get("a")


def test_file_store_rejects_empty_values(file_store):
    with pytest.raises(ValueError):
        file_store.set("k", "")
    with pytest.raises(ValueError):
        file_store.set("k", None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Keyring backend (against a fake in-process keyring module)
# ---------------------------------------------------------------------------
@pytest.fixture()
def fake_keyring(monkeypatch):
    vault: dict[tuple[str, str], str] = {}
    module = types.ModuleType("keyring")
    module.set_password = lambda svc, key, val: vault.__setitem__((svc, key), val)
    module.get_password = lambda svc, key: vault.get((svc, key))

    def _delete(svc, key):
        if (svc, key) not in vault:
            raise RuntimeError("not found")
        del vault[(svc, key)]

    module.delete_password = _delete
    monkeypatch.setitem(sys.modules, "keyring", module)
    return vault


def test_keyring_store_roundtrip_with_names_index(fake_keyring, tmp_path):
    store = KeyringSecretStore(tmp_path / "secrets.index")
    store.set("mail_pw", "hunter2")
    assert store.get("mail_pw") == "hunter2"
    assert store.names() == ("mail_pw",)
    # The index holds names only — never values.
    index_text = (tmp_path / "secrets.index").read_text()
    assert "mail_pw" in index_text and "hunter2" not in index_text
    assert store.delete("mail_pw") is True
    assert store.names() == ()


def test_backend_selection(fake_keyring, tmp_path, monkeypatch):
    config = SecretsConfig(
        backend="auto",
        file_path=str(tmp_path / "s.enc"),
        key_path=str(tmp_path / "k.key"),
        index_path=str(tmp_path / "i.index"),
    )
    assert create_secret_store(config).name == "keyring"  # auto prefers OS
    monkeypatch.delitem(sys.modules, "keyring")
    monkeypatch.setattr(
        "builtins.__import__",
        _blocking_import("keyring", __import__),
    )
    assert create_secret_store(config).name == "file"  # auto falls back
    with pytest.raises(SecretsError, match="keyring"):
        create_secret_store(SecretsConfig(backend="keyring",
                                          index_path=str(tmp_path / "i2")))


def _blocking_import(blocked: str, real):
    def _import(name, *args, **kwargs):
        if name == blocked:
            raise ImportError(f"{blocked} blocked for test")
        return real(name, *args, **kwargs)
    return _import


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def test_cli_set_get_list_delete(tmp_path, monkeypatch, capsys):
    config = tmp_path / "config.yaml"
    config.write_text(
        "secrets:\n"
        "  backend: file\n"
        f"  file_path: {tmp_path / 's.enc'}\n"
        f"  key_path: {tmp_path / 'k.key'}\n"
        f"  index_path: {tmp_path / 'i.index'}\n"
    )
    base = ["--config", str(config)]

    assert secrets_cli.main(base + ["set", "gh_token"],
                            value_reader=lambda: "tok-123") == 0
    assert secrets_cli.main(base + ["list"]) == 0
    out = capsys.readouterr().out
    assert "gh_token" in out and "tok-123" not in out

    assert secrets_cli.main(base + ["get", "gh_token"]) == 0
    out = capsys.readouterr().out
    assert "exists" in out and "tok-123" not in out  # hidden by default

    assert secrets_cli.main(base + ["get", "gh_token", "--reveal"]) == 0
    assert "tok-123" in capsys.readouterr().out

    assert secrets_cli.main(base + ["delete", "gh_token"]) == 0
    assert secrets_cli.main(base + ["delete", "gh_token"]) == 1  # gone
    assert secrets_cli.main(base + ["get", "gh_token"]) == 1
