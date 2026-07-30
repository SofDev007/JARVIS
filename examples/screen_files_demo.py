#!/usr/bin/env python3
"""M10 demo — reading the screen, and touching real files (behind gates).

No screenshot binary, no tesseract and no camera are needed: a scripted
capturer and OCR engine stand in, but the dispatcher, permission policy,
confirmation gates and audit log are the real ones, and the files acted on
are real files in a throwaway directory. Four acts:

1. **read_screen** — a SENSITIVE action captures the screen once, OCRs it
   locally, and publishes the text on ``perception.screen``. The action
   result and audit log carry only character counts; the text itself never
   touches them.
2. **The reasoner sees the screen** — the OCR'd text is injected into the
   next chat prompt, so "what's on my screen?" is answerable.
3. **File intelligence** — list, write a new file, then find the duplicate
   we just created. Every step is gated; each is confined to the one
   allowed root.
4. **The DANGEROUS clamp, finally exercised** — ``delete_file`` is
   configured ``allow``, yet the human is *still* asked to confirm (the
   floor the permission system has enforced in code since M3). On approval
   the file is moved to a recoverable trash directory, not destroyed.

Run from the repository root::

    python examples/screen_files_demo.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from tempfile import mkdtemp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from digital_twin.automation.dispatcher import ActionDispatcher  # noqa: E402
from digital_twin.automation.file_actions import register_file_actions  # noqa: E402
from digital_twin.automation.registry import ActionRegistry  # noqa: E402
from digital_twin.configuration.settings import (  # noqa: E402
    AutomationConfig,
    FilesConfig,
    LLMConfig,
    ScreenReadingConfig,
)
from digital_twin.core.bus import EventBus  # noqa: E402
from digital_twin.core.events import Event, Topics  # noqa: E402
from digital_twin.perception.screen.actions import register_screen_actions  # noqa: E402
from digital_twin.perception.screen.capture import ScriptedCapturer  # noqa: E402
from digital_twin.perception.screen.module import ScreenReadingModule  # noqa: E402
from digital_twin.perception.screen.ocr import ScriptedRecognizer  # noqa: E402
from digital_twin.reasoning.chat_reasoner import ChatReasoner  # noqa: E402
from digital_twin.reasoning.llm import ScriptedModel  # noqa: E402
from digital_twin.security.audit import AuditLog  # noqa: E402
from digital_twin.security.confirmation import ConfirmationProvider  # noqa: E402
from digital_twin.security.permissions import PermissionPolicy  # noqa: E402


SCREEN_TEXT = (
    "Inbox — 3 unread\n"
    "  Invoice #4471 — DUE FRIDAY — $1,845.00\n"
    "  Re: standup notes\n"
    "  password reset code: 771903\n"
)


class NarratingConfirmation(ConfirmationProvider):
    """Prints each gate it sees, then approves — so the demo shows the
    clamp firing without needing a TTY."""

    name = "narrating"

    def __init__(self) -> None:
        self.requests: list[str] = []

    def request(self, action, params, timeout_s) -> bool:
        self.requests.append(action)
        print(f"    [confirm?] {action}({dict(params)}) -> approved")
        return True


def _await(predicate, timeout=3.0) -> None:
    deadline = time.time() + timeout
    while not predicate() and time.time() < deadline:
        time.sleep(0.01)


def main() -> int:
    work = Path(mkdtemp(prefix="dtwin-m10-demo-"))
    (work / "report.txt").write_text("Quarterly revenue up 9%.\n")

    bus = EventBus()
    bus.start()

    # --- perception: on-demand screen reader (scripted capture + OCR) ---
    screen = ScreenReadingModule(
        ScreenReadingConfig(),
        capturer_factory=lambda: ScriptedCapturer(),
        recognizer_factory=lambda: ScriptedRecognizer([SCREEN_TEXT]),
    )
    screen.start(bus)

    # --- automation: dispatcher + screen action + file actions ----------
    registry = ActionRegistry()
    register_screen_actions(registry, screen)
    register_file_actions(registry, FilesConfig(allowed_roots=[str(work)]))
    audit = AuditLog(work / "audit.jsonl")
    confirmation = NarratingConfirmation()
    dispatcher = ActionDispatcher(
        config=AutomationConfig(action_timeout_s=5.0),
        registry=registry,
        # delete_file is DANGEROUS and here even *configured* to 'allow' —
        # the policy clamps it to CONFIRM regardless.
        policy=PermissionPolicy(
            risk_defaults={"safe": "allow", "sensitive": "allow",
                           "dangerous": "deny"},
            overrides={"delete_file": "allow"},
        ),
        confirmation=confirmation,
        audit=audit,
    )
    dispatcher.start(bus)

    # --- reasoning: a scripted model that answers from the screen -------
    model = ScriptedModel([
        '{"reply": "Your inbox shows an invoice #4471 due Friday for '
        '$1,845.00.", "intent": null, "plan": null, "remember": null, '
        '"reasoning": "read it from the screen text"}',
    ])
    reasoner = ChatReasoner(LLMConfig(memory_results=0), model=model,
                            allowed_intents=())
    reasoner.start(bus)

    results: list[Event] = []
    bus.subscribe(Topics.ACTION_RESULT, results.append)
    replies: list[Event] = []
    bus.subscribe(Topics.CHAT_RESPONSE, replies.append)

    print("=" * 66)
    print("Act 1 — read_screen (SENSITIVE): one gated capture + local OCR")
    print("=" * 66)
    bus.publish(Event(Topics.ACTION_EXECUTE, "demo",
                      {"action": "read_screen", "params": {}}))
    _await(lambda: len(results) >= 1)
    bus.flush(2.0)
    print(f"    result detail : {results[-1].payload['detail']}")
    print("    (note: character counts only — the OCR'd text, including the")
    print("     password reset code, never enters the result or the audit log)")

    print("\n" + "=" * 66)
    print("Act 2 — the reasoner answers *from the screen*")
    print("=" * 66)
    bus.publish(Event(Topics.CHAT, "demo",
                      {"text": "what's on my screen right now?"}))
    _await(lambda: len(replies) >= 1)
    print(f"    user     : what's on my screen right now?")
    print(f"    assistant: {replies[-1].payload['text']}")

    print("\n" + "=" * 66)
    print("Act 3 — file intelligence (each step gated, one allowed root)")
    print("=" * 66)
    before = len(results)
    bus.publish(Event(Topics.ACTION_EXECUTE, "demo",
                      {"action": "list_files", "params": {"path": str(work)}}))
    _await(lambda: len(results) >= before + 1)
    print(f"    list_files    : {results[-1].payload['detail']}")

    bus.publish(Event(Topics.ACTION_EXECUTE, "demo", {
        "action": "write_text_file",
        "params": {"path": str(work / "report_copy.txt"),
                   "text": "Quarterly revenue up 9%.\n"},
    }))
    _await(lambda: len(results) >= before + 2)
    print(f"    write_text_file: {results[-1].payload['detail']}")

    bus.publish(Event(Topics.ACTION_EXECUTE, "demo",
                      {"action": "find_duplicates",
                       "params": {"path": str(work)}}))
    _await(lambda: len(results) >= before + 3)
    print(f"    find_duplicates: {results[-1].payload['detail']}")

    print("\n" + "=" * 66)
    print("Act 4 — DANGEROUS delete_file: 'allow' in config, STILL confirmed")
    print("=" * 66)
    before = len(results)
    bus.publish(Event(Topics.ACTION_EXECUTE, "demo",
                      {"action": "delete_file",
                       "params": {"path": str(work / "report.txt")}}))
    _await(lambda: len(results) >= before + 1)
    print(f"    result        : {results[-1].payload['status']}")
    trashed = list((work / ".digital_twin_trash").iterdir())
    print(f"    report.txt now: {'gone from workspace' if not (work / 'report.txt').exists() else 'still here'}")
    print(f"    recoverable in trash: {trashed[0].name if trashed else '(none)'}")
    print(f"\n    confirmation gates fired this run: {confirmation.requests}")

    reasoner.stop()
    dispatcher.stop()
    screen.stop()
    bus.stop()
    print("\nDemo complete. (Throwaway workspace left at "
          f"{work} — safe to delete.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
