"""Tests for the Argo CD drift reconcile (DRIFT_CANDIDATE finding lifecycle)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from homelab_helper.db.base import Base
from homelab_helper.db.enums import FindingKind, FindingSeverity, FindingStatus
from homelab_helper.db.models import ReconciliationFinding
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.argocd_drift import reconcile_argocd_drift


@pytest.fixture
async def engine():
    eng = make_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
def sessionmaker(engine):
    return make_sessionmaker(engine)


def _app(name: str, *, sync: str = "Synced", health: str = "Healthy", oos=None) -> dict:
    return {
        "name": name,
        "namespace": name,
        "repo_url": "https://git/hl.git",
        "target_revision": "main",
        "sync_status": sync,
        "health_status": health,
        "out_of_sync_resources": oos or [],
    }


async def _findings(sm) -> list[ReconciliationFinding]:
    async with sm() as s:
        return list((await s.execute(select(ReconciliationFinding))).scalars().all())


# ---------------------------------------------------------------------------
# open / resolve / reopen
# ---------------------------------------------------------------------------


async def test_drifted_app_opens_finding(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        result = await reconcile_argocd_drift(s, [_app("loki", sync="OutOfSync")])
    assert result.opened == ["loki"]
    findings = await _findings(sessionmaker)
    assert len(findings) == 1
    assert findings[0].kind == FindingKind.DRIFT_CANDIDATE
    assert findings[0].status == FindingStatus.OPEN


async def test_healthy_app_opens_nothing(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        result = await reconcile_argocd_drift(s, [_app("grafana")])
    assert result.opened == []
    assert await _findings(sessionmaker) == []


async def test_reconcile_is_idempotent(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        first = await reconcile_argocd_drift(s, [_app("loki", sync="OutOfSync")])
        second = await reconcile_argocd_drift(s, [_app("loki", sync="OutOfSync")])
    assert first.opened == ["loki"]
    assert second.opened == []
    assert second.updated == ["loki"]  # re-seen, same row
    assert len(await _findings(sessionmaker)) == 1


async def test_healthy_again_resolves_finding(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await reconcile_argocd_drift(s, [_app("loki", sync="OutOfSync")])
        result = await reconcile_argocd_drift(s, [_app("loki")])  # now Synced/Healthy
    assert result.resolved == ["loki"]
    findings = await _findings(sessionmaker)
    assert findings[0].status == FindingStatus.RESOLVED
    assert findings[0].resolved_at is not None


async def test_recurrence_reopens_same_row_and_resets_first_seen(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await reconcile_argocd_drift(s, [_app("loki", sync="OutOfSync")])
        await reconcile_argocd_drift(s, [_app("loki")])  # resolve
    async with sessionmaker() as s:
        resolved = (await s.execute(select(ReconciliationFinding))).scalar_one()
        first_seen_before = resolved.first_seen

    async with session_scope(sessionmaker) as s:
        result = await reconcile_argocd_drift(s, [_app("loki", sync="OutOfSync")])  # drifts again
    assert result.reopened == ["loki"]
    findings = await _findings(sessionmaker)
    assert len(findings) == 1  # same row, not a new one
    assert findings[0].status == FindingStatus.OPEN
    assert findings[0].first_seen >= first_seen_before


# ---------------------------------------------------------------------------
# severity + invariant #1 (absence must not auto-resolve)
# ---------------------------------------------------------------------------


async def test_degraded_health_is_high_severity(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await reconcile_argocd_drift(s, [_app("loki", sync="OutOfSync", health="Degraded")])
    findings = await _findings(sessionmaker)
    assert findings[0].severity == FindingSeverity.HIGH


async def test_out_of_sync_but_healthy_is_medium(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await reconcile_argocd_drift(s, [_app("loki", sync="OutOfSync", health="Healthy")])
    findings = await _findings(sessionmaker)
    assert findings[0].severity == FindingSeverity.MEDIUM


async def test_vanished_app_is_not_auto_resolved(sessionmaker) -> None:
    """An app no longer reported by Argo CD must not silently resolve (invariant #1)."""
    async with session_scope(sessionmaker) as s:
        await reconcile_argocd_drift(s, [_app("loki", sync="OutOfSync")])
        # Next run: loki is absent entirely (not reported as healthy — just gone).
        result = await reconcile_argocd_drift(s, [_app("grafana")])
    assert result.resolved == []  # grafana was never drifted; loki untouched
    findings = await _findings(sessionmaker)
    loki = next(f for f in findings if "loki" in f.title)
    assert loki.status == FindingStatus.OPEN  # still open — absence != resolved


async def test_out_of_sync_resources_land_in_description(sessionmaker) -> None:
    oos = [{"kind": "Deployment", "name": "loki", "namespace": "mon", "status": "OutOfSync"}]
    async with session_scope(sessionmaker) as s:
        await reconcile_argocd_drift(s, [_app("loki", sync="OutOfSync", oos=oos)])
    findings = await _findings(sessionmaker)
    assert "Deployment/loki" in findings[0].description
