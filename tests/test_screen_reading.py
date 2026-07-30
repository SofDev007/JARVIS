"""Tests for screen reading: capture backends, OCR, module, gated action."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from digital_twin.configuration.settings import (
    AutomationConfig,
    ScreenReadingConfig,
    SecurityConfig,
)
from digital_twin.core.bus import EventBus
from digital_twin.core.events import Event, Topics
from digital_twin.core.module import ModuleState
from digital_twin.automation.dispatcher import ActionDispatcher
from digital_twin.automation.registry import ActionRegistry
from digital_twin.perception.screen.actions import register_screen_actions
from digital_twin.perception.screen.capture import (
    ScreenCaptureError,
    ScriptedCapturer,
    create_screen_capturer,
)
from digital_twin.perception.screen.module import (
    ScreenReadingModule,
    normalize_ocr_text,
)
from digital_twin.perception.screen.ocr import (
    OCRError,
    ScriptedRecognizer,
    TesseractRecognizer,
    create_text_recognizer,
)
from digital_twin.security.audit import AuditLog
from digital_twin.security.confirmation import ScriptedConfirmation
from digital_twin.security.permissions import PermissionPolicy, RiskLevel


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------
def test_create_capturer_raises_with_guidance_when_none_available(monkeypatch):
    import digital_twin.perception.screen.capture as capture

    for backend in ("ScrotCapture", "ImageMagickCapture",
                    "GnomeScreenshotCapture", "MacScreencapture",
                    "WindowsPowerShellCapture"):
        monkeypatch.setattr(
            f"digital_twin.perception.screen.capture.{backend}.available",
            classmethod(lambda cls: False),
        )
    with pytest.raises(ScreenCaptureError, match="scrot"):
        create_screen_capturer("auto")
    with pytest.raises(ScreenCaptureError, match="Unknown"):
        create_screen_capturer("polaroid")
    # Explicit preference for an unavailable backend also fails loudly.
    with pytest.raises(ScreenCaptureError):
        create_screen_capturer("scrot")


def test_capture_verifies_a_file_was_produced(monkeypatch, tmp_path):
    from digital_twin.perception.screen.capture import ScrotCapture

    calls = []

    class FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, **kwargs):
        calls.append(command)
        return FakeResult()  # exits 0 but never writes the file

    monkeypatch.setattr("subprocess.run", fake_run)
    with pytest.raises(ScreenCaptureError, match="no screenshot"):
        ScrotCapture().capture(tmp_path / "shot.png", timeout_s=1.0)
    assert calls and calls[0][0] == "scrot"


def test_tesseract_recognizer_parses_stdout(monkeypatch, tmp_path):
    class FakeResult:
        returncode = 0
        stdout = "Hello\nScreen\n"
        stderr = ""

    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        return FakeResult()

    monkeypatch.setattr("subprocess.run", fake_run)
    image = tmp_path / "img.png"
    image.write_bytes(b"x")
    text = TesseractRecognizer(language="eng+deu").recognize(image, 5.0)
    assert text == "Hello\nScreen\n"
    assert seen["command"][:3] == ["tesseract", str(image), "stdout"]
    assert seen["command"][3:] == ["-l", "eng+deu"]


def test_create_recognizer_guides_when_tesseract_missing(monkeypatch):
    monkeypatch.setattr(
        "digital_twin.perception.screen.ocr.TesseractRecognizer.available",
        classmethod(lambda cls: False),
    )
    with pytest.raises(OCRError, match="tesseract-ocr"):
        create_text_recognizer()


def test_normalize_ocr_text_collapses_noise():
    raw = "  Title \n\n\n   line two\t \n\n"
    assert normalize_ocr_text(raw) == "Title\nline two"


# ---------------------------------------------------------------------------
# Module behaviour
# ---------------------------------------------------------------------------
@pytest.fixture()
def bus():
    bus = EventBus()
    bus.start()
    yield bus
    bus.stop()


def _module(bus, texts, max_chars=4000) -> tuple[ScreenReadingModule,
                                                 ScriptedCapturer]:
    capturer = ScriptedCapturer()
    module = ScreenReadingModule(
        ScreenReadingConfig(max_chars=max_chars),
        capturer_factory=lambda: capturer,
        recognizer_factory=lambda: ScriptedRecognizer(texts),
    )
    module.start(bus)
    return module, capturer


def test_read_now_publishes_bounded_text_and_returns_counts_only(bus):
    events = []
    bus.subscribe(Topics.SCREEN, events.append)
    module, _ = _module(bus, ["  Report Q3 \n\n Revenue up 9% \n"])
    try:
        summary = module.read_now()
        bus.flush(2.0)
    finally:
        module.stop()
    assert summary == f"read {len('Report Q3' + chr(10) + 'Revenue up 9%')} characters from the screen"
    assert "Revenue" not in summary  # counts only — this reaches the audit
    assert len(events) == 1
    payload = events[0].payload
    assert payload["text"] == "Report Q3\nRevenue up 9%"
    assert payload["chars"] == len(payload["text"])
    assert payload["truncated"] is False


def test_read_now_truncates_and_flags_long_text(bus):
    module, _ = _module(bus, ["A" * 500], max_chars=100)
    events = []
    bus.subscribe(Topics.SCREEN, events.append)
    try:
        summary = module.read_now()
        bus.flush(2.0)
    finally:
        module.stop()
    assert "truncated to 100" in summary
    assert events[0].payload["truncated"] is True
    assert len(events[0].payload["text"]) == 100


def test_screenshot_file_is_deleted_even_when_ocr_fails(bus):
    capturer = ScriptedCapturer()

    class ExplodingRecognizer(ScriptedRecognizer):
        def recognize(self, image_path, timeout_s):
            super().recognize(image_path, timeout_s)
            raise OCRError("boom")

    recognizer = ExplodingRecognizer()
    module = ScreenReadingModule(
        ScreenReadingConfig(),
        capturer_factory=lambda: capturer,
        recognizer_factory=lambda: recognizer,
    )
    module.start(bus)
    try:
        with pytest.raises(OCRError):
            module.read_now()
    finally:
        module.stop()
    shot = capturer.captures[0]
    assert not shot.exists()
    assert not shot.parent.exists()  # the private temp dir is gone too


def test_read_now_refuses_unless_running(bus):
    module, _ = _module(bus, ["text"])
    module.pause()
    with pytest.raises(RuntimeError, match="not running"):
        module.read_now()
    module.resume()
    assert module.read_now().startswith("read ")
    module.stop()
    with pytest.raises(RuntimeError):
        module.read_now()


def test_missing_backend_fails_at_use_time_not_start(bus):
    def broken_factory():
        raise ScreenCaptureError("install scrot")

    module = ScreenReadingModule(
        ScreenReadingConfig(),
        capturer_factory=broken_factory,
        recognizer_factory=lambda: ScriptedRecognizer(["x"]),
    )
    module.start(bus)  # kernel starts everywhere — no crash here
    try:
        assert module.state is ModuleState.RUNNING
        with pytest.raises(ScreenCaptureError, match="install scrot"):
            module.read_now()
    finally:
        module.stop()


# ---------------------------------------------------------------------------
# The gated read_screen action, end to end through the dispatcher
# ---------------------------------------------------------------------------
class _Pipeline:
    def __init__(self, bus, tmp_path, answers=None, permissions=None):
        self.registry = ActionRegistry()
        self.confirmation = ScriptedConfirmation(answers or [])
        self.audit = AuditLog(tmp_path / "audit.jsonl")
        security = SecurityConfig(permissions=permissions or {})
        self.dispatcher = ActionDispatcher(
            config=AutomationConfig(action_timeout_s=5.0),
            registry=self.registry,
            policy=PermissionPolicy(security.risk_defaults, security.permissions),
            confirmation=self.confirmation,
            audit=self.audit,
        )
        self.results = []
        bus.subscribe(Topics.ACTION_RESULT, self.results.append)

    def wait(self, count=1, timeout=3.0):
        deadline = time.time() + timeout
        while len(self.results) < count and time.time() < deadline:
            time.sleep(0.01)
        return self.results


def test_read_screen_is_sensitive_confirm_gated_and_audit_holds_no_text(
    bus, tmp_path
):
    pipeline = _Pipeline(bus, tmp_path, answers=[True])
    module, _ = _module(bus, ["SECRET password hunter2"])
    register_screen_actions(pipeline.registry, module)
    assert pipeline.registry.get("read_screen").risk is RiskLevel.SENSITIVE

    screen_events = []
    bus.subscribe(Topics.SCREEN, screen_events.append)
    pipeline.dispatcher.start(bus)
    try:
        bus.publish(Event(Topics.ACTION_EXECUTE, "test",
                          {"action": "read_screen", "params": {}}))
        results = pipeline.wait()
        bus.flush(2.0)
    finally:
        pipeline.dispatcher.stop()
        module.stop()

    assert pipeline.confirmation.requests  # the gate was exercised
    assert results[0].payload["status"] == "completed"
    assert "hunter2" not in results[0].payload["detail"]
    for entry in pipeline.audit.tail(10):
        assert "hunter2" not in str(entry)
    assert screen_events and "hunter2" in screen_events[0].payload["text"]


def test_read_screen_denied_by_one_permission_rule(bus, tmp_path):
    pipeline = _Pipeline(bus, tmp_path,
                         permissions={"read_screen": "deny"})
    module, capturer = _module(bus, ["text"])
    register_screen_actions(pipeline.registry, module)
    pipeline.dispatcher.start(bus)
    try:
        bus.publish(Event(Topics.ACTION_EXECUTE, "test",
                          {"action": "read_screen", "params": {}}))
        results = pipeline.wait()
    finally:
        pipeline.dispatcher.stop()
        module.stop()
    assert results[0].payload["status"] == "denied"
    assert capturer.captures == []  # denied means no capture ever happened


def test_read_screen_rejects_parameters(bus, tmp_path):
    pipeline = _Pipeline(bus, tmp_path)
    module, _ = _module(bus, ["text"])
    register_screen_actions(pipeline.registry, module)
    with pytest.raises(ValueError):
        pipeline.registry.get("read_screen").validate({"window": "all"})
    module.stop()


# ---------------------------------------------------------------------------
# Reasoner consumes perception.screen
# ---------------------------------------------------------------------------
def test_reasoner_injects_fresh_screen_text_and_drops_stale(bus):
    from digital_twin.configuration.settings import LLMConfig
    from digital_twin.reasoning.chat_reasoner import ChatReasoner
    from digital_twin.reasoning.llm import ScriptedModel

    model = ScriptedModel(['{"reply": "ok", "intent": null, '
                          '"plan": null, "remember": null, "reasoning": "r"}'] * 2)
    reasoner = ChatReasoner(LLMConfig(memory_results=0), model=model,
                            allowed_intents=())
    reasoner.start(bus)
    try:
        bus.publish(Event(Topics.SCREEN, "screen_reader",
                          {"text": "Quarterly numbers: 42", "chars": 21,
                           "truncated": False}))
        bus.flush(2.0)
        bus.publish(Event(Topics.CHAT, "test", {"text": "what do you see?"}))
        deadline = time.time() + 3.0
        while not model.calls and time.time() < deadline:
            time.sleep(0.01)
        assert "Quarterly numbers: 42" in model.calls[0]["system"]

        reasoner._screen_at = time.time() - 10_000  # age the capture
        bus.publish(Event(Topics.CHAT, "test", {"text": "and now?"}))
        deadline = time.time() + 3.0
        while len(model.calls) < 2 and time.time() < deadline:
            time.sleep(0.01)
        assert "Quarterly numbers" not in model.calls[1]["system"]
    finally:
        reasoner.stop()
