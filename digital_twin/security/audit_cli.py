"""Audit CLI: verify or migrate the tamper-evident audit log from a terminal.

The audit log is a SHA-256 hash chain (each record carries the hash of the
one before it, across rollover boundaries). ``verify`` walks the whole chain —
every rolled file plus the active one — and reports the first break, if any.
``migrate`` writes a hash-chained copy of a legacy (unchained) log without
touching the original.

PowerShell::

    python -m digital_twin.security.audit_cli verify
    python -m digital_twin.security.audit_cli verify --path logs\audit.jsonl
    python -m digital_twin.security.audit_cli migrate --path logs\audit.jsonl

Exit codes: 0 = chain intact (verify) / file written (migrate); 1 = chain
broken or an error occurred.
"""

from __future__ import annotations

import argparse
import sys

from digital_twin.configuration.settings import load_config
from digital_twin.security.audit import (
    migrate_audit_chain,
    verify_chain,
    verify_with_anchor,
)


def build_parser() -> argparse.ArgumentParser:
    # Common flags live on the subparsers (parents) only — not also on the top
    # parser — so a value given after the subcommand (`verify --path X`) is not
    # clobbered by the other parser's default. Flags follow the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=None,
                        help="Configuration file (defaults resolved as the "
                             "kernel does); supplies the default audit path.")
    common.add_argument("--path", default=None,
                        help="Audit log path (overrides the configured "
                             "security.audit_file).")

    parser = argparse.ArgumentParser(
        prog="audit_cli",
        description="Verify or migrate the tamper-evident audit log.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("verify", parents=[common],
                        help="Verify the whole chain.")
    migrate = commands.add_parser(
        "migrate", parents=[common],
        help="Write a hash-chained copy (original untouched).")
    migrate.add_argument("--dest", default=None,
                         help="Output path (default: <name>.chained<suffix>).")
    return parser


def _verify_default(config, path: str):
    """Verify the configured log *with* its tail anchor when available."""
    from digital_twin.security.dpapi import is_available
    from digital_twin.security.tail_anchor import TailAnchor

    anchor_file = str(getattr(config.security, "audit_anchor_file", "")).strip()
    if anchor_file and is_available():
        return verify_with_anchor(path, TailAnchor(anchor_file).read())
    return verify_chain(path)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = None if args.path else load_config(args.config)
        path = args.path or config.security.audit_file
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.command == "verify":
        # An explicit --path is a plain chain check (arbitrary file); the
        # configured log also gets its tail anchor verified.
        broken = (verify_chain(path) if args.path
                  else _verify_default(config, path))
        if broken is None:
            print(f"audit chain intact: {path}")
            return 0
        if broken.get("anchor_missing"):
            print(f"audit chain intact, but no tail anchor yet: {path}\n"
                  f"  {broken['reason']}")
            return 0
        location = (f"record {broken['index']} (file {broken['file']})"
                    if "index" in broken else "the tail")
        print(
            f"AUDIT CHAIN BROKEN in {path}\n"
            f"  first break at {location}\n"
            f"  reason: {broken['reason']}",
            file=sys.stderr,
        )
        return 1

    if args.command == "migrate":
        try:
            source, dest = migrate_audit_chain(path, args.dest)
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"migrated (original untouched):\n  source: {source}\n"
              f"  chained: {dest}")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
