#!/usr/bin/env python3
"""M17 demo — the dashboard is now the whole cockpit.

No browser, no camera: this drives the dashboard's HTTP API exactly as
the page does and prints what each new panel serves.

1. **Plugin panel** — `/api/plugins` reports every loaded plugin, its
   health, whether it is sandboxed, and its actions (a disabled plugin
   shows its error).
2. **Settings panel** — `/api/settings` returns the active config with
   every non-default field flagged; it is read-only by design (the
   config is frozen at startup, a safety property).
3. **Live view + MJPEG** — a stand-in perception producer pushes JPEG
   frames into the FrameHub; `/api/frames` lists sources and
   `/api/frames/<name>` streams them as multipart/x-mixed-replace, the
   gesture debug view finally living in the browser instead of an
   OpenCV window.
4. **Push-driven page** — the served page uses an EventSource against
   `/api/stream`, so it updates the instant something happens.

Run from the repository root::

    python examples/ui_completion_demo.py
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from digital_twin.configuration.settings import (  # noqa: E402
    AppConfig,
    DashboardConfig,
    KnowledgeConfig,
)
from digital_twin.core.bus import EventBus  # noqa: E402
from digital_twin.core.registry import ModuleRegistry  # noqa: E402
from digital_twin.dashboard.frames import FrameHub  # noqa: E402
from digital_twin.dashboard.module import DashboardModule  # noqa: E402
from digital_twin.plugins.loader import LoadedPlugin  # noqa: E402


def _get(port: int, path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}",
                                timeout=5) as response:
        return json.loads(response.read())


def main() -> int:
    bus = EventBus()
    bus.start()
    registry = ModuleRegistry(bus)
    hub = FrameHub()
    reports = [
        LoadedPlugin(name="calendar", version="1.0.0",
                     actions=["calendar.upcoming_events",
                              "calendar.create_event"]),
        LoadedPlugin(name="untrusted", version="0.3.0",
                     actions=["untrusted.fetch"], sandboxed=True),
        LoadedPlugin(name="typo", version="?",
                     error="action 'wsve' is not declared in plugin.yaml"),
    ]
    config = AppConfig(
        dashboard=DashboardConfig(enabled=True, port=0),
        knowledge=KnowledgeConfig(embedder="semantic", top_k=5),
    )
    dashboard = DashboardModule(
        config.dashboard, registry,
        plugin_reports=reports, app_config=config, frame_hub=hub,
    )
    registry.register(dashboard)
    dashboard.start(bus)
    port = dashboard.port

    print("=" * 68)
    print(f"Dashboard live at http://127.0.0.1:{port}")
    print("=" * 68)

    print("\nAct 1 — /api/plugins")
    for plugin in _get(port, "/api/plugins")["plugins"]:
        if plugin["ok"]:
            tag = " [sandboxed]" if plugin["sandboxed"] else ""
            print(f"    ● {plugin['name']} {plugin['version']}{tag} — "
                  f"{len(plugin['actions'])} action(s)")
        else:
            print(f"    ✕ {plugin['name']} — {plugin['error']}")

    print("\nAct 2 — /api/settings (non-default fields only)")
    sections = _get(port, "/api/settings")["sections"]
    for section, fields in sections.items():
        for key, info in fields.items():
            if isinstance(info, dict) and info.get("default") is False:
                print(f"    {section}.{key} = {json.dumps(info['value'])}")

    print("\nAct 3 — /api/frames + MJPEG stream")
    print(f"    sources before any push: {_get(port, '/api/frames')['sources']}")
    sink = hub.sink("gesture")
    sink(b"\xff\xd8DEMO-FRAME-1\xff\xd9")
    print(f"    sources after a push:    {_get(port, '/api/frames')['sources']}")

    conn = socket.create_connection(("127.0.0.1", port), timeout=5)
    conn.sendall(b"GET /api/frames/gesture HTTP/1.1\r\nHost: x\r\n\r\n")

    def pump():
        for index in range(2, 5):
            time.sleep(0.2)
            sink(f"\xff\xd8DEMO-FRAME-{index}\xff\xd9".encode("latin-1"))

    threading.Thread(target=pump).start()
    seen = b""
    conn.settimeout(0.5)
    deadline = time.time() + 4
    while time.time() < deadline and b"FRAME-4" not in seen:
        try:
            chunk = conn.recv(256)
            if not chunk:
                break
            seen += chunk
        except socket.timeout:
            pass
    conn.close()
    boundaries = seen.count(b"--dtframe")
    print(f"    streamed {boundaries} multipart frame(s) over one "
          f"connection (latest-only, dropped-frame safe)")

    print("\nAct 4 — the page itself")
    page = urllib.request.urlopen(f"http://127.0.0.1:{port}/",
                                  timeout=5).read().decode()
    print(f"    EventSource(/api/stream) in page: {'EventSource' in page}")
    print(f"    panels present: " + ", ".join(
        panel for panel in ("live", "plugins", "settings", "frames",
                            "memory", "knowledge")
        if f'id=\"{panel}\"' in page))

    dashboard.stop()
    bus.stop()
    print("\nDemo complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
