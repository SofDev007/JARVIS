#!/usr/bin/env python3
"""M16 demo — the assistant reaches the outside world, carefully.

Four acts, no hardware, no network:

1. **Write-risk taxonomy live** — the connectors' read actions are
   SENSITIVE; the write actions (``send_email``, ``create_event``) are
   DANGEROUS, so the M3 clamp guarantees a human sees every outbound
   message *with its full content in the confirmation prompt* before it
   leaves the machine.
2. **Mis-send containment** — a recipient outside
   ``allowed_recipient_domains`` is refused at validation, before any
   confirmation even appears; then a real ``create_event`` runs through
   the gate and writes a proper ``.ics``.
3. **Wake word** — a scripted transcriber whispers "hey twin" and the
   always-on detector presses push-to-talk (one ``voice.control``
   event); ordinary speech does nothing, and the detector never
   publishes an utterance — privacy by structure.
4. **Installer** — ``digital-twin-setup`` builds a fresh
   ``DIGITAL_TWIN_HOME`` in a temp dir, idempotently.

Run from the repository root::

    python examples/write_wake_setup_demo.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from tempfile import mkdtemp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from digital_twin.automation.dispatcher import ActionDispatcher  # noqa: E402
from digital_twin.automation.registry import ActionRegistry  # noqa: E402
from digital_twin.configuration.settings import (  # noqa: E402
    AutomationConfig,
    PluginsConfig,
    SecurityConfig,
    VoiceConfig,
)
from digital_twin.core.bus import EventBus  # noqa: E402
from digital_twin.core.events import Event, Topics  # noqa: E402
from digital_twin.plugins.loader import load_plugins  # noqa: E402
from digital_twin.security.audit import AuditLog  # noqa: E402
from digital_twin.security.confirmation import ConfirmationProvider  # noqa: E402
from digital_twin.security.permissions import PermissionPolicy  # noqa: E402
from digital_twin.voice.audio import AudioSource  # noqa: E402
from digital_twin.voice.transcriber import (  # noqa: E402
    ScriptedTranscriber,
    TranscriptChunk,
)
from digital_twin.voice.wake import WakeWordModule  # noqa: E402

CONNECTORS = Path(__file__).resolve().parents[1] / "plugins" / "examples"


class NarratingConfirmation(ConfirmationProvider):
    name = "narrating"

    def request(self, action, params, timeout_s) -> bool:
        shown = {key: (value[:60] + "…" if isinstance(value, str)
                       and len(value) > 60 else value)
                 for key, value in params.items()}
        print(f"    [confirm?] {action} {shown} -> approved")
        return True


class _ScriptedAudio(AudioSource):
    name = "scripted-audio"

    def __init__(self, chunks: int = 2):
        self._left = chunks

    def open(self) -> None: ...
    def close(self) -> None: ...

    def read(self, timeout_s: float):
        if self._left > 0:
            self._left -= 1
            return b"\x00\x00"
        time.sleep(timeout_s)
        return None


def main() -> int:
    work = Path(mkdtemp(prefix="dtwin-m16-demo-"))

    print("=" * 68)
    print("Act 1 — write actions are DANGEROUS by declaration")
    print("=" * 68)
    actions = ActionRegistry()
    load_plugins(PluginsConfig(paths=[str(CONNECTORS)]), actions, None)
    for name in ("email.check_email", "email.send_email",
                 "calendar.upcoming_events", "calendar.create_event"):
        print(f"    {name:28s} -> {actions.get(name).risk.value}")

    print("\n" + "=" * 68)
    print("Act 2 — mis-send containment, then a confirmed calendar write")
    print("=" * 68)
    # A corp-configured copy: recipients outside corp.com are refused
    # at validation, before any confirmation could even appear.
    import shutil
    corp = work / "plugins" / "email_corp"
    corp.mkdir(parents=True)
    shutil.copyfile(CONNECTORS / "email_imap" / "plugin.py",
                    corp / "plugin.py")
    (corp / "plugin.yaml").write_text(
        "name: corpmail\nversion: '1'\nentry: plugin.py\n"
        "actions:\n  check_email: sensitive\n  send_email: dangerous\n"
        "config:\n  username: me@corp.com\n  smtp_host: smtp.corp.com\n"
        "  allowed_recipient_domains: [corp.com]\n")

    # A real DANGEROUS write through the full gate pipeline:
    bus = EventBus()
    bus.start()
    security = SecurityConfig(permissions={"democal.create_event": "allow"})
    dispatcher = ActionDispatcher(
        config=AutomationConfig(action_timeout_s=10.0),
        registry=actions,
        policy=PermissionPolicy(security.risk_defaults, security.permissions),
        confirmation=NarratingConfirmation(),
        audit=AuditLog(work / "audit.jsonl"),
    )
    # Point the calendar plugin's output at the demo workspace by loading
    # a copy configured for it.
    demo_cal = work / "plugins" / "calendar_ics"
    demo_cal.mkdir(parents=True)
    shutil.copyfile(CONNECTORS / "calendar_ics" / "plugin.py",
                    demo_cal / "plugin.py")
    (demo_cal / "plugin.yaml").write_text(
        "name: democal\nversion: '1'\nentry: plugin.py\n"
        "actions:\n  upcoming_events: sensitive\n  create_event: dangerous\n"
        f"config:\n  ics_dir: {work / 'cals'}\n")
    load_plugins(PluginsConfig(paths=[str(work / 'plugins')]),
                 dispatcher.registry, None)
    corp_send = dispatcher.registry.get("corpmail.send_email")
    try:
        corp_send.validate({"to": "stranger@evil.example",
                            "subject": "hi", "body": "…"})
    except ValueError as exc:
        print(f"    off-list recipient: {str(exc)[:66]}…")
    corp_send.validate({"to": "colleague@corp.com",
                        "subject": "hi", "body": "…"})
    print("    corp.com recipient: validation passes (would then be "
          "clamp-confirmed)")
    results = []
    bus.subscribe(Topics.ACTION_RESULT, results.append)
    dispatcher.start(bus)
    bus.publish(Event(Topics.ACTION_EXECUTE, "demo",
                      {"action": "democal.create_event",
                       "params": {"summary": "Sprint review with Sangeetha",
                                  "start": "20260722T150000",
                                  "duration_minutes": 30}}))
    deadline = time.time() + 5
    while not results and time.time() < deadline:
        time.sleep(0.02)
    dispatcher.stop()
    payload = results[0].payload
    print(f"    result: {payload['status']} — {payload['detail'][:70]}")
    ics = next((work / "cals").glob("*.ics"))
    print(f"    on disk: {ics.name} "
          f"({'SUMMARY:Sprint review' in ics.read_text()})"
          f" — the clamp confirmed despite configured 'allow'")

    print("\n" + "=" * 68)
    print("Act 3 — wake word: 'hey twin' presses push-to-talk")
    print("=" * 68)
    triggers, utterances = [], []
    bus.subscribe(Topics.VOICE_CONTROL, triggers.append)
    bus.subscribe(Topics.VOICE, utterances.append)
    wake = WakeWordModule(
        VoiceConfig(wake_word="hey twin"),
        audio_source=_ScriptedAudio(),
        transcriber=ScriptedTranscriber(
            [TranscriptChunk(text="hey twin", final=True)]),
    )
    wake.start(bus)
    deadline = time.time() + 3
    while not triggers and time.time() < deadline:
        time.sleep(0.02)
    wake.stop()
    print(f"    voice.control published: {triggers[0].payload}")
    print(f"    utterances published by the detector: {len(utterances)} "
          f"(it only triggers; it never transcribes for anyone)")
    bus.stop()

    print("\n" + "=" * 68)
    print("Act 4 — installer: digital-twin-setup")
    print("=" * 68)
    from digital_twin.setup_cli import main as setup_main
    setup_main(["--home", str(work / "home")])

    print(f"\nDemo complete. (Throwaway workspace at {work} — safe to delete.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
