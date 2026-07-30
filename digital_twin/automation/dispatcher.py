"""Action dispatcher: the guarded bridge from intents to real effects.

Consumes ``intent.detected`` events and runs each bound action through a
fixed security pipeline — every stage audited, every terminal state
published on the bus::

    intent ─▶ binding lookup ─▶ param validation ─▶ permission policy
                                                      │
                              DENY ◀──────────────────┼─▶ ALLOW
                                │            CONFIRM ─▶ confirmation gate
                                │                         approve │ deny/timeout
                                ▼                                 ▼
                             audited                    execute (worker pool,
                                                        hard timeout) ─▶ audited

Key properties:

* **Never blocks the bus.** The subscription handler only enqueues; the
  pipeline (including interactive confirmation) runs on the dispatcher's
  own worker thread. Overflow drops the *intent* (audited), never stalls
  perception.
* **Intent ↔ action decoupling.** What an intent *does* is a config table
  (``automation.intent_bindings``); reasoning decides meaning, this module
  decides execution, and neither hardcodes the other.
* **Provenance everywhere.** Every audit entry and result event carries the
  intent event id and the original perception event id — a launched app is
  traceable to the exact thumbs-up that caused it.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any, Mapping

from digital_twin.configuration.settings import AutomationConfig, SecurityConfig
from digital_twin.core.events import Event, Topics
from digital_twin.core.module import BaseModule, ModuleState
from functools import partial

from digital_twin.automation.builtin import register_builtin_actions
from digital_twin.automation.input_actions import register_input_actions
from digital_twin.automation.input_backend import create_input_backend
from digital_twin.automation.registry import ActionRegistry, ActionSpec
from digital_twin.security.audit import AuditLog
from digital_twin.security.confirmation import (
    ConfirmationProvider,
    create_confirmation_provider,
)
from digital_twin.security.permissions import Decision, PermissionPolicy

logger = logging.getLogger(__name__)

#: Sentinel that wakes the worker for shutdown.
_SHUTDOWN = object()


class ActionDispatcher(BaseModule):
    """Executes intent-bound actions behind permission and confirmation gates."""

    name = "actions"
    topics = (Topics.ACTION, Topics.ACTION_RESULT, Topics.CONFIRMATION)

    def __init__(
        self,
        config: AutomationConfig,
        registry: ActionRegistry,
        policy: PermissionPolicy,
        confirmation: ConfirmationProvider,
        audit: AuditLog,
        confirmation_timeout_s: float = 30.0,
    ):
        super().__init__()
        self._config = config
        self._registry = registry
        self._policy = policy
        self._confirmation = confirmation
        self._audit = audit
        self._confirm_timeout = confirmation_timeout_s

        self._queue: queue.Queue = queue.Queue(maxsize=max(1, config.max_queue_size))
        self._worker: threading.Thread | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._subscriptions: list = []
        self._counts: dict[str, int] = {}
        self._counts_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def _on_start(self) -> None:
        assert self._bus is not None
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="action-exec"
        )
        self._worker = threading.Thread(
            target=self._work_loop, name="action-dispatcher", daemon=True
        )
        self._worker.start()
        self._subscriptions = [
            self._bus.subscribe(Topics.INTENT, self._on_intent,
                                name="actions.intent"),
            self._bus.subscribe(Topics.ACTION_EXECUTE, self._on_intent,
                                name="actions.execute"),
        ]

    def _on_stop(self) -> None:
        for subscription in self._subscriptions:
            subscription.cancel()
        self._subscriptions = []
        self._queue.put(_SHUTDOWN)
        if self._worker is not None:
            self._worker.join(timeout=5.0)
            self._worker = None
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    # Pause keeps the subscription; the handler ignores events while not
    # RUNNING. Work already queued keeps draining — an approved action is
    # not un-approved by a pause.
    def _on_pause(self) -> None:
        """Paused dispatcher stops accepting new intents."""

    def _on_resume(self) -> None:
        """Nothing to rebuild."""

    # ------------------------------------------------------------------
    # Bus handler (dispatcher thread — enqueue only, never work)
    # ------------------------------------------------------------------
    def _on_intent(self, event: Event) -> None:
        if self.state is not ModuleState.RUNNING:
            return
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self._count("dropped")
            self._audit.record(
                status="dropped",
                intent=event.payload.get("intent"),
                intent_event=event.event_id,
                detail="action queue full",
            )
            logger.warning("Action queue full; dropped intent %s", event)

    # ------------------------------------------------------------------
    # Worker pipeline
    # ------------------------------------------------------------------
    def _work_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is _SHUTDOWN:
                break
            try:
                self._process(item)
            except Exception:  # the pipeline must survive anything
                logger.exception("Action pipeline failed on %s", item)

    def _process(self, event: Event) -> None:
        if event.topic == Topics.ACTION_EXECUTE:
            self._process_direct(event)
            return
        intent = event.payload.get("intent")
        if not isinstance(intent, str):
            self._finish(event, action=None, status="invalid",
                         detail="malformed intent event")
            return

        binding = self._config.intent_bindings.get(intent)
        if binding is None:
            # Unbound intents are normal (not every meaning has an effect
            # yet); audit quietly, no result event, no log spam.
            self._count("unbound")
            self._audit.record(status="unbound", intent=intent,
                               intent_event=event.event_id,
                               perception_event=event.payload.get("source_event"))
            return

        action_name = binding.get("action")
        params: Mapping[str, Any] = binding.get("params", {})
        spec = self._registry.get(action_name) if isinstance(action_name, str) else None
        if spec is None:
            self._finish(event, action=action_name, status="unknown_action",
                         detail=f"no registered action {action_name!r}")
            return

        try:
            spec.validate(params)
        except ValueError as exc:
            self._finish(event, action=spec.name, status="invalid",
                         detail=str(exc))
            return

        self._publish(Event(
            topic=Topics.ACTION, source=self.name,
            payload={"action": spec.name, "intent": intent,
                     "params": dict(params), "risk": spec.risk.value},
        ))

        decision = self._policy.evaluate(spec.name, spec.risk)
        if decision is Decision.DENY:
            self._finish(event, action=spec.name, status="denied",
                         detail="permission policy", params=params)
            return
        if decision is Decision.CONFIRM:
            if not self._confirm(event, spec, params):
                return

        self._execute(event, spec, params)

    def _process_direct(self, event: Event) -> None:
        """Gated execution of one explicit (action, params) — plan steps.

        Identical pipeline to the intent path minus binding lookup: the
        publisher already names the action, but names buy nothing — the
        registry, validators, permission policy, confirmation gates and
        audit apply to every step exactly as they do to a gesture.
        """
        action_name = event.payload.get("action")
        params: Mapping[str, Any] = event.payload.get("params", {}) or {}
        spec = self._registry.get(action_name) if isinstance(action_name, str) else None
        if spec is None:
            self._finish(event, action=action_name, status="unknown_action",
                         detail=f"no registered action {action_name!r}")
            return
        try:
            spec.validate(params)
        except ValueError as exc:
            self._finish(event, action=spec.name, status="invalid",
                         detail=str(exc))
            return

        self._publish(Event(
            topic=Topics.ACTION, source=self.name,
            payload={"action": spec.name,
                     "intent": event.payload.get("label") or None,
                     "params": dict(params), "risk": spec.risk.value,
                     **self._plan_fields(event)},
        ))

        decision = self._policy.evaluate(spec.name, spec.risk)
        if decision is Decision.DENY:
            self._finish(event, action=spec.name, status="denied",
                         detail="permission policy", params=params)
            return
        if decision is Decision.CONFIRM:
            if not self._confirm(event, spec, params):
                return
        self._execute(event, spec, params)

    @staticmethod
    def _plan_fields(event: Event) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        for key in ("plan_id", "step", "label"):
            value = event.payload.get(key)
            if value is not None:
                fields[key] = value
        return fields

    # ------------------------------------------------------------------
    def _confirm(self, event: Event, spec: ActionSpec, params: Mapping[str, Any]) -> bool:
        self._publish_confirmation(spec.name, "pending")
        try:
            approved = self._confirmation.request(
                spec.name, params, self._confirm_timeout
            )
        except Exception:
            logger.exception("Confirmation provider failed; denying")
            approved = False
        self._publish_confirmation(spec.name, "approved" if approved else "denied")
        if not approved:
            self._finish(event, action=spec.name, status="rejected",
                         detail="confirmation denied or timed out", params=params)
        return approved

    def _execute(self, event: Event, spec: ActionSpec, params: Mapping[str, Any]) -> None:
        executor = self._executor
        if executor is None:  # stopping
            self._finish(event, action=spec.name, status="failed",
                         detail="dispatcher stopped", params=params)
            return
        started = time.perf_counter()
        future: Future = executor.submit(spec.handler, params)
        try:
            detail = future.result(timeout=self._config.action_timeout_s)
            status, detail = "completed", (detail or "")
        except FutureTimeout:
            status = "timeout"
            detail = f"exceeded {self._config.action_timeout_s:.0f}s (still running!)"
            logger.error("Action %r timed out", spec.name)
        except Exception as exc:
            status, detail = "failed", f"{type(exc).__name__}: {exc}"
            logger.exception("Action %r failed", spec.name)
        duration_ms = round((time.perf_counter() - started) * 1000.0, 1)
        self._finish(event, action=spec.name, status=status, detail=str(detail)[:300],
                     params=params, duration_ms=duration_ms)

    # ------------------------------------------------------------------
    def _finish(
        self,
        event: Event,
        action: str | None,
        status: str,
        detail: str = "",
        params: Mapping[str, Any] | None = None,
        duration_ms: float | None = None,
    ) -> None:
        self._count(status)
        entry: dict[str, Any] = {
            "status": status,
            "action": action,
            "intent": event.payload.get("intent"),
            "context": event.payload.get("context"),
            "detail": detail,
            "intent_event": event.event_id,
            "perception_event": event.payload.get("source_event"),
        }
        if params is not None:
            entry["params"] = dict(params)
        if duration_ms is not None:
            entry["duration_ms"] = duration_ms
        entry.update(self._plan_fields(event))
        self._audit.record(**entry)

        payload = {k: v for k, v in entry.items() if k != "params" and v is not None}
        self._publish(Event(topic=Topics.ACTION_RESULT, source=self.name,
                            payload=payload))

    def _publish_confirmation(self, action: str, state: str) -> None:
        self._publish(Event(topic=Topics.CONFIRMATION, source=self.name,
                            payload={"action": action, "state": state}))

    def _count(self, status: str) -> None:
        with self._counts_lock:
            self._counts[status] = self._counts.get(status, 0) + 1

    # ------------------------------------------------------------------
    @property
    def registry(self) -> ActionRegistry:
        """The action catalog (kernel composition may extend it pre-start)."""
        return self._registry

    def actions_catalog(self) -> tuple[tuple[str, str, str], ...]:
        """(name, risk, description) for every registered action —
        consumed by the chat reasoner's prompt."""
        return tuple(
            (spec.name, spec.risk.value, spec.description)
            for spec in (self._registry.get(name) for name in self._registry.names)
            if spec is not None
        )

    def _metrics(self) -> dict[str, Any]:
        with self._counts_lock:
            counts = dict(self._counts)
        return {
            "queued": self._queue.qsize(),
            "outcomes": counts,
            "actions_available": len(self._registry.names),
        }


# ---------------------------------------------------------------------------
def build_action_dispatcher(
    automation: AutomationConfig, security: SecurityConfig,
    confirmation: "ConfirmationProvider | None" = None,
) -> ActionDispatcher:
    """Wire a dispatcher with built-in actions and configured security.

    ``confirmation`` overrides the configured provider — kernel
    composition uses this to hand the dispatcher a live
    ``WebConfirmation`` when ``security.confirmation: web``.
    """
    registry = ActionRegistry()
    register_builtin_actions(registry, applications=automation.applications)
    register_input_actions(
        registry,
        backend_factory=partial(create_input_backend, automation.input_backend),
        max_type_text_chars=automation.max_type_text_chars,
    )
    policy = PermissionPolicy(
        risk_defaults=security.risk_defaults, overrides=security.permissions
    )
    provider = confirmation or create_confirmation_provider(
        security.confirmation)
    audit = AuditLog(security.audit_file, max_bytes=security.audit_max_bytes)
    return ActionDispatcher(
        config=automation,
        registry=registry,
        policy=policy,
        confirmation=provider,
        audit=audit,
        confirmation_timeout_s=security.confirmation_timeout_s,
    )
