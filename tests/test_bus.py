"""Tests for the event bus: delivery, isolation, backpressure, cascades."""

from __future__ import annotations

import threading

import pytest

from digital_twin.core.bus import EventBus
from digital_twin.core.events import Event


@pytest.fixture()
def bus():
    bus = EventBus(max_queue_size=64)
    bus.start()
    yield bus
    bus.stop()


def _collector():
    received: list[Event] = []
    return received, received.append


def test_delivery_to_matching_subscribers(bus):
    gestures, on_gesture = _collector()
    everything, on_any = _collector()
    other, on_other = _collector()
    bus.subscribe("perception.gesture", on_gesture)
    bus.subscribe("*", on_any)
    bus.subscribe("intent.detected", on_other)

    bus.publish(Event("perception.gesture", "test", {"gesture": "ok"}))
    assert bus.flush(timeout=2.0)

    assert len(gestures) == 1 and gestures[0].payload["gesture"] == "ok"
    assert len(everything) == 1
    assert other == []


def test_unsubscribe_stops_delivery(bus):
    received, callback = _collector()
    subscription = bus.subscribe("t", callback)
    bus.publish(Event("t", "test"))
    bus.flush(timeout=2.0)
    subscription.cancel()
    bus.publish(Event("t", "test"))
    bus.flush(timeout=2.0)
    assert len(received) == 1


def test_failing_subscriber_is_isolated(bus):
    received, good = _collector()

    def bad(event: Event) -> None:
        raise RuntimeError("boom")

    bus.subscribe("t", bad, name="bad")
    bus.subscribe("t", good, name="good")
    for _ in range(3):
        bus.publish(Event("t", "test"))
    assert bus.flush(timeout=2.0)

    assert len(received) == 3
    assert bus.stats.handler_errors == 3


def test_handlers_may_publish_followup_events_and_flush_waits(bus):
    intents, on_intent = _collector()

    def map_to_intent(event: Event) -> None:
        bus.publish(Event("intent.detected", "mapper", {"intent": "next"}))

    bus.subscribe("perception.gesture", map_to_intent)
    bus.subscribe("intent.detected", on_intent)

    bus.publish(Event("perception.gesture", "test", {"gesture": "thumbs_up"}))
    assert bus.flush(timeout=2.0)
    assert len(intents) == 1


def test_overflow_drops_oldest_and_counts():
    bus = EventBus(max_queue_size=4)  # not started: nothing drains the queue
    bus._running = True  # simulate a stalled dispatcher
    for i in range(10):
        bus.publish(Event("t", "test", {"i": i}))
    stats = bus.stats
    assert stats.published == 10
    assert stats.dropped == 6
    # The queue holds the *newest* four events.
    kept = [bus._queue.get_nowait().payload["i"] for _ in range(4)]
    assert kept == [6, 7, 8, 9]
    bus._running = False


def test_publish_after_stop_is_dropped():
    bus = EventBus()
    bus.start()
    bus.stop()
    assert bus.publish(Event("t", "test")) is False
    assert bus.stats.dropped == 1


def test_thread_safety_under_concurrent_publishers():
    # Own bus with headroom: this test verifies delivery correctness under
    # concurrency, not backpressure (covered by the overflow test).
    bus = EventBus(max_queue_size=4096)
    bus.start()
    try:
        received, callback = _collector()
        bus.subscribe("load.*", callback)

        def worker(n: int) -> None:
            for i in range(100):
                bus.publish(Event(f"load.{n}", "test", {"i": i}))

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert bus.flush(timeout=5.0)
        assert len(received) == 400
        assert bus.stats.dropped == 0
    finally:
        bus.stop()
