"""LLM backend tests against httpx.MockTransport — no live model."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

from homelab_helper.llm.backends import (
    AnthropicBackend,
    OllamaBackend,
    OpenAIBackend,
    OpenAICompatibleBackend,
    backends_from_env,
)
from homelab_helper.llm.router import BackendError, CapabilityTier

_MSGS = [{"role": "user", "content": "hi"}]


def _client(base: str, handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base, transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------


async def test_ollama_complete_shape() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "hello"}})

    b = OllamaBackend(client=_client("http://o", handler))
    try:
        out = await b.complete("sys", _MSGS)
    finally:
        await b.aclose()
    assert out == "hello"
    assert seen["stream"] is False
    assert seen["messages"][0] == {"role": "system", "content": "sys"}
    assert seen["messages"][1]["role"] == "user"


async def test_ollama_available_model_not_pulled() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "model not found"})

    b = OllamaBackend(client=_client("http://o", handler))
    try:
        ok, why = await b.available()
    finally:
        await b.aclose()
    assert ok is False
    assert "not pulled" in (why or "")


async def test_ollama_malformed_response_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    b = OllamaBackend(client=_client("http://o", handler))
    try:
        with pytest.raises(BackendError, match="malformed"):
            await b.complete("sys", _MSGS)
    finally:
        await b.aclose()


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


async def test_anthropic_complete_shape() -> None:
    seen: dict = {}
    headers: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        headers.update(request.headers)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "claude says"}]})

    client = httpx.AsyncClient(
        base_url="https://api.anthropic.com",
        headers={"x-api-key": "k", "anthropic-version": "2023-06-01"},
        transport=httpx.MockTransport(handler),
    )
    b = AnthropicBackend(api_key="k", client=client)
    try:
        out = await b.complete("sys", _MSGS)
    finally:
        await b.aclose()
    assert out == "claude says"
    assert seen["system"] == "sys"  # system is a top-level field, not a message
    assert seen["messages"] == _MSGS
    assert seen["max_tokens"] > 0
    assert headers["x-api-key"] == "k"


async def test_anthropic_http_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid x-api-key"}})

    b = AnthropicBackend(api_key="bad", client=_client("https://api.anthropic.com", handler))
    try:
        with pytest.raises(BackendError, match="401"):
            await b.complete("sys", _MSGS)
    finally:
        await b.aclose()


# ---------------------------------------------------------------------------
# OpenAI + compatible
# ---------------------------------------------------------------------------


async def test_openai_complete_shape() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "gpt says"}}]}
        )

    b = OpenAIBackend(api_key="k", client=_client("https://api.openai.com/v1", handler))
    try:
        out = await b.complete("sys", _MSGS)
    finally:
        await b.aclose()
    assert out == "gpt says"
    assert seen["messages"][0] == {"role": "system", "content": "sys"}


def test_compat_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_HELPER_OPENAI_COMPAT_BASE_URL", "http://vllm.lan:8000/v1")
    monkeypatch.setenv("HOMELAB_HELPER_OPENAI_COMPAT_MODEL", "qwen")
    monkeypatch.setenv("HOMELAB_HELPER_OPENAI_COMPAT_TIER", "mid")
    b = OpenAICompatibleBackend.from_env()
    assert b is not None
    assert b.local is True
    assert b.tier == CapabilityTier.MID
    assert b.model == "qwen"


def test_compat_absent_without_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMELAB_HELPER_OPENAI_COMPAT_BASE_URL", raising=False)
    assert OpenAICompatibleBackend.from_env() is None


def test_bad_tier_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_HELPER_OLLAMA_TIER", "enormous")
    with pytest.raises(ValueError, match="enormous"):
        OllamaBackend.from_env()


# ---------------------------------------------------------------------------
# roster assembly
# ---------------------------------------------------------------------------


def test_backends_from_env_default_is_ollama_only(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "HOMELAB_HELPER_ANTHROPIC_API_KEY",
        "ANTHROPIC_API_KEY",
        "HOMELAB_HELPER_OPENAI_API_KEY",
        "HOMELAB_HELPER_OPENAI_COMPAT_BASE_URL",
        "HOMELAB_HELPER_OLLAMA_TIER",
    ):
        monkeypatch.delenv(var, raising=False)
    roster = backends_from_env()
    assert [b.name for b in roster] == ["ollama"]


def test_backends_from_env_adds_byok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMELAB_HELPER_OLLAMA_TIER", raising=False)
    monkeypatch.setenv("HOMELAB_HELPER_ANTHROPIC_API_KEY", "k1")
    monkeypatch.setenv("HOMELAB_HELPER_OPENAI_API_KEY", "k2")
    monkeypatch.setenv("HOMELAB_HELPER_OPENAI_COMPAT_BASE_URL", "http://lms.lan:1234/v1")
    names = [b.name for b in backends_from_env()]
    assert names == ["ollama", "openai-compatible", "anthropic", "openai"]
