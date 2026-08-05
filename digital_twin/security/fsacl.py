"""Owner-only filesystem ACLs, cross-platform.

POSIX permission bits do not exist on NTFS: ``os.chmod(path, 0o600)`` there
only toggles the read-only attribute and never touches the ACL. A file created
under ``data/`` or ``logs/`` therefore *inherits* whatever broad ACEs the parent
tree carries (typically ``BUILTIN\\Users`` and ``Authenticated Users``), so the
"owner-only" guarantee ``os.open(..., 0o600)`` is meant to give is not met on
Windows. This module closes that gap.

The **primary mechanism is directory-level**: :func:`ensure_private_dir` strips
inheritance from the directory and grants only the current user (retaining
SYSTEM and Administrators — already-privileged principals needed for backup and
recovery). The ``(OI)(CI)`` inheritance flags then propagate that owner-only ACL
to every file created inside, which is why per-file locking is a backstop, not
the main event. :func:`verify_private` is the durability check: it reports any
broad principal still granted access, so a regenerated directory is caught
before secrets land in it.

On POSIX the same three helpers use ``chmod`` (0o700 for dirs, 0o600 for files).

Implemented with ``subprocess`` + ``icacls`` rather than pywin32: it adds no
dependency and stays consistent with ``tests/_keyfile.py``, which already parses
``icacls`` output.
"""

from __future__ import annotations

import getpass
import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

#: Broad principals that must never appear in a private path's ACL. Same set
#: ``tests/_keyfile.py`` asserts against, with ``Everyone`` added. SYSTEM and
#: Administrators are deliberately absent — their presence is not the finding.
BROAD_PRINCIPALS = ("Everyone", "BUILTIN\\Users", "Authenticated Users")

#: Well-known SIDs we retain, addressed by SID so the grant works regardless of
#: how the account names are localised. NT AUTHORITY\SYSTEM and
#: BUILTIN\Administrators.
_SYSTEM_SID = "*S-1-5-18"
_ADMINS_SID = "*S-1-5-32-544"


def _icacls(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["icacls", *args], capture_output=True, text=True)


def _current_user() -> str:
    """``DOMAIN\\user`` (unambiguous) or the bare user name as a fallback."""
    domain = os.environ.get("USERDOMAIN", "").strip()
    user = (os.environ.get("USERNAME") or getpass.getuser()).strip()
    return f"{domain}\\{user}" if domain else user


def _grant(path: Path, inheritable: bool) -> None:
    """icacls: strip inheritance, grant only owner + SYSTEM + Administrators."""
    flags = "(OI)(CI)F" if inheritable else "F"
    grants = [
        f"{_current_user()}:{flags}",
        f"{_SYSTEM_SID}:{flags}",
        f"{_ADMINS_SID}:{flags}",
    ]
    proc = _icacls(str(path), "/inheritance:r", "/grant:r", *grants)
    if proc.returncode != 0:
        logger.warning(
            "icacls could not secure %s: %s",
            path, (proc.stderr or proc.stdout).strip(),
        )


def ensure_private_dir(path: str | Path) -> Path:
    """Create *path* if absent, then make it owner-only (inheritable).

    Idempotent and safe to call on every startup — a directory that was
    regenerated with broad inherited ACEs is re-secured here.
    """
    path = Path(path).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    if _IS_WINDOWS:
        _grant(path, inheritable=True)
    else:
        os.chmod(path, 0o700)
    return path


def ensure_private_file(path: str | Path) -> Path:
    """Make an existing file owner-only. Per-file backstop to directory
    inheritance; no-op-ish if the parent ACL already did the job."""
    path = Path(path).expanduser()
    if _IS_WINDOWS:
        _grant(path, inheritable=False)
    else:
        os.chmod(path, 0o600)
    return path


def verify_private(path: str | Path) -> list[str]:
    """Return the broad principals granted access to *path* (``[]`` = private).

    Reads the ACL and never mutates. Missing paths return ``[]`` (nothing to
    leak yet). Never raises — a check that crashes the app is worse than one
    that logs and moves on.
    """
    path = Path(path).expanduser()
    try:
        if not path.exists():
            return []
        if _IS_WINDOWS:
            proc = _icacls(str(path))
            if proc.returncode != 0:
                logger.warning("icacls read failed for %s: %s",
                               path, proc.stderr.strip())
                return []
            out = proc.stdout
            return [p for p in BROAD_PRINCIPALS if p in out]
        mode = path.stat().st_mode & 0o777
        limit = 0o700 if path.is_dir() else 0o600
        if mode & 0o077:
            return [f"mode {oct(mode)} (expected {oct(limit)})"]
        return []
    except OSError as exc:
        logger.warning("Could not read ACL of %s: %s", path, exc)
        return []


def _state_dirs(config) -> list[Path]:
    """State directories that must stay owner-only, derived from config paths.

    There is deliberately no single chokepoint for these paths (they are literal
    relative strings across several config classes — see the M18 CHANGELOG debt
    note), so we gather the parents of every at-rest file plus the log dir and
    dedupe. Today they all collapse to ``data/`` and ``logs/``.
    """
    files = [
        config.secrets.file_path,
        config.secrets.key_path,
        config.secrets.index_path,
        config.memory.db_path,
        config.memory.key_path,
        config.knowledge.db_path,
        config.security.audit_file,
    ]
    dirs = {Path(f).expanduser().parent for f in files}
    dirs.add(Path(config.logging.directory).expanduser())
    return sorted(dirs, key=str)


def secure_and_verify_state(config) -> list[tuple[Path, str]]:
    """Startup hardening: secure the state directories, then verify them and
    their contents. Logs a prominent warning for every broad grant found and
    returns the offenders as ``(path, principal)`` pairs. Never raises.

    Directories are checked, not just known filenames, so a regenerated
    directory is caught *before* any secret is written into it.
    """
    offenders: list[tuple[Path, str]] = []
    for directory in _state_dirs(config):
        try:
            ensure_private_dir(directory)
        except OSError as exc:
            logger.warning("Could not secure state directory %s: %s",
                           directory, exc)
        targets = [directory]
        try:
            targets.extend(sorted(directory.iterdir()))
        except OSError:
            pass
        for target in targets:
            for principal in verify_private(target):
                logger.warning(
                    "INSECURE PERMISSIONS: %s grants access to broad principal "
                    "'%s' — expected owner-only. Remediating on next write; "
                    "investigate if this recurs.",
                    target, principal,
                )
                offenders.append((target, principal))
    return offenders
