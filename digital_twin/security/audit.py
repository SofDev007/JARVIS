"""Audit log: an append-only, tamper-evident record of everything the
assistant tried to do.

Every action request — allowed, confirmed, denied, failed, dropped — lands
here as one JSON line with full provenance: the intent event id and the
original perception event id, so any executed action can be traced back to
the exact gesture (or, later, voice command) that caused it.

Design choices:

* **JSONL, append-only.** Trivially greppable, machine-parseable, and no
  in-place mutation — the audit trail is evidence, not state.
* **Hash chain (M18 Phase B).** Each record carries ``prev``, the SHA-256 of
  the *canonical* form of the record before it (the first chains to a genesis
  constant). Any mutation of a middle record, deletion, or reordering breaks
  the chain and :func:`verify_chain` names the first break. This is only
  meaningful because M18 Phase A made the log owner-only: a hash chain over a
  file any local account can rewrite wholesale is theatre — the attacker just
  recomputes the chain over doctored records.
* **Size-based rollover** to timestamped files keeps the active log bounded
  without ever discarding history. The chain continues *across* the boundary:
  the first record of a new file chains to the last record of the rolled one,
  because the running hash lives in memory and survives the rename.
* **Never breaks the pipeline.** A full disk or unwritable path logs an error
  and drops the entry; auditing failures must not stop the assistant (they
  are, however, loudly visible in the normal logs).

Tail-record note: the chain protects every record whose successor exists, so
mutation of the *last* record on disk is not detectable by the chain alone
(there is no following ``prev`` to contradict). Owner-only ACLs (Phase A) are
what bound that gap; a sealed tail marker is future work.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: The first record chains to this. 64 hex zeros = "no predecessor".
GENESIS = "0" * 64


# ---------------------------------------------------------------------------
# Canonical serialisation + hashing (module-level so the writer, the verifier
# and the migration tool all agree byte-for-byte)
# ---------------------------------------------------------------------------
def _canonical(entry: dict[str, Any]) -> bytes:
    """Reproducible bytes for hashing: sorted keys, fixed separators, UTF-8.

    The on-disk line *is* this canonical form, so re-reading a line, parsing
    it, and re-canonicalising reproduces exactly these bytes — which is what
    makes the stored hash verifiable independently of who wrote it.
    """
    return json.dumps(
        entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _record_hash(entry: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(entry)).hexdigest()


def _normalise(entry: dict[str, Any]) -> dict[str, Any]:
    """Round-trip through JSON so non-native values (enums, etc.) become the
    exact strings the line will hold — otherwise the hash would not survive a
    write/read cycle."""
    return json.loads(json.dumps(entry, ensure_ascii=False, default=str))


# ---------------------------------------------------------------------------
# Chain file ordering (rolled files, oldest first, then the active file)
# ---------------------------------------------------------------------------
def _ordered_chain_files(path: Path) -> list[Path]:
    """Every file in the chain, chronological: rolled files then the active one.

    Rolled files are named ``<stem>-YYYYMMDDTHHMMSS<suffix>`` and, for
    same-second rolls, ``<stem>-YYYYMMDDTHHMMSS-<n><suffix>``. A plain lexical
    sort is *wrong* there — ``-1`` sorts before ``.`` — so we sort on the
    parsed ``(stamp, counter)`` key, which is the actual creation order.
    """
    pattern = re.compile(
        re.escape(path.stem) + r"-(\d{8}T\d{6})(?:-(\d+))?"
        + re.escape(path.suffix) + r"$"
    )
    rolled: list[tuple[str, int, Path]] = []
    for candidate in path.parent.glob(f"{path.stem}-*{path.suffix}"):
        match = pattern.match(candidate.name)
        if match:
            rolled.append((match.group(1), int(match.group(2) or 0), candidate))
    rolled.sort(key=lambda item: (item[0], item[1]))
    files = [candidate for _, _, candidate in rolled]
    if path.exists():
        files.append(path)
    return files


def _walk_chain(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    """Walk the whole chain once. Return ``(break_or_None, record_hashes)``.

    ``record_hashes`` holds every verified record's hash in order (up to a
    break, if any) — the anchor check needs these, the plain verifier ignores
    them.
    """
    expected = GENESIS
    index = 0
    hashes: list[str] = []
    for chain_file in _ordered_chain_files(path):
        try:
            lines = chain_file.read_text(encoding="utf-8-sig").splitlines()
        except OSError as exc:
            return ({"index": index, "file": chain_file.name,
                     "reason": f"cannot read file: {exc}"}, hashes)
        for line in lines:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                return ({"index": index, "file": chain_file.name,
                         "reason": f"line is not valid JSON: {exc}"}, hashes)
            if "prev" not in entry:
                return ({"index": index, "file": chain_file.name,
                         "reason": "record has no 'prev' field — log predates "
                                   "hash chaining (run 'python -m "
                                   "digital_twin.security.audit_cli migrate')"},
                        hashes)
            if entry["prev"] != expected:
                return ({"index": index, "file": chain_file.name,
                         "reason": "chain broken: 'prev' does not match the hash "
                                   "of the preceding record (mutation, deletion, "
                                   "or reordering)"}, hashes)
            expected = _record_hash(entry)
            hashes.append(expected)
            index += 1
    return (None, hashes)


def verify_chain(path: str | Path) -> dict[str, Any] | None:
    """Verify the whole chain (rolled files + active). Return ``None`` if
    intact, else ``{"index", "file", "reason"}`` for the first break.

    Read-only and standalone — the CLI uses this without constructing an
    :class:`AuditLog` (so verifying never re-secures or writes anything).
    """
    return _walk_chain(Path(path))[0]


def verify_with_anchor(
    path: str | Path, anchor_tail: str | None
) -> dict[str, Any] | None:
    """Verify the chain *and* that its tail matches the DPAPI-anchored hash.

    Catches what the chain alone cannot: mutation of the last record and
    truncation from the end. ``anchor_tail`` is the hash read back from the
    anchor (``None`` if absent). Returns ``None`` if all is well; otherwise a
    dict describing the problem — with ``anchor_missing: True`` for the benign
    "no anchor yet" case so callers can warn rather than alarm.
    """
    break_info, hashes = _walk_chain(Path(path))
    if break_info is not None:
        return break_info  # a chain break trumps any anchor question
    if anchor_tail is None:
        return {"reason": "no audit tail anchor present — tail mutation and "
                          "truncation are not detectable until one is written",
                "anchor_missing": True}
    if hashes and hashes[-1] == anchor_tail:
        return None  # exact match: nothing removed or altered at the tail
    if anchor_tail in hashes:
        # The anchored record still exists with newer records after it — the
        # anchor merely lagged behind later appends (e.g. a crash between the
        # append and the anchor write). Benign.
        return None
    return {"reason": "TAIL ANCHOR MISMATCH: the anchored tail hash is absent "
                      "from the chain — the last record was mutated or records "
                      "were truncated from the end",
            "tail_anchor": True}


def _tail_hash(path: Path) -> str:
    """Hash of the last record across the chain, or ``GENESIS`` if empty.

    Called at construction so appends after a process restart continue the
    existing chain instead of starting a new one.
    """
    for chain_file in reversed(_ordered_chain_files(path)):
        try:
            lines = chain_file.read_text(encoding="utf-8-sig").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if line.strip():
                try:
                    return _record_hash(json.loads(line))
                except json.JSONDecodeError:
                    return _record_hash({"_unparseable": line})
    return GENESIS


def migrate_audit_chain(
    source: str | Path, dest: str | Path | None = None
) -> tuple[Path, Path]:
    """Write a hash-chained copy of *source* to *dest*, non-destructively.

    The original is never touched. Existing ``prev`` fields (if any) are
    dropped and the chain is rebuilt from genesis over the records as read.
    Returns ``(source, dest)``. Refuses to overwrite an existing *dest*.
    """
    source = Path(source)
    dest = Path(dest) if dest is not None else source.with_name(
        f"{source.stem}.chained{source.suffix}")
    if dest.exists():
        raise FileExistsError(f"refusing to overwrite existing file: {dest}")
    expected = GENESIS
    out: list[bytes] = []
    for line in source.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        entry.pop("prev", None)  # rebuild the chain from scratch
        entry["prev"] = expected
        entry = _normalise(entry)
        blob = _canonical(entry)
        out.append(blob)
        expected = hashlib.sha256(blob).hexdigest()
    dest.write_bytes(b"\n".join(out) + (b"\n" if out else b""))
    return source, dest


class AuditLog:
    """Thread-safe, append-only, hash-chained JSONL audit writer with rollover."""

    def __init__(self, path: str | Path, max_bytes: int = 5_000_000,
                 anchor: "TailAnchor | None" = None):
        from digital_twin.security.fsacl import ensure_private_dir

        self._path = Path(path)
        self._max_bytes = max(1024, int(max_bytes))
        self._lock = threading.Lock()
        self._anchor = anchor  # DPAPI tail anchor (None = feature off)
        # Owner-only log directory: the audit trail is evidence, so a local
        # account must not be able to read (or later, rewrite) it.
        ensure_private_dir(self._path.parent)
        # Resume the chain: the running hash survives a process restart and,
        # crucially, a rollover rename — that is what chains new files to old.
        self._last_hash = _tail_hash(self._path)
        # Startup integrity check (never fatal).
        self._verify_at_startup()

    @property
    def path(self) -> Path:
        """Location of the active audit file."""
        return self._path

    # ------------------------------------------------------------------
    def record(self, **fields: Any) -> dict[str, Any]:
        """Append one entry (timestamp + chain hash added) and return it."""
        with self._lock:
            entry = {"timestamp": datetime.now(timezone.utc).isoformat(), **fields}
            entry["prev"] = self._last_hash  # reserved — set by the log
            entry = _normalise(entry)
            blob = _canonical(entry)
            try:
                self._rollover_if_needed(len(blob) + 1)
                with self._path.open("ab") as handle:
                    handle.write(blob + b"\n")
                # Advance only after a durable write, so a dropped entry does
                # not leave the in-memory chain ahead of the file.
                self._last_hash = hashlib.sha256(blob).hexdigest()
                if self._anchor is not None:
                    # Best-effort tail anchor (never raises) — closes the
                    # tail-mutation / truncation gap the chain alone can't.
                    self._anchor.update(self._last_hash)
            except OSError:
                logger.exception("Audit write failed; entry dropped: %s", blob)
        return entry

    def tail(self, count: int = 20) -> list[dict[str, Any]]:
        """Return the last ``count`` entries of the active file (oldest first)."""
        with self._lock:
            try:
                lines = self._path.read_text(encoding="utf-8-sig").splitlines()
            except FileNotFoundError:
                return []
            except OSError:
                logger.exception("Audit read failed")
                return []
        entries: list[dict[str, Any]] = []
        for line in lines[-count:]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("Skipping corrupt audit line: %.80s", line)
        return entries

    def verify_chain(self) -> dict[str, Any] | None:
        """Verify the whole chain; ``None`` if intact, else the first break."""
        with self._lock:
            return verify_chain(self._path)

    def verify_with_anchor(self) -> dict[str, Any] | None:
        """Verify the chain *and* the tail against the anchor (if configured).

        Falls back to a plain chain verify when no anchor is set.
        """
        with self._lock:
            if self._anchor is None:
                return verify_chain(self._path)
            return verify_with_anchor(self._path, self._anchor.read())

    # ------------------------------------------------------------------
    def _verify_at_startup(self) -> None:
        try:
            if self._anchor is not None:
                broken = verify_with_anchor(self._path, self._anchor.read())
            else:
                broken = verify_chain(self._path)
        except Exception:  # verification must never stop the assistant
            logger.exception("Audit chain verification failed to run")
            return
        if not broken:
            return
        if broken.get("anchor_missing"):
            # Degrade to a warning: an install predating the anchor still starts.
            logger.warning("Audit tail anchor: %s", broken["reason"])
            return
        logger.warning(
            "AUDIT CHAIN BROKEN in %s: %s — the audit log may have been "
            "tampered with.", self._path, broken["reason"],
        )

    def _rollover_if_needed(self, incoming: int) -> None:
        try:
            size = self._path.stat().st_size
        except FileNotFoundError:
            return
        if size + incoming <= self._max_bytes:
            return
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        rolled = self._path.with_name(f"{self._path.stem}-{stamp}{self._path.suffix}")
        counter = 1
        while rolled.exists():  # same-second rollovers
            rolled = self._path.with_name(
                f"{self._path.stem}-{stamp}-{counter}{self._path.suffix}"
            )
            counter += 1
        self._path.rename(rolled)
        # NOTE: self._last_hash is deliberately NOT reset — the first record of
        # the new file must chain to the last record of the rolled file.
        logger.info("Audit log rolled over to %s", rolled.name)


def build_audit_log(security) -> AuditLog:
    """Construct the production audit log from ``SecurityConfig``, with the
    DPAPI tail anchor enabled on Windows.

    The single wiring point shared by the kernel and the CLIs so they all write
    the same chain and keep the same anchor current.
    """
    from digital_twin.security.dpapi import is_available
    from digital_twin.security.tail_anchor import TailAnchor

    anchor_file = str(getattr(security, "audit_anchor_file", "")).strip()
    anchor = (TailAnchor(anchor_file)
              if anchor_file and is_available() else None)
    return AuditLog(security.audit_file,
                    max_bytes=security.audit_max_bytes, anchor=anchor)
