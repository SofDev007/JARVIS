"""Plan model: what a multi-step task looks like before it runs.

A plan is a sequence of **stages**; each stage is one or more
:class:`PlanStep` items. Steps inside a stage have no ordering dependency
(they may run concurrently when the executor has capacity); stages run
strictly in order — the planner waits for every result in a stage before
publishing the next. This gives real dependency semantics today and real
parallelism later without changing a single plan definition.

Plans arrive from three sources and all normalise here:

* **config** — named routines in ``planner.plans`` (support the
  ``{parallel: [...]}`` stage syntax),
* **llm** — sequential steps proposed by the chat reasoner,
* **skill** — previously successful LLM plans replayed from memory.

Validation is strict and happens *before* anything executes: shapes,
string lengths, step caps. What it deliberately does **not** check is
whether actions exist or are permitted — that is the dispatcher's job,
per step, through the same gates as everything else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

MAX_LABEL_CHARS = 120

#: Plan-level failure policies.
ON_ERROR_ABORT = "abort"
ON_ERROR_CONTINUE = "continue"


@dataclass(frozen=True)
class PlanStep:
    """One gated action invocation inside a plan."""

    action: str
    params: dict = field(default_factory=dict)
    label: str = ""

    def describe(self) -> str:
        """Human-readable one-liner for progress events and logs."""
        return self.label or self.action


@dataclass(frozen=True)
class Plan:
    """A validated, ready-to-run plan."""

    name: str
    stages: tuple[tuple[PlanStep, ...], ...]
    source: str  # "config" | "llm" | "skill"
    description: str = ""
    on_error: str = ON_ERROR_ABORT

    @property
    def total_steps(self) -> int:
        return sum(len(stage) for stage in self.stages)

    def to_data(self) -> dict:
        """JSON-serialisable form (used when saving skills)."""
        return {
            "name": self.name,
            "description": self.description,
            "on_error": self.on_error,
            "stages": [
                [
                    {"action": step.action, "params": dict(step.params),
                     "label": step.label}
                    for step in stage
                ]
                for stage in self.stages
            ],
        }


def _parse_step(raw: Any) -> PlanStep:
    if not isinstance(raw, Mapping):
        raise ValueError(f"plan step must be a mapping, got {type(raw).__name__}")
    action = raw.get("action")
    if not isinstance(action, str) or not action.strip():
        raise ValueError("plan step needs a non-empty 'action' string")
    params = raw.get("params", {})
    if not isinstance(params, Mapping):
        raise ValueError(f"step {action!r}: 'params' must be a mapping")
    label = raw.get("label", "")
    if not isinstance(label, str):
        raise ValueError(f"step {action!r}: 'label' must be a string")
    return PlanStep(
        action=action.strip(),
        params=dict(params),
        label=label.strip()[:MAX_LABEL_CHARS],
    )


def parse_stages(raw_steps: Any, max_steps: int) -> tuple[tuple[PlanStep, ...], ...]:
    """Normalise a raw step list into stages.

    Each item is either a step mapping (a stage of one) or
    ``{"parallel": [step, ...]}`` (one multi-step stage). Raises
    ``ValueError`` with a precise message on any structural problem.
    """
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("plan needs a non-empty 'steps' list")
    stages: list[tuple[PlanStep, ...]] = []
    for item in raw_steps:
        if isinstance(item, Mapping) and "parallel" in item:
            group = item["parallel"]
            if not isinstance(group, list) or not group:
                raise ValueError("'parallel' must hold a non-empty step list")
            stages.append(tuple(_parse_step(step) for step in group))
        else:
            stages.append((_parse_step(item),))
    total = sum(len(stage) for stage in stages)
    if total > max_steps:
        raise ValueError(f"plan has {total} steps; the limit is {max_steps}")
    return tuple(stages)


def plan_from_config(name: str, raw: Mapping, max_steps: int) -> Plan:
    """Build a named routine from a ``planner.plans`` entry."""
    if not isinstance(raw, Mapping):
        raise ValueError(f"plan {name!r} must be a mapping")
    on_error = raw.get("on_error", ON_ERROR_ABORT)
    if on_error not in (ON_ERROR_ABORT, ON_ERROR_CONTINUE):
        raise ValueError(f"plan {name!r}: on_error must be abort or continue")
    description = raw.get("description", "")
    if not isinstance(description, str):
        raise ValueError(f"plan {name!r}: description must be a string")
    return Plan(
        name=name,
        description=description,
        on_error=on_error,
        stages=parse_stages(raw.get("steps"), max_steps),
        source="config",
    )


def plan_from_llm(payload: Mapping, max_steps: int) -> Plan:
    """Build a plan from an LLM proposal: ``{goal, steps: [...]}`` only.

    LLM plans are deliberately sequential — the model reasons step by
    step; parallel semantics stay a human (config) affordance.
    """
    goal = payload.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("llm plan needs a non-empty 'goal'")
    steps = payload.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("llm plan needs a non-empty 'steps' list")
    for step in steps:
        if isinstance(step, Mapping) and "parallel" in step:
            raise ValueError("llm plans may not contain parallel groups")
    return Plan(
        name=goal.strip()[:MAX_LABEL_CHARS],
        description=goal.strip(),
        stages=parse_stages(steps, max_steps),
        source="llm",
    )


def plan_from_skill(data: Mapping, max_steps: int) -> Plan:
    """Rebuild a plan from skill-memory data (``Plan.to_data`` shape)."""
    stages_raw = data.get("stages")
    if not isinstance(stages_raw, list) or not stages_raw:
        raise ValueError("skill data has no stages")
    flattened: list[Any] = []
    for stage in stages_raw:
        if not isinstance(stage, list) or not stage:
            raise ValueError("skill stage must be a non-empty list")
        flattened.append({"parallel": stage} if len(stage) > 1 else stage[0])
    return Plan(
        name=str(data.get("name") or "recalled skill")[:MAX_LABEL_CHARS],
        description=str(data.get("description") or ""),
        on_error=(data.get("on_error")
                  if data.get("on_error") in (ON_ERROR_ABORT, ON_ERROR_CONTINUE)
                  else ON_ERROR_ABORT),
        stages=parse_stages(flattened, max_steps),
        source="skill",
    )
