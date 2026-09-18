"""Orb configuration: notes/media folders the board exposes, and the path
jail every filesystem-touching endpoint in :mod:`.server` relies on.

Orb paths are personal (a notes vault, a props folder) so they live in a
gitignored YAML (``airboard.orbs_file``, see ``config/airboard.local.yaml.
example``) rather than the tracked app config. Missing or malformed file
-> falls back to an empty orb list; the board still starts, just empty.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Orb:
    title: str
    path: str
    kind: str  # "notes" | "media"


def load_orbs(orbs_file: str | Path) -> list[Orb]:
    path = Path(orbs_file)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.info("No usable airboard orbs file at %s (%s) — board starts "
                    "empty; copy %s.example to get started.", path, exc, path)
        return []
    orbs = []
    for entry in raw.get("orbs", []) or []:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title", "?"))
        orb_path = str(entry.get("path", "")).strip()
        kind = str(entry.get("kind", "notes"))
        if not orb_path or kind not in ("notes", "media"):
            continue
        orbs.append(Orb(title=title, path=orb_path, kind=kind))
    return orbs


def resolve_root(path: str) -> Path:
    """Relative paths resolve against the CWD; absolute paths are honored
    as-is (so a user can point at an existing vault without copying files
    into the repo)."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    return p.resolve()


def media_root(orbs: list[Orb], default_media_dir: str) -> Path:
    for orb in orbs:
        if orb.kind == "media":
            return resolve_root(orb.path)
    return resolve_root(default_media_dir)
