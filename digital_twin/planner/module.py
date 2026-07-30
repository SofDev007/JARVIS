"""The planner: multi-step tasks, every step through every gate.

The planner never executes anything itself. It publishes each step of the
current stage as an ``action.execute`` event and waits for the matching
``action.result`` events — which means every step individually passes the
dispatcher's registry check, parameter validation, permission policy,
confirmation gate and audit. A plan is choreography, not privilege.

Plan sources:

* ``plan.request {plan: <name>}`` — named routines from configuration,
* ``plan.request {goal, steps}`` — LLM proposals (accepted only when
  ``planner.accept_llm_plans`` is on; rejected ones are announced),
* ``plan.request {skill: <query>}`` — replay the best-matching skill from
  memory,
* ``intent.detected`` matching ``planner.intent_triggers`` — a gesture can
  start a routine.

Lifecycle is fully observable on ``plan.progress`` (started /
step_completed / step_failed / completed / failed / cancelled / rejected),
plans are cancellable between steps via ``plan.cancel``, stages carry a
timeout, and failure policy is per-plan (``abort`` or ``continue``).

**Skill memory's first producer**: an LLM plan that completes successfully
is saved as a ``skill`` memory (through the injected memory module — its
store, its encryption, its user controls) and can be replayed later by a
text query. The assistant now learns workflows.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from digital_twin.configuration.settings import PlannerConfig
from digital_twin.core.bus import Subscription
from digital_twin.core.events import Event, Topics
from digital_twin.core.module import BaseModule, ModuleState
from digital_twin.planner.plan import (
    ON_ERROR_ABORT,
    Plan,
    plan_from_config,
    plan_from_llm,
    plan_from_skill,
)

logger = logging.getLogger(__name__)

_SHUTDOWN = object()
_TERMINAL_OK = "completed"


@dataclass
class _PlanRun:
    """Mutable state of one running plan (touched only by the worker)."""

    plan: Plan
    plan_id: str
    provenance: str
    stage_index: int = 0
    pending: set[int] = field(default_factory=set)
    completed_steps: int = 0
    failed_steps: int = 0
    deadline: float = 0.0

    def steps_before_stage(self) -> int:
        return sum(len(stage) for stage in self.plan.stages[: self.stage_index])


class PlannerModule(BaseModule):
    """Runs plans by choreographing gated ``action.execute`` steps."""

    name = "planner"
    topics = (Topics.PLAN_PROGRESS, Topics.ACTION_EXECUTE)

    def __init__(self, config: PlannerConfig, memory: Any | None = None):
        super().__init__()
        self._config = config
        self._memory = memory  # MemoryModule, injected by the kernel
        self._plans: dict[str, Plan] = {}
        self._runs: dict[str, _PlanRun] = {}
        self._queue: queue.Queue = queue.Queue(maxsize=64)
        self._worker: threading.Thread | None = None
        self._subscriptions: list[Subscription] = []
        self._counts: dict[str, int] = {}

        # Config plans are parsed (and therefore validated) at build time —
        # a broken routine fails startup, not the moment you need it.
        for plan_name, raw in config.plans.items():
            self._plans[plan_name] = plan_from_config(
                plan_name, raw, config.max_steps
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def _on_start(self) -> None:
        assert self._bus is not None
        self._worker = threading.Thread(
            target=self._work_loop, name="planner", daemon=True
        )
        self._worker.start()
        self._subscriptions = [
            self._bus.subscribe(Topics.PLAN_REQUEST, self._enqueue,
                                name="planner.request"),
            self._bus.subscribe(Topics.PLAN_CANCEL, self._enqueue,
                                name="planner.cancel"),
            self._bus.subscribe(Topics.ACTION_RESULT, self._enqueue,
                                name="planner.results"),
            self._bus.subscribe(Topics.INTENT, self._enqueue,
                                name="planner.intents"),
        ]

    def _on_stop(self) -> None:
        for subscription in self._subscriptions:
            subscription.cancel()
        self._subscriptions = []
        self._queue.put(_SHUTDOWN)
        if self._worker is not None:
            self._worker.join(timeout=5.0)
            self._worker = None
        self._runs.clear()

    def _on_pause(self) -> None:
        """Paused planner accepts no new requests; running plans drain."""

    def _on_resume(self) -> None:
        """Nothing to rebuild."""

    # ------------------------------------------------------------------
    # Bus handlers (enqueue only; worker owns all state)
    # ------------------------------------------------------------------
    def _enqueue(self, event: Event) -> None:
        if self.state is not ModuleState.RUNNING:
            return
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            logger.warning("Planner queue full; dropped %s", event)

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------
    def _work_loop(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.25)
            except queue.Empty:
                self._check_deadlines()
                continue
            if item is _SHUTDOWN:
                break
            try:
                self._handle(item)
            except Exception:  # the planner must survive anything
                logger.exception("Planner failed handling %s", item)
            self._check_deadlines()

    def _handle(self, event: Event) -> None:
        if event.topic == Topics.PLAN_REQUEST:
            self._handle_request(event)
        elif event.topic == Topics.PLAN_CANCEL:
            self._handle_cancel(event)
        elif event.topic == Topics.ACTION_RESULT:
            self._handle_result(event)
        elif event.topic == Topics.INTENT:
            self._handle_intent(event)

    # ------------------------------------------------------------------
    # Plan intake
    # ------------------------------------------------------------------
    def _handle_intent(self, event: Event) -> None:
        intent = event.payload.get("intent")
        plan_name = self._config.intent_triggers.get(intent) if intent else None
        if plan_name:
            self._start_named(plan_name, provenance=event.event_id)

    def _handle_request(self, event: Event) -> None:
        payload = event.payload
        if isinstance(payload.get("plan"), str):
            self._start_named(payload["plan"], provenance=event.event_id)
            return
        if isinstance(payload.get("skill"), str):
            self._start_skill(payload["skill"], provenance=event.event_id)
            return
        if payload.get("steps") is not None:
            self._start_llm(event)
            return
        self._announce_stillborn("(unspecified)",
                                 "request needs 'plan', 'skill' or 'steps'",
                                 status="rejected")

    def _start_named(self, plan_name: str, provenance: str) -> None:
        plan = self._plans.get(plan_name)
        if plan is None:
            self._announce_stillborn(plan_name,
                                     f"no configured plan {plan_name!r}",
                                     status="failed")
            return
        self._launch(plan, provenance)

    def _start_llm(self, event: Event) -> None:
        goal = str(event.payload.get("goal") or "")
        if not self._config.accept_llm_plans:
            self._count("llm_rejected")
            self._announce_stillborn(goal or "(llm plan)",
                                     "planner.accept_llm_plans is disabled",
                                     status="rejected")
            return
        try:
            plan = plan_from_llm(event.payload, self._config.max_steps)
        except ValueError as exc:
            self._announce_stillborn(goal or "(llm plan)", str(exc),
                                     status="rejected")
            return
        self._launch(plan, provenance=event.event_id)

    def _start_skill(self, query: str, provenance: str) -> None:
        if self._memory is None:
            self._announce_stillborn(query, "no memory module available",
                                     status="failed")
            return
        try:
            hits = self._memory.store.search(query, kinds=("skill",), limit=1)
        except Exception:
            logger.exception("Skill lookup failed")
            hits = []
        if not hits:
            self._announce_stillborn(query, f"no skill matching {query!r}",
                                     status="failed")
            return
        try:
            plan = plan_from_skill(hits[0].record.data or {},
                                   self._config.max_steps)
        except ValueError as exc:
            self._announce_stillborn(query, f"stored skill unusable: {exc}",
                                     status="failed")
            return
        self._launch(plan, provenance)

    # ------------------------------------------------------------------
    # Execution choreography
    # ------------------------------------------------------------------
    def _launch(self, plan: Plan, provenance: str) -> None:
        run = _PlanRun(plan=plan, plan_id=uuid.uuid4().hex, provenance=provenance)
        self._runs[run.plan_id] = run
        self._count("started")
        self._progress(run, "started",
                       detail=plan.description or plan.name)
        self._publish_stage(run)

    def _publish_stage(self, run: _PlanRun) -> None:
        stage = run.plan.stages[run.stage_index]
        base = run.steps_before_stage()
        run.pending = set(range(base, base + len(stage)))
        run.deadline = time.monotonic() + self._config.step_timeout_s
        for offset, step in enumerate(stage):
            self._publish(Event(
                topic=Topics.ACTION_EXECUTE,
                source=self.name,
                payload={
                    "action": step.action,
                    "params": dict(step.params),
                    "label": step.describe(),
                    "plan_id": run.plan_id,
                    "step": base + offset,
                    "source_event": run.provenance,
                },
            ))

    def _handle_result(self, event: Event) -> None:
        plan_id = event.payload.get("plan_id")
        run = self._runs.get(plan_id) if isinstance(plan_id, str) else None
        if run is None:
            return
        step = event.payload.get("step")
        if step not in run.pending:
            return
        run.pending.discard(step)
        status = event.payload.get("status")
        ok = status == _TERMINAL_OK
        if ok:
            run.completed_steps += 1
            self._progress(run, "step_completed", step=step,
                           detail=str(event.payload.get("action")),
                           result=event.payload.get("detail"))
        else:
            run.failed_steps += 1
            self._progress(run, "step_failed", step=step,
                           detail=f"{event.payload.get('action')}: {status}")
            if run.plan.on_error == ON_ERROR_ABORT:
                self._finish_run(run, "failed",
                                 f"step {step} {status}; aborting")
                return
        if not run.pending:
            self._advance(run)

    def _advance(self, run: _PlanRun) -> None:
        run.stage_index += 1
        if run.stage_index >= len(run.plan.stages):
            if run.failed_steps:
                self._finish_run(
                    run, "completed_with_errors",
                    f"{run.completed_steps} ok, {run.failed_steps} failed")
            else:
                self._finish_run(run, "completed",
                                 f"{run.completed_steps} steps")
                self._maybe_save_skill(run)
            return
        self._publish_stage(run)

    def _handle_cancel(self, event: Event) -> None:
        plan_id = event.payload.get("plan_id")
        run = self._runs.get(plan_id) if isinstance(plan_id, str) else None
        if run is None:
            return
        self._count("cancelled")
        self._finish_run(run, "cancelled", "cancelled by request")

    def _check_deadlines(self) -> None:
        now = time.monotonic()
        for run in list(self._runs.values()):
            if run.pending and now > run.deadline:
                self._finish_run(
                    run, "failed",
                    f"stage {run.stage_index} timed out waiting for steps "
                    f"{sorted(run.pending)}")

    def _finish_run(self, run: _PlanRun, status: str, detail: str) -> None:
        self._runs.pop(run.plan_id, None)
        self._count(status)
        self._progress(run, status, detail=detail)

    # ------------------------------------------------------------------
    # Skills
    # ------------------------------------------------------------------
    def _maybe_save_skill(self, run: _PlanRun) -> None:
        if run.plan.source != "llm" or self._memory is None:
            return
        try:
            record = self._memory.store.add(
                kind="skill",
                content=f"Learned workflow: {run.plan.description}",
                data=run.plan.to_data(),
                source=self.name,
                importance=0.6,
                tags=("skill", "plan"),
            )
        except Exception:
            logger.exception("Failed to save skill")
            return
        self._count("skills_saved")
        logger.info("Saved skill %s: %s", record.id[:8], run.plan.description)

    # ------------------------------------------------------------------
    def _progress(self, run: _PlanRun, status: str,
                  step: int | None = None, detail: str = "",
                  result: Any = None) -> None:
        payload: dict[str, Any] = {
            "plan_id": run.plan_id,
            "plan": run.plan.name,
            "plan_source": run.plan.source,
            "status": status,
            "total_steps": run.plan.total_steps,
            "source_event": run.provenance,
        }
        if step is not None:
            payload["step"] = step
        if detail:
            payload["detail"] = detail
        if result is not None:
            payload["result"] = result
        self._publish(Event(topic=Topics.PLAN_PROGRESS, source=self.name,
                            payload=payload))

    def _announce_stillborn(self, name: str, detail: str, status: str) -> None:
        self._count(status)
        logger.warning("Plan %r %s: %s", name, status, detail)
        self._publish(Event(
            topic=Topics.PLAN_PROGRESS, source=self.name,
            payload={"plan_id": "", "plan": name, "status": status,
                     "total_steps": 0, "detail": detail},
        ))

    def _count(self, key: str) -> None:
        self._counts[key] = self._counts.get(key, 0) + 1

    # ------------------------------------------------------------------
    def _metrics(self) -> dict[str, Any]:
        return {
            "configured_plans": len(self._plans),
            "running": len(self._runs),
            **self._counts,
        }
