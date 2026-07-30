"""Standardized events: the single language every module speaks.

Every perception module, reasoning component and automation engine in the
Digital Twin communicates exclusively through :class:`Event` objects on the
event bus. An event is an immutable envelope (topic, source, timestamp, id)
around a topic-specific payload, so new modules can be added without any
existing code learning about them.

Serialised form (``to_dict``) matches the platform contract, e.g.::

    {
        "event_id": "9f2c…",
        "timestamp": "2026-07-06T12:00:00.412000+00:00",
        "module": "gesture",
        "type": "perception.gesture",
        "gesture": "thumbs_up",
        "confidence": 0.97,
        "hand": "right"
    }
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping


class Topics:
    """Well-known topic names.

    Topics are hierarchical, dot-separated strings. Subscribers may match a
    topic exactly, a subtree with ``"prefix.*"``, or everything with ``"*"``.
    Modules are free to introduce new topics; these constants exist so the
    core pipeline never relies on magic strings.
    """

    #: Semantic gesture detections: ``{gesture, confidence, hand, repeat}``.
    GESTURE = "perception.gesture"
    #: Hand presence changes: ``{hand, present}``.
    HAND = "perception.hand"
    #: Application/activity context changes: ``{context}``.
    CONTEXT = "context.changed"
    #: Mapped user intents: ``{intent, context, ...source fields}``.
    INTENT = "intent.detected"
    #: Accepted automation work: ``{action, intent, params}``.
    ACTION = "action.requested"
    #: Terminal outcome of an action: ``{action, status, detail, ...}``.
    ACTION_RESULT = "action.result"
    #: Direct gated execution of one action (plan steps): ``{action, params, plan_id, step}``.
    ACTION_EXECUTE = "action.execute"
    #: Ask the planner to run a plan: ``{plan|goal+steps|skill, ...}``.
    PLAN_REQUEST = "plan.request"
    #: Plan lifecycle/progress: ``{plan_id, name, status, step, total_steps}``.
    PLAN_PROGRESS = "plan.progress"
    #: Cancel a running plan: ``{plan_id}``.
    PLAN_CANCEL = "plan.cancel"
    #: Confirmation gate state changes: ``{action, state}``.
    CONFIRMATION = "action.confirmation"
    #: A memory was persisted: ``{memory_id, kind}``.
    MEMORY = "memory.stored"
    #: Typed user text: ``{text, user}``.
    CHAT = "perception.chat"
    #: OCR'd visible screen text (on demand only): ``{text, chars, truncated}``.
    SCREEN = "perception.screen"
    #: Final spoken utterances: ``{text}``.
    VOICE = "perception.voice"
    #: Live partial transcripts (UI captioning): ``{text}``.
    VOICE_PARTIAL = "perception.voice.partial"
    #: Listening control: ``{command: start|stop|toggle}``.
    VOICE_CONTROL = "voice.control"
    #: Assistant replies: ``{text, reasoning, ...}``.
    CHAT_RESPONSE = "chat.response"
    #: Module lifecycle announcements: ``{module, state, action, detail}``.
    MODULE = "system.module"


#: Envelope keys that payloads must not shadow when flattened by ``to_dict``.
RESERVED_KEYS = frozenset({"event_id", "timestamp", "module", "type"})


def topic_matches(pattern: str, topic: str) -> bool:
    """Return whether ``topic`` matches a subscription ``pattern``.

    ``"*"`` matches every topic; ``"perception.*"`` matches ``perception``
    and any descendant such as ``perception.gesture``; anything else is an
    exact match.
    """
    if pattern == "*":
        return True
    if pattern.endswith(".*"):
        root = pattern[:-2]
        return topic == root or topic.startswith(root + ".")
    return topic == pattern


@dataclass(frozen=True)
class Event:
    """Immutable event envelope exchanged on the bus."""

    topic: str
    """Hierarchical topic, e.g. ``"perception.gesture"``."""

    source: str
    """Name of the module that produced the event."""

    payload: Mapping[str, Any] = field(default_factory=dict)
    """Topic-specific data. Copied and frozen at construction time."""

    timestamp: float = field(default_factory=time.time)
    """Unix epoch seconds at creation."""

    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    """Unique identifier for tracing and audit logs."""

    def __post_init__(self) -> None:
        if not self.topic or not isinstance(self.topic, str):
            raise ValueError("Event.topic must be a non-empty string")
        if not self.source or not isinstance(self.source, str):
            raise ValueError("Event.source must be a non-empty string")
        clash = RESERVED_KEYS.intersection(self.payload)
        if clash:
            raise ValueError(
                f"Event payload keys collide with envelope fields: {sorted(clash)}"
            )
        # Freeze the payload so events can be shared across threads safely.
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """Flat, JSON-serialisable representation (platform contract)."""
        return {
            "event_id": self.event_id,
            "timestamp": datetime.fromtimestamp(
                self.timestamp, tz=timezone.utc
            ).isoformat(),
            "module": self.source,
            "type": self.topic,
            **self.payload,
        }

    def __str__(self) -> str:  # concise log form
        data = ", ".join(f"{k}={v!r}" for k, v in self.payload.items())
        return f"[{self.topic}] from {self.source}: {data}"
