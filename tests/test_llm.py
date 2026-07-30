"""Tests for the language-model interface and reply parsing (M7)."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from digital_twin.configuration.settings import LLMConfig
from digital_twin.reasoning.chat_reasoner import parse_model_reply
from digital_twin.reasoning.llm import (
    AnthropicModel,
    ChatMessage,
    GeminiModel,
    LLMError,
    OllamaModel,
    ScriptedModel,
    create_language_model,
)


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture()
def capture(monkeypatch):
    """Capture urlopen requests and return a canned Anthropic-style body."""
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["headers"] = {k.lower(): v for k, v in request.header_items()}
        seen["body"] = json.loads(request.data.decode("utf-8"))
        seen["timeout"] = timeout
        return _FakeResponse(json.dumps(seen.pop("reply_body")).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return seen


# ---------------------------------------------------------------------------
# Gemini backend (default provider)
# ---------------------------------------------------------------------------
def test_gemini_requires_env_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        GeminiModel(model="gemini-2.5-flash")


def test_gemini_request_shape_and_reply(monkeypatch, capture):
    monkeypatch.setenv("GEMINI_API_KEY", "g-test-123")
    capture["reply_body"] = {
        "candidates": [{"content": {"parts": [{"text": "hi there"}]}}]
    }
    model = GeminiModel(model="gemini-2.5-flash", timeout_s=9)
    out = model.complete(
        [ChatMessage("user", "hello"), ChatMessage("assistant", "hey"),
         ChatMessage("user", "how are you")],
        system="be brief", max_tokens=64, temperature=0.1,
    )
    assert out == "hi there"
    assert capture["url"] == (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.5-flash:generateContent")
    # Key travels via header, never the URL — can't leak into a raised/logged URL.
    assert capture["headers"]["x-goog-api-key"] == "g-test-123"
    assert "g-test-123" not in capture["url"]
    body = capture["body"]
    assert body["system_instruction"] == {"parts": [{"text": "be brief"}]}
    assert body["contents"] == [
        {"role": "user", "parts": [{"text": "hello"}]},
        {"role": "model", "parts": [{"text": "hey"}]},  # assistant -> model
        {"role": "user", "parts": [{"text": "how are you"}]},
    ]
    assert body["generationConfig"] == {"maxOutputTokens": 64, "temperature": 0.1}
    assert capture["timeout"] == 9


def test_gemini_blocked_prompt_raises_with_reason(monkeypatch, capture):
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    capture["reply_body"] = {
        "candidates": [],
        "promptFeedback": {"blockReason": "SAFETY"},
    }
    with pytest.raises(LLMError, match="SAFETY"):
        GeminiModel(model="gemini-2.5-flash").complete([ChatMessage("user", "x")])


def test_gemini_http_error_maps_to_llmerror(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")

    def boom(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 403, "forbidden", None, io.BytesIO(b"bad key")
        )

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(LLMError, match="HTTP 403"):
        GeminiModel(model="m").complete([ChatMessage("user", "x")])


# ---------------------------------------------------------------------------
# Anthropic backend
# ---------------------------------------------------------------------------
def test_anthropic_requires_env_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        AnthropicModel(model="claude-haiku-4-5")


def test_anthropic_request_shape_and_reply(monkeypatch, capture):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
    capture["reply_body"] = {
        "content": [{"type": "text", "text": '{"reply": "hi"}'}]
    }
    model = AnthropicModel(model="claude-haiku-4-5", timeout_s=7)
    out = model.complete(
        [ChatMessage("user", "hello")], system="be brief",
        max_tokens=64, temperature=0.1,
    )
    assert out == '{"reply": "hi"}'
    assert capture["url"] == "https://api.anthropic.com/v1/messages"
    assert capture["headers"]["x-api-key"] == "sk-test-123"
    assert capture["headers"]["anthropic-version"] == "2023-06-01"
    body = capture["body"]
    assert body["model"] == "claude-haiku-4-5"
    assert body["system"] == "be brief"
    assert body["messages"] == [{"role": "user", "content": "hello"}]
    assert body["max_tokens"] == 64 and capture["timeout"] == 7


def test_anthropic_http_error_maps_to_llmerror(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    def boom(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 401, "unauthorized", None, io.BytesIO(b"bad key")
        )

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(LLMError, match="HTTP 401"):
        AnthropicModel(model="m").complete([ChatMessage("user", "x")])


def test_anthropic_network_error_maps_to_llmerror(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda request, timeout=None: (_ for _ in ()).throw(
            urllib.error.URLError("refused")),
    )
    with pytest.raises(LLMError, match="Could not reach"):
        AnthropicModel(model="m").complete([ChatMessage("user", "x")])


# ---------------------------------------------------------------------------
# Ollama backend
# ---------------------------------------------------------------------------
def test_ollama_request_shape_and_reply(capture):
    capture["reply_body"] = {"message": {"content": "local hello"}}
    model = OllamaModel(model="llama3", base_url="http://localhost:11434/")
    out = model.complete([ChatMessage("user", "hi")], system="sys")
    assert out == "local hello"
    assert capture["url"] == "http://localhost:11434/api/chat"
    body = capture["body"]
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["messages"][1] == {"role": "user", "content": "hi"}
    assert body["stream"] is False


# ---------------------------------------------------------------------------
# Factory + scripted model
# ---------------------------------------------------------------------------
def test_factory_selects_provider(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert create_language_model(LLMConfig()).name == "gemini"
    assert create_language_model(
        LLMConfig(provider="anthropic", api_key_env="ANTHROPIC_API_KEY",
                  api_key_secret="anthropic_api_key")).name == "anthropic"
    assert create_language_model(LLMConfig(provider="ollama")).name == "ollama"


def test_scripted_model_records_and_exhausts():
    model = ScriptedModel(["one"])
    assert model.complete([ChatMessage("user", "q")], system="s") == "one"
    assert model.calls[0]["system"] == "s"
    with pytest.raises(LLMError):
        model.complete([ChatMessage("user", "q2")])


# ---------------------------------------------------------------------------
# Reply parsing
# ---------------------------------------------------------------------------
def test_parse_clean_json():
    decision = parse_model_reply(
        '{"reply": "ok", "intent": "next_slide", "remember": null,'
        ' "reasoning": "asked to advance"}'
    )
    assert decision == {"reply": "ok", "intent": "next_slide", "plan": None,
                        "remember": None, "reasoning": "asked to advance"}


def test_parse_json_in_markdown_fence():
    text = 'Sure!\n```json\n{"reply": "done", "intent": null}\n```'
    decision = parse_model_reply(text)
    assert decision["reply"] == "done" and decision["intent"] is None


def test_parse_json_with_surrounding_chatter():
    text = 'thinking... {"reply": "hi", "reasoning": "greeting"} hope that helps'
    decision = parse_model_reply(text)
    assert decision["reply"] == "hi" and decision["reasoning"] == "greeting"


def test_parse_garbage_degrades_to_plain_reply():
    decision = parse_model_reply("just plain prose, no json here")
    assert decision["reply"] == "just plain prose, no json here"
    assert decision["intent"] is None and decision["remember"] is None


def test_parse_wrong_types_sanitised():
    decision = parse_model_reply('{"reply": "x", "intent": 42, "remember": []}')
    assert decision["intent"] is None and decision["remember"] is None
