"""The dashboard module: the kernel's window, rendered in a browser.

A normal :class:`BaseModule` — start/stop/pause through the registry like
everything else — that (a) subscribes to the interesting topics and keeps
a bounded ring buffer of recent events, (b) aggregates module statuses
and bus statistics on demand, and (c) owns a
:class:`~digital_twin.dashboard.server.DashboardServer` for its lifetime.
Chat typed into the page publishes ``perception.chat`` — the same topic
the console chat module uses, so the reasoner cannot tell the difference.

The dashboard *displays*; it does not decide. Confirmations shown in the
page come from the ``web`` confirmation provider, and approving one there
is exactly the console 'y' — same gates, same audit, same clamp.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

from digital_twin import __version__
from digital_twin.configuration.settings import DashboardConfig
from digital_twin.core.bus import EventBus, Subscription
from digital_twin.core.events import Event, Topics
from digital_twin.core.module import BaseModule
from digital_twin.dashboard.server import DashboardServer
from digital_twin.dashboard.web_confirmation import WebConfirmation

logger = logging.getLogger(__name__)

_FEED_TOPICS = (
    Topics.GESTURE,
    Topics.CONTEXT,
    Topics.CHAT,
    Topics.CHAT_RESPONSE,
    Topics.VOICE,
    Topics.SCREEN,
    Topics.INTENT,
    Topics.ACTION,
    Topics.ACTION_RESULT,
    Topics.PLAN_PROGRESS,
    Topics.MODULE,
)


class DashboardModule(BaseModule):
    """Serve the live dashboard for this kernel."""

    name = "dashboard"
    topics = (Topics.CHAT,)  # chat typed into the page

    def __init__(
        self,
        config: DashboardConfig,
        module_registry,  # ModuleRegistry (kernel-composed)
        confirmations: WebConfirmation | None = None,
        dispatcher=None,  # optional, for its metrics
        memory_store=None,     # optional MemoryStore for the memory panel
        knowledge_store=None,  # optional KnowledgeStore for the knowledge panel
        plugin_reports=None,   # list[LoadedPlugin] for the plugin panel
        app_config=None,       # AppConfig for the read-only settings panel
        frame_hub=None,        # FrameHub for /api/frames MJPEG streaming
        device_registry=None,  # M18 Phase 3: DeviceRegistry for mTLS
        device_confirmation=None,  # M18 Phase 3: DeviceConfirmationProvider
        security_config=None,  # M18 Phase 3: SecurityConfig for devices_dir
    ):
        super().__init__()
        self._config = config
        self._registry = module_registry
        self._confirmations = confirmations
        self._dispatcher = dispatcher
        self._memory = memory_store
        self._knowledge = knowledge_store
        self._plugin_reports = plugin_reports or []
        self._app_config = app_config
        self._frame_hub = frame_hub
        self._device_registry = device_registry
        self._device_confirmation = device_confirmation
        self._security_config = security_config
        self._events: deque[dict[str, Any]] = deque(
            maxlen=config.recent_events)
        self._events_lock = threading.Lock()
        self._subscriptions: list[Subscription] = []
        self._server: DashboardServer | None = None

    # ------------------------------------------------------------------
    @property
    def port(self) -> int:
        """Bound port once running (supports ``port: 0`` in tests)."""
        if self._server is None:
            raise RuntimeError("dashboard is not running")
        return self._server.port

    @property
    def token(self) -> str:
        if self._server is None:
            raise RuntimeError("dashboard is not running")
        return self._server.token

    # ------------------------------------------------------------------
    def _on_start(self) -> None:
        for topic in _FEED_TOPICS:
            self._subscriptions.append(
                self._bus.subscribe(topic, self._record))
        data_sources: dict[str, Any] = {}
        if self._memory is not None:
            data_sources["memory"] = self._memory_view
        if self._knowledge is not None:
            data_sources["knowledge"] = self._knowledge_view
        data_sources["plugins"] = self._plugins_view
        if self._app_config is not None:
            data_sources["settings"] = self._settings_view

        # M18 Phase 3: Use device confirmation for DANGEROUS actions if available
        confirmations = self._confirmations
        if self._device_confirmation is not None:
            confirmations = self._device_confirmation

        self._server = DashboardServer(
            self._config.host,
            self._config.port,
            version=__version__,
            status_source=self._status,
            events_source=self._recent,
            chat_sink=self._chat,
            confirmations=confirmations,
            data_sources=data_sources,
            stream_source=self._since,
            frame_hub=self._frame_hub,
            device_registry=self._device_registry,
            device_confirmation=self._device_confirmation,
        )
        self._server.start()

    def _on_stop(self) -> None:
        for subscription in self._subscriptions:
            subscription.cancel()
        self._subscriptions.clear()
        if self._server is not None:
            self._server.stop()
            self._server = None

    def _on_pause(self) -> None:
        self._on_stop()

    def _on_resume(self) -> None:
        self._on_start()

    # ------------------------------------------------------------------
    def _record(self, event: Event) -> None:
        entry = {
            "topic": event.topic,
            "source": event.source,
            "timestamp": round(event.timestamp, 3),
            "payload": _jsonable(dict(event.payload)),
        }
        with self._events_lock:
            self._events.append(entry)

    def _recent(self) -> list[dict[str, Any]]:
        with self._events_lock:
            return list(self._events)[-100:]

    def _since(self, timestamp: float) -> list[dict[str, Any]]:
        """Events recorded after ``timestamp`` — the SSE stream source."""
        with self._events_lock:
            return [event for event in self._events
                    if event["timestamp"] > timestamp]

    def _memory_view(self) -> dict[str, Any]:
        store = getattr(self._memory, "store", self._memory)
        if store is None:
            return {"entries": [], "error": "memory not started"}
        try:
            recent = store.list(limit=25)
        except Exception:
            return {"entries": [], "error": "memory unavailable"}
        entries = [{
            "kind": record.kind,
            "content": str(record.content)[:200],
            "source": record.source,
        } for record in recent]
        return {"entries": entries}

    def _knowledge_view(self) -> dict[str, Any]:
        try:
            docs = self._knowledge.documents()
        except Exception:
            return {"documents": [], "error": "knowledge unavailable"}
        documents = [{
            "id": doc.doc_id,
            "title": doc.title,
            "chunks": doc.chunks,
            "source": doc.source,
        } for doc in docs]
        embedder = getattr(getattr(self._knowledge, "_embedder", None),
                           "name", "?")
        return {"documents": documents, "embedder": embedder}

    def _status(self) -> dict[str, Any]:
        stats = self._bus.stats if self._bus is not None else None
        modules = []
        for status in self._registry.statuses():
            modules.append({
                "name": status.name,
                "state": status.state.value,
                "detail": status.detail,
                "metrics": _jsonable(status.metrics),
            })
        payload: dict[str, Any] = {
            "version": __version__,
            "time": time.time(),
            "modules": modules,
            "bus": {
                "published": getattr(stats, "published", 0),
                "delivered": getattr(stats, "delivered", 0),
                "dropped": getattr(stats, "dropped", 0),
                "handler_errors": getattr(stats, "handler_errors", 0),
            },
        }
        return payload

    def _chat(self, text: str) -> None:
        if self._bus is None:
            return
        self._bus.publish(Event(
            topic=Topics.CHAT,
            source=self.name,
            payload={"text": text, "user": "dashboard"},
        ))

    def _plugins_view(self) -> dict[str, Any]:
        plugins = [{
            "name": report.name,
            "version": report.version,
            "ok": report.ok,
            "sandboxed": bool(getattr(report, "sandboxed", False)),
            "actions": list(report.actions),
            "modules": list(report.modules),
            "error": report.error,
        } for report in self._plugin_reports]
        return {"plugins": plugins}

    def _settings_view(self) -> dict[str, Any]:
        """The active configuration, read-only, with non-default fields
        marked. Editing is deliberately absent: the config is frozen at
        startup (a safety property — modules never see it change under
        them); edit the YAML and restart to apply."""
        import dataclasses

        from digital_twin.configuration.settings import AppConfig

        active = dataclasses.asdict(self._app_config)
        defaults = dataclasses.asdict(AppConfig())
        sections = {}
        for section, values in active.items():
            if not isinstance(values, dict):
                sections[section] = {"value": _jsonable(values),
                                     "default": values == defaults.get(section)}
                continue
            fields = {}
            for key, value in values.items():
                fields[key] = {
                    "value": _jsonable(value),
                    "default": value == defaults.get(section, {}).get(key),
                }
            sections[section] = fields
        return {"sections": sections,
                "note": "read-only: edit the YAML and restart to apply"}

    def _metrics(self) -> dict[str, Any]:
        with self._events_lock:
            buffered = len(self._events)
        return {"events_buffered": buffered,
                "port": self._server.port if self._server else None}


def _jsonable(value: Any, depth: int = 0) -> Any:
    """Best-effort conversion of payloads into JSON-safe structures."""
    if depth > 4:
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v, depth + 1) for v in value]
    return str(value)
