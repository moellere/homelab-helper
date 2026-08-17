"""LLMRouter — the single entry point for every LLM call (Phase 4).

Locked decision (``docs/architecture.md``): inputs are a task class, a minimum
capability tier, and the privacy policy; output is a completion from the best
available backend. Two hard rules fall out of it:

1. **Never silently send to cloud when policy says local-only.** Under
   ``strict-local`` a non-local backend is not merely deprioritized — it is
   not a candidate at all.
2. **Never silently downgrade quality.** If no candidate meets the task's
   tier, the router raises :class:`RouterRefusal` with a message that names
   the task, the tier it needs, the policy in force, what each backend offered,
   and the operator's options — it does not quietly use a weaker model.

Selection order within policy: ``prefer-local`` (default) tries capable local
backends before capable cloud ones; ``open`` tries the most capable first
(local winning ties); ``strict-local`` considers local only. Unavailable or
erroring backends fall through to the next candidate — failover is fine,
downgrade is not.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homelab_helper.llm.backends import _HTTPBackend


class CapabilityTier(IntEnum):
    """Model capability ladder; comparisons are meaningful (MID < FRONTIER)."""

    TINY = 1
    SMALL = 2
    MID = 3
    FRONTIER = 4


class TaskClass(StrEnum):
    NARRATION = "narration"
    CHAT = "chat"
    DISCOVERY = "discovery"
    PLANNING = "planning"


# Per architecture: Narrator is Tiny/Small; chat + discovery Small/Mid;
# planning Mid/Frontier. The value here is the *minimum* tier that may serve.
MIN_TIER: dict[TaskClass, CapabilityTier] = {
    TaskClass.NARRATION: CapabilityTier.TINY,
    TaskClass.CHAT: CapabilityTier.SMALL,
    TaskClass.DISCOVERY: CapabilityTier.SMALL,
    TaskClass.PLANNING: CapabilityTier.MID,
}


class PrivacyPolicy(StrEnum):
    STRICT_LOCAL = "strict-local"
    PREFER_LOCAL = "prefer-local"
    OPEN = "open"


PRIVACY_ENV_VAR = "HOMELAB_HELPER_LLM_PRIVACY"
DEFAULT_PRIVACY = PrivacyPolicy.PREFER_LOCAL


class BackendError(RuntimeError):
    """A backend request failed (network, HTTP error, or malformed payload)."""

    def __init__(self, backend: str, detail: str) -> None:
        super().__init__(f"{backend}: {detail}")
        self.backend = backend
        self.detail = detail


class RouterRefusal(RuntimeError):
    """No backend satisfies (policy, tier) — refuse loudly, never downgrade."""

    def __init__(
        self, task: TaskClass, needed: CapabilityTier, policy: PrivacyPolicy, reasons: list[str]
    ) -> None:
        self.task = task
        self.needed = needed
        self.policy = policy
        self.reasons = reasons
        lines = "; ".join(reasons) or "no backends configured"
        super().__init__(
            f"task {task.value!r} requires {needed.name} capability or better; "
            f"privacy policy is {policy.value!r}. Backends: {lines}. "
            f"Options: use a smaller task, change {PRIVACY_ENV_VAR}, or configure a "
            "more capable backend (e.g. pull a larger Ollama model and set "
            "HOMELAB_HELPER_OLLAMA_TIER)."
        )


@dataclass(frozen=True)
class RouterResult:
    text: str
    backend: str
    model: str
    tier: CapabilityTier
    local: bool


def privacy_from_env() -> PrivacyPolicy:
    raw = os.environ.get(PRIVACY_ENV_VAR)
    if not raw:
        return DEFAULT_PRIVACY
    try:
        return PrivacyPolicy(raw.strip().lower())
    except ValueError as exc:
        allowed = ", ".join(p.value for p in PrivacyPolicy)
        raise ValueError(f"{PRIVACY_ENV_VAR}={raw!r} is not one of: {allowed}") from exc


class LLMRouter:
    """Policy-ordered failover across configured backends."""

    def __init__(
        self, backends: list[_HTTPBackend], policy: PrivacyPolicy = DEFAULT_PRIVACY
    ) -> None:
        self.backends = backends
        self.policy = policy

    def _candidates(self, needed: CapabilityTier) -> tuple[list[_HTTPBackend], list[str]]:
        """Backends eligible for this tier under the policy, in try-order,
        plus human-readable reasons for every exclusion (refusals cite them)."""
        excluded: list[str] = []
        eligible: list[_HTTPBackend] = []
        for b in self.backends:
            label = f"{b.name} ({b.model}, {b.tier.name.lower()})"
            if self.policy == PrivacyPolicy.STRICT_LOCAL and not b.local:
                excluded.append(f"{label}: cloud, excluded by strict-local")
            elif b.tier < needed:
                excluded.append(f"{label}: below {needed.name} tier")
            else:
                eligible.append(b)
        if self.policy == PrivacyPolicy.OPEN:
            eligible.sort(key=lambda b: (-b.tier, not b.local))
        else:
            eligible.sort(key=lambda b: (not b.local,))
        return eligible, excluded

    async def complete(
        self,
        task: TaskClass,
        system: str,
        messages: list[dict[str, str]],
        *,
        min_tier: CapabilityTier | None = None,
    ) -> RouterResult:
        """Complete via the first capable, policy-permitted, live backend."""
        needed = min_tier or MIN_TIER[task]
        eligible, reasons = self._candidates(needed)
        for backend in eligible:
            label = f"{backend.name} ({backend.model}, {backend.tier.name.lower()})"
            ok, why = await backend.available()
            if not ok:
                reasons.append(f"{label}: unavailable — {why}")
                continue
            try:
                text = await backend.complete(system, messages)
            except BackendError as exc:
                reasons.append(f"{label}: failed — {exc.detail}")
                continue
            return RouterResult(
                text=text,
                backend=backend.name,
                model=backend.model,
                tier=backend.tier,
                local=backend.local,
            )
        raise RouterRefusal(task, needed, self.policy, reasons)

    async def aclose(self) -> None:
        for backend in self.backends:
            await backend.aclose()

    def describe(self) -> list[dict[str, object]]:
        """Backend roster for the config surface — no secrets."""
        return [
            {
                "backend": b.name,
                "model": b.model,
                "tier": b.tier.name.lower(),
                "local": b.local,
            }
            for b in self.backends
        ]


__all__ = [
    "DEFAULT_PRIVACY",
    "MIN_TIER",
    "PRIVACY_ENV_VAR",
    "BackendError",
    "CapabilityTier",
    "LLMRouter",
    "PrivacyPolicy",
    "RouterRefusal",
    "RouterResult",
    "TaskClass",
    "privacy_from_env",
]
