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

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

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


async def _verify_not_implemented(
    session: AsyncSession,
    assertion: ConfigurationAssertion,
) -> VerifierResult:
    del session  # unused — the stub doesn't query anything
    return VerifierResult(
        status=AssertionStatus.SKIP,
        evidence={"kind": assertion.kind.value},
        error=(
            f"verifier for kind {assertion.kind.value!r} not yet implemented; "
            "wires in with its adapter slice"
        ),
    )


_VERIFIERS: dict[
    AssertionKind,
    Callable[[AsyncSession, ConfigurationAssertion], Any],
] = {
    AssertionKind.OBSERVATION_PREDICATE: _verify_observation_predicate,
    AssertionKind.SSH_COMMAND: _verify_not_implemented,
    AssertionKind.HTTP_CHECK: _verify_not_implemented,
    AssertionKind.API_QUERY: _verify_not_implemented,
    AssertionKind.FILE_HASH: _verify_not_implemented,
}


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
            verifier = _VERIFIERS.get(assertion.kind, _verify_not_implemented)
            result = await verifier(session, assertion)

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
