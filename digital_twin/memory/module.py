"""Memory module: the assistant's diary, fed by the event stream.

Subscribes to the bus and maintains two memory layers:

* **Working memory** (RAM): every intent, context switch and action result
  lands in the recent-activity buffer — "what is going on right now".
* **Episodic memory** (persisted): every terminal action result becomes a
  searchable record of *what the assistant did and what came of it*, with
  full provenance (intent + perception event ids) carried in the data
  payload. Denials and rejections are stored with *higher* importance than
  routine successes — "the user said no to X" is a preference signal worth
  keeping.

Semantic memory (facts, preferences) is exposed via :meth:`remember_fact`
for callers (and, from M7, the reasoning layer); automatic fact extraction
is deliberately not attempted without an LLM.

Pruning runs on an interval and enforces the configured retention and cap
for episodic records — semantic facts are user-curated and never
auto-pruned. Pause stops all recording and clears working memory (privacy
over convenience); the persistent store is untouched.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from digital_twin.configuration.settings import MemoryConfig
from digital_twin.core.bus import Subscription
from digital_twin.core.events import Event, Topics
from digital_twin.core.module import BaseModule, ModuleState
from digital_twin.memory.codec import create_codec
from digital_twin.memory.store import MemoryRecord, MemoryStore
from digital_twin.memory.working import WorkingMemory

logger = logging.getLogger(__name__)

#: Episodic importance per action outcome. Refusals outrank successes:
#: they encode user preferences and security posture.
_STATUS_IMPORTANCE = {
    "completed": 0.5,
    "failed": 0.6,
    "timeout": 0.6,
    "denied": 0.75,
    "rejected": 0.75,
    "invalid": 0.4,
    "unknown_action": 0.4,
}


class MemoryModule(BaseModule):
    """Feeds working and episodic memory from the event bus."""

    name = "memory"
    topics = (Topics.MEMORY,)

    def __init__(self, config: MemoryConfig, store: MemoryStore | None = None):
        super().__init__()
        self._config = config
        self._injected_store = store
        self._store: MemoryStore | None = None
        self.working = WorkingMemory(
            capacity=config.working_capacity, window_s=config.working_window_s
        )
        self._subscriptions: list[Subscription] = []
        self._prune_stop = threading.Event()
        self._prune_thread: threading.Thread | None = None

    @property
    def store(self) -> "MemoryStore | None":
        """The live store once started (used by the dashboard panel)."""
        return self._store

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def _on_start(self) -> None:
        assert self._bus is not None
        if self._store is None:
            self._store = self._injected_store or MemoryStore(
                self._config.db_path,
                codec=create_codec(self._config.encryption, self._config.key_path),
                search_half_life_days=self._config.search_half_life_days,
            )
        self._subscriptions = [
            self._bus.subscribe(Topics.ACTION_RESULT, self._on_action_result,
                                name="memory.results"),
            self._bus.subscribe(Topics.INTENT, self._on_activity,
                                name="memory.intents"),
            self._bus.subscribe(Topics.CONTEXT, self._on_activity,
                                name="memory.context"),
            self._bus.subscribe(Topics.CHAT, self._on_activity,
                                name="memory.chat"),
            self._bus.subscribe(Topics.CHAT_RESPONSE, self._on_activity,
                                name="memory.chat_response"),
        ]
        self._prune_stop.clear()
        self._prune_thread = threading.Thread(
            target=self._prune_loop, name="memory-prune", daemon=True
        )
        self._prune_thread.start()

    def _on_stop(self) -> None:
        for subscription in self._subscriptions:
            subscription.cancel()
        self._subscriptions = []
        self._prune_stop.set()
        if self._prune_thread is not None:
            self._prune_thread.join(timeout=3.0)
            self._prune_thread = None
        if self._store is not None and self._injected_store is None:
            self._store.close()
        self._store = None

    def _on_pause(self) -> None:
        """Stop recording; drop the working buffer (privacy over convenience)."""
        self.working.clear()

    def _on_resume(self) -> None:
        """Handlers resume recording; nothing to rebuild."""

    # ------------------------------------------------------------------
    # Public memory API
    # ------------------------------------------------------------------
    @property
    def store(self) -> MemoryStore:
        """The underlying persistent store (raises before start)."""
        if self._store is None:
            raise RuntimeError("memory module is not started")
        return self._store

    def remember_fact(
        self,
        content: str,
        tags: tuple[str, ...] = (),
        importance: float = 0.7,
        source: str = "user",
    ) -> MemoryRecord:
        """Persist a semantic fact/preference (never auto-pruned)."""
        record = self.store.add(
            kind="semantic", content=content, source=source,
            importance=importance, tags=tags,
        )
        self._announce(record)
        return record

    # ------------------------------------------------------------------
    # Bus handlers
    # ------------------------------------------------------------------
    def _on_activity(self, event: Event) -> None:
        if self.state is not ModuleState.RUNNING:
            return
        summary = ", ".join(
            f"{key}={value}" for key, value in event.payload.items()
            if key not in ("source_event",)
        )
        self.working.add(event.topic, summary, data=dict(event.payload))

    def _on_action_result(self, event: Event) -> None:
        if self.state is not ModuleState.RUNNING:
            return
        self._on_activity(event)
        payload = event.payload
        status = str(payload.get("status", "unknown"))
        action = payload.get("action")
        intent = payload.get("intent")
        context = payload.get("context")
        detail = payload.get("detail", "")

        content = f"Action {action} {status}"
        if intent:
            content += f" for intent {intent}"
        if context:
            content += f" in context {context}"
        if detail:
            content += f": {detail}"

        tags = tuple(
            str(tag) for tag in (status, action, context) if tag
        )
        try:
            record = self.store.add(
                kind="episodic",
                content=content,
                data=dict(payload),
                source=self.name,
                importance=_STATUS_IMPORTANCE.get(status, 0.5),
                tags=tags,
            )
        except Exception:
            logger.exception("Failed to persist episodic memory")
            return
        self._announce(record)

    def _announce(self, record: MemoryRecord) -> None:
        self._publish(Event(
            topic=Topics.MEMORY,
            source=self.name,
            payload={"memory_id": record.id, "kind": record.kind},
        ))

    # ------------------------------------------------------------------
    # Pruning
    # ------------------------------------------------------------------
    def _prune_loop(self) -> None:
        while not self._prune_stop.wait(self._config.prune_interval_s):
            self._prune_once()

    def _prune_once(self) -> int:
        store = self._store
        if store is None:
            return 0
        try:
            return store.prune(
                kind="episodic",
                max_records=self._config.episodic_max_records,
                retention_days=self._config.retention_days,
            )
        except Exception:
            logger.exception("Memory pruning failed")
            return 0

    # ------------------------------------------------------------------
    def _metrics(self) -> dict[str, Any]:
        metrics: dict[str, Any] = {"working_items": len(self.working)}
        store = self._store
        if store is not None:
            metrics["episodic"] = store.count("episodic")
            metrics["semantic"] = store.count("semantic")
        return metrics
