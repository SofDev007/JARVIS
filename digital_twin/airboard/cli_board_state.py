"""``digital-twin-board`` state reader — the agent's eyes: GET /state,
read-only, and pretty-print what's currently on the board (position zone,
size, held-in-hand, etc.) so an agent can answer "what's on the board?"
from truth instead of guessing. Ported from barehands' board-state.sh.

PowerShell::

    digital-twin-board-state
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request

from digital_twin.configuration.settings import load_config


def _zone(x: float, y: float) -> str:
    col = "left" if x < 0.33 else "right" if x > 0.67 else "center"
    row = "top" if y < 0.33 else "bottom" if y > 0.67 else "middle"
    if col == "center" and row == "middle":
        return "center"
    return f"{row} {col}"


def _flags(item: dict) -> str:
    flags = []
    if item.get("g"):
        flags.append("IN THE USER'S HAND")
    scale = item.get("scale", 1.0) or 1.0
    if scale >= 1.6:
        flags.append("blown up large")
    elif scale <= 0.55:
        flags.append("shrunk small")
    if item.get("op", 1.0) < 0.5:
        flags.append("faded out")
    return f" [{', '.join(flags)}]" if flags else ""


def _describe(item: dict) -> str:
    kind = item.get("type", "?")
    if kind == "card":
        body = re.sub(r"\s+", " ", str(item.get("body", ""))).strip()[:70]
        return f'card "{item.get("title", "?")}" — {body}'
    if kind == "img":
        name = str(item.get("src", "?")).rsplit("/", 1)[-1]
        extra = []
        if item.get("fx"):
            extra.append("fx, frameless")
        if item.get("vid"):
            extra.append("video")
        suffix = f" ({', '.join(extra)})" if extra else ""
        return f"image {name}{suffix}"
    if kind == "model":
        name = str(item.get("src", "?")).rsplit("/", 1)[-1]
        mode = "solid" if item.get("mm") == "solid" else "hologram wireframe"
        explode = item.get("ex", 0) or 0
        extra = f", EXPLODED {round(explode * 100)}%" if explode > 0.02 else ""
        return f"3D model {name} ({mode}{extra})"
    if kind == "panel":
        return f'open note "{item.get("title", "?")}"'
    if kind == "browser":
        return f'file browser "{item.get("title", "?")}"'
    if kind == "widget":
        return "the assistant ring"
    if kind == "orb":
        return f'orb "{item.get("title", "?")}"'
    return f'{kind} "{item.get("title") or item.get("src", "?")}"'


def _render(state: dict) -> str:
    items = state.get("items") or []
    if not items:
        return "The board is EMPTY (as of the tracker's last heartbeat)."
    lines = []
    for item in items:
        zone = _zone(item.get("x", 0.5), item.get("y", 0.5))
        lines.append(f"- {_describe(item)} — {zone}{_flags(item)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="digital-twin-board-state",
        description="Read-only: what's currently on the air board.")
    parser.add_argument("--config", default=None,
                        help="Configuration file (defaults resolved as the "
                             "kernel does).")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    url = f"http://{config.airboard.host}:{config.airboard.port}/state"
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            raw = response.read()
    except (urllib.error.URLError, OSError):
        print("The board is dark — the air board server isn't running.",
              file=sys.stderr)
        return 1

    try:
        state = json.loads(raw)
        if not state:
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        print("Server is up, but no tracker page has connected yet.")
        return 0

    print(_render(state))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
