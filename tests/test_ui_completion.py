"""Tests for M17 UI completion: the FrameHub + MJPEG streaming, the
plugin and settings panels, the push-driven page, and the headless
gesture debug view sink."""

from __future__ import annotations

import json
import threading
import time
import urllib.request

import pytest

from digital_twin.configuration.settings import AppConfig, DashboardConfig
from digital_twin.core.bus import EventBus
from digital_twin.core.registry import ModuleRegistry
from digital_twin.dashboard.frames import FrameHub
from digital_twin.dashboard.module import DashboardModule
from digital_twin.plugins.loader import LoadedPlugin

FAKE_JPEG_1 = b"\xff\xd8FRAME-ONE\xff\xd9"
FAKE_JPEG_2 = b"\xff\xd8FRAME-TWO\xff\xd9"


def _get(port: int, path: str):
    with urllib.request.urlopen(
            f"http://127.0.0.1:{port}{path}", timeout=5) as response:
        return response.status, response.read()


# ---------------------------------------------------------------------------
# FrameHub semantics
# ---------------------------------------------------------------------------
def test_frame_hub_latest_only_and_wait():
    hub = FrameHub()
    assert hub.sources() == ()
    assert hub.wait_next("gesture", 0, timeout_s=0.05) is None  # timeout

    sink = hub.sink("gesture")
    sink(FAKE_JPEG_1)
    sink(FAKE_JPEG_2)  # overwrites: latest-only, no queue growth
    assert hub.sources() == ("gesture",)
    assert hub.latest("gesture") == FAKE_JPEG_2

    jpeg, seq = hub.wait_next("gesture", 0, timeout_s=1.0)
    assert jpeg == FAKE_JPEG_2 and seq == 2
    assert hub.wait_next("gesture", seq, timeout_s=0.05) is None  # no newer


def test_frame_hub_wait_unblocks_on_push():
    hub = FrameHub()
    results = []

    def waiter():
        results.append(hub.wait_next("cam", 0, timeout_s=3.0))

    thread = threading.Thread(target=waiter)
    thread.start()
    time.sleep(0.05)
    hub.push("cam", FAKE_JPEG_1)
    thread.join(timeout=3.0)
    assert results and results[0][0] == FAKE_JPEG_1


def test_frame_hub_ignores_empty_frames():
    hub = FrameHub()
    hub.push("cam", b"")
    assert hub.sources() == ()


# ---------------------------------------------------------------------------
# Dashboard: panels + MJPEG endpoint
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
    hub = FrameHub()
    reports = [
        LoadedPlugin(name="greeter", version="1.0.0",
                     actions=["greeter.wave"], sandboxed=False),
        LoadedPlugin(name="broken", version="?",
                     error="entry file does not define register(api)"),
        LoadedPlugin(name="echoer", version="2.0.0",
                     actions=["echoer.do"], sandboxed=True),
    ]
    module = DashboardModule(
        DashboardConfig(enabled=True, port=0), registry,
        plugin_reports=reports,
        app_config=AppConfig(dashboard=DashboardConfig(enabled=True,
                                                       port=9999)),
        frame_hub=hub,
    )
    registry.register(module)
    module.start(bus)
    module._hub = hub  # test handle
    yield module
    if module.is_active:
        module.stop()


def test_plugins_endpoint(dashboard):
    status, body = _get(dashboard.port, "/api/plugins")
    plugins = json.loads(body)["plugins"]
    assert status == 200 and len(plugins) == 3
    by_name = {p["name"]: p for p in plugins}
    assert by_name["greeter"]["ok"] and not by_name["greeter"]["sandboxed"]
    assert by_name["echoer"]["sandboxed"] is True
    assert not by_name["broken"]["ok"]
    assert "register(api)" in by_name["broken"]["error"]


def test_settings_endpoint_marks_non_defaults(dashboard):
    status, body = _get(dashboard.port, "/api/settings")
    payload = json.loads(body)
    assert status == 200
    dash = payload["sections"]["dashboard"]
    assert dash["enabled"]["value"] is True and dash["enabled"]["default"] is False
    assert dash["port"]["value"] == 9999 and dash["port"]["default"] is False
    assert dash["host"]["default"] is True  # untouched field
    assert "read-only" in payload["note"]


def test_frames_listing_and_mjpeg_stream(dashboard):
    status, body = _get(dashboard.port, "/api/frames")
    assert json.loads(body)["sources"] == []  # nothing pushed yet

    dashboard._hub.push("gesture", FAKE_JPEG_1)
    status, body = _get(dashboard.port, "/api/frames")
    assert json.loads(body)["sources"] == ["gesture"]

    # A raw socket, not http.client: an http.client response mis-buffers a
    # never-ending multipart/x-mixed-replace body, so read from the wire.
    import socket

    sock = socket.create_connection(("127.0.0.1", dashboard.port), timeout=8)
    sock.sendall(b"GET /api/frames/gesture HTTP/1.1\r\nHost: x\r\n\r\n")

    def pump():
        for frame in (FAKE_JPEG_1, FAKE_JPEG_2):
            time.sleep(0.15)
            dashboard._hub.push("gesture", frame)

    pusher = threading.Thread(target=pump)
    pusher.start()
    seen = b""
    sock.settimeout(0.5)
    deadline = time.time() + 6
    while time.time() < deadline and b"FRAME-TWO" not in seen:
        try:
            chunk = sock.recv(256)
            if not chunk:
                break
            seen += chunk
        except socket.timeout:
            pass
    pusher.join()
    sock.close()
    assert b"multipart/x-mixed-replace" in seen
    assert b"--dtframe" in seen and b"Content-Type: image/jpeg" in seen
    assert b"FRAME-TWO" in seen  # newer frames keep flowing


def test_page_is_push_driven(dashboard):
    _, body = _get(dashboard.port, "/")
    page = body.decode()
    assert "EventSource" in page and "/api/stream" in page
    for panel in ("plugins", "settings", "live", "frames"):
        assert f'id="{panel}"' in page


# ---------------------------------------------------------------------------
# Headless debug view: annotate + sink without a display
# ---------------------------------------------------------------------------
def test_debug_view_headless_sinks_frames(monkeypatch):
    pytest.importorskip("cv2")
    import numpy as np

    from digital_twin.perception.gesture.debug_view import DebugView

    class _Frame:
        def __init__(self, seq):
            self.seq = seq
            self.image = np.zeros((24, 32, 3), dtype=np.uint8)

    class _Camera:
        def __init__(self):
            self._seq = 0

        def latest(self):
            self._seq += 1
            return _Frame(self._seq)

    class _Worker:
        fps = 12.0

        def latest(self):
            return None

    frames = []
    view = DebugView(fps_limit=60.0, frame_sink=frames.append, headless=True)
    monkeypatch.delenv("DISPLAY", raising=False)  # truly headless
    view.start(_Camera(), _Worker())
    deadline = time.time() + 3
    while not frames and time.time() < deadline:
        time.sleep(0.02)
    view.stop()
    assert frames, "headless debug view produced no frames"
    assert frames[0][:2] == b"\xff\xd8"  # JPEG magic
