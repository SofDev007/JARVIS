"""Tests for M14 hardening odds and ends: LLM API keys via the secret
store (with environment fallback) and the wheel-safe asset resolver."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from digital_twin.configuration.settings import LLMConfig
from digital_twin.paths import asset_guidance, resolve_asset
from digital_twin.reasoning.llm import LLMError, create_language_model
from digital_twin.security.secrets import EncryptedFileSecretStore


# ---------------------------------------------------------------------------
# LLM keys: secret store first, environment fallback, never config
# ---------------------------------------------------------------------------
@pytest.fixture()
def store(tmp_path):
    return EncryptedFileSecretStore(tmp_path / "s.enc", tmp_path / "k.key")


def test_llm_key_from_secret_store(store, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    store.set("anthropic_api_key", "sk-from-store")
    model = create_language_model(
        LLMConfig(provider="anthropic", api_key_env="ANTHROPIC_API_KEY",
                  api_key_secret="anthropic_api_key"),
        secrets=store)
    assert model._api_key == "sk-from-store"


def test_llm_secret_beats_environment(store, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    store.set("anthropic_api_key", "sk-from-store")
    model = create_language_model(
        LLMConfig(provider="anthropic", api_key_env="ANTHROPIC_API_KEY",
                  api_key_secret="anthropic_api_key"),
        secrets=store)
    assert model._api_key == "sk-from-store"


def test_llm_falls_back_to_environment(store, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    model = create_language_model(
        LLMConfig(provider="anthropic", api_key_env="ANTHROPIC_API_KEY",
                  api_key_secret="anthropic_api_key"),
        secrets=store)  # store has no key
    assert model._api_key == "sk-from-env"


def test_llm_no_key_anywhere_mentions_the_cli(store, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMError, match="secrets_cli"):
        create_language_model(
            LLMConfig(provider="anthropic", api_key_env="ANTHROPIC_API_KEY",
                      api_key_secret="anthropic_api_key"),
            secrets=store)


# ---------------------------------------------------------------------------
# Same key-resolution discipline for the default Gemini backend
# ---------------------------------------------------------------------------
def test_gemini_key_from_secret_store(store, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    store.set("gemini_api_key", "g-from-store")
    model = create_language_model(LLMConfig(provider="gemini"), secrets=store)
    assert model._api_key == "g-from-store"


def test_gemini_secret_beats_environment(store, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-from-env")
    store.set("gemini_api_key", "g-from-store")
    model = create_language_model(LLMConfig(provider="gemini"), secrets=store)
    assert model._api_key == "g-from-store"


def test_gemini_falls_back_to_environment(store, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-from-env")
    model = create_language_model(LLMConfig(provider="gemini"), secrets=store)
    assert model._api_key == "g-from-env"


def test_gemini_no_key_anywhere_mentions_the_cli(store, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(LLMError, match="secrets_cli"):
        create_language_model(LLMConfig(provider="gemini"), secrets=store)


# ---------------------------------------------------------------------------
# Asset resolver
# ---------------------------------------------------------------------------
def test_resolver_finds_repo_assets_from_anywhere(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    resolved = resolve_asset("config/default_config.yaml")
    assert resolved.is_file()
    assert resolved.name == "default_config.yaml"


def test_resolver_prefers_cwd_then_home(tmp_path, monkeypatch):
    (tmp_path / "models").mkdir()
    local = tmp_path / "models" / "x.task"
    local.write_text("cwd wins")
    monkeypatch.chdir(tmp_path)
    assert resolve_asset("models/x.task") == tmp_path / "models" / "x.task"

    home = tmp_path / "dt-home"
    (home / "models").mkdir(parents=True)
    (home / "models" / "y.task").write_text("home")
    monkeypatch.setenv("DIGITAL_TWIN_HOME", str(home))
    assert resolve_asset("models/y.task") == home / "models" / "y.task"


def test_resolver_missing_returns_original_and_guides(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    ghost = "models/does_not_exist.task"
    assert resolve_asset(ghost) == Path(ghost)  # unchanged
    guidance = asset_guidance(ghost)
    assert "DIGITAL_TWIN_HOME" in guidance and "does_not_exist" in guidance
