"""Tests for typed configuration loading, overrides and validation."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from digital_twin.configuration.settings import AppConfig, load_config


def test_defaults_load_without_file():
    config = load_config(None)
    assert isinstance(config, AppConfig)
    assert config.gesture.enabled is True
    assert config.intent.default_context == "desktop"
    assert config.bus.max_queue_size == 1024


def test_yaml_overrides_subset(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "gesture:\n"
        "  camera_index: 2\n"
        "  repeat_interval_s: 1.5\n"
        "intent:\n"
        "  default_context: media\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.gesture.camera_index == 2
    assert config.gesture.repeat_interval_s == 1.5
    assert config.intent.default_context == "media"
    # Untouched values keep their defaults.
    assert config.gesture.frame_width == 1280
    assert config.bus.max_queue_size == 1024


def test_intent_mappings_replaced_whole(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "intent:\n"
        "  mappings:\n"
        "    media:\n"
        "      peace: play_pause\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.intent.mappings == {"media": {"peace": "play_pause"}}


def test_unknown_key_warns_but_does_not_crash(tmp_path, caplog):
    path = tmp_path / "config.yaml"
    path.write_text("gesture:\n  camera_indx: 2\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        config = load_config(path)
    assert config.gesture.camera_index == 0  # typo ignored, default kept
    assert any("camera_indx" in record.message for record in caplog.records)


def test_missing_and_malformed_files_fall_back(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        assert load_config(tmp_path / "nope.yaml") == AppConfig()
    bad = tmp_path / "bad.yaml"
    bad.write_text("gesture: [unclosed\n", encoding="utf-8")
    with caplog.at_level(logging.ERROR):
        assert load_config(bad) == AppConfig()


@pytest.mark.parametrize(
    "yaml_text",
    [
        "bus:\n  max_queue_size: 0\n",
        "gesture:\n  detection_confidence: 1.5\n",
        "gesture:\n  max_hands: 9\n",
        "gesture:\n  min_votes: 99\n",
        "gesture:\n  poll_interval_s: 0\n",
        "gesture:\n  repeat_interval_s: -1\n",
        "intent:\n  default_context: ''\n",
        "intent:\n  mappings:\n    media: not_a_table\n",
        "intent:\n  mappings:\n    media:\n      peace: 7\n",
        "security:\n  risk_defaults:\n    safe: maybe\n",
        "security:\n  risk_defaults:\n    scary: deny\n",
        "security:\n  permissions:\n    open_url: shrug\n",
        "security:\n  confirmation: telepathy\n",
        "security:\n  confirmation_timeout_s: 0\n",
        "security:\n  audit_max_bytes: 10\n",
        "automation:\n  max_queue_size: 0\n",
        "automation:\n  action_timeout_s: 0\n",
        "automation:\n  applications:\n    editor: ''\n",
        "automation:\n  intent_bindings:\n    like: not_a_table\n",
        "automation:\n  intent_bindings:\n    like: {params: {}}\n",
        "llm:\n  provider: skynet\n",
        "llm:\n  model: ''\n",
        "llm:\n  temperature: 1.5\n",
        "llm:\n  max_tokens: 4\n",
        "llm:\n  history_turns: 0\n",
        "llm:\n  memory_results: -1\n",
    ],
)
def test_invalid_values_raise(tmp_path, yaml_text):
    path = tmp_path / "config.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


def test_shipped_default_config_matches_dataclass_defaults():
    shipped = Path(__file__).parent.parent / "config" / "default_config.yaml"
    assert shipped.exists()
    assert load_config(shipped) == AppConfig()
