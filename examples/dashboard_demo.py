#!/usr/bin/env python3
"""M12 demo — the dashboard, driven exactly as a browser would.

No browser needed: this script starts a real kernel slice (bus, module
registry, dispatcher with the **web** confirmation provider, dashboard on
an ephemeral loopback port) and then plays the human, talking to the same
HTTP API the page uses:

1. reads ``/api/status`` — live module states and bus counters;
2. sends chat through ``POST /api/chat`` (with the page's CSRF token) and
   watches it appear on ``perception.chat``;
3. triggers a DANGEROUS action configured ``allow`` — the clamp forces a
   confirmation, which shows up on ``/api/confirmations`` and is
   **approved over HTTP**: the stdin conflict between console chat and
   console confirmations is gone;
4. shows the token guard: the same POST without the token is rejected.

Run from the repository root::

    python examples/dashboard_demo.py
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import mkdtemp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from digital_twin.automation.dispatcher import ActionDispatcher  # noqa: E402
from digital_twin.automation.registry import ActionRegistry, ActionSpec  # noqa: E402
from digital_twin.configuration.settings import (  # noqa: E402
    AutomationConfig,
    DashboardConfig,
    SecurityConfig,
)
from digital_twin.core.bus import EventBus  # noqa: E402
from digital_twin.core.events import Event, Topics  # noqa: E402
from digital_twin.core.registry import ModuleRegistry  # noqa: E402
from digital_twin.dashboard.module import DashboardModule  # noqa: E402
from digital_twin.dashboard.web_confirmation import WebConfirmation  # noqa: E402
from digital_twin.security.audit import AuditLog  # noqa: E402
from digital_twin.security.permissions import (  # noqa: E402
    PermissionPolicy,
    RiskLevel,
)


def _get(port: int, path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}",
                                timeout=5) as response:
        return json.loads(response.read())


def _post(port: int, path: str, body: dict, token: str | None) -> tuple:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    if token:
        request.add_header("X-Dashboard-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def main() -> int:
    work = Path(mkdtemp(prefix="dtwin-m12-demo-"))
    bus = EventBus()
    bus.start()
    registry = ModuleRegistry(bus)

    confirmations = WebConfirmation()  # security.confirmation: web
    actions = ActionRegistry()
    actions.register(ActionSpec(
        name="archive_everything", description="pretend-destructive demo",
        risk=RiskLevel.DANGEROUS,
        handler=lambda params: "archived (pretend)",
    ))
    security = SecurityConfig(permissions={"archive_everything": "allow"})
    dispatcher = ActionDispatcher(
        config=AutomationConfig(action_timeout_s=15.0),
        registry=actions,
        policy=PermissionPolicy(security.risk_defaults, security.permissions),
        confirmation=confirmations,
        audit=AuditLog(work / "audit.jsonl"),
    )
    dispatcher.start(bus)

    dashboard = DashboardModule(
        DashboardConfig(enabled=True, port=0),  # ephemeral port for the demo
        registry, confirmations=confirmations, dispatcher=dispatcher,
    )
    registry.register(dashboard)
    dashboard.start(bus)
    port, token = dashboard.port, dashboard.token

    print("=" * 68)
    print(f"Dashboard live at http://127.0.0.1:{port}  (ephemeral demo port)")
    print("=" * 68)

    status = _get(port, "/api/status")
    print(f"\n/api/status → version {status['version']}, modules: " + ", ".join(
        f"{module['name']}={module['state']}" for module in status["modules"]))

    print("\nChat through the page's API:")
    received = []
    bus.subscribe(Topics.CHAT, received.append)
    code, _ = _post(port, "/api/chat", {"text": "hello from the browser"},
                    token)
    time.sleep(0.3)
    print(f"    POST /api/chat [{code}] → perception.chat: "
          f"{received[0].payload['text']!r} (user={received[0].payload['user']})")

    print("\nDANGEROUS action, configured 'allow', approved from the web:")
    results = []
    bus.subscribe(Topics.ACTION_RESULT, results.append)
    bus.publish(Event(Topics.ACTION_EXECUTE, "demo",
                      {"action": "archive_everything", "params": {}}))

    def approve_from_the_web():
        deadline = time.time() + 5
        while time.time() < deadline:
            pending = _get(port, "/api/confirmations")["pending"]
            if pending:
                entry = pending[0]
                print(f"    /api/confirmations shows: {entry['action']} "
                      f"({entry['expires_in_s']}s left)")
                bad_code, _ = _post(
                    port, f"/api/confirmations/{entry['id']}",
                    {"approve": True}, token=None)
                print(f"    without token → HTTP {bad_code} (CSRF guard)")
                good_code, body = _post(
                    port, f"/api/confirmations/{entry['id']}",
                    {"approve": True}, token=token)
                print(f"    with token    → HTTP {good_code}, "
                      f"resolved={body['resolved']}")
                return
            time.sleep(0.05)

    approver = threading.Thread(target=approve_from_the_web)
    approver.start()
    deadline = time.time() + 8
    while not results and time.time() < deadline:
        time.sleep(0.05)
    approver.join()
    payload = results[0].payload
    print(f"    action result: {payload['status']} — {payload['detail']}")
    print("    (the clamp fired on 'allow'; approval travelled over HTTP —")
    print("     no stdin was involved anywhere)")

    dashboard.stop()
    dispatcher.stop()
    bus.stop()
    print(f"\nDemo complete. (Throwaway workspace at {work} — safe to delete.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
