"""Replaceable language-model interface.

The reasoning layer speaks to *a* language model, never to a vendor: one
small interface (:class:`LanguageModel`), stdlib-only HTTP, and a factory
that picks the configured backend. Swapping models is a config change —
the spec's "replaceable with different AI models" requirement taken
literally.

Backends:

* **gemini** — Google's Gemini API (default: it has a genuine free tier,
  no credit card required). Key resolution order matches Anthropic's
  (M14): secret store first, then the environment variable
  (``GEMINI_API_KEY`` by default), never config files. Sent via the
  ``x-goog-api-key`` header — never a URL query parameter, so it can't
  leak into a logged or raised URL.
* **anthropic** — the Claude API. The key comes **only** from an
  environment variable (configurable name, ``ANTHROPIC_API_KEY`` by
  default); it is never read from, written to, or logged via config
  files. Missing key → loud startup failure with guidance.
* **ollama** — a local model server (``http://localhost:11434``); no key,
  fully offline.
* **scripted** — deterministic canned responses for tests and demos; it
  records every request it receives.

Failures raise :class:`LLMError` with actionable messages; callers decide
how to degrade (the chat reasoner answers apologetically, never crashes).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """The language model could not produce a completion."""


@dataclass(frozen=True)
class ChatMessage:
    """One turn of a conversation."""

    role: str  # "user" | "assistant"
    content: str


class LanguageModel(ABC):
    """Minimal completion interface the reasoning layer depends on."""

    name: str = "abstract"

    @abstractmethod
    def complete(
        self,
        messages: list[ChatMessage],
        system: str = "",
        max_tokens: int = 512,
        temperature: float = 0.3,
    ) -> str:
        """Return the assistant's reply text for the conversation so far."""


def _post_json(url: str, body: dict, headers: dict, timeout_s: float) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:300]
        except Exception:
            pass
        raise LLMError(f"{url} returned HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LLMError(f"Could not reach {url}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise LLMError(f"{url} returned invalid JSON") from exc


class AnthropicModel(LanguageModel):
    """Claude via the Anthropic Messages API.

    Key resolution order (M14): an explicit ``api_key`` (the kernel
    resolves it from the **secret store** by name), then the environment
    variable. Keys are never read from config files.
    """

    name = "anthropic"
    _ENDPOINT = "https://api.anthropic.com/v1/messages"
    _VERSION = "2023-06-01"

    def __init__(self, model: str, api_key_env: str = "ANTHROPIC_API_KEY",
                 timeout_s: float = 30.0, api_key: str | None = None):
        api_key = (api_key or "").strip() or os.environ.get(
            api_key_env, "").strip()
        if not api_key:
            raise LLMError(
                "No API key: store one with `python -m "
                "digital_twin.security.secrets_cli set anthropic_api_key`, "
                f"or set the {api_key_env} environment variable (keys are "
                "never read from config files), or switch llm.provider to "
                "'ollama' for a local model."
            )
        self._api_key = api_key
        self._model = model
        self._timeout_s = timeout_s

    def complete(self, messages, system="", max_tokens=512, temperature=0.3) -> str:
        body: dict = {
            "model": self._model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in messages
            ],
        }
        if system:
            body["system"] = system
        data = _post_json(
            self._ENDPOINT,
            body,
            headers={"x-api-key": self._api_key, "anthropic-version": self._VERSION},
            timeout_s=self._timeout_s,
        )
        try:
            parts = [
                block["text"] for block in data["content"]
                if block.get("type") == "text"
            ]
            return "\n".join(parts).strip()
        except (KeyError, TypeError) as exc:
            raise LLMError("Unexpected Anthropic response shape") from exc


def _resolve_api_key(secrets, secret_name: str, env_name: str) -> str:
    """Secret store first, environment variable fallback, config never."""
    if secrets is not None and secret_name:
        try:
            value = secrets.get(secret_name)
            if value:
                logger.info("LLM API key resolved from the secret store "
                            "('%s')", secret_name)
                return value
        except Exception:
            pass  # fall through to the environment
    return os.environ.get(env_name, "").strip()


class GeminiModel(LanguageModel):
    """Google Gemini via the generateContent API.

    Key resolution matches Anthropic's (M14): an explicit ``api_key`` (the
    kernel resolves it from the **secret store** by name), then the
    environment variable. Keys are never read from config files, and the
    key is sent via the ``x-goog-api-key`` header — never a URL query
    parameter — so it can't leak into a logged or raised URL.
    """

    name = "gemini"
    _ENDPOINT_TEMPLATE = (
        "https://generativelanguage.googleapis.com/v1beta/models/{model}"
        ":generateContent"
    )

    def __init__(self, model: str, api_key_env: str = "GEMINI_API_KEY",
                 timeout_s: float = 30.0, api_key: str | None = None):
        api_key = (api_key or "").strip() or os.environ.get(
            api_key_env, "").strip()
        if not api_key:
            raise LLMError(
                "No API key: store one with `python -m "
                "digital_twin.security.secrets_cli set gemini_api_key`, "
                f"or set the {api_key_env} environment variable (keys are "
                "never read from config files; get a free key at "
                "https://aistudio.google.com/apikey), or switch "
                "llm.provider to 'ollama' for a local model."
            )
        self._api_key = api_key
        self._model = model
        self._timeout_s = timeout_s

    def complete(self, messages, system="", max_tokens=512, temperature=0.3) -> str:
        contents = [
            {
                "role": "model" if message.role == "assistant" else "user",
                "parts": [{"text": message.content}],
            }
            for message in messages
        ]
        body: dict = {
            "contents": contents,
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": temperature,
            },
        }
        if system:
            body["system_instruction"] = {"parts": [{"text": system}]}
        url = self._ENDPOINT_TEMPLATE.format(model=self._model)
        data = _post_json(
            url, body,
            headers={"x-goog-api-key": self._api_key},
            timeout_s=self._timeout_s,
        )
        try:
            candidates = data.get("candidates") or []
            if not candidates:
                block_reason = (data.get("promptFeedback", {})
                                .get("blockReason"))
                raise LLMError(
                    "Gemini returned no candidates"
                    + (f" (blocked: {block_reason})" if block_reason else ""))
            parts = candidates[0]["content"]["parts"]
            text = "".join(part.get("text", "") for part in parts)
            return text.strip()
        except (KeyError, TypeError, IndexError) as exc:
            raise LLMError("Unexpected Gemini response shape") from exc


class OllamaModel(LanguageModel):
    """A local model behind the Ollama chat API — no key, fully offline."""

    name = "ollama"

    def __init__(self, model: str, base_url: str = "http://localhost:11434",
                 timeout_s: float = 30.0):
        self._model = model
        self._url = base_url.rstrip("/") + "/api/chat"
        self._timeout_s = timeout_s

    def complete(self, messages, system="", max_tokens=512, temperature=0.3) -> str:
        chat: list[dict] = []
        if system:
            chat.append({"role": "system", "content": system})
        chat.extend(
            {"role": message.role, "content": message.content}
            for message in messages
        )
        data = _post_json(
            self._url,
            {
                "model": self._model,
                "messages": chat,
                "stream": False,
                "options": {"temperature": temperature, "num_predict": max_tokens},
            },
            headers={},
            timeout_s=self._timeout_s,
        )
        try:
            return str(data["message"]["content"]).strip()
        except (KeyError, TypeError) as exc:
            raise LLMError("Unexpected Ollama response shape") from exc


class ScriptedModel(LanguageModel):
    """Deterministic model for tests/demos: pops canned replies, records calls."""

    name = "scripted"

    def __init__(self, replies: list[str] | None = None):
        self._replies = list(replies or [])
        self.calls: list[dict] = []

    def complete(self, messages, system="", max_tokens=512, temperature=0.3) -> str:
        self.calls.append({
            "messages": [(message.role, message.content) for message in messages],
            "system": system,
        })
        if not self._replies:
            raise LLMError("scripted model exhausted")
        return self._replies.pop(0)


def create_language_model(config, secrets=None) -> LanguageModel:
    """Build the configured backend from an ``LLMConfig``.

    ``secrets`` is an optional SecretStore; when it holds
    ``config.api_key_secret``, that key wins over the environment.
    """
    provider = config.provider
    if provider == "gemini":
        api_key = _resolve_api_key(secrets, config.api_key_secret,
                                   config.api_key_env)
        return GeminiModel(
            model=config.model,
            api_key_env=config.api_key_env,
            timeout_s=config.timeout_s,
            api_key=api_key,
        )
    if provider == "anthropic":
        api_key = _resolve_api_key(secrets, config.api_key_secret,
                                   config.api_key_env)
        return AnthropicModel(
            model=config.model,
            api_key_env=config.api_key_env,
            timeout_s=config.timeout_s,
            api_key=api_key,
        )
    if provider == "ollama":
        return OllamaModel(
            model=config.model,
            base_url=config.ollama_url,
            timeout_s=config.timeout_s,
        )
    raise LLMError(f"Unknown llm.provider {provider!r}")
