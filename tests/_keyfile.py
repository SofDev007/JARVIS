"""Shared assertion: an encryption key file must be readable only by its
owner.

On POSIX that guarantee is a file mode of ``0o600``. On Windows ``st_mode``
is meaningless for this — NTFS has no POSIX permission bits, and
``os.open(..., 0o600)`` only toggles the read-only attribute, never the ACL.
So on Windows we inspect the ACL via ``icacls`` and require that no broad
principal (Everyone / BUILTIN\\Users / Authenticated Users) is granted access,
while the owner still is.

NOTE (scope): this verifies the ACL of the *file that was created*, which is
inherited from its parent directory. It confirms owner-only creation in a
properly scoped directory (e.g. a per-user temp dir). It does NOT prove the
key-creation code enforces an owner-only ACL independent of where the file
lands — on Windows it currently does not. See CHANGELOG / M18 notes.
"""

from __future__ import annotations

import getpass
import subprocess
import sys
from pathlib import Path

_BROAD_PRINCIPALS = ("Everyone", "BUILTIN\\Users", "Authenticated Users")


def assert_key_owner_only(key_path: Path | str) -> None:
    key_path = Path(key_path)
    if sys.platform != "win32":
        mode = key_path.stat().st_mode & 0o777
        assert mode == 0o600, f"{key_path} mode is {oct(mode)}, expected 0o600"
        return

    out = subprocess.run(
        ["icacls", str(key_path)], capture_output=True, text=True, check=True
    ).stdout
    for principal in _BROAD_PRINCIPALS:
        assert principal not in out, (
            f"{key_path} grants access to broad principal {principal!r}:\n{out}"
        )
    # And the owner must still have access — directly or via the OWNER RIGHTS
    # SID — so a file nobody can read does not silently pass.
    assert getpass.getuser().lower() in out.lower() or "OWNER RIGHTS" in out, (
        f"{key_path} grants the owner no access:\n{out}"
    )
