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
from digital_twin.security.audit import migrate_audit_chain, verify_chain


def build_parser() -> argparse.ArgumentParser:
    # Common flags live on a parent parser so they are accepted *after* the
    # subcommand too (e.g. `verify --path logs\audit.jsonl`).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=None,
                        help="Configuration file (defaults resolved as the "
                             "kernel does); supplies the default audit path.")
    common.add_argument("--path", default=None,
                        help="Audit log path (overrides the configured "
                             "security.audit_file).")

    parser = argparse.ArgumentParser(
        prog="audit_cli",
        parents=[common],
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


def _audit_path(args) -> str:
    if args.path:
        return args.path
    return load_config(args.config).security.audit_file


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        path = _audit_path(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.command == "verify":
        broken = verify_chain(path)
        if broken is None:
            print(f"audit chain intact: {path}")
            return 0
        print(
            f"AUDIT CHAIN BROKEN in {path}\n"
            f"  first break at record {broken['index']} "
            f"(file {broken['file']})\n"
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
