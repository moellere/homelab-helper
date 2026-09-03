"""LLM backends — Ollama (local default) and BYOK cloud, one interface.

Every backend answers two questions: "are you reachable right now?"
(:meth:`available`) and "complete this conversation" (:meth:`complete`). The
router owns *which* backend gets asked; backends never make policy decisions.

- :class:`OllamaBackend` — the default. Pre-configured at ``localhost:11434``
  even with nothing set, so the first-run experience is "install Ollama and it
  works"; availability probing is what discovers whether it's actually there
  and has the model pulled.
- :class:`AnthropicBackend` / :class:`OpenAIBackend` — BYOK cloud; only built
  when a key is configured. Marked non-local so ``strict-local`` policy can
  exclude them wholesale.
- :class:`OpenAICompatibleBackend` — vLLM / llama.cpp server / LM Studio /
  Open WebUI. Speaks the OpenAI chat API but is *local*: it only exists when
  the operator points ``HOMELAB_HELPER_OPENAI_COMPAT_BASE_URL`` at their own
  box, so it counts toward strict-local.

A backend's capability tier is declared, not benchmarked: cloud frontier
models are ``FRONTIER``; local backends default to ``SMALL`` and the operator
can raise/lower via ``*_TIER`` env vars ("my 70B deserves MID"). Tier honesty
matters because the router refuses rather than silently downgrading.

Tests inject ``httpx.AsyncClient`` with ``MockTransport`` — no live model.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from homelab_helper.llm.router import BackendError, CapabilityTier
from homelab_helper.secrets import secret_from_env

_HTTP_ERROR_THRESHOLD = 400

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "llama3.2"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"
DEFAULT_OPENAI_MODEL = "gpt-5.2"
_ANTHROPIC_API = "https://api.anthropic.com"
_OPENAI_API = "https://api.openai.com/v1"
_ANTHROPIC_VERSION = "2023-06-01"
_MAX_TOKENS = 2048


def _tier_from_env(var: str, default: CapabilityTier) -> CapabilityTier:
    raw = os.environ.get(var)
    if not raw:
        return default
    try:
        return CapabilityTier[raw.strip().upper()]
    except KeyError as exc:
        names = ", ".join(t.name.lower() for t in CapabilityTier)
        raise ValueError(f"{var}={raw!r} is not a capability tier (one of: {names})") from exc


class _HTTPBackend:
    """Shared client plumbing: lazy build, injection, owner-closes semantics."""

    name: str = "base"
    local: bool = False
    tier: CapabilityTier = CapabilityTier.SMALL
    model: str = ""

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None

    def _build_client(self) -> httpx.AsyncClient:
        raise NotImplementedError

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def _post_json(self, path: str, body: dict[str, Any]) -> Any:
        try:
            response = await self.client.post(path, json=body)
        except httpx.HTTPError as exc:
            raise BackendError(self.name, f"request failed: {exc}") from exc
        if response.status_code >= _HTTP_ERROR_THRESHOLD:
            detail = response.text.strip()[:300] or response.reason_phrase
            raise BackendError(self.name, f"HTTP {response.status_code}: {detail}")
        return response.json()

    async def available(self) -> tuple[bool, str | None]:
        raise NotImplementedError

    async def complete(self, system: str, messages: list[dict[str, str]]) -> str:
        raise NotImplementedError


class OllamaBackend(_HTTPBackend):
    """Local Ollama over its native ``/api/chat``."""

    name = "ollama"
    local = True

    def __init__(
        self,
        url: str = DEFAULT_OLLAMA_URL,
        model: str = DEFAULT_OLLAMA_MODEL,
        tier: CapabilityTier = CapabilityTier.SMALL,
        *,
        timeout_s: float = 120.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(client=client)
        self.url = url.rstrip("/")
        self.model = model
        self.tier = tier
        self.timeout_s = timeout_s

    @classmethod
    def from_env(cls) -> OllamaBackend:
        return cls(
            url=os.environ.get("HOMELAB_HELPER_OLLAMA_URL") or DEFAULT_OLLAMA_URL,
            model=os.environ.get("HOMELAB_HELPER_OLLAMA_MODEL") or DEFAULT_OLLAMA_MODEL,
            tier=_tier_from_env("HOMELAB_HELPER_OLLAMA_TIER", CapabilityTier.SMALL),
        )

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.url, timeout=self.timeout_s)

    async def available(self) -> tuple[bool, str | None]:
        """Reachable, and the configured model is pulled."""
        try:
            await self._post_json("/api/show", {"model": self.model})
        except BackendError as exc:
            if "HTTP 404" in exc.detail:
                return False, f"ollama is running but model {self.model!r} is not pulled"
            return False, str(exc)
        return True, None

    async def complete(self, system: str, messages: list[dict[str, str]]) -> str:
        payload = await self._post_json(
            "/api/chat",
            {
                "model": self.model,
                "stream": False,
                "messages": [{"role": "system", "content": system}, *messages],
            },
        )
        content = (
            (payload.get("message") or {}).get("content") if isinstance(payload, dict) else None
        )
        if not isinstance(content, str):
            raise BackendError(self.name, "malformed response: no message.content")
        return content


class AnthropicBackend(_HTTPBackend):
    """Anthropic Messages API (BYOK)."""

    name = "anthropic"
    local = False
    tier = CapabilityTier.FRONTIER

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_ANTHROPIC_MODEL,
        *,
        timeout_s: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(client=client)
        self._api_key = api_key
        self.model = model
        self.timeout_s = timeout_s

    @classmethod
    def from_env(cls) -> AnthropicBackend | None:
        key = secret_from_env("HOMELAB_HELPER_ANTHROPIC_API_KEY") or secret_from_env(
            "ANTHROPIC_API_KEY"
        )
        if not key:
            return None
        return cls(
            api_key=key,
            model=os.environ.get("HOMELAB_HELPER_ANTHROPIC_MODEL") or DEFAULT_ANTHROPIC_MODEL,
        )

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=_ANTHROPIC_API,
            headers={"x-api-key": self._api_key, "anthropic-version": _ANTHROPIC_VERSION},
            timeout=self.timeout_s,
        )

    async def available(self) -> tuple[bool, str | None]:
        """Key present ⇒ assumed available; a real probe would spend tokens."""
        return True, None

    async def complete(self, system: str, messages: list[dict[str, str]]) -> str:
        payload = await self._post_json(
            "/v1/messages",
            {
                "model": self.model,
                "max_tokens": _MAX_TOKENS,
                "system": system,
                "messages": messages,
            },
        )
        blocks = payload.get("content") if isinstance(payload, dict) else None
        if isinstance(blocks, list):
            text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
            if text:
                return text
        raise BackendError(self.name, "malformed response: no text content")


class OpenAIBackend(_HTTPBackend):
    """OpenAI chat-completions API (BYOK)."""

    name = "openai"
    local = False
    tier = CapabilityTier.FRONTIER

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_OPENAI_MODEL,
        base_url: str = _OPENAI_API,
        *,
        timeout_s: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(client=client)
        self._api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    @classmethod
    def from_env(cls) -> OpenAIBackend | None:
        key = secret_from_env("HOMELAB_HELPER_OPENAI_API_KEY")
        if not key:
            return None
        return cls(
            api_key=key,
            model=os.environ.get("HOMELAB_HELPER_OPENAI_MODEL") or DEFAULT_OPENAI_MODEL,
        )

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=self.timeout_s,
        )

    async def available(self) -> tuple[bool, str | None]:
        return True, None

    async def complete(self, system: str, messages: list[dict[str, str]]) -> str:
        payload = await self._post_json(
            "/chat/completions",
            {
                "model": self.model,
                "messages": [{"role": "system", "content": system}, *messages],
            },
        )
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            content = None
        if not isinstance(content, str):
            raise BackendError(self.name, "malformed response: no choices[0].message.content")
        return content


class OpenAICompatibleBackend(OpenAIBackend):
    """A local OpenAI-compatible server (vLLM, llama.cpp, LM Studio, ...)."""

    name = "openai-compatible"
    local = True

    @classmethod
    def from_env(cls) -> OpenAICompatibleBackend | None:
        base = os.environ.get("HOMELAB_HELPER_OPENAI_COMPAT_BASE_URL")
        if not base:
            return None
        backend = cls(
            api_key=secret_from_env("HOMELAB_HELPER_OPENAI_COMPAT_API_KEY") or "none",
            model=os.environ.get("HOMELAB_HELPER_OPENAI_COMPAT_MODEL") or "default",
            base_url=base,
        )
        backend.tier = _tier_from_env("HOMELAB_HELPER_OPENAI_COMPAT_TIER", CapabilityTier.SMALL)
        return backend


def backends_from_env() -> list[_HTTPBackend]:
    """All configured backends, local first. Ollama is always present (default
    URL) — availability probing, not configuration, decides whether it answers."""
    out: list[_HTTPBackend] = [OllamaBackend.from_env()]
    compat = OpenAICompatibleBackend.from_env()
    if compat is not None:
        out.append(compat)
    anthropic = AnthropicBackend.from_env()
    if anthropic is not None:
        out.append(anthropic)
    openai = OpenAIBackend.from_env()
    if openai is not None:
        out.append(openai)
    return out


__all__ = [
    "DEFAULT_ANTHROPIC_MODEL",
    "DEFAULT_OLLAMA_MODEL",
    "DEFAULT_OLLAMA_URL",
    "DEFAULT_OPENAI_MODEL",
    "AnthropicBackend",
    "BackendError",
    "OllamaBackend",
    "OpenAIBackend",
    "OpenAICompatibleBackend",
    "backends_from_env",
]
