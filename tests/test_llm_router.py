"""LLMRouter policy tests — fake backends, no HTTP."""

from __future__ import annotations

import pytest

from homelab_helper.llm.router import (
    BackendError,
    CapabilityTier,
    LLMRouter,
    PrivacyPolicy,
    RouterRefusal,
    TaskClass,
    privacy_from_env,
)


class FakeBackend:
    def __init__(
        self,
        name: str,
        *,
        local: bool,
        tier: CapabilityTier,
        up: bool = True,
        fail: bool = False,
        reply: str = "ok",
    ) -> None:
        self.name = name
        self.local = local
        self.tier = tier
        self.model = f"{name}-model"
        self._up = up
        self._fail = fail
        self._reply = reply
        self.calls: list[list[dict[str, str]]] = []

    async def available(self) -> tuple[bool, str | None]:
        return (True, None) if self._up else (False, "connection refused")

    async def complete(self, system: str, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        if self._fail:
            raise BackendError(self.name, "boom")
        return self._reply

    async def aclose(self) -> None:
        pass


def _msgs() -> list[dict[str, str]]:
    return [{"role": "user", "content": "hi"}]


async def test_prefer_local_picks_capable_local_over_cloud() -> None:
    local = FakeBackend("ollama", local=True, tier=CapabilityTier.SMALL, reply="local")
    cloud = FakeBackend("anthropic", local=False, tier=CapabilityTier.FRONTIER, reply="cloud")
    router = LLMRouter([local, cloud], PrivacyPolicy.PREFER_LOCAL)
    result = await router.complete(TaskClass.CHAT, "s", _msgs())
    assert result.backend == "ollama"
    assert result.local is True


async def test_prefer_local_falls_to_cloud_when_local_down() -> None:
    local = FakeBackend("ollama", local=True, tier=CapabilityTier.SMALL, up=False)
    cloud = FakeBackend("anthropic", local=False, tier=CapabilityTier.FRONTIER, reply="cloud")
    router = LLMRouter([local, cloud], PrivacyPolicy.PREFER_LOCAL)
    result = await router.complete(TaskClass.CHAT, "s", _msgs())
    assert result.backend == "anthropic"


async def test_failover_on_backend_error() -> None:
    flaky = FakeBackend("ollama", local=True, tier=CapabilityTier.SMALL, fail=True)
    cloud = FakeBackend("anthropic", local=False, tier=CapabilityTier.FRONTIER, reply="cloud")
    router = LLMRouter([flaky, cloud], PrivacyPolicy.PREFER_LOCAL)
    result = await router.complete(TaskClass.CHAT, "s", _msgs())
    assert result.backend == "anthropic"


async def test_open_policy_prefers_highest_tier() -> None:
    local = FakeBackend("ollama", local=True, tier=CapabilityTier.SMALL, reply="local")
    cloud = FakeBackend("anthropic", local=False, tier=CapabilityTier.FRONTIER, reply="cloud")
    router = LLMRouter([local, cloud], PrivacyPolicy.OPEN)
    result = await router.complete(TaskClass.CHAT, "s", _msgs())
    assert result.backend == "anthropic"


async def test_open_policy_local_wins_tier_tie() -> None:
    compat = FakeBackend("openai-compatible", local=True, tier=CapabilityTier.FRONTIER)
    cloud = FakeBackend("anthropic", local=False, tier=CapabilityTier.FRONTIER)
    router = LLMRouter([cloud, compat], PrivacyPolicy.OPEN)
    result = await router.complete(TaskClass.CHAT, "s", _msgs())
    assert result.backend == "openai-compatible"


# ---------------------------------------------------------------------------
# refusals — AC5 and the no-silent-downgrade rule
# ---------------------------------------------------------------------------


async def test_strict_local_never_uses_cloud() -> None:
    """AC5: strict-local + a task needing more than local offers → clean refusal."""
    local = FakeBackend("ollama", local=True, tier=CapabilityTier.SMALL)
    cloud = FakeBackend("anthropic", local=False, tier=CapabilityTier.FRONTIER, reply="cloud")
    router = LLMRouter([local, cloud], PrivacyPolicy.STRICT_LOCAL)
    with pytest.raises(RouterRefusal) as excinfo:
        await router.complete(TaskClass.PLANNING, "s", _msgs())
    msg = str(excinfo.value)
    assert "strict-local" in msg
    assert "MID" in msg
    assert "excluded by strict-local" in msg
    assert "Options:" in msg
    assert cloud.calls == []  # the cloud backend was never even tried


async def test_no_silent_downgrade() -> None:
    """A lower-tier backend must not quietly serve a higher-tier task."""
    small = FakeBackend("ollama", local=True, tier=CapabilityTier.SMALL, reply="wrong")
    router = LLMRouter([small], PrivacyPolicy.OPEN)
    with pytest.raises(RouterRefusal):
        await router.complete(TaskClass.PLANNING, "s", _msgs())
    assert small.calls == []


async def test_strict_local_serves_capable_local() -> None:
    local = FakeBackend("ollama", local=True, tier=CapabilityTier.MID, reply="local")
    router = LLMRouter([local], PrivacyPolicy.STRICT_LOCAL)
    result = await router.complete(TaskClass.PLANNING, "s", _msgs())
    assert result.backend == "ollama"


async def test_refusal_cites_unavailability() -> None:
    down = FakeBackend("ollama", local=True, tier=CapabilityTier.SMALL, up=False)
    router = LLMRouter([down], PrivacyPolicy.STRICT_LOCAL)
    with pytest.raises(RouterRefusal) as excinfo:
        await router.complete(TaskClass.CHAT, "s", _msgs())
    assert "unavailable" in str(excinfo.value)
    assert "connection refused" in str(excinfo.value)


async def test_narration_accepts_tiny() -> None:
    tiny = FakeBackend("ollama", local=True, tier=CapabilityTier.TINY, reply="prose")
    router = LLMRouter([tiny], PrivacyPolicy.PREFER_LOCAL)
    result = await router.complete(TaskClass.NARRATION, "s", _msgs())
    assert result.text == "prose"


# ---------------------------------------------------------------------------
# env parsing
# ---------------------------------------------------------------------------


def test_privacy_from_env_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMELAB_HELPER_LLM_PRIVACY", raising=False)
    assert privacy_from_env() == PrivacyPolicy.PREFER_LOCAL


def test_privacy_from_env_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_HELPER_LLM_PRIVACY", "strict-local")
    assert privacy_from_env() == PrivacyPolicy.STRICT_LOCAL


def test_privacy_from_env_rejects_junk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_HELPER_LLM_PRIVACY", "cloud-please")
    with pytest.raises(ValueError, match="cloud-please"):
        privacy_from_env()
