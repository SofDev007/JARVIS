"""First-run setup — make an installed copy of the assistant runnable.

From a checkout everything is relative and just works; from a wheel the
package has no writable ``config/`` or ``data/`` beside it. This command
creates a home directory (default ``~/.digital_twin``) containing a
copy of the default configuration and the ``data/``, ``logs/`` and
``models/`` folders, then tells the user to point ``DIGITAL_TWIN_HOME``
at it. It is idempotent and never overwrites an existing config without
``--force``.

Usage::

    digital-twin-setup                 # ~/.digital_twin
    digital-twin-setup --home /opt/dt  # a chosen location
    digital-twin-setup --force         # overwrite an existing config
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from digital_twin import __version__
from digital_twin.paths import resolve_asset

_DEFAULT_HOME = Path.home() / ".digital_twin"
_SUBDIRS = ("data", "logs", "models")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="digital-twin-setup",
        description="Create a Digital Twin home directory for an installed copy.",
    )
    parser.add_argument("--home", type=Path, default=_DEFAULT_HOME,
                        help=f"target directory (default: {_DEFAULT_HOME})")
    parser.add_argument("--force", action="store_true",
                        help="overwrite an existing config file")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    home: Path = args.home.expanduser()

    print(f"Digital Twin {__version__} — setting up {home}")
    home.mkdir(parents=True, exist_ok=True)
    for name in _SUBDIRS:
        (home / name).mkdir(exist_ok=True)
        print(f"  ✓ {name}/")

    source_config = resolve_asset("config/default_config.yaml")
    if not source_config.is_file():
        print(f"error: cannot find the bundled default config "
              f"(looked via resolve_asset)", file=sys.stderr)
        return 1
    target_config_dir = home / "config"
    target_config_dir.mkdir(exist_ok=True)
    target_config = target_config_dir / "default_config.yaml"
    if target_config.exists() and not args.force:
        print(f"  · config/default_config.yaml exists (use --force to "
              f"overwrite) — left as is")
    else:
        shutil.copyfile(source_config, target_config)
        print(f"  ✓ config/default_config.yaml")

    print("\nDone. To use this home directory, set the environment variable:")
    marker = "set" if os.name == "nt" else "export"
    print(f"    {marker} DIGITAL_TWIN_HOME={home}")
    print("Then run:  digital-twin")
    if (home / "models").exists():
        print("\nOptional: drop a Vosk model into models/ for voice "
              "(see the README).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
