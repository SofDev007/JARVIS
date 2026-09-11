"""Knowledge actions: ingestion and recall through the normal gates.

Risk reasoning:

* ``ingest_document`` — SENSITIVE. It reads a user file into the index;
  the path must resolve inside ``files.allowed_roots`` (the exact same
  containment as file actions — reused, not reimplemented).
* ``ingest_text`` — SENSITIVE. The LLM (or a plan) can teach the
  assistant a document; a human approves what enters the corpus.
* ``search_knowledge`` — SENSITIVE. Recall surfaces stored content into
  results; treat it like ``read_text_file``.
* ``list_knowledge`` — SENSITIVE (titles and sources are user data).
* ``forget_document`` — SENSITIVE, not DANGEROUS: it deletes *derived*
  index data, recoverable by re-ingesting; the original file is never
  touched (contrast ``delete_file``).
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from digital_twin.automation.file_actions import resolve_roots, resolve_within
from digital_twin.automation.registry import ActionRegistry, ActionSpec
from digital_twin.configuration.settings import FilesConfig, KnowledgeConfig
from digital_twin.knowledge.store import KnowledgeStore
from digital_twin.security.permissions import RiskLevel
from digital_twin.security.privacy import PrivacyTier

logger = logging.getLogger(__name__)

_MAX_INGEST_CHARS = 200_000
_PRIVACY_TIERS = {tier.value for tier in PrivacyTier}


def _validate_privacy_tier(params: Mapping[str, Any]) -> None:
    tier = params.get("privacy_tier")
    if tier is not None and tier not in _PRIVACY_TIERS:
        raise ValueError(f"privacy_tier must be one of {_PRIVACY_TIERS}, got {tier!r}")


def register_knowledge_actions(
    registry: ActionRegistry,
    store: KnowledgeStore,
    knowledge: KnowledgeConfig,
    files: FilesConfig,
) -> None:
    roots = resolve_roots(files.allowed_roots)

    # -- ingest_document (SENSITIVE) -----------------------------------------
    def validate_ingest_document(params: Mapping[str, Any]) -> None:
        resolve_within(params.get("path"), roots)
        _validate_privacy_tier(params)

    def handle_ingest_document(params: Mapping[str, Any]) -> str:
        path = resolve_within(params.get("path"), roots)
        if not path.is_file():
            raise ValueError(f"not a file: {path}")
        from digital_twin.knowledge.extraction import (
            ExtractionError,
            extract_text,
        )

        try:
            text = extract_text(path)
        except ExtractionError as exc:
            raise ValueError(str(exc)) from exc
        except OSError as exc:
            raise ValueError(f"cannot read {path}: {exc}") from exc
        if len(text) > _MAX_INGEST_CHARS:
            text = text[:_MAX_INGEST_CHARS]
        doc_id, chunks, created = store.ingest(
            title=path.name, text=text, source=str(path),
            replace_source=True,
            privacy_tier=params.get("privacy_tier", "local_only"))
        state = "ingested" if created else "already known (unchanged)"
        return f"{state}: '{path.name}' as document {doc_id} ({chunks} chunks)"

    registry.register(ActionSpec(
        name="ingest_document",
        description=("Read one allow-listed file into the knowledge index "
                     "(chunked + embedded locally)."),
        risk=RiskLevel.SENSITIVE,
        handler=handle_ingest_document,
        validate=validate_ingest_document,
    ))

    # -- ingest_text (SENSITIVE) ------------------------------------------------
    def validate_ingest_text(params: Mapping[str, Any]) -> None:
        text = params.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("a non-empty 'text' string is required")
        if len(text) > _MAX_INGEST_CHARS:
            raise ValueError(f"'text' exceeds {_MAX_INGEST_CHARS} characters")
        _validate_privacy_tier(params)

    def handle_ingest_text(params: Mapping[str, Any]) -> str:
        title = str(params.get("title", "note"))
        doc_id, chunks, created = store.ingest(
            title=title, text=str(params["text"]), source="chat",
            privacy_tier=params.get("privacy_tier", "local_only"))
        state = "ingested" if created else "already known (unchanged)"
        return f"{state}: '{title}' as document {doc_id} ({chunks} chunks)"

    registry.register(ActionSpec(
        name="ingest_text",
        description="Store a piece of text in the knowledge index.",
        risk=RiskLevel.SENSITIVE,
        handler=handle_ingest_text,
        validate=validate_ingest_text,
    ))

    # -- search_knowledge (SENSITIVE) ----------------------------------------------
    def validate_search(params: Mapping[str, Any]) -> None:
        query = params.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("a non-empty 'query' string is required")

    def handle_search(params: Mapping[str, Any]) -> str:
        hits = store.search(
            str(params["query"]),
            top_k=int(params.get("top_k") or knowledge.top_k),
            min_score=knowledge.min_score,
        )
        if not hits:
            return "no matching knowledge"
        parts = [
            f"[{hit.title}#{hit.position} {hit.score:.2f}] {hit.content}"
            for hit in hits
        ]
        return f"{len(hits)} hit(s): " + " | ".join(parts)

    registry.register(ActionSpec(
        name="search_knowledge",
        description="Rank stored knowledge against a query (local vectors).",
        risk=RiskLevel.SENSITIVE,
        handler=handle_search,
        validate=validate_search,
    ))

    # -- list_knowledge (SENSITIVE) -----------------------------------------------
    def handle_list(params: Mapping[str, Any]) -> str:
        documents = store.documents()
        if not documents:
            return "knowledge index is empty"
        listing = ", ".join(
            f"#{info.doc_id} '{info.title}' ({info.chunks} chunks)"
            for info in documents[:20]
        )
        return f"{len(documents)} document(s): {listing}"

    registry.register(ActionSpec(
        name="list_knowledge",
        description="List ingested knowledge documents.",
        risk=RiskLevel.SENSITIVE,
        handler=handle_list,
    ))

    # -- forget_document (SENSITIVE) ------------------------------------------------
    def validate_forget(params: Mapping[str, Any]) -> None:
        doc_id = params.get("doc_id")
        if not isinstance(doc_id, int) or doc_id < 1:
            raise ValueError("'doc_id' must be a positive integer")

    def handle_forget(params: Mapping[str, Any]) -> str:
        doc_id = int(params["doc_id"])
        if store.forget(doc_id):
            return f"forgot document {doc_id} (original file untouched)"
        raise ValueError(f"no such document: {doc_id}")

    registry.register(ActionSpec(
        name="forget_document",
        description=("Remove one document from the knowledge index "
                     "(derived data only; re-ingest to restore)."),
        risk=RiskLevel.SENSITIVE,
        handler=handle_forget,
        validate=validate_forget,
    ))
