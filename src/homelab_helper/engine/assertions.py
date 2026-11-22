"""AssertionEngine — one-shot verifier execution + finding lifecycle.

A ConfigurationAssertion is an operator-declared expectation: "this thing
should be true." The engine dispatches each assertion to its kind-specific
verifier, records an append-only AssertionRun row with the outcome, and
manages a CONFIG_DRIFT finding that mirrors the verifier result.

Finding lifecycle (per assertion):

- ``FAIL``  → upsert a CONFIG_DRIFT finding by deterministic fingerprint;
              link the AssertionRun.finding_id. Re-running FAIL on the same
              assertion hits the same fingerprint and bumps ``last_seen``.
- ``PASS``  → resolve any OPEN/ACKNOWLEDGED finding with that fingerprint.
              Reopen happens automatically on the next ``FAIL`` because the
              fingerprint is identical (same as the reconciler's pattern).
- ``ERROR`` → record the error in the run; leave findings untouched (we
              don't know the state of the world).
- ``SKIP``  → record reason; leave findings untouched.

Verifier kinds wired today:

- ``OBSERVATION_PREDICATE`` — pure local query against the latest
  ``Observation`` for a (target, key); applies a predicate.

Stubbed (SKIP with a clear reason) until their adapters land:

- ``SSH_COMMAND``  — needs credentials + SSH adapter wiring.
- ``HTTP_CHECK``   — needs an HTTP client and TLS posture decision.
- ``API_QUERY``    — needs the Proxmox/K8s/UniFi adapters.
- ``FILE_HASH``    — needs SSH + bytes read; subset of SSH_COMMAND.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import ssl
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

from homelab_helper.adapters import factories
from homelab_helper.adapters.kernel_ssh import KernelSSHAdapter
from homelab_helper.db.enums import (
    AssertionKind,
    AssertionScope,
    AssertionStatus,
    FindingKind,
    FindingStatus,
    IntentTargetType,
)
from homelab_helper.db.models import (
    AssertionRun,
    ConfigurationAssertion,
    Host,
    Observation,
    ReconciliationFinding,
)
from homelab_helper.engine.fingerprint import make_fingerprint
from homelab_helper.engine.reconciler import normalize_arch
from homelab_helper.secrets import redact

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class VerifierResult:
    """Single verifier's outcome.

    ``status`` is the AssertionRun.status to record. ``evidence`` is a JSON-
    able dict surfaced in CLI output and stored verbatim on the AssertionRun.
    ``error`` is set only for ``ERROR`` (verifier crashed) or ``SKIP`` (a
    precondition wasn't met) — it's a free-text reason, not a stack trace.
    """

    status: AssertionStatus
    evidence: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class RunResult:
    """End-to-end outcome of one ``run_assertion`` call."""

    assertion_id: uuid.UUID
    assertion_name: str
    status: AssertionStatus
    evidence: dict[str, Any]
    error: str | None
    finding_opened: bool = False
    finding_updated: bool = False
    finding_resolved: bool = False


# ---------------------------------------------------------------------------
# Verifiers
# ---------------------------------------------------------------------------


# Predicate functions — return True/False or None for unknown predicates.
# Each takes (observed_value, expected_value). Lazy/non-strict comparisons
# fall to False rather than raising so a bad spec produces a FAIL, not a
# crash; predicate names themselves go through _PREDICATES validation.


def _pred_eq(observed: Any, expected: Any) -> bool:
    return bool(observed == expected)


def _pred_ne(observed: Any, expected: Any) -> bool:
    return bool(observed != expected)


def _pred_gt(observed: Any, expected: Any) -> bool:
    try:
        return bool(observed > expected)
    except TypeError:
        return False


def _pred_gte(observed: Any, expected: Any) -> bool:
    try:
        return bool(observed >= expected)
    except TypeError:
        return False


def _pred_lt(observed: Any, expected: Any) -> bool:
    try:
        return bool(observed < expected)
    except TypeError:
        return False


def _pred_lte(observed: Any, expected: Any) -> bool:
    try:
        return bool(observed <= expected)
    except TypeError:
        return False


def _pred_regex(observed: Any, expected: Any) -> bool:
    if not isinstance(observed, str) or not isinstance(expected, str):
        return False
    try:
        return re.search(expected, observed) is not None
    except re.error:
        return False


def _pred_contains(observed: Any, expected: Any) -> bool:
    try:
        return bool(expected in observed)
    except TypeError:
        return False


_PREDICATES: dict[str, Callable[[Any, Any], bool]] = {
    "eq": _pred_eq,
    "ne": _pred_ne,
    "gt": _pred_gt,
    "gte": _pred_gte,
    "lt": _pred_lt,
    "lte": _pred_lte,
    "regex": _pred_regex,
    "contains": _pred_contains,
    # "exists" handled at the engine level since it's about row presence,
    # not value comparison.
}


def _parse_observation_spec(
    assertion: ConfigurationAssertion,
) -> tuple[str, str, IntentTargetType, str, Any] | VerifierResult:
    """Validate the verifier_spec. Returns the unpacked tuple, or an ERROR result."""
    spec = assertion.verifier_spec or {}
    key = spec.get("key")
    predicate = spec.get("predicate", "eq")
    expected = spec.get("value")
    target_type_str = spec.get("target_type", "host")
    target_id = spec.get("scope_target_id") or assertion.scope_target

    if not isinstance(key, str) or not key:
        return VerifierResult(status=AssertionStatus.ERROR, error="verifier_spec missing 'key'")
    if not isinstance(target_id, str) or not target_id:
        return VerifierResult(
            status=AssertionStatus.ERROR,
            error="no target_id (scope_target or verifier_spec.scope_target_id)",
        )
    try:
        target_type = IntentTargetType(target_type_str)
    except ValueError:
        return VerifierResult(
            status=AssertionStatus.ERROR,
            error=f"unknown target_type {target_type_str!r}",
        )
    return key, predicate, target_type, target_id, expected


async def _verify_observation_predicate(
    session: AsyncSession,
    assertion: ConfigurationAssertion,
) -> VerifierResult:
    """Compare the latest Observation for a (target, key) against an expected value.

    verifier_spec shape:
        {
            "key": "host.cpu.cores",          # required
            "predicate": "gte",               # default "eq"; or "exists"
            "value": 4,                       # required except for "exists"
            "target_type": "host",            # default "host"
            "scope_target_id": "<host_uuid>", # default: assertion.scope_target
        }
    """
    parsed = _parse_observation_spec(assertion)
    if isinstance(parsed, VerifierResult):
        return parsed
    key, predicate, target_type, target_id, expected = parsed

    stmt = (
        select(Observation)
        .where(
            Observation.target_type == target_type,
            Observation.target_id == target_id,
            Observation.key == key,
        )
        .order_by(Observation.recorded_at.desc(), Observation.id.desc())
        .limit(1)
    )
    obs = (await session.execute(stmt)).scalar_one_or_none()

    base_evidence: dict[str, Any] = {
        "key": key,
        "target_type": target_type.value,
        "target_id": target_id,
        "predicate": predicate,
        "expected": expected,
    }

    if obs is None:
        if predicate == "exists":
            return VerifierResult(
                status=AssertionStatus.FAIL,
                evidence={**base_evidence, "observed": None},
            )
        return VerifierResult(
            status=AssertionStatus.SKIP,
            evidence=base_evidence,
            error=f"no observation for key {key!r} on target {target_id!r}",
        )

    evidence = {
        **base_evidence,
        "observed": obs.value,
        "recorded_at": obs.recorded_at.isoformat(),
    }
    if predicate == "exists":
        return VerifierResult(status=AssertionStatus.PASS, evidence=evidence)

    fn = _PREDICATES.get(predicate)
    if fn is None:
        return VerifierResult(
            status=AssertionStatus.ERROR,
            evidence=evidence,
            error=f"unknown predicate {predicate!r}",
        )
    passed = fn(obs.value, expected)
    return VerifierResult(
        status=AssertionStatus.PASS if passed else AssertionStatus.FAIL,
        evidence=evidence,
    )


def _extract_path(data: Any, path: str | None) -> tuple[bool, Any]:
    """Traverse a dotted key/index path (``"quorate"``, ``"0.status"``) into ``data``."""
    if not path:
        return True, data
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return False, None
    return True, cur


async def _resolve_target_host(
    session: AsyncSession, assertion: ConfigurationAssertion
) -> Host | None:
    spec = assertion.verifier_spec or {}
    raw = spec.get("scope_target_id") or assertion.scope_target
    if not isinstance(raw, str) or not raw:
        return None
    try:
        hid = uuid.UUID(raw)
        host = (await session.execute(select(Host).where(Host.id == hid))).scalar_one_or_none()
        if host is not None:
            return host
    except ValueError:
        pass
    return (await session.execute(select(Host).where(Host.hostname == raw))).scalar_one_or_none()


def _resolve_ssh_creds(host: Host, spec: dict[str, Any]) -> tuple[str, str | None] | None:
    from homelab_helper.secrets import resolve_ssh_credentials_ref  # noqa: PLC0415

    user = spec.get("ssh_user")
    key_path = spec.get("ssh_key_path")
    if isinstance(user, str) and user:
        return user, str(key_path) if key_path else None
    return resolve_ssh_credentials_ref(host.credentials_ref)


async def _run_ssh_command(
    adapter: Any, address: str, command: str, *, user: str, key_path: str | None
) -> Any:
    run_cmd = getattr(adapter, "run_command", None)
    if callable(run_cmd):
        return await run_cmd(address, command, user=user, key_path=key_path)
    async with adapter.session(address, user=user, key_path=key_path) as ssh:
        return await ssh.run(command)


async def _tls_days_remaining(url: str, timeout_s: float = 5.0) -> int | None:
    """Connect with TLS and return remaining certificate validity in whole days."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    port = parsed.port or 443
    ctx = ssl.create_default_context()
    writer = None
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(
                parsed.hostname, port, ssl=ctx, server_hostname=parsed.hostname
            ),
            timeout=timeout_s,
        )
        ssl_obj = writer.get_extra_info("ssl_object")
        cert = ssl_obj.getpeercert() if ssl_obj is not None else None
        not_after = cert.get("notAfter") if isinstance(cert, dict) else None
        if not isinstance(not_after, str):
            return None
        expires = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=UTC)
        return (expires - datetime.now(UTC)).days
    except Exception:
        return None
    finally:
        if writer is not None:
            writer.close()


SSH_COMMANDS_VAR = "HOMELAB_HELPER_ASSERT_SSH_COMMANDS"
"""Set to ``1`` to let ``ssh-command`` assertions run. Off by default: the command is whatever
the assertion says, run with the host's own login, and no trust cell stands in front of it."""


def ssh_commands_allowed() -> bool:
    return os.environ.get(SSH_COMMANDS_VAR, "").strip().lower() in ("1", "true", "yes", "on")


async def _verify_ssh_command(
    engine: AssertionEngine,
    session: AsyncSession,
    assertion: ConfigurationAssertion,
) -> VerifierResult:
    """Run the assertion's command over KernelSSHAdapter and check exit code / stdout.

    Nothing here can tell a read from a write, so the verifier is off until the operator
    sets ``HOMELAB_HELPER_ASSERT_SSH_COMMANDS``; until then every such assertion SKIPs.
    """
    spec = assertion.verifier_spec or {}
    command = spec.get("command")
    if not isinstance(command, str) or not command.strip():
        return VerifierResult(
            status=AssertionStatus.SKIP if not spec else AssertionStatus.ERROR,
            evidence={"kind": assertion.kind.value},
            error="verifier_spec missing 'command'",
        )
    if not ssh_commands_allowed():
        return VerifierResult(
            status=AssertionStatus.SKIP,
            evidence={"command": command},
            error=f"ssh-command assertions are off; set {SSH_COMMANDS_VAR}=1 to run them",
        )
    host = await _resolve_target_host(session, assertion)
    if host is None:
        return VerifierResult(
            status=AssertionStatus.SKIP,
            evidence={"command": command},
            error="target host not found for ssh-command assertion",
        )
    creds = _resolve_ssh_creds(host, spec)
    if creds is None:
        return VerifierResult(
            status=AssertionStatus.SKIP,
            evidence={"host": host.hostname, "command": command},
            error=f"host {host.hostname!r} has no ssh credentials configured",
        )
    user, key_path = creds
    adapter = engine.ssh_adapter or KernelSSHAdapter()
    address = host.primary_ip or host.hostname
    try:
        res = await _run_ssh_command(adapter, address, command, user=user, key_path=key_path)
    except Exception as exc:
        return VerifierResult(
            status=AssertionStatus.SKIP,
            evidence={"host": host.hostname, "command": command},
            error=f"ssh connection failed: {exc}",
        )
    expected_code = int(spec.get("expected_exit_code", 0))
    stdout = res.stdout.strip()
    passed = res.exit_code == expected_code
    if passed and "stdout_eq" in spec:
        passed = stdout == str(spec["stdout_eq"]).strip()
    if passed and "stdout_contains" in spec:
        passed = str(spec["stdout_contains"]) in stdout
    if passed and "stdout_regex" in spec:
        passed = _pred_regex(stdout, str(spec["stdout_regex"]))
    return VerifierResult(
        status=AssertionStatus.PASS if passed else AssertionStatus.FAIL,
        evidence={
            "host": host.hostname,
            "command": command,
            "exit_code": res.exit_code,
            "expected_exit_code": expected_code,
            "stdout": redact(stdout[:500]),
        },
    )


async def _verify_file_hash(
    engine: AssertionEngine,
    session: AsyncSession,
    assertion: ConfigurationAssertion,
) -> VerifierResult:
    """Verify a remote file's SHA-256 digest over SSH via ``sha256sum``."""
    spec = assertion.verifier_spec or {}
    path = spec.get("path")
    expected_hash = spec.get("sha256")
    if (
        not isinstance(path, str)
        or not path
        or not isinstance(expected_hash, str)
        or not expected_hash
    ):
        return VerifierResult(
            status=AssertionStatus.SKIP if not spec else AssertionStatus.ERROR,
            evidence={"kind": assertion.kind.value},
            error="verifier_spec requires 'path' and 'sha256'",
        )
    host = await _resolve_target_host(session, assertion)
    if host is None:
        return VerifierResult(
            status=AssertionStatus.SKIP,
            evidence={"path": path},
            error="target host not found for file-hash assertion",
        )
    creds = _resolve_ssh_creds(host, spec)
    if creds is None:
        return VerifierResult(
            status=AssertionStatus.SKIP,
            evidence={"host": host.hostname, "path": path},
            error=f"host {host.hostname!r} has no ssh credentials configured",
        )
    user, key_path = creds
    adapter = engine.ssh_adapter or KernelSSHAdapter()
    address = host.primary_ip or host.hostname
    try:
        res = await _run_ssh_command(
            adapter, address, f"sha256sum -- {shlex.quote(path)}", user=user, key_path=key_path
        )
    except Exception as exc:
        return VerifierResult(
            status=AssertionStatus.SKIP,
            evidence={"host": host.hostname, "path": path},
            error=f"ssh connection failed: {exc}",
        )
    observed = res.stdout.strip().split()[0].lower() if res.ok and res.stdout.strip() else None
    expected_norm = expected_hash.strip().lower()
    passed = observed == expected_norm
    return VerifierResult(
        status=AssertionStatus.PASS if passed else AssertionStatus.FAIL,
        evidence={
            "host": host.hostname,
            "path": path,
            "expected_sha256": expected_norm,
            "observed_sha256": observed,
            "exit_code": res.exit_code,
        },
    )


HTTP_CHECK_METHODS: tuple[str, ...] = ("GET", "HEAD")
"""An assertion observes; it never sends a method that changes the far end."""


async def _verify_http_check(
    engine: AssertionEngine,
    session: AsyncSession,
    assertion: ConfigurationAssertion,
) -> VerifierResult:
    """Verify an HTTP(S) endpoint's status code, body content, and optional TLS expiry window."""
    del session
    spec = assertion.verifier_spec or {}
    url = spec.get("url")
    if not isinstance(url, str) or not url.strip():
        return VerifierResult(
            status=AssertionStatus.SKIP if not spec else AssertionStatus.ERROR,
            evidence={"kind": assertion.kind.value},
            error="verifier_spec missing 'url'",
        )
    method = str(spec.get("method", "GET")).upper()
    if method not in HTTP_CHECK_METHODS:
        return VerifierResult(
            status=AssertionStatus.ERROR,
            evidence={"url": url, "method": method},
            error=f"http-check only sends {' or '.join(HTTP_CHECK_METHODS)}, not {method}",
        )
    raw_expected = spec.get("expected_status", 200)
    expected_statuses = (
        [int(x) for x in raw_expected] if isinstance(raw_expected, list) else [int(raw_expected)]
    )
    timeout_s = float(spec.get("timeout_s", 10.0))
    verify_ssl = bool(spec.get("verify_ssl", True))

    client = engine.http_client
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(verify=verify_ssl, timeout=timeout_s)
    try:
        resp = await client.request(method, url)
    except Exception as exc:
        if spec.get("skip_on_unreachable"):
            return VerifierResult(
                status=AssertionStatus.SKIP,
                evidence={"url": url},
                error=f"http request failed: {exc}",
            )
        return VerifierResult(
            status=AssertionStatus.FAIL,
            evidence={"url": url, "error": str(exc)},
        )
    finally:
        if owns_client:
            await client.aclose()

    passed = resp.status_code in expected_statuses
    body = resp.text
    if passed and "body_contains" in spec:
        passed = str(spec["body_contains"]) in body
    if passed and "body_regex" in spec:
        passed = _pred_regex(body, str(spec["body_regex"]))

    evidence: dict[str, Any] = {
        "url": url,
        "status_code": resp.status_code,
        "expected_status": expected_statuses,
    }
    min_tls_days = spec.get("min_tls_days")
    if min_tls_days is not None:
        tls_fn = engine.tls_days_fn or _tls_days_remaining
        days = await tls_fn(url)
        evidence["tls_days_remaining"] = days
        evidence["min_tls_days"] = int(min_tls_days)
        if days is None or days < int(min_tls_days):
            passed = False

    return VerifierResult(
        status=AssertionStatus.PASS if passed else AssertionStatus.FAIL,
        evidence=evidence,
    )


API_QUERIES: dict[str, str] = {
    "cluster_status": "cluster_status",
    "nodes": "list_nodes",
    "vms": "list_vms",
    "storage": "list_storage",
    "applications": "list_applications",
    "dns_records": "list_dns_records",
    "networks": "list_networks",
    "clients": "list_clients",
    "filesystems": "list_filesystems",
    "shares": "list_shared_folders",
    "config": "get_config",
    "states": "list_states",
    "workloads": "list_workloads",
}
"""The reads an ``api-query`` assertion may ask for. A name outside it is an ERROR, never a
method lookup: an assertion must not be able to reach an adapter's write methods."""

API_SOURCES: tuple[str, ...] = (
    "proxmox",
    "netbox",
    "k8s",
    "unifi",
    "cloudflare",
    "argocd",
    "omv",
    "hass",
    "mikrotik",
    "printwarden",
)
"""The ``adapters.factories`` builders an ``api-query`` assertion may name."""


async def _run_adapter_query(adapter: Any, query: str) -> Any:
    method_name = API_QUERIES.get(query)
    fn = getattr(adapter, method_name, None) if method_name else None
    if not callable(fn):
        raise ValueError(f"adapter does not support query {query!r}")
    return await fn()


async def _verify_api_query(  # noqa: PLR0911
    engine: AssertionEngine,
    session: AsyncSession,
    assertion: ConfigurationAssertion,
) -> VerifierResult:
    """Query a management-plane adapter and apply a predicate to the response."""
    del session
    spec = assertion.verifier_spec or {}
    source = spec.get("source") or spec.get("adapter")
    query = spec.get("query") or spec.get("resource")
    if not isinstance(source, str) or not source or not isinstance(query, str) or not query:
        return VerifierResult(
            status=AssertionStatus.SKIP if not spec else AssertionStatus.ERROR,
            evidence={"kind": assertion.kind.value},
            error="verifier_spec requires 'source' and 'query'",
        )
    predicate = str(spec.get("predicate", "eq"))
    expected = spec.get("value")
    key_path = spec.get("key")

    adapter = (engine.adapters or {}).get(source)
    owns_adapter = False
    if adapter is None:
        factory = getattr(factories, source, None) if source in API_SOURCES else None
        if not callable(factory):
            return VerifierResult(
                status=AssertionStatus.ERROR,
                evidence={"source": source, "query": query},
                error=f"unknown adapter source {source!r}",
            )
        try:
            adapter = factory()
            owns_adapter = True
        except Exception as exc:
            return VerifierResult(
                status=AssertionStatus.SKIP,
                evidence={"source": source, "query": query},
                error=f"adapter {source!r} is not configured: {exc}",
            )
    try:
        payload = await _run_adapter_query(adapter, query)
    except ValueError as exc:
        return VerifierResult(
            status=AssertionStatus.ERROR,
            evidence={"source": source, "query": query},
            error=str(exc),
        )
    except Exception as exc:
        return VerifierResult(
            status=AssertionStatus.SKIP,
            evidence={"source": source, "query": query},
            error=f"adapter query failed: {exc}",
        )
    finally:
        if owns_adapter and hasattr(adapter, "aclose"):
            await adapter.aclose()

    found, observed = _extract_path(payload, str(key_path) if key_path is not None else None)
    evidence = {
        "source": source,
        "query": query,
        "key": key_path,
        "predicate": predicate,
        "expected": expected,
        "observed": observed,
    }
    if predicate == "exists":
        return VerifierResult(
            status=AssertionStatus.PASS if found and observed is not None else AssertionStatus.FAIL,
            evidence=evidence,
        )
    if not found:
        return VerifierResult(
            status=AssertionStatus.FAIL,
            evidence=evidence,
        )
    fn = _PREDICATES.get(predicate)
    if fn is None:
        return VerifierResult(
            status=AssertionStatus.ERROR,
            evidence=evidence,
            error=f"unknown predicate {predicate!r}",
        )
    passed = fn(observed, expected)
    return VerifierResult(
        status=AssertionStatus.PASS if passed else AssertionStatus.FAIL,
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


async def _arch_skip(session: AsyncSession, assertion: ConfigurationAssertion) -> str | None:
    """Return a SKIP reason if the host's arch is outside the assertion's scope.

    Reads ``verifier_spec.applies_to_arch`` (a list of arch strings). Only
    meaningful for host-scoped assertions. Returns ``None`` (run normally) when
    no arch scope is set, the assertion isn't host-scoped, or the host's arch
    matches. Architecture spellings are normalized, so ``x86_64`` and ``amd64``
    are equivalent.
    """
    spec = assertion.verifier_spec or {}
    arch_scope = spec.get("applies_to_arch")
    if not arch_scope:
        return None
    if assertion.scope is not AssertionScope.HOST or not assertion.scope_target:
        return None
    try:
        host_id = uuid.UUID(assertion.scope_target)
    except (ValueError, AttributeError):
        return None
    host = (await session.execute(select(Host).where(Host.id == host_id))).scalar_one_or_none()
    if host is None:
        return None
    allowed = {normalize_arch(a) for a in arch_scope}
    if host.arch in allowed:
        return None
    return f"arch {host.arch.value} not in scope {sorted(a.value for a in allowed)}"


def _scope_target_type(assertion: ConfigurationAssertion) -> str:
    """Map AssertionScope onto the finding's affected.target_type string."""
    if assertion.scope == AssertionScope.GLOBAL:
        return "global"
    return assertion.scope.value


def _assertion_fingerprint(assertion: ConfigurationAssertion) -> str:
    """Deterministic finding fingerprint per assertion.

    Same assertion always produces the same fingerprint, so re-failing the
    same assertion hits the same finding row and bumps last_seen; passing
    afterwards resolves it; failing again reopens the same row.
    """
    target_type = _scope_target_type(assertion)
    target_id = assertion.scope_target or "global"
    return make_fingerprint(
        FindingKind.CONFIG_DRIFT.value,
        target_type,
        target_id,
        f"assertion:{assertion.name}",
    )


def _finding_affected(assertion: ConfigurationAssertion) -> list[dict[str, str]]:
    target_id = assertion.scope_target or "global"
    return [
        {"target_type": _scope_target_type(assertion), "target_id": target_id},
        {"target_type": "assertion", "target_id": assertion.name},
    ]


class AssertionEngine:
    """One-shot dispatch. Stateless and cheap to instantiate; caller commits."""

    def __init__(
        self,
        *,
        ssh_adapter: Any = None,
        http_client: Any = None,
        adapters: dict[str, Any] | None = None,
        tls_days_fn: Callable[[str], Any] | None = None,
    ) -> None:
        self.ssh_adapter = ssh_adapter
        self.http_client = http_client
        self.adapters = adapters
        self.tls_days_fn = tls_days_fn

    async def _dispatch_verifier(
        self,
        session: AsyncSession,
        assertion: ConfigurationAssertion,
    ) -> VerifierResult:
        if assertion.kind is AssertionKind.OBSERVATION_PREDICATE:
            return await _verify_observation_predicate(session, assertion)
        verifiers: dict[AssertionKind, Callable[..., Any]] = {
            AssertionKind.SSH_COMMAND: _verify_ssh_command,
            AssertionKind.FILE_HASH: _verify_file_hash,
            AssertionKind.HTTP_CHECK: _verify_http_check,
            AssertionKind.API_QUERY: _verify_api_query,
        }
        verifier = verifiers.get(assertion.kind)
        if verifier is not None:
            out: VerifierResult = await verifier(self, session, assertion)
            return out
        return VerifierResult(
            status=AssertionStatus.ERROR,
            evidence={"kind": str(assertion.kind)},
            error=f"unsupported assertion kind {assertion.kind!r}",
        )

    async def run_assertion(
        self,
        session: AsyncSession,
        assertion: ConfigurationAssertion,
    ) -> RunResult:
        """Run ONE assertion, persist AssertionRun + Finding side effects."""
        # Architecture scope: SKIP (don't FAIL) on hosts the assertion doesn't
        # apply to — e.g. an `aes`/`avx2` check scoped to amd64 on an arm node.
        arch_reason = await _arch_skip(session, assertion)
        if arch_reason is not None:
            result: VerifierResult = VerifierResult(status=AssertionStatus.SKIP, error=arch_reason)
        else:
            result = await self._dispatch_verifier(session, assertion)

        run = AssertionRun(
            assertion_id=assertion.id,
            status=result.status,
            evidence=result.evidence,
            error=result.error,
        )
        session.add(run)
        await session.flush()

        rr = RunResult(
            assertion_id=assertion.id,
            assertion_name=assertion.name,
            status=result.status,
            evidence=result.evidence,
            error=result.error,
        )

        if result.status == AssertionStatus.FAIL:
            finding, opened = await self._upsert_finding(session, assertion, result)
            run.finding_id = finding.id
            rr.finding_opened = opened
            rr.finding_updated = not opened
        elif result.status == AssertionStatus.PASS:
            rr.finding_resolved = await self._resolve_finding_if_open(session, assertion)

        await session.flush()
        return rr

    async def run_all(
        self,
        session: AsyncSession,
        *,
        enabled_only: bool = True,
    ) -> list[RunResult]:
        """Run every (enabled) assertion in the DB."""
        stmt = select(ConfigurationAssertion)
        if enabled_only:
            stmt = stmt.where(ConfigurationAssertion.enabled.is_(True))
        stmt = stmt.order_by(ConfigurationAssertion.name)
        assertions = (await session.execute(stmt)).scalars().all()
        results: list[RunResult] = []
        for a in assertions:
            results.append(await self.run_assertion(session, a))
        return results

    # ------------------------------------------------------------------ findings

    async def _upsert_finding(
        self,
        session: AsyncSession,
        assertion: ConfigurationAssertion,
        result: VerifierResult,
    ) -> tuple[ReconciliationFinding, bool]:
        """Upsert a CONFIG_DRIFT finding for a FAILing assertion. Returns (finding, opened_new)."""
        fp = _assertion_fingerprint(assertion)
        existing = (
            await session.execute(
                select(ReconciliationFinding).where(ReconciliationFinding.fingerprint == fp)
            )
        ).scalar_one_or_none()
        now_ts = datetime.now(UTC)
        title = f"Assertion failed: {assertion.name}"
        description = assertion.description or "(no description supplied)"

        if existing is not None:
            opened_new = False
            if existing.status == FindingStatus.RESOLVED:
                existing.status = FindingStatus.OPEN
                existing.resolved_at = None
                existing.first_seen = now_ts
                opened_new = True
            existing.last_seen = now_ts
            existing.severity = assertion.severity_on_fail
            existing.title = title
            existing.description = description
            existing.affected = _finding_affected(assertion)
            existing.evidence_refs = [
                {"type": "assertion_run", "id": "latest"},
                *[
                    {"type": k, "id": str(v)}
                    for k, v in (result.evidence or {}).items()
                    if k in {"target_type", "target_id", "key"}
                ],
            ]
            return existing, opened_new

        finding = ReconciliationFinding(
            kind=FindingKind.CONFIG_DRIFT,
            severity=assertion.severity_on_fail,
            fingerprint=fp,
            title=title,
            description=description,
            affected=_finding_affected(assertion),
            evidence_refs=[{"type": "assertion_run", "id": "latest"}],
            status=FindingStatus.OPEN,
            first_seen=now_ts,
            last_seen=now_ts,
        )
        session.add(finding)
        await session.flush()
        return finding, True

    async def _resolve_finding_if_open(
        self,
        session: AsyncSession,
        assertion: ConfigurationAssertion,
    ) -> bool:
        """If a finding exists for this assertion in OPEN/ACK, mark RESOLVED."""
        fp = _assertion_fingerprint(assertion)
        existing = (
            await session.execute(
                select(ReconciliationFinding).where(ReconciliationFinding.fingerprint == fp)
            )
        ).scalar_one_or_none()
        if existing is None:
            return False
        if existing.status not in {FindingStatus.OPEN, FindingStatus.ACKNOWLEDGED}:
            return False
        existing.status = FindingStatus.RESOLVED
        existing.resolved_at = datetime.now(UTC)
        return True


__all__ = [
    "AssertionEngine",
    "RunResult",
    "VerifierResult",
]
