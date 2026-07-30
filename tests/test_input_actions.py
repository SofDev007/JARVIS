"""Tests for input backends and input actions (M5).

Backends are exercised against monkeypatched subprocess; actions against a
recording fake backend; the pipeline test proves the risk split's purpose:
slides advance with zero prompts while chords stay confirmation-gated.
"""

from __future__ import annotations

import subprocess
import time

import pytest

from digital_twin.automation.dispatcher import ActionDispatcher
from digital_twin.automation.input_actions import register_input_actions
from digital_twin.automation.input_backend import (
    InputBackend,
    InputBackendError,
    XdotoolBackend,
    create_input_backend,
    is_valid_key,
)
from digital_twin.automation.registry import ActionRegistry
from digital_twin.configuration.settings import AutomationConfig
from digital_twin.core.bus import EventBus
from digital_twin.core.events import Event, Topics
from digital_twin.security.audit import AuditLog
from digital_twin.security.confirmation import ScriptedConfirmation
from digital_twin.security.permissions import PermissionPolicy, RiskLevel


# ---------------------------------------------------------------------------
# Canonical key model
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("key,expected", [
    ("ctrl", True), ("right", True), ("play_pause", True), ("f11", True),
    ("a", True), ("7", True),
    ("F13", False), ("ctrl+c", False), ("Return", False), ("", False),
    ("XF86AudioPlay", False), (None, False), ("ab", False),
])
def test_is_valid_key(key, expected):
    assert is_valid_key(key) is expected


# ---------------------------------------------------------------------------
# Xdotool backend (subprocess monkeypatched)
# ---------------------------------------------------------------------------
class _Recorder:
    def __init__(self, fail=False, stdout=""):
        self.calls: list[tuple[list[str], str | None]] = []
        self._fail = fail
        self._stdout = stdout

    def __call__(self, command, input=None, **kwargs):
        self.calls.append((list(command), input))
        return subprocess.CompletedProcess(
            command, 1 if self._fail else 0,
            stdout=self._stdout, stderr="boom" if self._fail else "",
        )


def test_xdotool_chord_mapping(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(subprocess, "run", recorder)
    XdotoolBackend().press_keys(["ctrl", "shift", "s"])
    XdotoolBackend().press_keys(["play_pause"])
    XdotoolBackend().press_keys(["right"])
    assert recorder.calls[0][0] == ["xdotool", "key", "--clearmodifiers", "ctrl+shift+s"]
    assert recorder.calls[1][0][-1] == "XF86AudioPlay"
    assert recorder.calls[2][0][-1] == "Right"


def test_xdotool_type_guards_leading_dash(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(subprocess, "run", recorder)
    XdotoolBackend().type_text("-rf hello")
    command, _ = recorder.calls[0]
    assert command[-2:] == ["--", "-rf hello"]


def test_xdotool_failure_raises(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _Recorder(fail=True))
    with pytest.raises(InputBackendError):
        XdotoolBackend().press_keys(["right"])


def test_xdotool_clipboard_pipes_text(monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(subprocess, "run", recorder)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/xclip" if name == "xclip" else None)
    XdotoolBackend().set_clipboard("secret text")
    command, piped = recorder.calls[0]
    assert command[0] == "xclip" and piped == "secret text"


def test_xdotool_clipboard_requires_helper(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(InputBackendError, match="xclip"):
        XdotoolBackend().set_clipboard("x")


def test_xdotool_focus_window(monkeypatch):
    recorder = _Recorder(stdout="4321\n8765\n")
    monkeypatch.setattr(subprocess, "run", recorder)
    assert XdotoolBackend().focus_window("impress") is True
    assert recorder.calls[1][0] == ["xdotool", "windowactivate", "--sync", "4321"]

    monkeypatch.setattr(subprocess, "run", _Recorder(stdout=""))
    assert XdotoolBackend().focus_window("nothing") is False


def test_create_backend_guidance_when_none_available(monkeypatch):
    for backend in ("XdotoolBackend", "PynputBackend"):
        monkeypatch.setattr(
            f"digital_twin.automation.input_backend.{backend}.available",
            classmethod(lambda cls: False),
        )
    with pytest.raises(InputBackendError, match="xdotool"):
        create_input_backend("auto")
    with pytest.raises(InputBackendError, match="not available"):
        create_input_backend("xdotool")
    with pytest.raises(InputBackendError, match="Unknown"):
        create_input_backend("telepathy")


# ---------------------------------------------------------------------------
# Input actions with a fake backend
# ---------------------------------------------------------------------------
class FakeBackend(InputBackend):
    name = "fake"

    def __init__(self, focus_result=True):
        self.calls: list[tuple] = []
        self._focus_result = focus_result

    @classmethod
    def available(cls) -> bool:
        return True

    def press_keys(self, keys):
        self.calls.append(("press", tuple(keys)))

    def type_text(self, text):
        self.calls.append(("type", text))

    def set_clipboard(self, text):
        self.calls.append(("clipboard", text))

    def focus_window(self, title_substring):
        self.calls.append(("focus", title_substring))
        return self._focus_result


@pytest.fixture()
def rig():
    backend = FakeBackend()
    registry = ActionRegistry()
    register_input_actions(registry, backend_factory=lambda: backend)
    return registry, backend


def test_nav_key_repeats_and_validates(rig):
    registry, backend = rig
    spec = registry.get("nav_key")
    spec.validate({"key": "right", "repeat": 3})
    assert spec.handler({"key": "right", "repeat": 3}) == "pressed right x3"
    assert backend.calls == [("press", ("right",))] * 3
    for bad in ({"key": "enter"}, {"key": "right", "repeat": 0},
                {"key": "right", "repeat": 11}, {"key": "right", "repeat": True}, {}):
        with pytest.raises(ValueError):
            spec.validate(bad)


def test_media_key_action(rig):
    registry, backend = rig
    spec = registry.get("media_key")
    assert spec.risk is RiskLevel.SAFE
    spec.handler({"key": "play_pause"})
    assert backend.calls == [("press", ("play_pause",))]
    with pytest.raises(ValueError):
        spec.validate({"key": "right"})


def test_press_keys_chord_rules(rig):
    registry, backend = rig
    spec = registry.get("press_keys")
    assert spec.risk is RiskLevel.SENSITIVE
    spec.validate({"keys": ["ctrl", "shift", "s"]})
    spec.validate({"keys": ["escape"]})
    for bad in (
        {"keys": []},
        {"keys": ["ctrl", "alt", "shift", "super", "s"]},   # > 4
        {"keys": ["s", "ctrl"]},                            # modifier last
        {"keys": ["ctrl"]},                                 # chord ends on modifier
        {"keys": ["a", "b"]},                               # non-modifier prefix
        {"keys": ["play_pause"]},                           # media via wrong action
        {"keys": ["Return"]},                               # non-canonical
        {"keys": "escape"},
    ):
        with pytest.raises(ValueError):
            spec.validate(bad)
    spec.handler({"keys": ["ctrl", "s"]})
    assert backend.calls == [("press", ("ctrl", "s"))]


def test_type_text_rejects_newlines_and_control(rig):
    registry, backend = rig
    spec = registry.get("type_text")
    spec.validate({"text": "hello world\twith tab"})
    for bad in ("line1\nline2", "carriage\rreturn", "bell\x07", "", "x" * 501):
        with pytest.raises(ValueError):
            spec.validate({"text": bad})
    spec.handler({"text": "hello"})
    assert backend.calls == [("type", "hello")]


def test_type_text_cap_is_configurable():
    backend = FakeBackend()
    registry = ActionRegistry()
    register_input_actions(registry, backend_factory=lambda: backend,
                           max_type_text_chars=10)
    with pytest.raises(ValueError):
        registry.get("type_text").validate({"text": "x" * 11})


def test_clipboard_and_focus_window(rig):
    registry, backend = rig
    registry.get("set_clipboard").handler({"text": "copy me"})
    assert ("clipboard", "copy me") in backend.calls
    with pytest.raises(ValueError):
        registry.get("set_clipboard").validate({"text": "x" * 10_001})

    assert "focused" in registry.get("focus_window").handler({"title": "impress"})


def test_focus_window_no_match_fails():
    backend = FakeBackend(focus_result=False)
    registry = ActionRegistry()
    register_input_actions(registry, backend_factory=lambda: backend)
    with pytest.raises(InputBackendError, match="no window matching"):
        registry.get("focus_window").handler({"title": "ghost"})


def test_backend_resolved_lazily_and_failure_is_informative():
    registry = ActionRegistry()

    def unavailable():
        raise InputBackendError("install xdotool")

    register_input_actions(registry, backend_factory=unavailable)  # no raise here
    with pytest.raises(InputBackendError, match="install xdotool"):
        registry.get("nav_key").handler({"key": "right"})


# ---------------------------------------------------------------------------
# Pipeline: the risk split in action
# ---------------------------------------------------------------------------
@pytest.fixture()
def bus():
    bus = EventBus()
    bus.start()
    yield bus
    bus.stop()


def _intent(name: str) -> Event:
    return Event(Topics.INTENT, "intent", {
        "intent": name, "context": "presentation", "gesture": "thumbs_up",
        "source_event": "p1",
    })


def _wait(predicate, timeout=5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_slides_advance_without_prompts_chords_stay_gated(bus, tmp_path):
    backend = FakeBackend()
    registry = ActionRegistry()
    register_input_actions(registry, backend_factory=lambda: backend)
    confirmation = ScriptedConfirmation([True])
    dispatcher = ActionDispatcher(
        config=AutomationConfig(),  # default bindings incl. next_slide etc.
        registry=registry,
        policy=PermissionPolicy(risk_defaults={
            "safe": "allow", "sensitive": "confirm", "dangerous": "deny"}),
        confirmation=confirmation,
        audit=AuditLog(tmp_path / "audit.jsonl"),
        confirmation_timeout_s=1.0,
    )
    results: list[Event] = []
    bus.subscribe(Topics.ACTION_RESULT, results.append)
    dispatcher.start(bus)
    try:
        for _ in range(3):
            bus.publish(_intent("next_slide"))
        bus.publish(_intent("play_pause"))
        assert _wait(lambda: len(results) == 4)
        # Four SAFE actions executed, zero confirmation requests.
        assert {e.payload["status"] for e in results} == {"completed"}
        assert confirmation.requests == []
        assert backend.calls == [("press", ("right",))] * 3 + \
            [("press", ("play_pause",))]

        bus.publish(_intent("end_presentation"))  # press_keys → gated
        assert _wait(lambda: len(results) == 5)
        assert confirmation.requests[0][0] == "press_keys"
        assert results[-1].payload["status"] == "completed"
        assert backend.calls[-1] == ("press", ("escape",))
    finally:
        dispatcher.stop()


def test_backend_failure_reported_as_failed_result(bus, tmp_path):
    registry = ActionRegistry()
    register_input_actions(
        registry,
        backend_factory=lambda: (_ for _ in ()).throw(
            InputBackendError("install xdotool")),
    )
    dispatcher = ActionDispatcher(
        config=AutomationConfig(),
        registry=registry,
        policy=PermissionPolicy(risk_defaults={"safe": "allow"}),
        confirmation=ScriptedConfirmation(),
        audit=AuditLog(tmp_path / "audit.jsonl"),
    )
    results: list[Event] = []
    bus.subscribe(Topics.ACTION_RESULT, results.append)
    dispatcher.start(bus)
    try:
        bus.publish(_intent("next_slide"))
        assert _wait(lambda: results)
        assert results[0].payload["status"] == "failed"
        assert "install xdotool" in results[0].payload["detail"]
    finally:
        dispatcher.stop()
