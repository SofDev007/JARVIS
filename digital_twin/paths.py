"""Asset path resolution: make repo-relative paths survive a wheel.

Configuration defaults reference assets by repo-relative path
(``config/default_config.yaml``, ``models/hand_landmarker.task``). From
a checkout that just works; from a wheel install the working directory
can be anywhere. :func:`resolve_asset` searches, in order:

1. the path as given, if absolute or existing relative to the CWD;
2. ``$DIGITAL_TWIN_HOME/<path>`` — the documented home for assets and
   data when running installed;
3. the repository root inferred from this package's location (covers
   editable installs and checkouts invoked from elsewhere).

If nothing exists the *original* path is returned unchanged so the
caller's own error message fires — with the search locations attached
via :func:`asset_guidance` for humane errors. Resolution never invents
files; it only finds existing ones.
"""

from __future__ import annotations

import os
from pathlib import Path

_HOME_ENV = "DIGITAL_TWIN_HOME"


def _candidates(path: str | Path) -> list[Path]:
    path = Path(path)
    if path.is_absolute():
        return [path]
    found = [Path.cwd() / path]
    home = os.environ.get(_HOME_ENV, "").strip()
    if home:
        found.append(Path(home).expanduser() / path)
    found.append(Path(__file__).resolve().parents[1] / path)  # repo root
    return found


def resolve_asset(path: str | Path) -> Path:
    """First existing candidate, else the original path unchanged."""
    for candidate in _candidates(path):
        if candidate.exists():
            return candidate
    return Path(path)


def asset_guidance(path: str | Path) -> str:
    """Human-readable list of everywhere we looked."""
    looked = "; ".join(str(candidate) for candidate in _candidates(path))
    return (f"'{path}' not found (searched: {looked}) — set the "
            f"{_HOME_ENV} environment variable to the directory holding "
            "your config/ and models/ assets")
