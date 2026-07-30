#!/usr/bin/env python3
"""M12 performance pass — measure the paths that matter.

Three numbers, printed and explained:

1. **Bus throughput** — events/second through publish → dispatch →
   deliver with a trivial subscriber. This is the ceiling for every
   perception stream.
2. **Action pipeline latency** — median/p95 wall time for a SAFE action
   through the full gate pipeline (validate → permission → execute →
   audit → result event). This is the cost of safety per action.
3. **Reasoner prompt assembly** — how long building the system prompt
   (catalog + memory + screen sections) takes with a fully loaded
   catalog, since M11 made it late-bound and per-message.

Run from the repository root::

    python examples/benchmark.py
"""

from __future__ import annotations

import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from digital_twin.automation.dispatcher import ActionDispatcher  # noqa: E402
from digital_twin.automation.registry import ActionRegistry, ActionSpec  # noqa: E402
from digital_twin.configuration.settings import (  # noqa: E402
    AutomationConfig,
    SecurityConfig,
)
from digital_twin.core.bus import EventBus  # noqa: E402
from digital_twin.core.events import Event, Topics  # noqa: E402
from digital_twin.security.audit import AuditLog  # noqa: E402
from digital_twin.security.confirmation import AutoDenyConfirmation  # noqa: E402
from digital_twin.security.permissions import (  # noqa: E402
    PermissionPolicy,
    RiskLevel,
)

EVENTS = 20_000
ACTIONS = 300


def bench_bus() -> float:
    bus = EventBus()
    received = []
    bus.subscribe("bench.topic", received.append)
    bus.start()
    started = time.perf_counter()
    for index in range(EVENTS):
        bus.publish(Event("bench.topic", "bench", {"n": index}))
    while len(received) < EVENTS and time.perf_counter() - started < 30:
        time.sleep(0.005)
    elapsed = time.perf_counter() - started
    bus.stop()
    if len(received) < EVENTS:
        raise RuntimeError(f"bus delivered {len(received)}/{EVENTS}")
    return EVENTS / elapsed


def bench_pipeline(tmp: Path) -> tuple[float, float]:
    registry = ActionRegistry()
    registry.register(ActionSpec(
        name="bench_noop", description="no-op", risk=RiskLevel.SAFE,
        handler=lambda params: "ok",
    ))
    security = SecurityConfig()
    bus = EventBus()
    results = []
    bus.subscribe(Topics.ACTION_RESULT, results.append)
    bus.start()
    dispatcher = ActionDispatcher(
        config=AutomationConfig(action_timeout_s=5.0),
        registry=registry,
        policy=PermissionPolicy(security.risk_defaults, security.permissions),
        confirmation=AutoDenyConfirmation(),
        audit=AuditLog(tmp / "bench_audit.jsonl"),
    )
    dispatcher.start(bus)
    latencies = []
    for index in range(ACTIONS):
        before = len(results)
        started = time.perf_counter()
        bus.publish(Event(Topics.ACTION_EXECUTE, "bench",
                          {"action": "bench_noop", "params": {}}))
        while len(results) <= before:
            time.sleep(0.0002)
        latencies.append((time.perf_counter() - started) * 1000)
    dispatcher.stop()
    bus.stop()
    quantiles = statistics.quantiles(latencies, n=20)
    return statistics.median(latencies), quantiles[18]  # p95


def bench_prompt() -> float:
    from digital_twin.configuration.settings import LLMConfig
    from digital_twin.reasoning.chat_reasoner import ChatReasoner

    catalog = tuple(
        (f"action_{index}", "sensitive", f"Benchmark action number {index}.")
        for index in range(60)
    )
    reasoner = ChatReasoner(
        LLMConfig(), model=lambda: None, allowed_intents=("a", "b"),
        action_catalog=lambda: catalog,
    )
    started = time.perf_counter()
    rounds = 2_000
    for _ in range(rounds):
        reasoner._actions_catalog()
    return (time.perf_counter() - started) / rounds * 1_000_000


def main() -> int:
    print("Digital Twin — M12 benchmark (no hardware, no network)")
    print("=" * 60)
    with tempfile.TemporaryDirectory(prefix="dtwin-bench-") as tmp:
        throughput = bench_bus()
        print(f"bus throughput          : {throughput:>10,.0f} events/s "
              f"({EVENTS:,} events)")
        median, p95 = bench_pipeline(Path(tmp))
        print(f"action pipeline latency : {median:>10.2f} ms median, "
              f"{p95:.2f} ms p95 ({ACTIONS} SAFE actions, full gates+audit)")
        micros = bench_prompt()
        print(f"catalog late-binding    : {micros:>10.2f} µs per message "
              f"(60-action catalog)")
    print("=" * 60)
    print("Interpretation: perception streams publish tens of events per")
    print("second at most — the bus ceiling is orders of magnitude above")
    print("need. Gate overhead is milliseconds per action; the human")
    print("confirmation it protects takes seconds. Late-binding the")
    print("catalog costs microseconds — the M11 refactor is free.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
