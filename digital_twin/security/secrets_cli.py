"""Secrets CLI: manage the assistant's secret store from a terminal.

Values are **never accepted on the command line** — process argument
lists are world-readable on most systems. ``set`` reads the value from
an interactive hidden prompt (or stdin with ``--stdin`` for scripting),
and ``get`` prints only existence unless ``--reveal`` is passed.

Usage::

    python -m digital_twin.security.secrets_cli list
    python -m digital_twin.security.secrets_cli set github_token
    echo -n "$TOKEN" | python -m digital_twin.security.secrets_cli set github_token --stdin
    python -m digital_twin.security.secrets_cli get github_token [--reveal]
    python -m digital_twin.security.secrets_cli delete github_token
"""

from __future__ import annotations

import argparse
import getpass
import sys

from digital_twin.configuration.settings import load_config
from digital_twin.security.secrets import SecretsError, create_secret_store


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="secrets_cli",
        description="Manage Digital Twin secrets (values by name, never by value).",
    )
    parser.add_argument("--config", default=None,
                        help="Configuration file (defaults resolved as the kernel does).")
    commands = parser.add_subparsers(dest="command", required=True)

    listing = commands.add_parser("list", help="List secret names.")  # noqa: F841

    setter = commands.add_parser("set", help="Store a secret (value prompted, never on argv).")
    setter.add_argument("name")
    setter.add_argument("--stdin", action="store_true",
                        help="Read the value from stdin instead of prompting.")

    getter = commands.add_parser("get", help="Check a secret exists (value hidden by default).")
    getter.add_argument("name")
    getter.add_argument("--reveal", action="store_true",
                        help="Print the value to stdout (visible in the terminal!).")

    deleter = commands.add_parser("delete", help="Delete one secret.")
    deleter.add_argument("name")
    return parser


def main(argv: list[str] | None = None,
         value_reader=None) -> int:
    """Entry point. ``value_reader`` is injectable for tests."""
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    try:
        store = create_secret_store(config.secrets)
    except SecretsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        if args.command == "list":
            names = store.names()
            if not names:
                print("(no secrets stored)")
            for name in names:
                print(name)
            return 0

        if args.command == "set":
            if value_reader is not None:
                value = value_reader()
            elif args.stdin:
                value = sys.stdin.read().strip()
            else:
                value = getpass.getpass(f"Value for '{args.name}': ")
            store.set(args.name, value)
            print(f"stored '{args.name}' ({store.name} backend)")
            return 0

        if args.command == "get":
            value = store.get(args.name)
            if args.reveal:
                print(value)
            else:
                print(f"'{args.name}' exists "
                      f"({len(value)} chars; use --reveal to print)")
            return 0

        if args.command == "delete":
            if store.delete(args.name):
                print(f"deleted '{args.name}'")
                return 0
            print(f"no such secret: '{args.name}'", file=sys.stderr)
            return 1
    except (SecretsError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
