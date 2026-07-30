"""The event bus: asynchronous, thread-safe publish/subscribe backbone.

Design goals, in priority order:

1. **Isolation** — a crashing or misbehaving subscriber must never take
   down the bus or other subscribers. Every callback runs inside its own
   ``try/except`` and errors are counted and logged.
2. **Non-blocking producers** — perception modules publish from their own
   capture/inference threads; ``publish`` never blocks. Delivery happens on
   a single dedicated dispatcher thread, which also gives subscribers a
   simple guarantee: callbacks for one bus are never invoked concurrently.
3. **Bounded memory** — the queue is bounded; under overload the *oldest*
   events are dropped (perception data ages badly) and drops are counted.
4. **Observability** — delivery statistics and slow-handler warnings make
   misbehaving consumers visible instead of silently degrading the system.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass

from digital_twin.core.events import Event, topic_matches

logger = logging.getLogger(__name__)

#: Sentinel pushed onto the queue to wake the dispatcher for shutdown.
_SHUTDOWN = object()


@dataclass
class BusStats:
    """Cumulative event-bus counters (snapshot)."""

    published: int = 0
    delivered: int = 0
    dropped: int = 0
    handler_errors: int = 0


class Subscription:
    """Handle returned by :meth:`EventBus.subscribe`; cancel to stop delivery."""

    __slots__ = ("pattern", "callback", "name", "_bus", "_last_slow_warn")

    def __init__(self, pattern: str, callback, name: str, bus: "EventBus"):
        self.pattern = pattern
        self.callback = callback
        self.name = name
        self._bus = bus
        self._last_slow_warn = 0.0

    def cancel(self) -> None:
        """Stop receiving events on this subscription (idempotent)."""
        self._bus._remove(self)

    def __repr__(self) -> str:
        return f"Subscription({self.name!r} -> {self.pattern!r})"


class EventBus:
    """Topic-based publish/subscribe with a dedicated dispatcher thread."""

    def __init__(
        self,
        max_queue_size: int = 1024,
        slow_handler_warn_ms: float = 50.0,
    ):
        self._queue: queue.Queue = queue.Queue(maxsize=max(1, max_queue_size))
        self._subscriptions: tuple[Subscription, ...] = ()
        self._subs_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False
        self._slow_ms = slow_handler_warn_ms
        self._stats = BusStats()
        self._stats_lock = threading.Lock()
        # Outstanding = published-but-not-yet-dispatched; lets flush() wait
        # for cascades (handlers that publish follow-up events) to settle.
        self._outstanding = 0
        self._idle = threading.Condition()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Start the dispatcher thread (idempotent)."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._dispatch_loop, name="event-bus", daemon=True
        )
        self._thread.start()
        logger.info("Event bus started (queue=%d)", self._queue.maxsize)

    def stop(self, timeout: float = 5.0) -> None:
        """Drain pending events, then stop the dispatcher (idempotent)."""
        if not self._running:
            return
        self.flush(timeout=timeout)
        self._running = False
        self._queue.put(_SHUTDOWN)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        logger.info("Event bus stopped (%s)", self.stats)

    # ------------------------------------------------------------------
    # Subscription management
    # ------------------------------------------------------------------
    def subscribe(self, pattern: str, callback, name: str | None = None) -> Subscription:
        """Register ``callback`` for every event whose topic matches ``pattern``.

        :param pattern: exact topic, ``"prefix.*"`` subtree, or ``"*"``.
        :param callback: ``callable(event: Event) -> None``; runs on the
            dispatcher thread, so it must be quick or hand off its work.
        :param name: label used in logs; defaults to the callable's name.
        """
        if name is None:
            name = getattr(callback, "__qualname__", repr(callback))
        subscription = Subscription(pattern, callback, name, self)
        with self._subs_lock:
            self._subscriptions = self._subscriptions + (subscription,)
        logger.debug("Subscribed %s to %r", name, pattern)
        return subscription

    def _remove(self, subscription: Subscription) -> None:
        with self._subs_lock:
            self._subscriptions = tuple(
                s for s in self._subscriptions if s is not subscription
            )

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------
    def publish(self, event: Event) -> bool:
        """Enqueue ``event`` for delivery; never blocks.

        Returns ``False`` if the event was rejected (bus stopped) or the
        queue overflowed in a way that lost it — callers generally ignore
        the result; the statistics record every drop.
        """
        if not self._running:
            with self._stats_lock:
                self._stats.dropped += 1
            logger.debug("Bus not running; dropped %s", event)
            return False

        with self._idle:
            self._outstanding += 1
        with self._stats_lock:
            self._stats.published += 1

        for _ in range(4):  # bounded retries under producer contention
            try:
                self._queue.put_nowait(event)
                return True
            except queue.Full:
                self._drop_oldest()
        # Could not enqueue even after shedding; count this event as lost.
        self._settle_one()
        with self._stats_lock:
            self._stats.dropped += 1
        logger.warning("Event bus saturated; dropped %s", event)
        return False

    def _drop_oldest(self) -> None:
        """Shed the oldest queued event to make room (recency wins)."""
        try:
            dropped = self._queue.get_nowait()
        except queue.Empty:
            return
        if dropped is _SHUTDOWN:  # never swallow the shutdown signal
            self._queue.put(dropped)
            return
        self._settle_one()
        with self._stats_lock:
            self._stats.dropped += 1

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    @property
    def stats(self) -> BusStats:
        """Snapshot of the cumulative counters."""
        with self._stats_lock:
            return BusStats(**vars(self._stats))

    def flush(self, timeout: float = 5.0) -> bool:
        """Block until every published event (and its cascade) is dispatched.

        Primarily for tests and orderly shutdown. Returns ``False`` on
        timeout.
        """
        deadline = time.monotonic() + timeout
        with self._idle:
            while self._outstanding > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._idle.wait(remaining)
        return True

    # ------------------------------------------------------------------
    # Dispatcher
    # ------------------------------------------------------------------
    def _dispatch_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is _SHUTDOWN:
                break
            try:
                self._deliver(item)
            finally:
                self._settle_one()

    def _deliver(self, event: Event) -> None:
        with self._subs_lock:
            subscriptions = self._subscriptions
        for subscription in subscriptions:
            if not topic_matches(subscription.pattern, event.topic):
                continue
            started = time.perf_counter()
            try:
                subscription.callback(event)
                with self._stats_lock:
                    self._stats.delivered += 1
            except Exception:
                with self._stats_lock:
                    self._stats.handler_errors += 1
                logger.exception(
                    "Subscriber %s failed on %s", subscription.name, event
                )
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            if elapsed_ms > self._slow_ms:
                now = time.monotonic()
                if now - subscription._last_slow_warn > 10.0:
                    subscription._last_slow_warn = now
                    logger.warning(
                        "Slow subscriber %s took %.1f ms on %r "
                        "(delaying all other deliveries)",
                        subscription.name,
                        elapsed_ms,
                        event.topic,
                    )

    def _settle_one(self) -> None:
        with self._idle:
            self._outstanding -= 1
            if self._outstanding <= 0:
                self._idle.notify_all()
