"""Tests for M16: write-side connectors (send_email/create_event risk +
guards), the wake-word detector (trigger-only, privacy), and the setup
CLI."""

from __future__ import annotations

import time
import zipfile
from pathlib import Path

import pytest

from digital_twin.automation.registry import ActionRegistry
from digital_twin.configuration.settings import PluginsConfig, VoiceConfig
from digital_twin.core.bus import EventBus
from digital_twin.core.events import Event, Topics
from digital_twin.core.registry import ModuleRegistry
from digital_twin.plugins.loader import load_plugins
from digital_twin.security.permissions import RiskLevel
from digital_twin.voice.audio import AudioSource
from digital_twin.voice.transcriber import ScriptedTranscriber, TranscriptChunk
from digital_twin.voice.wake import WakeWordModule, _normalise

CONNECTORS = Path(__file__).resolve().parents[1] / "plugins" / "examples"


# ---------------------------------------------------------------------------
# Write-side connectors: risk classification and guards
# ---------------------------------------------------------------------------
def _load_connectors():
    registry = ActionRegistry()
    bus = EventBus()
    load_plugins(PluginsConfig(paths=[str(CONNECTORS)]), registry, None)
    return registry


def test_write_actions_are_dangerous():
    registry = _load_connectors()
    assert registry.get("email.send_email").risk is RiskLevel.DANGEROUS
    assert registry.get("calendar.create_event").risk is RiskLevel.DANGEROUS
    # read-side stays sensitive
    assert registry.get("email.check_email").risk is RiskLevel.SENSITIVE
    assert registry.get("calendar.upcoming_events").risk is RiskLevel.SENSITIVE


def test_send_email_validates_recipient_and_fields():
    registry = _load_connectors()
    validate = registry.get("email.send_email").validate
    with pytest.raises(ValueError, match="to"):
        validate({"subject": "x", "body": "y"})
    with pytest.raises(ValueError, match="to"):
        validate({"to": "not-an-email", "subject": "x", "body": "y"})
    with pytest.raises(ValueError, match="subject"):
        validate({"to": "a@b.com", "subject": "", "body": "y"})
    with pytest.raises(ValueError, match="body"):
        validate({"to": "a@b.com", "subject": "x", "body": ""})
    validate({"to": "a@b.com", "subject": "x", "body": "y"})  # ok


def test_send_email_recipient_allow_list(tmp_path):
    """A configured allow-list refuses off-domain recipients pre-gate."""
    directory = tmp_path / "email"
    directory.mkdir()
    (directory / "plugin.yaml").write_text(
        "name: email\nversion: '1'\nentry: plugin.py\n"
        "actions:\n  check_email: sensitive\n  send_email: dangerous\n"
        "config:\n  username: me@corp.com\n  smtp_host: smtp.corp.com\n"
        "  allowed_recipient_domains: [corp.com]\n")
    (directory / "plugin.py").write_text(
        (CONNECTORS / "email_imap" / "plugin.py").read_text())
    registry = ActionRegistry()
    load_plugins(PluginsConfig(paths=[str(tmp_path)]), registry, None)
    validate = registry.get("email.send_email").validate
    validate({"to": "colleague@corp.com", "subject": "hi", "body": "there"})
    with pytest.raises(ValueError, match="allowed_recipient_domains"):
        validate({"to": "stranger@other.com", "subject": "hi", "body": "x"})


def test_create_event_writes_ics(tmp_path):
    module_path = CONNECTORS / "calendar_ics" / "plugin.py"
    from importlib import util

    spec = util.spec_from_file_location("cal_direct", module_path)
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class API:
        config = {"ics_dir": str(tmp_path / "cals")}
        specs: dict = {}

        def register_action(self, action_spec):
            self.specs[action_spec.name] = action_spec

    api = API()
    api.specs = {}
    module.register(api)
    create = api.specs["create_event"]
    with pytest.raises(ValueError, match="start"):
        create.validate({"summary": "x", "start": "not-a-date"})
    detail = create.handler({"summary": "Launch review",
                             "start": "20260720T140000",
                             "duration_minutes": 45})
    assert "Launch review" in detail
    files = list((tmp_path / "cals").glob("*.ics"))
    assert len(files) == 1
    text = files[0].read_text()
    assert "SUMMARY:Launch review" in text and "BEGIN:VEVENT" in text


# ---------------------------------------------------------------------------
# Wake word detector
# ---------------------------------------------------------------------------
class _ScriptedAudio(AudioSource):
    """Emits a fixed number of identical PCM chunks, then blocks as silence."""

    name = "scripted-audio"

    def __init__(self, chunks: int = 3):
        self._left = chunks
        self.opened = False
        self.closed = False

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True

    def read(self, timeout_s: float):
        if self._left > 0:
            self._left -= 1
            return b"\x00\x00"
        time.sleep(timeout_s)
        return None


@pytest.fixture()
def bus():
    bus = EventBus()
    bus.start()
    yield bus
    bus.stop()


def test_normalise_strips_punctuation_and_case():
    assert _normalise("Hey, Twin!") == "hey twin"


def test_wake_word_triggers_voice_control(bus):
    transcriber = ScriptedTranscriber([
        TranscriptChunk(text="hey twin", final=True),
    ])
    module = WakeWordModule(
        VoiceConfig(wake_word="hey twin"),
        audio_source=_ScriptedAudio(chunks=2),
        transcriber=transcriber,
    )
    triggers = []
    bus.subscribe(Topics.VOICE_CONTROL, triggers.append)
    module.start(bus)
    try:
        deadline = time.time() + 3
        while not triggers and time.time() < deadline:
            time.sleep(0.02)
    finally:
        module.stop()
    assert triggers, "wake word did not fire"
    assert triggers[0].payload == {"command": "start", "reason": "wake_word"}


def test_wake_word_ignores_other_speech(bus):
    transcriber = ScriptedTranscriber([
        TranscriptChunk(text="what is the weather", final=True),
    ])
    module = WakeWordModule(
        VoiceConfig(wake_word="hey twin"),
        audio_source=_ScriptedAudio(chunks=2),
        transcriber=transcriber,
    )
    triggers = []
    bus.subscribe(Topics.VOICE_CONTROL, triggers.append)
    module.start(bus)
    try:
        time.sleep(0.6)
    finally:
        module.stop()
    assert triggers == []  # non-wake speech never triggers, never published


def test_wake_word_requires_a_phrase():
    with pytest.raises(ValueError, match="wake_word"):
        WakeWordModule(VoiceConfig(wake_word=""))


def test_wake_word_never_publishes_utterances(bus):
    transcriber = ScriptedTranscriber([
        TranscriptChunk(text="hey twin turn off the lights", final=True),
    ])
    module = WakeWordModule(
        VoiceConfig(wake_word="hey twin"),
        audio_source=_ScriptedAudio(chunks=2),
        transcriber=transcriber,
    )
    utterances = []
    bus.subscribe(Topics.VOICE, utterances.append)
    module.start(bus)
    try:
        time.sleep(0.5)
    finally:
        module.stop()
    assert utterances == []  # only the voice module publishes utterances


# ---------------------------------------------------------------------------
# Setup CLI
# ---------------------------------------------------------------------------
def test_setup_cli_creates_home(tmp_path, capsys):
    from digital_twin.setup_cli import main

    home = tmp_path / "dt"
    assert main(["--home", str(home)]) == 0
    assert (home / "config" / "default_config.yaml").is_file()
    for sub in ("data", "logs", "models"):
        assert (home / sub).is_dir()
    out = capsys.readouterr().out
    assert "DIGITAL_TWIN_HOME" in out


def test_setup_cli_is_idempotent_and_respects_force(tmp_path):
    from digital_twin.setup_cli import main

    home = tmp_path / "dt"
    main(["--home", str(home)])
    config = home / "config" / "default_config.yaml"
    config.write_text("# user edited\n")
    main(["--home", str(home)])                 # no --force
    assert config.read_text() == "# user edited\n"   # preserved
    main(["--home", str(home), "--force"])      # --force
    assert config.read_text() != "# user edited\n"   # overwritten
