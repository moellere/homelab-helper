"""Argo CD adapter — read-only desired-state (Git → cluster) source (Phase 3, L1).

Argo CD is the seam where a Git repository's declared intent meets a live
cluster. Each Argo CD ``Application`` names the repo, path, and target revision
that *should* be running, and carries Argo CD's own computed verdict on whether
the cluster matches: ``sync.status`` (``Synced`` / ``OutOfSync``) and
``health.status`` (``Healthy`` / ``Degraded`` / ``Missing`` / …). Reading it
gives the harness the git-vs-cluster drift signal without re-implementing a
manifest differ — Argo CD already did the diff.

**Read-only at L1,** with the Phase-7 exception below: ``sync_application`` and
``rollback_application`` exist solely for ``engine/executor.py`` /
``engine/rollback.py`` behind the trust gradient. Nothing else triggers a sync.

Auth is a bearer token (an Argo CD API/account token). Argo CD ships a
self-signed cert by default, so ``verify_ssl`` defaults to ``False``. The API
lives under ``<url>/api/v1``; the applications list wraps its payload in
``{"metadata": ..., "items": [...]}``, which :meth:`list_applications` unwraps.

Configuration::

    HOMELAB_HELPER_ARGOCD_URL         https://argocd.example.lan
    HOMELAB_HELPER_ARGOCD_API_TOKEN   <argocd account token>

Tests inject an ``httpx.AsyncClient`` with ``MockTransport`` — no live server.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

from homelab_helper.secrets import secret_from_env

_HTTP_ERROR_THRESHOLD = 400
_FALSEY = {"0", "false", "no"}
_SYNCED = "Synced"
_HEALTHY = "Healthy"


class ArgoCDConfigError(RuntimeError):
    """Raised when required Argo CD configuration is missing."""


class ArgoCDAPIError(RuntimeError):
    """Non-2xx response from the Argo CD API."""

    def __init__(self, status_code: int, detail: str, *, method: str, path: str) -> None:
        super().__init__(f"ArgoCD {method} {path} -> {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class ArgoCDConfig:
    url: str
    api_token: str
    verify_ssl: bool = False  # Argo CD ships a self-signed cert by default
    timeout_s: float = 10.0

    @classmethod
    def from_env(cls) -> ArgoCDConfig:
        url = os.environ.get("HOMELAB_HELPER_ARGOCD_URL")
        token = secret_from_env("HOMELAB_HELPER_ARGOCD_API_TOKEN")
        if not url or not token:
            raise ArgoCDConfigError(
                "Argo CD URL + API token are required. Set HOMELAB_HELPER_ARGOCD_URL and "
                "HOMELAB_HELPER_ARGOCD_API_TOKEN."
            )
        verify = os.environ.get("HOMELAB_HELPER_ARGOCD_VERIFY_SSL", "false").lower() not in _FALSEY
        return cls(url=url, api_token=token, verify_ssl=verify)


def parse_application(raw: dict[str, Any]) -> dict[str, Any]:
    """Shape one Argo CD ``Application`` into a stable, drift-focused dict.

    Pulls the git desired-state (repo/path/target revision) alongside Argo CD's
    computed sync + health verdict, and lists the individual resources Argo CD
    reports as not ``Synced`` — the concrete drift.
    """
    metadata = raw.get("metadata") or {}
    spec = raw.get("spec") or {}
    source = spec.get("source") or {}
    destination = spec.get("destination") or {}
    status = raw.get("status") or {}
    sync = status.get("sync") or {}
    health = status.get("health") or {}
    resources = status.get("resources") or []
    out_of_sync = [
        {
            "kind": r.get("kind"),
            "name": r.get("name"),
            "namespace": r.get("namespace"),
            "status": r.get("status"),
        }
        for r in resources
        if r.get("status") and r.get("status") != _SYNCED
    ]
    history = [
        {
            "id": h.get("id"),
            "revision": h.get("revision"),
            "deployed_at": h.get("deployedAt"),
        }
        for h in (status.get("history") or [])
        if isinstance(h, dict) and h.get("id") is not None
    ]
    operation = status.get("operationState") or {}
    return {
        "name": metadata.get("name"),
        "namespace": destination.get("namespace"),
        "server": destination.get("server"),
        "repo_url": source.get("repoURL"),
        "path": source.get("path"),
        "target_revision": source.get("targetRevision"),
        "sync_status": sync.get("status"),
        "health_status": health.get("status"),
        "out_of_sync_resources": out_of_sync,
        "revision": sync.get("revision"),
        "history": history,
        "operation_phase": operation.get("phase"),
    }


def application_is_drifted(app: dict[str, Any]) -> bool:
    """True when the cluster no longer matches git, or the app is unhealthy.

    ``OutOfSync`` is the git-vs-cluster mismatch proper; a non-``Healthy`` health
    status (``Degraded`` / ``Missing`` / ``Progressing``) is the softer signal
    that the declared intent isn't fully live. Either is worth surfacing.
    """
    sync = app.get("sync_status")
    health = app.get("health_status")
    if sync and sync != _SYNCED:
        return True
    return bool(health and health != _HEALTHY)


class ArgoCDAdapter:
    """Read-only async client for the Argo CD API."""

    name = "argocd"

    def __init__(
        self,
        config: ArgoCDConfig | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if config is None and client is None:
            raise ArgoCDConfigError("ArgoCDAdapter needs a config or an injected client")
        self.config = config or ArgoCDConfig(url="http://injected", api_token="x")
        self._client = client
        self._owns_client = client is None

    @classmethod
    def from_env(cls) -> ArgoCDAdapter:
        return cls(ArgoCDConfig.from_env())

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.config.url.rstrip("/") + "/api/v1",
            headers={
                "Authorization": f"Bearer {self.config.api_token}",
                "Accept": "application/json",
            },
            timeout=self.config.timeout_s,
            verify=self.config.verify_ssl,
        )

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> ArgoCDAdapter:
        _ = self.client
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _request(self, method: str, path: str, *, json: Any | None = None) -> Any:
        response = await self.client.request(method, path, json=json)
        if response.status_code >= _HTTP_ERROR_THRESHOLD:
            detail = response.text.strip()[:300] or response.reason_phrase
            raise ArgoCDAPIError(response.status_code, detail, method=method, path=path)
        if not response.content:
            return None
        return response.json()

    # ------------------------------------------------------------------ reads

    async def list_applications(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", "/applications")
        items = payload.get("items") if isinstance(payload, dict) else payload
        return [parse_application(a) for a in (items or [])]

    async def get_application(self, name: str) -> dict[str, Any]:
        """One application with its deployed revision and sync history. Read-only —
        the rollback orchestrator's probe for ``argocd-history``."""
        payload = await self._request("GET", f"/applications/{name}")
        if not isinstance(payload, dict):
            raise ArgoCDAPIError(404, f"application {name!r} not found", method="GET", path=name)
        return parse_application(payload)

    # ----------------------------------------------------------------- writes
    #
    # The Phase-7 write surface, and it exists solely for the executor: every
    # call site must have routed through engine.trust.decide() first. Nothing
    # else in the codebase may call these (tests/test_write_isolation.py).

    async def sync_application(
        self, name: str, *, revision: str | None = None, prune: bool = False, dry_run: bool = False
    ) -> Any:
        """Ask Argo CD to sync one application to git. Executor-only."""
        body: dict[str, Any] = {"prune": prune, "dryRun": dry_run}
        if revision:
            body["revision"] = revision
        return await self._request("POST", f"/applications/{name}/sync", json=body)

    async def rollback_application(self, name: str, history_id: int) -> Any:
        """Roll one application back to a sync-history entry. Rollback use only."""
        return await self._request(
            "POST", f"/applications/{name}/rollback", json={"id": int(history_id)}
        )

    async def health_check(self) -> tuple[bool, str | None]:
        """Quick reachability/auth probe against the session userinfo endpoint."""
        try:
            await self._request("GET", "/session/userinfo")
        except (ArgoCDAPIError, httpx.HTTPError) as exc:
            return False, str(exc)
        return True, None


__all__ = [
    "ArgoCDAPIError",
    "ArgoCDAdapter",
    "ArgoCDConfig",
    "ArgoCDConfigError",
    "application_is_drifted",
    "parse_application",
]
