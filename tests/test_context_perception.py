"""Tests for screen-context perception: probes, classification, module, chain."""

from __future__ import annotations

import subprocess
import time

import pytest

from digital_twin.configuration.settings import ContextPerceptionConfig, IntentConfig
from digital_twin.core.bus import EventBus
from digital_twin.core.events import Topics
from digital_twin.perception.context.module import (
    ContextPerceptionModule,
    classify,
    compile_rules,
)
from digital_twin.perception.context.probe import (
    LinuxXdotoolProbe,
    WindowInfo,
    WindowProbe,
    create_window_probe,
)
from digital_twin.reasoning.intent import IntentEngine

RULES = compile_rules(
    [
        {"context": "presentation", "any": ["powerpoint", "impress"]},
        {"context": "media", "any": ["youtube", "vlc"]},
        {"context": "coding", "any": ["code", "vim"]},
    ]
)


# ---------------------------------------------------------------------------
# Classification (pure function)
# ---------------------------------------------------------------------------
def test_first_matching_rule_wins():
    # "youtube" (media) appears but the presentation rule is listed first.
    info = WindowInfo(title="YouTube - PowerPoint tutorial", process="firefox")
    assert classify(info, RULES, "desktop") == "presentation"


def test_match_is_case_insensitive_and_covers_process():
    assert classify(WindowInfo("Watching stuff", "VLC"), RULES, "desktop") == "media"
    assert classify(WindowInfo("MY SLIDES - IMPRESS", ""), RULES, "desktop") == "presentation"


def test_no_match_and_unknown_window_fall_back():
    assert classify(WindowInfo("Inbox", "thunderbird"), RULES, "desktop") == "desktop"
    assert classify(None, RULES, "desktop") == "desktop"
    assert classify(WindowInfo("anything", "x"), compile_rules([]), "d") == "d"


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------
def test_linux_probe_availability_logic(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/xdotool")
    assert LinuxXdotoolProbe.available() is True
    monkeypatch.delenv("DISPLAY")
    assert LinuxXdotoolProbe.available() is False


def test_linux_probe_parses_xdotool_output(monkeypatch, tmp_path):
    outputs = {
        ("xdotool", "getactivewindow"): "12345",
        ("xdotool", "getwindowname", "12345"): "My Slides - Impress",
        ("xdotool", "getwindowpid", "12345"): "999",
    }

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command, 0, stdout=outputs[tuple(command)] + "\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    comm = tmp_path / "comm"
    comm.write_text("soffice\n")
    monkeypatch.setattr(
        "digital_twin.perception.context.probe.Path",
        lambda path: comm,  # /proc/999/comm → our temp file
    )
    info = LinuxXdotoolProbe().active_window()
    assert info == WindowInfo(title="My Slides - Impress", process="soffice")


def test_linux_probe_returns_none_on_failure(monkeypatch):
    def fail_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="boom")

    monkeypatch.setattr(subprocess, "run", fail_run)
    assert LinuxXdotoolProbe().active_window() is None


def test_create_window_probe_raises_with_guidance_when_none_available(monkeypatch):
    for backend in ("LinuxXdotoolProbe", "WindowsProbe", "MacProbe"):
        monkeypatch.setattr(
            f"digital_twin.perception.context.probe.{backend}.available",
            classmethod(lambda cls: False),
        )
    with pytest.raises(RuntimeError, match="xdotool"):
        create_window_probe()


# ---------------------------------------------------------------------------
# Module behaviour with a scripted probe
# ---------------------------------------------------------------------------
class ScriptedProbe(WindowProbe):
    name = "scripted"

    def __init__(self, windows):
        self._windows = list(windows)

    @classmethod
    def available(cls) -> bool:
        return True

    def active_window(self):
        if not self._windows:
            return None
        return self._windows.pop(0) if len(self._windows) > 1 else self._windows[0]


@pytest.fixture()
def bus():
    bus = EventBus()
    bus.start()
    yield bus
    bus.stop()


def _module(bus, probe, **config_kwargs) -> ContextPerceptionModule:
    config = ContextPerceptionConfig(
        poll_interval_s=0.005,
        rules=[
            {"context": "presentation", "any": ["impress"]},
            {"context": "media", "any": ["youtube"]},
        ],
        **config_kwargs,
    )
    module = ContextPerceptionModule(config, probe_factory=lambda: probe)
    module.start(bus)
    return module


def _record(bus):
    events = []
    bus.subscribe(Topics.CONTEXT, events.append)
    return events


def test_context_published_on_change_only(bus):
    events = _record(bus)
    module = _module(bus, ScriptedProbe([None]))
    try:
        impress = WindowInfo("Deck - Impress", "soffice")
        module._process_window(impress)
        module._process_window(impress)          # held → no repeat
        module._process_window(WindowInfo("YouTube", "firefox"))
        module._process_window(WindowInfo("Inbox", "mail"))  # fallback
        bus.flush(timeout=2.0)
        assert [e.payload["context"] for e in events] == [
            "presentation", "media", "desktop",
        ]
    finally:
        module.stop()


def test_window_info_stays_private_by_default(bus):
    events = _record(bus)
    module = _module(bus, ScriptedProbe([None]))
    try:
        module._process_window(WindowInfo("Secret deck - Impress", "soffice"))
        bus.flush(timeout=2.0)
        (event,) = events
        assert "window_title" not in event.payload
        assert "process" not in event.payload
    finally:
        module.stop()


def test_window_info_published_when_opted_in(bus):
    events = _record(bus)
    module = _module(bus, ScriptedProbe([None]), publish_window_info=True)
    try:
        module._process_window(WindowInfo("Deck - Impress", "soffice"))
        bus.flush(timeout=2.0)
        (event,) = events
        assert event.payload["window_title"] == "Deck - Impress"
        assert event.payload["process"] == "soffice"
    finally:
        module.stop()


def test_probe_failure_at_start_marks_module_failed(bus):
    def broken_probe():
        raise RuntimeError("no probe here")

    module = ContextPerceptionModule(
        ContextPerceptionConfig(), probe_factory=broken_probe
    )
    with pytest.raises(RuntimeError):
        module.start(bus)
    assert module.status().state.value == "failed"
    assert "no probe here" in module.status().detail


def test_polling_thread_end_to_end_and_metrics(bus):
    events = _record(bus)
    probe = ScriptedProbe([
        WindowInfo("Deck - Impress", "soffice"),
        WindowInfo("YouTube", "firefox"),   # last item repeats forever
    ])
    module = _module(bus, probe)
    try:
        deadline = time.monotonic() + 5.0
        while len(events) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        bus.flush(timeout=2.0)
        assert [e.payload["context"] for e in events] == ["presentation", "media"]
        metrics = module.status().metrics
        assert metrics["context"] == "media"
        assert metrics["probe"] == "scripted"
        assert metrics["polls"] >= 2
    finally:
        module.stop()


def test_paused_module_publishes_nothing(bus):
    events = _record(bus)
    module = _module(bus, ScriptedProbe([WindowInfo("YouTube", "firefox")]))
    try:
        module.pause()
        module._process_window(WindowInfo("Deck - Impress", "soffice"))
        bus.flush(timeout=2.0)
        assert events == []
        module.resume()
        assert module.is_active
    finally:
        module.stop()


def test_hands_free_chain_window_switch_changes_intent_meaning(bus):
    """The M4 payoff: same gesture, different intent, zero manual context."""
    from digital_twin.core.events import Event

    engine = IntentEngine(IntentConfig(
        default_context="desktop",
        mappings={
            "presentation": {"thumbs_up": "next_slide"},
            "media": {"thumbs_up": "like"},
        },
    ))
    engine.start(bus)
    module = _module(bus, ScriptedProbe([None]))
    intents = []
    bus.subscribe(Topics.INTENT, intents.append)

    def thumbs_up():
        bus.publish(Event(Topics.GESTURE, "gesture", {
            "gesture": "thumbs_up", "confidence": 0.95,
            "hand": "right", "repeat": False,
        }))
        bus.flush(timeout=2.0)

    try:
        module._process_window(WindowInfo("Deck - Impress", "soffice"))
        bus.flush(timeout=2.0)
        thumbs_up()
        module._process_window(WindowInfo("YouTube", "firefox"))
        bus.flush(timeout=2.0)
        thumbs_up()
        assert [e.payload["intent"] for e in intents] == ["next_slide", "like"]
        # Provenance: the intent engine saw contexts from the screen module.
        assert [e.payload["context"] for e in intents] == ["presentation", "media"]
    finally:
        module.stop()
        engine.stop()
