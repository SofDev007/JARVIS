#!/usr/bin/env python3
"""M13 demo — the assistant learns your documents and reads your world.

Entirely offline, nothing external touched: a fake IMAP server-in-a-class
and generated .ics/task files stand in, but the knowledge store, the
embedder, the plugin loader, the secrets manager, the reasoner's RAG
section and the dispatcher gates are all the real thing.

1. **Ingest → recall.** Two documents enter the index through the gated
   ``ingest_text`` action; a question ranks the right chunk on top; the
   reasoner's actual prompt section is printed — the RAG loop, visible.
2. **Idempotence + forgetting.** Re-ingesting identical content is a
   no-op; ``forget_document`` removes derived data only.
3. **Connectors.** The three shipped example plugins (calendar/.ics,
   tasks/JSON, email/IMAP) load through the manifest contract and run
   through the same gates — with the email password resolved **by name**
   from the secrets manager and provably absent from every result.

Run from the repository root::

    python examples/knowledge_connectors_demo.py
"""

from __future__ import annotations

import sys
import time
import types
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import mkdtemp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from digital_twin.automation.dispatcher import ActionDispatcher  # noqa: E402
from digital_twin.automation.registry import ActionRegistry  # noqa: E402
from digital_twin.configuration.settings import (  # noqa: E402
    AutomationConfig,
    FilesConfig,
    KnowledgeConfig,
    LLMConfig,
    PluginsConfig,
    SecurityConfig,
)
from digital_twin.core.bus import EventBus  # noqa: E402
from digital_twin.core.events import Event, Topics  # noqa: E402
from digital_twin.core.registry import ModuleRegistry  # noqa: E402
from digital_twin.knowledge.actions import register_knowledge_actions  # noqa: E402
from digital_twin.knowledge.embedding import HashingEmbedder  # noqa: E402
from digital_twin.knowledge.store import KnowledgeStore  # noqa: E402
from digital_twin.plugins.loader import load_plugins  # noqa: E402
from digital_twin.reasoning.chat_reasoner import ChatReasoner  # noqa: E402
from digital_twin.security.audit import AuditLog  # noqa: E402
from digital_twin.security.confirmation import ConfirmationProvider  # noqa: E402
from digital_twin.security.permissions import PermissionPolicy  # noqa: E402
from digital_twin.security.secrets import EncryptedFileSecretStore  # noqa: E402

POLICY = """The PTO policy grants engineers 25 days of paid leave per year.

Unused PTO days carry over for one quarter and then expire. Leave
requests above five consecutive days need manager approval."""

RUNBOOK = """To restart the ingestion service, run systemctl restart
ingest and check the health endpoint afterwards.

Rollbacks are performed by redeploying the previous tag from CI."""


class QuietApprove(ConfirmationProvider):
    name = "quiet"

    def request(self, action, params, timeout_s):
        print(f"    [confirm?] {action} -> approved")
        return True


class FakeIMAP:
    def __init__(self, host):
        pass

    def login(self, user, password):
        self.password_seen = password

    def select(self, mailbox, readonly=False):
        pass

    def search(self, charset, criterion):
        return "OK", [b"11 12"]

    def fetch(self, message_id, parts):
        return "OK", [(b"h", b"From: sangeetha@moveinsync.com\r\n"
                             b"Subject: v1.0 sign-off notes\r\n\r\n")]

    def logout(self):
        pass


def main() -> int:
    work = Path(mkdtemp(prefix="dtwin-m13-demo-"))
    bus = EventBus()
    bus.start()

    knowledge_config = KnowledgeConfig(min_score=0.05)
    store = KnowledgeStore(work / "knowledge.db", HashingEmbedder(256),
                           chunk_chars=400, chunk_overlap=60)
    actions = ActionRegistry()
    register_knowledge_actions(actions, store, knowledge_config,
                               FilesConfig(allowed_roots=[str(work)]))

    # Connector plugins + secrets
    secrets = EncryptedFileSecretStore(work / "s.enc", work / "k.key")
    secrets.set("email_password", "hunter2-IMAP-SECRET")
    sys.modules["imaplib"] = types.ModuleType("imaplib")
    sys.modules["imaplib"].IMAP4_SSL = FakeIMAP

    connectors = Path(__file__).resolve().parent.parent / "plugins" / "examples"
    reports = load_plugins(PluginsConfig(paths=[str(connectors)]),
                           actions, ModuleRegistry(bus), secrets=secrets)

    # Give the calendar and tasks connectors something to read
    calendar_dir = work / "data" / "calendars"
    calendar_dir.mkdir(parents=True)
    soon = datetime.now() + timedelta(days=1, hours=3)
    (calendar_dir / "work.ics").write_text(
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
        f"DTSTART:{soon:%Y%m%dT%H%M%S}\nSUMMARY:GovernanceHub v1.0 demo\n"
        "END:VEVENT\nEND:VCALENDAR\n")
    import os
    os.chdir(work)  # connectors use relative data/ paths from their config

    security = SecurityConfig()
    dispatcher = ActionDispatcher(
        config=AutomationConfig(action_timeout_s=10.0),
        registry=actions,
        policy=PermissionPolicy(security.risk_defaults, security.permissions),
        confirmation=QuietApprove(),
        audit=AuditLog(work / "audit.jsonl"),
    )
    results = []
    bus.subscribe(Topics.ACTION_RESULT, results.append)
    dispatcher.start(bus)

    def run(action, params):
        before = len(results)
        bus.publish(Event(Topics.ACTION_EXECUTE, "demo",
                          {"action": action, "params": params}))
        deadline = time.time() + 5
        while len(results) <= before and time.time() < deadline:
            time.sleep(0.01)
        payload = results[-1].payload
        print(f"    {action:26s} -> {payload['status']}: "
              f"{payload['detail']}")
        return payload

    print("=" * 68)
    print("Act 1 — ingest, rank, and the reasoner's RAG section")
    print("=" * 68)
    run("ingest_text", {"title": "hr-policy", "text": POLICY})
    run("ingest_text", {"title": "ops-runbook", "text": RUNBOOK})
    run("search_knowledge", {"query": "how many PTO days do engineers get"})

    reasoner = ChatReasoner(LLMConfig(), model=lambda: None,
                            allowed_intents=(),
                            knowledge=(store, knowledge_config))
    section = reasoner._knowledge_section("how many PTO days do I get?")
    print("\n    what the LLM prompt will contain for that question:")
    for line in section.strip().splitlines():
        print(f"      | {line}")

    print("\n" + "=" * 68)
    print("Act 2 — idempotence and forgetting")
    print("=" * 68)
    run("ingest_text", {"title": "hr-policy-copy", "text": POLICY})
    run("forget_document", {"doc_id": 2})
    run("list_knowledge", {})

    print("\n" + "=" * 68)
    print("Act 3 — connectors: three plugins under the manifest contract")
    print("=" * 68)
    for report in reports:
        print(f"    LOADED {report.name} {report.version} -> "
              f"{report.actions}")
    run("calendar.upcoming_events", {"days": 7})
    run("tasks.add_task", {"text": "prepare M13 walkthrough for Sangeetha"})
    run("tasks.list_tasks", {})
    payload = run("email.check_email", {})
    print(f"    password anywhere in result/audit: "
          f"{'hunter2' in str(payload) or any('hunter2' in str(entry) for entry in AuditLog(work / 'audit.jsonl').tail(30))}")

    dispatcher.stop()
    bus.stop()
    store.close()
    print(f"\nDemo complete. (Throwaway workspace at {work} — safe to delete.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
