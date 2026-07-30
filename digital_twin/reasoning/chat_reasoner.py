"""Chat reasoner: free-form text → explained replies and *gated* intents.

The security architecture of this module matters more than its prompt:

* **The LLM has no direct access to actions.** It can only nominate an
  intent from the configured allow-list; the nomination is published as a
  normal ``intent.detected`` event and flows through the *same* dispatcher
  pipeline as a gesture — bindings, validation, permission policy,
  confirmation gates, audit. A hallucinated or hostile intent name is
  dropped here; a legitimate one still cannot skip a single gate.
* **Memory in, memory out.** Relevant memories (M6 ranked search over the
  user's words) are injected into the prompt — reasoning finally consumes
  what the assistant remembers — and facts the user teaches are persisted
  via the memory module (its store, its rules, its user controls).
* **Explainability is part of the contract**: the model must return a
  one-line ``reasoning`` string, which travels on every ``chat.response``
  event and into the logs.
* **Slow work never blocks the bus**: the subscription handler enqueues;
  a worker thread talks to the model (same shape as the action
  dispatcher). Model failures produce apologetic responses, never crashes.

Wiring note: the memory module is *injected by the kernel* (composition in
``main.py``), not imported across modules — pull-style queries get read
interfaces; the bus remains the only push channel.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
from collections import deque
from typing import Any, Callable

from digital_twin.configuration.settings import LLMConfig
from digital_twin.core.bus import Subscription
from digital_twin.core.events import Event, Topics
from digital_twin.core.module import BaseModule, ModuleState
from digital_twin.reasoning.llm import ChatMessage, LanguageModel, LLMError

logger = logging.getLogger(__name__)

_SHUTDOWN = object()

#: Screen text older than this is considered stale and not injected —
#: the screen has almost certainly changed since the capture.
_SCREEN_TTL_S = 600.0
#: Hard bound on screen text injected into one prompt.
_SCREEN_PROMPT_CHARS = 4000

_SYSTEM_TEMPLATE = """\
{persona}

You are this assistant's reasoning engine. Never invent capabilities you \
don't have.

Current application context: {context}

You may trigger EXACTLY ONE of these intents when the user asks you to \
do something, and no other: {intents}
Every intent still passes a permission and confirmation system before \
anything executes.

{memories}{screen}{knowledge}{plans}Respond ONLY with a JSON object, no other text:
{{"reply": "<what to say to the user>",
 "intent": "<one allowed intent, or null>",
 "plan": <a plan object as described above when a multi-step task is needed, else null>,
 "remember": "<a short fact worth persisting if the user taught you one, or null>",
 "reasoning": "<one sentence explaining your decision>"}}"""

_PLANS_TEMPLATE = """\
For MULTI-STEP requests that no single intent covers, you may propose a
plan using ONLY these actions (every step is validated and permission-
gated individually; sensitive steps require user confirmation):
{catalog}
Propose it as "plan": {{"goal": "<what the plan achieves>",
"steps": [{{"action": "<name>", "params": {{...}}, "label": "<short step description>"}}]}}.
Prefer a single intent when one suffices; then "plan" must be null.

"""


def parse_model_reply(text: str) -> dict[str, Any]:
    """Extract the structured decision from a model reply, forgivingly.

    Accepts raw JSON, JSON inside markdown fences, or JSON with
    surrounding chatter; anything unparseable degrades to treating the
    whole text as the reply (never an exception).
    """
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.DOTALL)
    if fenced:
        candidate = fenced.group(1)
    else:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start != -1 and end > start:
            candidate = candidate[start:end + 1]
    try:
        data = json.loads(candidate)
        if not isinstance(data, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        return {"reply": text.strip(), "intent": None, "plan": None,
                "remember": None, "reasoning": ""}
    plan = data.get("plan")
    if isinstance(plan, dict):
        goal = plan.get("goal")
        steps = plan.get("steps")
        if not (isinstance(goal, str) and goal.strip()
                and isinstance(steps, list) and steps
                and all(isinstance(step, dict)
                        and isinstance(step.get("action"), str)
                        and isinstance(step.get("params", {}), dict)
                        for step in steps)):
            plan = None
    else:
        plan = None
    return {
        "reply": str(data.get("reply") or "").strip() or text.strip(),
        "intent": data.get("intent") if isinstance(data.get("intent"), str) else None,
        "plan": plan,
        "remember": (data.get("remember")
                     if isinstance(data.get("remember"), str) else None),
        "reasoning": str(data.get("reasoning") or "").strip(),
    }


class ChatReasoner(BaseModule):
    """LLM-backed interpretation of ``perception.chat`` events."""

    name = "reasoner"
    topics = (Topics.CHAT_RESPONSE, Topics.INTENT)

    def __init__(
        self,
        config: LLMConfig,
        model: "LanguageModel | Callable[[], LanguageModel]",
        allowed_intents: tuple[str, ...],
        memory: Any | None = None,
        knowledge: Any | None = None,
        action_catalog: "tuple[tuple[str, str, str], ...] | Callable[[], tuple[tuple[str, str, str], ...]]" = (),
    ):
        super().__init__()
        self._config = config
        self._model_source = model
        self._model: LanguageModel | None = None
        self._allowed = tuple(sorted(allowed_intents))
        # Late-bound: a callable is evaluated per message, so actions
        # registered after this module is constructed (voice, plugins…)
        # still appear in the prompt. A tuple is snapshotted as before.
        self._catalog_source = action_catalog
        self._memory = memory  # MemoryModule, injected by the kernel
        self._knowledge = knowledge  # KnowledgeStore + config, kernel-injected
        self._context = "desktop"
        self._screen_text: str | None = None
        self._screen_at = 0.0
        self._history: deque[ChatMessage] = deque(maxlen=config.history_turns * 2)
        self._queue: queue.Queue = queue.Queue(maxsize=8)
        self._worker: threading.Thread | None = None
        self._subscriptions: list[Subscription] = []
        self._counts: dict[str, int] = {}
        # plan_id -> {"source_event": str, "results": [str, ...]}; lets a
        # plan the reasoner requested (e.g. "get_time") speak its actual
        # result once the planner finishes, instead of only the initial
        # "I'll check that" acknowledgment.
        self._active_plans: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def _on_start(self) -> None:
        assert self._bus is not None
        # Resolve the model here, not at construction: a missing API key
        # marks THIS module FAILED with guidance (announced on the bus)
        # while the rest of the assistant keeps running.
        self._model = (self._model_source() if callable(self._model_source)
                       else self._model_source)
        self._worker = threading.Thread(
            target=self._work_loop, name="chat-reasoner", daemon=True
        )
        self._worker.start()
        self._subscriptions = [
            self._bus.subscribe(Topics.CHAT, self._on_chat, name="reasoner.chat"),
            self._bus.subscribe(Topics.VOICE, self._on_chat,
                                name="reasoner.voice"),
            self._bus.subscribe(Topics.CONTEXT, self._on_context,
                                name="reasoner.context"),
            self._bus.subscribe(Topics.SCREEN, self._on_screen,
                                name="reasoner.screen"),
            self._bus.subscribe(Topics.PLAN_PROGRESS, self._on_plan_progress,
                                name="reasoner.plans"),
        ]

    def _on_stop(self) -> None:
        for subscription in self._subscriptions:
            subscription.cancel()
        self._subscriptions = []
        self._queue.put(_SHUTDOWN)
        if self._worker is not None:
            self._worker.join(timeout=5.0)
            self._worker = None

    def _on_pause(self) -> None:
        """Paused reasoner ignores new chat; queued work drains."""

    def _on_resume(self) -> None:
        """Nothing to rebuild."""

    # ------------------------------------------------------------------
    # Bus handlers (enqueue only)
    # ------------------------------------------------------------------
    def _on_context(self, event: Event) -> None:
        context = event.payload.get("context")
        if isinstance(context, str) and context:
            self._context = context

    def _on_screen(self, event: Event) -> None:
        """Latest OCR'd screen text (published only by the gated
        read_screen action — this handler never triggers a capture)."""
        text = event.payload.get("text")
        if isinstance(text, str) and text.strip():
            self._screen_text = text
            self._screen_at = time.time()
        else:
            self._screen_text = None

    def _on_plan_progress(self, event: Event) -> None:
        """Speak a plan's actual result once it finishes — the initial
        reply only ever announces intent ("I'll check that"), never the
        outcome, because the planner runs asynchronously."""
        if self.state is not ModuleState.RUNNING:
            return
        payload = event.payload
        plan_id = payload.get("plan_id")
        status = payload.get("status")
        if not plan_id:
            return
        if status == "started":
            source_event = payload.get("source_event")
            if source_event:
                self._active_plans[plan_id] = {
                    "source_event": source_event, "results": [],
                }
            return
        run = self._active_plans.get(plan_id)
        if run is None:
            return
        if status == "step_completed":
            result = payload.get("result")
            if isinstance(result, str) and result:
                run["results"].append(result)
            return
        if status in ("completed", "completed_with_errors", "failed", "cancelled"):
            del self._active_plans[plan_id]
            self._respond_to_plan(run, status, payload)

    def _respond_to_plan(self, run: dict[str, Any], status: str,
                         payload: dict[str, Any]) -> None:
        results = run["results"]
        if status in ("completed", "completed_with_errors") and results:
            text = " ".join(results)
            if status == "completed_with_errors":
                text += " (some steps failed)"
        elif status == "cancelled":
            text = "Cancelled that."
        else:
            text = f"That didn't work out: {payload.get('detail', status)}"
        self._count("plan_replies")
        self._publish(Event(
            topic=Topics.CHAT_RESPONSE,
            source=self.name,
            payload={
                "text": text,
                "reasoning": "plan result",
                "source_event": run["source_event"],
            },
        ))

    def _on_chat(self, event: Event) -> None:
        if self.state is not ModuleState.RUNNING:
            return
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self._count("dropped")
            self._respond(event, "I'm still working on your previous request — "
                                 "give me a moment.", reasoning="queue full")

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------
    def _work_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is _SHUTDOWN:
                break
            try:
                self._process(item)
            except Exception:  # the reasoner must survive anything
                logger.exception("Chat reasoning failed")
                self._respond(item, "Something went wrong while thinking about "
                                    "that; the details are in my logs.",
                              reasoning="internal error")

    def _process(self, event: Event) -> None:
        text = str(event.payload.get("text", "")).strip()
        if not text:
            return

        memories = self._recall(text)
        catalog_entries = self._actions_catalog()
        if catalog_entries:
            catalog = "\n".join(
                f"- {name} ({risk}): {description}"
                for name, risk, description in catalog_entries
            )
            plans_section = _PLANS_TEMPLATE.format(catalog=catalog)
        else:
            plans_section = ""
        system = _SYSTEM_TEMPLATE.format(
            persona=self._config.persona,
            context=self._context,
            intents=", ".join(self._allowed) or "(none configured)",
            memories=memories,
            screen=self._screen_section(),
            knowledge=self._knowledge_section(text),
            plans=plans_section,
        )
        messages = [*self._history, ChatMessage(role="user", content=text)]

        model = self._model
        if model is None:
            return
        try:
            raw = model.complete(
                messages,
                system=system,
                max_tokens=self._config.max_tokens,
                temperature=self._config.temperature,
            )
        except LLMError as exc:
            self._count("llm_errors")
            logger.error("LLM failure: %s", exc)
            self._respond(event, f"I couldn't reach my language model: {exc}",
                          reasoning="llm error")
            return

        decision = parse_model_reply(raw)
        self._history.append(ChatMessage(role="user", content=text))
        self._history.append(ChatMessage(role="assistant", content=decision["reply"]))

        intent = self._maybe_trigger_intent(event, decision)
        plan_goal = self._maybe_request_plan(event, decision)
        remembered = self._maybe_remember(decision)
        self._count("handled")
        self._respond(
            event,
            decision["reply"],
            reasoning=decision["reasoning"],
            intent_triggered=intent,
            plan_requested=plan_goal,
            remembered=remembered,
        )

    # ------------------------------------------------------------------
    def _knowledge_section(self, text: str) -> str:
        """RAG: rank the knowledge store against the user's message and
        inject the best chunks, bounded. Recall failures must never kill a
        reply — degraded answers beat no answers."""
        if self._knowledge is None:
            return ""
        try:
            store, config = self._knowledge
            hits = store.search(text, top_k=config.top_k,
                                min_score=config.min_score)
        except Exception:
            logger.exception("knowledge recall failed")
            return ""
        if not hits:
            return ""
        budget = config.prompt_max_chars
        parts: list[str] = []
        for hit in hits:
            entry = f"[{hit.title} · score {hit.score:.2f}]\n{hit.content}"
            if len(entry) > budget:
                entry = entry[:budget]
            parts.append(entry)
            budget -= len(entry)
            if budget <= 0:
                break
        return (
            "Relevant excerpts from the user's ingested documents "
            "(cite them when they answer the question; say so when they "
            "don't):\n" + "\n---\n".join(parts) + "\n\n"
        )

    # ------------------------------------------------------------------
    def _actions_catalog(self) -> tuple[tuple[str, str, str], ...]:
        source = self._catalog_source
        if callable(source):
            try:
                return tuple(source())
            except Exception:  # a broken catalog must not kill replies
                logger.exception("action catalog callable failed")
                return ()
        return tuple(source)

    # ------------------------------------------------------------------
    def _screen_section(self) -> str:
        """Freshly-read screen text for the prompt; stale text is dropped
        (the screen has almost certainly changed since the capture)."""
        text = self._screen_text
        if not text:
            return ""
        age = time.time() - self._screen_at
        if age > _SCREEN_TTL_S:
            self._screen_text = None
            return ""
        return (
            f"Text read from the user's screen {int(age)}s ago via OCR "
            f"(may contain recognition errors):\n"
            f"{text[:_SCREEN_PROMPT_CHARS]}\n\n"
        )

    def _recall(self, text: str) -> str:
        if self._memory is None:
            return ""
        try:
            hits = self._memory.store.search(
                text, limit=self._config.memory_results
            )
        except Exception:
            logger.debug("Memory recall unavailable", exc_info=True)
            return ""
        if not hits:
            return ""
        lines = "\n".join(f"- {hit.record.content}" for hit in hits)
        return f"Things you remember that may be relevant:\n{lines}\n\n"

    def _maybe_trigger_intent(self, event: Event, decision: dict) -> str | None:
        intent = decision.get("intent")
        if not intent:
            return None
        if intent not in self._allowed:
            self._count("intents_rejected")
            logger.warning("Model nominated unknown intent %r; dropped", intent)
            return None
        self._count("intents_triggered")
        self._publish(Event(
            topic=Topics.INTENT,
            source=self.name,
            payload={
                "intent": intent,
                "context": self._context,
                "reasoning": decision.get("reasoning", ""),
                "source_event": event.event_id,
            },
        ))
        return intent

    def _maybe_request_plan(self, event: Event, decision: dict) -> str | None:
        plan = decision.get("plan")
        if not plan or not self._actions_catalog():
            return None
        self._count("plans_proposed")
        self._publish(Event(
            topic=Topics.PLAN_REQUEST,
            source=self.name,
            payload={
                "goal": plan["goal"],
                "steps": plan["steps"],
                "source_event": event.event_id,
            },
        ))
        return str(plan["goal"])

    def _maybe_remember(self, decision: dict) -> str | None:
        fact = decision.get("remember")
        if not fact or self._memory is None:
            return None
        try:
            self._memory.remember_fact(fact, tags=("chat",), source=self.name)
        except Exception:
            logger.exception("Failed to persist fact from chat")
            return None
        self._count("facts_remembered")
        return fact

    def _respond(self, event: Event, text: str, reasoning: str = "",
                 intent_triggered: str | None = None,
                 plan_requested: str | None = None,
                 remembered: str | None = None) -> None:
        payload: dict[str, Any] = {
            "text": text,
            "reasoning": reasoning,
            "source_event": event.event_id,
        }
        if intent_triggered:
            payload["intent_triggered"] = intent_triggered
        if plan_requested:
            payload["plan_requested"] = plan_requested
        if remembered:
            payload["remembered"] = remembered
        self._publish(Event(topic=Topics.CHAT_RESPONSE, source=self.name,
                            payload=payload))

    def _count(self, key: str) -> None:
        self._counts[key] = self._counts.get(key, 0) + 1

    # ------------------------------------------------------------------
    def _metrics(self) -> dict[str, Any]:
        return {
            "model": self._model.name if self._model else "(unresolved)",
            "context": self._context,
            "history_turns": len(self._history) // 2,
            "allowed_intents": len(self._allowed),
            **self._counts,
        }
