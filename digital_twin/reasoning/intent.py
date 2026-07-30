"""Intent engine: context-aware interpretation of perception events.

The reasoning side of the platform contract. Perception modules publish
*what happened* (``thumbs_up`` from the right hand); this engine decides
*what it means* given the current application context — next slide during a
presentation, like on a video, accept suggestion while coding. The mapping
table lives in configuration and can be changed without touching any
perception code, which is exactly the decoupling the architecture demands.

Context updates arrive as ``context.changed`` events (published today by
callers or the demo, tomorrow by a screen-context perception module — the
engine cannot tell the difference, by design).
"""

from __future__ import annotations

import logging
import threading

from digital_twin.configuration.settings import IntentConfig
from digital_twin.core.bus import Subscription
from digital_twin.core.events import Event, Topics
from digital_twin.core.module import BaseModule, ModuleState
from digital_twin.perception.gesture.semantics import resolve

logger = logging.getLogger(__name__)

#: Fallback context consulted when the active context has no mapping.
WILDCARD_CONTEXT = "*"


class IntentEngine(BaseModule):
    """Maps semantic perception events to intents based on active context."""

    name = "intent"
    topics = (Topics.INTENT,)

    def __init__(self, config: IntentConfig):
        super().__init__()
        self._mappings = config.mappings
        self._context = config.default_context
        self._context_lock = threading.Lock()
        self._subscriptions: list[Subscription] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def _on_start(self) -> None:
        assert self._bus is not None
        self._subscriptions = [
            self._bus.subscribe(Topics.GESTURE, self._on_gesture, name="intent.gesture"),
            self._bus.subscribe(Topics.CONTEXT, self._on_context, name="intent.context"),
        ]

    def _on_stop(self) -> None:
        for subscription in self._subscriptions:
            subscription.cancel()
        self._subscriptions = []

    # Pause keeps the subscriptions but the handlers ignore events while
    # not RUNNING — cheaper than resubscribing, identical behaviour.
    def _on_pause(self) -> None:  # noqa: D401 - intentional no-op
        """Paused intent engine simply stops emitting."""

    def _on_resume(self) -> None:
        """Nothing to rebuild; handlers resume emitting."""

    # ------------------------------------------------------------------
    # Context
    # ------------------------------------------------------------------
    @property
    def context(self) -> str:
        """The currently active application context."""
        with self._context_lock:
            return self._context

    def set_context(self, context: str) -> None:
        """Programmatically switch context (equivalent to a context event)."""
        if not context or not isinstance(context, str):
            raise ValueError("context must be a non-empty string")
        with self._context_lock:
            previous, self._context = self._context, context
        if previous != context:
            logger.info("Context changed: %s -> %s", previous, context)

    def _on_context(self, event: Event) -> None:
        context = event.payload.get("context")
        if isinstance(context, str) and context:
            self.set_context(context)
        else:
            logger.warning("Ignoring malformed context event: %s", event)

    # ------------------------------------------------------------------
    # Gesture interpretation
    # ------------------------------------------------------------------
    def _on_gesture(self, event: Event) -> None:
        if self.state is not ModuleState.RUNNING:
            return
        gesture = event.payload.get("gesture")
        if not isinstance(gesture, str):
            logger.warning("Ignoring malformed gesture event: %s", event)
            return
        gesture = resolve(gesture)
        context = self.context

        intent = self._lookup(context, gesture)
        if intent is None:
            logger.debug("No intent for gesture %r in context %r", gesture, context)
            return

        self._publish(
            Event(
                topic=Topics.INTENT,
                source=self.name,
                payload={
                    "intent": intent,
                    "context": context,
                    "gesture": gesture,
                    "hand": event.payload.get("hand"),
                    "confidence": event.payload.get("confidence"),
                    "repeat": bool(event.payload.get("repeat", False)),
                    "source_event": event.event_id,
                },
            )
        )
        logger.info(
            "Intent %r (gesture %r, context %r)", intent, gesture, context
        )

    def _lookup(self, context: str, gesture: str) -> str | None:
        intent = self._mappings.get(context, {}).get(gesture)
        if intent is None:
            intent = self._mappings.get(WILDCARD_CONTEXT, {}).get(gesture)
        return intent

    # ------------------------------------------------------------------
    def _metrics(self) -> dict[str, object]:
        return {"context": self.context, "contexts_known": len(self._mappings)}
