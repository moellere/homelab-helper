"""LLM layer (Phase 4): router, backends, and the agents built on them.

Everything LLM-shaped goes through :class:`homelab_helper.llm.router.LLMRouter`
— no module calls a backend directly. See ``docs/architecture.md`` "Trust
boundaries": models are untrusted, receive only reconciled/synthesized facts,
and never see raw secrets or raw probe output.
"""

from homelab_helper.llm.backends import backends_from_env
from homelab_helper.llm.router import (
    BackendError,
    CapabilityTier,
    LLMRouter,
    PrivacyPolicy,
    RouterRefusal,
    RouterResult,
    TaskClass,
    privacy_from_env,
)


def router_from_env() -> LLMRouter:
    """The router as configured by the environment (the normal entry point)."""
    return LLMRouter(backends_from_env(), policy=privacy_from_env())


__all__ = [
    "BackendError",
    "CapabilityTier",
    "LLMRouter",
    "PrivacyPolicy",
    "RouterRefusal",
    "RouterResult",
    "TaskClass",
    "backends_from_env",
    "privacy_from_env",
    "router_from_env",
]
