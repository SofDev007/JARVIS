"""Tests for owner-only filesystem ACL enforcement (the M18 security finding).

The point these prove that the pre-existing key-file tests did not: the
guarantee holds *regardless of where the file lands*, even inside a directory
that carries broad inherited ACEs — because enforcement is now active
(strip-inheritance / chmod), not a happy accident of the temp dir's ACL.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import types

from digital_twin.security.fsacl import (
    ensure_private_dir,
    secure_and_verify_state,
    verify_private,
)
from digital_twin.security.secrets import EncryptedFileSecretStore
from tests._keyfile import assert_key_owner_only


def _make_broad(path, *, is_dir: bool) -> None:
    """Grant a broad principal access to *path* (BUILTIN\\Users / world rw)."""
    if sys.platform == "win32":
        flags = "(OI)(CI)F" if is_dir else "F"
        subprocess.run(
            ["icacls", str(path), "/grant", f"*S-1-5-32-545:{flags}"],
            capture_output=True, text=True, check=True,
        )
    else:
        os.chmod(path, 0o777 if is_dir else 0o666)


def test_key_is_owner_only_despite_a_broad_parent_directory(tmp_path):
    broad = tmp_path / "broad"
    broad.mkdir()
    _make_broad(broad, is_dir=True)
    assert verify_private(broad), "sanity: parent must actually be broad now"

    store = EncryptedFileSecretStore(broad / "secrets.enc", broad / "secrets.key")
    store.set("api_key", "hunter2")

    # The whole finding in one assertion: created in a world-accessible dir,
    # the key is still owner-only.
    assert_key_owner_only(broad / "secrets.key")
    assert verify_private(broad / "secrets.key") == []


def test_verify_private_detects_an_injected_broad_ace(tmp_path):
    directory = tmp_path / "d"
    ensure_private_dir(directory)
    assert verify_private(directory) == []  # locked down on creation

    _make_broad(directory, is_dir=True)
    assert verify_private(directory), "an injected broad ACE must be detected"


def test_startup_check_logs_and_does_not_raise(tmp_path, caplog):
    data = tmp_path / "data"
    logs = tmp_path / "logs"
    data.mkdir()
    logs.mkdir()
    ns = types.SimpleNamespace
    config = ns(
        secrets=ns(file_path=str(data / "s.enc"),
                   key_path=str(data / "s.key"),
                   index_path=str(data / "s.index")),
        memory=ns(db_path=str(data / "m.db"), key_path=str(data / "m.key")),
        knowledge=ns(db_path=str(data / "k.db")),
        security=ns(audit_file=str(logs / "audit.jsonl")),
        logging=ns(directory=str(logs)),
    )
    # A leftover file with an explicit broad ACE: ensure_private_dir re-secures
    # the directory but not this file, so the verify pass must still flag it.
    leftover = data / "leftover.txt"
    leftover.write_text("x")
    _make_broad(leftover, is_dir=False)

    with caplog.at_level(logging.WARNING):
        offenders = secure_and_verify_state(config)  # must not raise

    assert any(str(leftover) == str(path) for path, _ in offenders)
    assert any("INSECURE PERMISSIONS" in rec.message for rec in caplog.records)
    # And the directory itself was re-secured (regenerated-dir durability).
    assert verify_private(data) == []
