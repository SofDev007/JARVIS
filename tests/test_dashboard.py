"""Tests for the dashboard: HTTP endpoints, the CSRF token guard, the
web confirmation provider (fail-closed), and a DANGEROUS action approved
from 'the browser' end to end."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from digital_twin.automation.dispatcher import ActionDispatcher
from digital_twin.automation.registry import ActionRegistry, ActionSpec
from digital_twin.configuration.settings import (
    AutomationConfig,
    DashboardConfig,
    SecurityConfig,
    _validate,
)
from digital_twin.core.bus import EventBus
from digital_twin.core.events import Event, Topics
from digital_twin.core.registry import ModuleRegistry
from digital_twin.dashboard.module import DashboardModule
from digital_twin.dashboard.web_confirmation import WebConfirmation
from digital_twin.security.audit import AuditLog
from digital_twin.security.permissions import PermissionPolicy, RiskLevel


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def _get(port: int, path: str):
    with urllib.request.urlopen(
            f"http://127.0.0.1:{port}{path}", timeout=5) as response:
        return response.status, response.read()


def _post(port: int, path: str, body: dict, token: str | None = None):
    data = json.dumps(body).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    if token:
        request.add_header("X-Dashboard-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------
def test_dashboard_config_validation():
    from dataclasses import replace
    from digital_twin.configuration.settings import AppConfig

    config = AppConfig()
    with pytest.raises(ValueError, match="port"):
        _validate(replace(config, dashboard=DashboardConfig(port=99999)))
    with pytest.raises(ValueError, match="loopback"):
        _validate(replace(config, dashboard=DashboardConfig(host="0.0.0.0")))
    with pytest.raises(ValueError, match="requires dashboard"):
        _validate(replace(
            config, security=SecurityConfig(confirmation="web")))
    # allowed when explicit
    _validate(replace(
        config,
        dashboard=DashboardConfig(host="0.0.0.0", allow_remote=True)))


# ---------------------------------------------------------------------------
# WebConfirmation provider (fail-closed)
# ---------------------------------------------------------------------------
def test_web_confirmation_approve_deny_timeout():
    provider = WebConfirmation()

    def answer(approve: bool):
        deadline = time.time() + 2
        while time.time() < deadline:
            pending = provider.pending()
            if pending:
                provider.resolve(pending[0]["id"], approve)
                return
            time.sleep(0.01)

    thread = threading.Thread(target=answer, args=(True,))
    thread.start()
    assert provider.request("notify", {"m": 1}, timeout_s=3.0) is True
    thread.join()

    thread = threading.Thread(target=answer, args=(False,))
    thread.start()
    assert provider.request("notify", {"m": 2}, timeout_s=3.0) is False
    thread.join()

    # Nobody answers → timeout → deny, and the entry is cleaned up.
    assert provider.request("notify", {"m": 3}, timeout_s=0.1) is False
    assert provider.pending() == []
    assert provider.resolve("c999", True) is False  # unknown id


# ---------------------------------------------------------------------------
# Dashboard module + server
# ---------------------------------------------------------------------------
@pytest.fixture()
def kernel():
    bus = EventBus()
    bus.start()
    registry = ModuleRegistry(bus)
    yield bus, registry
    bus.stop()


@pytest.fixture()
def dashboard(kernel):
    bus, registry = kernel
    module = DashboardModule(
        DashboardConfig(enabled=True, port=0),  # ephemeral port
        registry,
        confirmations=WebConfirmation(),
    )
    registry.register(module)
    module.start(bus)
    yield module
    if module.is_active:
        module.stop()


def test_page_serves_with_embedded_token(dashboard):
    status, body = _get(dashboard.port, "/")
    assert status == 200
    assert b"Digital" in body
    assert dashboard.token.encode() in body  # the page knows the token


def test_status_endpoint_reports_modules_and_bus(dashboard, kernel):
    status, body = _get(dashboard.port, "/api/status")
    payload = json.loads(body)
    assert status == 200
    assert any(module["name"] == "dashboard" and module["state"] == "running"
               for module in payload["modules"])
    assert set(payload["bus"]) == {"published", "delivered", "dropped",
                                   "handler_errors"}
    assert payload["version"]


def test_event_feed_captures_bus_traffic(dashboard, kernel):
    bus, _ = kernel
    bus.publish(Event(Topics.INTENT, "test", {"intent": "next_slide"}))
    deadline = time.time() + 2
    while time.time() < deadline:
        _, body = _get(dashboard.port, "/api/events")
        events = json.loads(body)["events"]
        if any(event["topic"] == Topics.INTENT for event in events):
            break
        time.sleep(0.02)
    assert any(event["payload"].get("intent") == "next_slide"
               for event in events)


def test_post_without_token_is_rejected(dashboard):
    status, payload = _post(dashboard.port, "/api/chat", {"text": "hi"})
    assert status == 403
    status, payload = _post(dashboard.port, "/api/chat", {"text": "hi"},
                            token="wrong-token")
    assert status == 403


def test_chat_endpoint_publishes_perception_chat(dashboard, kernel):
    bus, _ = kernel
    received = []
    bus.subscribe(Topics.CHAT, received.append)
    status, payload = _post(dashboard.port, "/api/chat",
                            {"text": "hello twin"}, token=dashboard.token)
    assert status == 200 and payload["accepted"] is True
    deadline = time.time() + 2
    while not received and time.time() < deadline:
        time.sleep(0.01)
    assert received[0].payload["text"] == "hello twin"
    assert received[0].payload["user"] == "dashboard"


def test_dangerous_action_approved_from_the_web(dashboard, kernel, tmp_path):
    """The flagship: DANGEROUS action, clamp fires, approval arrives over
    HTTP, and the action runs — no stdin anywhere."""
    bus, _ = kernel
    provider = dashboard._confirmations
    actions = ActionRegistry()
    executed = []
    actions.register(ActionSpec(
        name="wipe_bench", description="pretend-destructive",
        risk=RiskLevel.DANGEROUS,
        handler=lambda params: executed.append(True) or "done",
    ))
    security = SecurityConfig(permissions={"wipe_bench": "allow"})  # clamped
    dispatcher = ActionDispatcher(
        config=AutomationConfig(action_timeout_s=10.0),
        registry=actions,
        policy=PermissionPolicy(security.risk_defaults, security.permissions),
        confirmation=provider,
        audit=AuditLog(tmp_path / "audit.jsonl"),
    )
    results = []
    bus.subscribe(Topics.ACTION_RESULT, results.append)
    dispatcher.start(bus)
    try:
        bus.publish(Event(Topics.ACTION_EXECUTE, "test",
                          {"action": "wipe_bench", "params": {}}))
        # Poll the API like the page does, approve over HTTP.
        pending = []
        deadline = time.time() + 3
        while not pending and time.time() < deadline:
            _, body = _get(dashboard.port, "/api/confirmations")
            pending = json.loads(body)["pending"]
            time.sleep(0.02)
        assert pending and pending[0]["action"] == "wipe_bench"
        status, payload = _post(
            dashboard.port, f"/api/confirmations/{pending[0]['id']}",
            {"approve": True}, token=dashboard.token)
        assert status == 200 and payload["resolved"] is True
        deadline = time.time() + 3
        while not results and time.time() < deadline:
            time.sleep(0.02)
    finally:
        dispatcher.stop()
    assert executed == [True]
    assert results[0].payload["status"] == "completed"


def test_stop_releases_the_port(kernel):
    bus, registry = kernel
    module = DashboardModule(DashboardConfig(enabled=True, port=0), registry)
    registry.register(module)
    module.start(bus)
    port = module.port
    module.stop()
    with pytest.raises(urllib.error.URLError):
        _get(port, "/api/status")


# ---------------------------------------------------------------------------
# M15 dashboard depth: memory + knowledge panels, SSE stream
# ---------------------------------------------------------------------------
def test_memory_and_knowledge_endpoints(kernel, tmp_path):
    from digital_twin.dashboard.module import DashboardModule
    from digital_twin.knowledge.embedding import HashingEmbedder
    from digital_twin.knowledge.store import KnowledgeStore
    from digital_twin.memory.store import MemoryStore

    bus, registry = kernel
    memory = MemoryStore(tmp_path / "m.db")
    memory.add(kind="semantic", content="user likes penguins", source="test")
    knowledge = KnowledgeStore(tmp_path / "k.db", HashingEmbedder(64))
    knowledge.ingest(title="notes.md", text="Penguins are birds.",
                     source="test")

    module = DashboardModule(
        DashboardConfig(enabled=True, port=0), registry,
        memory_store=memory, knowledge_store=knowledge,
    )
    registry.register(module)
    module.start(bus)
    try:
        _, body = _get(module.port, "/api/memory")
        entries = json.loads(body)["entries"]
        assert any("penguins" in e["content"] for e in entries)
        _, body = _get(module.port, "/api/knowledge")
        payload = json.loads(body)
        assert any(d["title"] == "notes.md" for d in payload["documents"])
        assert payload["embedder"].startswith("hashing")
    finally:
        module.stop()


def test_sse_stream_pushes_new_events(dashboard, kernel):
    import http.client

    bus, _ = kernel
    conn = http.client.HTTPConnection("127.0.0.1", dashboard.port, timeout=12)
    conn.request("GET", "/api/stream")
    response = conn.getresponse()
    assert response.status == 200
    assert "text/event-stream" in response.getheader("Content-Type")
    # Publish repeatedly: the stream samples once a second, so a single
    # pre-stream publish can be missed under load. Republishing until it
    # surfaces makes the test independent of scheduling jitter.
    deadline = time.time() + 12
    seen = ""
    while time.time() < deadline and "stream_probe" not in seen:
        bus.publish(Event(Topics.INTENT, "test", {"intent": "stream_probe"}))
        seen += response.read(64).decode("utf-8", "replace")
    conn.close()
    assert "stream_probe" in seen
