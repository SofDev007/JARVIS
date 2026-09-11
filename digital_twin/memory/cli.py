"""Memory CLI: the user's review/edit/delete controls, spec-mandated.

Operates directly on the configured store (same file, WAL mode), so it
works whether or not the assistant is running::

    python -m digital_twin.memory.cli list --kind episodic
    python -m digital_twin.memory.cli search "presentation escape"
    python -m digital_twin.memory.cli show <id>
    python -m digital_twin.memory.cli remember "Prefers dark themes" --tags ui
    python -m digital_twin.memory.cli edit <id> --importance 0.9
    python -m digital_twin.memory.cli delete <id>
    python -m digital_twin.memory.cli clear --kind episodic --yes
    python -m digital_twin.memory.cli export --out memories.json
    python -m digital_twin.memory.cli stats

Uses the same config resolution as the kernel (``--config``), including
the encryption key — encrypted stores are fully manageable.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from digital_twin.configuration.settings import load_config
from digital_twin.memory.codec import create_codec
from digital_twin.memory.store import KINDS, MemoryRecord, MemoryStore

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "default_config.yaml"


def _open_store(config_path: Path) -> MemoryStore:
    config = load_config(config_path).memory
    return MemoryStore(
        config.db_path,
        codec=create_codec(config.encryption, config.key_path),
        search_half_life_days=config.search_half_life_days,
    )


def _fmt(record: MemoryRecord, full: bool = False) -> str:
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(record.created_at))
    line = (
        f"{record.id[:12]}  {stamp}  [{record.kind}]  "
        f"imp={record.importance:.2f}  {record.content}"
    )
    if full:
        line += (
            f"\n  source: {record.source}   tags: {', '.join(record.tags) or '-'}"
            f"   accessed: {record.access_count}x"
            f"\n  data: {json.dumps(record.data, default=str)}"
        )
    return line


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="digital-twin-memory", description="Review, edit and delete memories."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    commands = parser.add_subparsers(dest="command", required=True)

    listing = commands.add_parser("list", help="List memories, newest first.")
    listing.add_argument("--kind", choices=KINDS)
    listing.add_argument("--limit", type=int, default=20)

    search = commands.add_parser("search", help="Ranked search.")
    search.add_argument("query")
    search.add_argument("--kind", choices=KINDS)
    search.add_argument("--limit", type=int, default=5)

    show = commands.add_parser("show", help="Show one memory in full.")
    show.add_argument("id")

    remember = commands.add_parser("remember", help="Store a semantic fact.")
    remember.add_argument("content")
    remember.add_argument("--tags", nargs="*", default=[])
    remember.add_argument("--importance", type=float, default=0.7)
    remember.add_argument("--privacy-tier", choices=["local_only", "cloud_ok"],
                          default="local_only",
                          help="local_only (default) is never sent to a "
                               "cloud LLM; cloud_ok is an explicit opt-in.")

    edit = commands.add_parser("edit", help="Edit content/importance/tags.")
    edit.add_argument("id")
    edit.add_argument("--content")
    edit.add_argument("--importance", type=float)
    edit.add_argument("--tags", nargs="*")

    delete = commands.add_parser("delete", help="Delete one memory.")
    delete.add_argument("id")

    clear = commands.add_parser("clear", help="Delete many memories.")
    clear.add_argument("--kind", choices=KINDS)
    clear.add_argument("--yes", action="store_true",
                       help="Required: confirms the bulk deletion.")

    export = commands.add_parser("export", help="Export decoded memories as JSON.")
    export.add_argument("--kind", choices=KINDS)
    export.add_argument("--out", type=Path)

    commands.add_parser("stats", help="Counts per kind.")
    return parser


def _resolve_id(store: MemoryStore, prefix: str) -> str | None:
    """Accept full ids or unambiguous prefixes (as printed by list)."""
    if store.get(prefix) is not None:
        return prefix
    matches = [
        record.id
        for record in store.list(limit=store.count())
        if record.id.startswith(prefix)
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        store = _open_store(args.config)
    except Exception as exc:
        print(f"Cannot open memory store: {exc}", file=sys.stderr)
        return 2

    try:
        return _dispatch(args, store)
    finally:
        store.close()


def _dispatch(args: argparse.Namespace, store: MemoryStore) -> int:
    if args.command == "list":
        records = store.list(kind=args.kind, limit=args.limit)
        if not records:
            print("No memories stored.")
            return 0
        for record in records:
            print(_fmt(record))
        return 0

    if args.command == "search":
        kinds = (args.kind,) if args.kind else None
        try:
            hits = store.search(args.query, kinds=kinds, limit=args.limit)
        except ValueError as exc:
            print(f"Invalid query: {exc}", file=sys.stderr)
            return 2
        if not hits:
            print("No matches.")
            return 0
        for hit in hits:
            print(f"{hit.score:5.2f}  {_fmt(hit.record)}")
        return 0

    if args.command == "show":
        memory_id = _resolve_id(store, args.id)
        record = store.get(memory_id, touch=True) if memory_id else None
        if record is None:
            print(f"No memory matching {args.id!r}", file=sys.stderr)
            return 1
        print(_fmt(record, full=True))
        return 0

    if args.command == "remember":
        record = store.add(
            kind="semantic", content=args.content, source="cli",
            importance=args.importance, tags=tuple(args.tags),
            privacy_tier=args.privacy_tier,
        )
        print(f"Stored: {_fmt(record)}")
        return 0

    if args.command == "edit":
        memory_id = _resolve_id(store, args.id)
        if memory_id is None:
            print(f"No memory matching {args.id!r}", file=sys.stderr)
            return 1
        try:
            record = store.update(
                memory_id,
                content=args.content,
                importance=args.importance,
                tags=tuple(args.tags) if args.tags is not None else None,
            )
        except (KeyError, ValueError) as exc:
            print(f"Edit failed: {exc}", file=sys.stderr)
            return 2
        print(f"Updated: {_fmt(record)}")
        return 0

    if args.command == "delete":
        memory_id = _resolve_id(store, args.id)
        if memory_id is None or not store.delete(memory_id):
            print(f"No memory matching {args.id!r}", file=sys.stderr)
            return 1
        print(f"Deleted {memory_id[:12]}")
        return 0

    if args.command == "clear":
        if not args.yes:
            print("Refusing bulk deletion without --yes", file=sys.stderr)
            return 2
        removed = store.clear(kind=args.kind)
        print(f"Deleted {removed} memories" + (f" of kind {args.kind}" if args.kind else ""))
        return 0

    if args.command == "export":
        dump = store.export(kind=args.kind)
        text = json.dumps(dump, indent=2, default=str)
        if args.out:
            args.out.write_text(text, encoding="utf-8")
            print(f"Exported {len(dump)} memories to {args.out}")
        else:
            print(text)
        return 0

    if args.command == "stats":
        for kind in KINDS:
            print(f"{kind:10s} {store.count(kind)}")
        print(f"{'total':10s} {store.count()}")
        return 0

    return 2  # unreachable with required=True


if __name__ == "__main__":
    raise SystemExit(main())
