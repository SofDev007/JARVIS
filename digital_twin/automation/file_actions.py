"""File-system intelligence: the first DANGEROUS-class actions.

Since M3 the permission system has carried a code-enforced invariant that
no action ever exercised: DANGEROUS + configured ``allow`` clamps to
CONFIRM. This module finally gives that clamp real work. The safety model
has three independent layers, each of which alone would prevent the worst
outcome:

1. **Containment.** Every path parameter must resolve — *after* symlink
   resolution — inside one of the configured ``files.allowed_roots``. The
   default allow-list is empty (the ``open_application`` precedent):
   the capability exists, but can touch nothing until the user names
   directories. Containment is enforced at validation time (before the
   permission gate, so nobody is asked to confirm garbage) and enforced
   *again* inside every handler (validation-time checks can go stale).
2. **Risk stratification.** Read-side actions (``list_files``,
   ``search_files``, ``read_text_file``, ``find_duplicates``) and
   non-clobbering writes (``create_directory``, ``write_text_file``,
   ``copy_file`` — all refuse to overwrite anything) are SENSITIVE:
   confirmed by default, allow-able per action. ``move_file`` and
   ``delete_file`` relocate existing user data — DANGEROUS: the policy
   floor guarantees a human approves each one, no matter what the
   configuration says.
3. **Recoverability.** ``delete_file`` never unlinks: it moves the file
   into a per-root trash directory with a timestamped name. A confirmed
   mistake is an inconvenience, not a loss. (Trash directories are hidden
   from listings/search/duplicate scans; restore = move the file back out.)

Batch operations come for free: the M8 planner runs any sequence of these
as plan steps, each individually gated — exactly the "batch operations
with confirmation" the master spec asked for.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from digital_twin.automation.registry import ActionRegistry, ActionSpec
from digital_twin.configuration.settings import FilesConfig
from digital_twin.security.permissions import RiskLevel

logger = logging.getLogger(__name__)

_MAX_PATTERN_LEN = 100
_MAX_QUERY_LEN = 100
_HASH_CHUNK = 1 << 16


# ---------------------------------------------------------------------------
# Path containment — the core primitive everything else leans on
# ---------------------------------------------------------------------------
def resolve_roots(roots: Iterable[str]) -> tuple[Path, ...]:
    """Expand and resolve the configured allow-list once."""
    return tuple(Path(root).expanduser().resolve() for root in roots)


def resolve_within(raw: Any, roots: tuple[Path, ...]) -> Path:
    """Resolve one path parameter and prove it lives inside a root.

    Raises ``ValueError`` (the validator contract) for anything else:
    missing configuration, non-string, relative paths, and — the case that
    matters — paths that escape the roots via ``..`` or symlinks, because
    resolution happens *before* the containment check.
    """
    if not roots:
        raise ValueError(
            "no files.allowed_roots configured — add the directories file "
            "actions may work in (e.g. [~/Documents]) to the files section"
        )
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("a non-empty path string is required")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError(f"path must be absolute: {raw!r}")
    resolved = path.resolve()
    for root in roots:
        if resolved == root or root in resolved.parents:
            return resolved
    raise ValueError(
        f"path {raw!r} is outside the allowed roots "
        f"{[str(root) for root in roots]}"
    )


def _in_trash(path: Path, trash_name: str) -> bool:
    return trash_name in path.parts


def _iter_files(base: Path, pattern: str, recursive: bool,
                trash_name: str) -> Iterable[Path]:
    """Yield matching entries under ``base``, never descending into trash."""
    iterator = base.rglob(pattern) if recursive else base.glob(pattern)
    for entry in iterator:
        if _in_trash(entry.relative_to(base), trash_name):
            continue
        yield entry


def _summarize(names: list[str], total: int, noun: str) -> str:
    listed = ", ".join(names[:10])
    more = f", … ({total} {noun} total)" if total > len(names[:10]) else ""
    return f"{total} {noun}: {listed}{more}" if total else f"0 {noun}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
def register_file_actions(registry: ActionRegistry, config: FilesConfig) -> None:
    """Register the file-intelligence actions onto ``registry``."""
    roots = resolve_roots(config.allowed_roots)
    trash_name = config.trash_dir_name

    def _dir(raw: Any) -> Path:
        path = resolve_within(raw, roots)
        if not path.is_dir():
            raise ValueError(f"not an existing directory: {raw!r}")
        return path

    def _existing_file(raw: Any) -> Path:
        path = resolve_within(raw, roots)
        if not path.is_file():
            raise ValueError(f"not an existing file: {raw!r}")
        return path

    def _validate_pattern(params: Mapping[str, Any], key: str,
                          max_len: int) -> None:
        value = params.get(key)
        if value is None:
            return
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"'{key}' must be a non-empty string")
        if len(value) > max_len:
            raise ValueError(f"'{key}' exceeds {max_len} characters")
        if "/" in value or "\\" in value or ".." in value:
            raise ValueError(f"'{key}' must not contain path separators")

    # -- list_files (SENSITIVE) -----------------------------------------
    def validate_list_files(params: Mapping[str, Any]) -> None:
        _dir(params.get("path"))
        _validate_pattern(params, "pattern", _MAX_PATTERN_LEN)
        if not isinstance(params.get("recursive", False), bool):
            raise ValueError("'recursive' must be a boolean")

    def handle_list_files(params: Mapping[str, Any]) -> str:
        base = _dir(params.get("path"))
        pattern = str(params.get("pattern") or "*")
        recursive = bool(params.get("recursive", False))
        names: list[str] = []
        total = 0
        for entry in _iter_files(base, pattern, recursive, trash_name):
            total += 1
            if total > config.max_list_entries:
                total = config.max_list_entries
                names.append("…capped")
                break
            suffix = "/" if entry.is_dir() else ""
            names.append(str(entry.relative_to(base)) + suffix)
        return _summarize(sorted(names), total, "entries")

    registry.register(ActionSpec(
        name="list_files",
        description="List directory contents inside the allowed roots.",
        risk=RiskLevel.SENSITIVE,
        handler=handle_list_files,
        validate=validate_list_files,
    ))

    # -- search_files (SENSITIVE) ---------------------------------------
    def validate_search_files(params: Mapping[str, Any]) -> None:
        _dir(params.get("path"))
        query = params.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("search_files requires a non-empty 'query' string")
        if len(query) > _MAX_QUERY_LEN:
            raise ValueError(f"'query' exceeds {_MAX_QUERY_LEN} characters")

    def handle_search_files(params: Mapping[str, Any]) -> str:
        base = _dir(params.get("path"))
        needle = str(params["query"]).lower()
        names: list[str] = []
        total = 0
        for entry in _iter_files(base, "*", recursive=True,
                                 trash_name=trash_name):
            if needle not in entry.name.lower():
                continue
            total += 1
            if total > config.max_list_entries:
                total = config.max_list_entries
                names.append("…capped")
                break
            names.append(str(entry.relative_to(base)))
        return _summarize(sorted(names), total, "matches")

    registry.register(ActionSpec(
        name="search_files",
        description="Find files by name substring under an allowed root.",
        risk=RiskLevel.SENSITIVE,
        handler=handle_search_files,
        validate=validate_search_files,
    ))

    # -- read_text_file (SENSITIVE) ---------------------------------------
    def validate_read_text_file(params: Mapping[str, Any]) -> None:
        _existing_file(params.get("path"))

    def handle_read_text_file(params: Mapping[str, Any]) -> str:
        path = _existing_file(params.get("path"))
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(config.max_read_chars + 1)
        truncated = len(text) > config.max_read_chars
        text = text[: config.max_read_chars]
        marker = " …(truncated)" if truncated else ""
        return f"{path.name} ({len(text)} chars{marker}): {text}"

    registry.register(ActionSpec(
        name="read_text_file",
        description="Read a bounded amount of text from a file.",
        risk=RiskLevel.SENSITIVE,
        handler=handle_read_text_file,
        validate=validate_read_text_file,
    ))

    # -- find_duplicates (SENSITIVE) --------------------------------------
    def validate_find_duplicates(params: Mapping[str, Any]) -> None:
        _dir(params.get("path"))

    def handle_find_duplicates(params: Mapping[str, Any]) -> str:
        base = _dir(params.get("path"))
        by_size: dict[int, list[Path]] = {}
        scanned = 0
        capped = False
        for entry in _iter_files(base, "*", recursive=True,
                                 trash_name=trash_name):
            if not entry.is_file():
                continue
            scanned += 1
            if scanned > config.max_scan_files:
                capped = True
                break
            by_size.setdefault(entry.stat().st_size, []).append(entry)
        groups: list[list[Path]] = []
        for candidates in by_size.values():
            if len(candidates) < 2:
                continue
            by_hash: dict[str, list[Path]] = {}
            for candidate in candidates:
                try:
                    by_hash.setdefault(_sha256(candidate), []).append(candidate)
                except OSError:
                    continue
            groups.extend(g for g in by_hash.values() if len(g) > 1)
        if not groups:
            note = " (scan capped)" if capped else ""
            return f"no duplicates among {min(scanned, config.max_scan_files)} files{note}"
        parts = [
            " == ".join(sorted(str(p.relative_to(base)) for p in group))
            for group in groups[:5]
        ]
        note = "; scan capped" if capped else ""
        return (f"{len(groups)} duplicate group(s): "
                + "; ".join(parts) + note)

    registry.register(ActionSpec(
        name="find_duplicates",
        description="Detect identical files (size + SHA-256) under a root.",
        risk=RiskLevel.SENSITIVE,
        handler=handle_find_duplicates,
        validate=validate_find_duplicates,
    ))

    # -- create_directory (SENSITIVE) --------------------------------------
    def validate_create_directory(params: Mapping[str, Any]) -> None:
        path = resolve_within(params.get("path"), roots)
        if path.exists():
            raise ValueError(f"already exists: {params.get('path')!r}")

    def handle_create_directory(params: Mapping[str, Any]) -> str:
        path = resolve_within(params.get("path"), roots)
        path.mkdir(parents=True, exist_ok=False)
        return f"created directory {path}"

    registry.register(ActionSpec(
        name="create_directory",
        description="Create a new directory inside the allowed roots.",
        risk=RiskLevel.SENSITIVE,
        handler=handle_create_directory,
        validate=validate_create_directory,
    ))

    # -- write_text_file (SENSITIVE — refuses to overwrite) ----------------
    def validate_write_text_file(params: Mapping[str, Any]) -> None:
        path = resolve_within(params.get("path"), roots)
        if path.exists():
            raise ValueError(
                f"refusing to overwrite existing path: {params.get('path')!r} "
                "(there is deliberately no overwrite mode — move or delete "
                "the existing file first, each behind its own gate)"
            )
        text = params.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError("write_text_file requires a non-empty 'text' string")
        if len(text) > config.max_write_chars:
            raise ValueError(
                f"write_text_file: text exceeds {config.max_write_chars} characters"
            )

    def handle_write_text_file(params: Mapping[str, Any]) -> str:
        path = resolve_within(params.get("path"), roots)
        if not path.parent.is_dir():
            raise ValueError(
                f"parent directory does not exist: {path.parent} "
                "(use create_directory first)"
            )
        # 'x' = exclusive create: overwrite is impossible even if the file
        # appeared between validation and execution.
        with path.open("x", encoding="utf-8") as handle:
            handle.write(str(params["text"]))
        return f"wrote {len(str(params['text']))} characters to {path}"

    registry.register(ActionSpec(
        name="write_text_file",
        description="Write text to a NEW file (never overwrites).",
        risk=RiskLevel.SENSITIVE,
        handler=handle_write_text_file,
        validate=validate_write_text_file,
    ))

    # -- copy_file (SENSITIVE — refuses to overwrite) -----------------------
    def validate_copy_file(params: Mapping[str, Any]) -> None:
        _existing_file(params.get("source"))
        destination = resolve_within(params.get("destination"), roots)
        if destination.exists():
            raise ValueError(
                f"destination already exists: {params.get('destination')!r}"
            )

    def handle_copy_file(params: Mapping[str, Any]) -> str:
        source = _existing_file(params.get("source"))
        destination = resolve_within(params.get("destination"), roots)
        if destination.exists():
            raise FileExistsError(f"destination already exists: {destination}")
        if not destination.parent.is_dir():
            raise ValueError(
                f"parent directory does not exist: {destination.parent}"
            )
        shutil.copy2(source, destination)
        return f"copied {source.name} -> {destination}"

    registry.register(ActionSpec(
        name="copy_file",
        description="Copy a file within the allowed roots (never overwrites).",
        risk=RiskLevel.SENSITIVE,
        handler=handle_copy_file,
        validate=validate_copy_file,
    ))

    # -- move_file (DANGEROUS) ----------------------------------------------
    def validate_move_file(params: Mapping[str, Any]) -> None:
        _existing_file(params.get("source"))
        destination = resolve_within(params.get("destination"), roots)
        if destination.exists():
            raise ValueError(
                f"destination already exists: {params.get('destination')!r}"
            )

    def handle_move_file(params: Mapping[str, Any]) -> str:
        source = _existing_file(params.get("source"))
        destination = resolve_within(params.get("destination"), roots)
        if destination.exists():
            raise FileExistsError(f"destination already exists: {destination}")
        if not destination.parent.is_dir():
            raise ValueError(
                f"parent directory does not exist: {destination.parent}"
            )
        shutil.move(str(source), str(destination))
        return f"moved {source} -> {destination}"

    registry.register(ActionSpec(
        name="move_file",
        description=("Move/rename a file within the allowed roots "
                     "(never overwrites; hard to undo unnoticed)."),
        risk=RiskLevel.DANGEROUS,
        handler=handle_move_file,
        validate=validate_move_file,
    ))

    # -- delete_file (DANGEROUS — trash, never unlink) ------------------------
    def validate_delete_file(params: Mapping[str, Any]) -> None:
        path = _existing_file(params.get("path"))
        if _in_trash(path, trash_name):
            raise ValueError(
                "already in the trash directory — restore or remove it "
                "manually; the assistant never destroys data permanently"
            )

    def handle_delete_file(params: Mapping[str, Any]) -> str:
        path = _existing_file(params.get("path"))
        root = next(r for r in roots if r == path or r in path.parents)
        trash = root / trash_name
        trash.mkdir(exist_ok=True)
        destination = trash / f"{int(time.time())}_{path.name}"
        counter = 0
        while destination.exists():  # same second, same name
            counter += 1
            destination = trash / f"{int(time.time())}_{counter}_{path.name}"
        shutil.move(str(path), str(destination))
        return f"moved to trash: {destination} (restore by moving it back)"

    registry.register(ActionSpec(
        name="delete_file",
        description=("Delete a file by moving it into the per-root trash "
                     "directory (recoverable; never unlinks)."),
        risk=RiskLevel.DANGEROUS,
        handler=handle_delete_file,
        validate=validate_delete_file,
    ))
