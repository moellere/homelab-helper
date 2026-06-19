"""Integration test: the committed lab fixture produces the day-one audit.

Closes roadmap AC3 ("at least eleven findings against the lab") and AC4
(idempotent re-runs) as a CI test with no live access — the fixture is replayed
into an in-memory DB and the finding corpus is asserted.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
from sqlalchemy import func, select

from homelab_helper.db.base import Base
from homelab_helper.db.enums import FindingKind, FindingStatus
from homelab_helper.db.models import Host, ReconciliationFinding
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.lab_replay import load_lab_fixture, parse_lab_fixture

_FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "example-lab.yaml"
_MIN_FINDINGS = 11  # AC3: "at least eleven findings"


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


async def _open_finding_count(sm) -> int:
    async with sm() as s:
        return (
            await s.execute(
                select(func.count(ReconciliationFinding.id)).where(
                    ReconciliationFinding.status == FindingStatus.OPEN
                )
            )
        ).scalar_one()


async def test_lab_fixture_produces_day_one_findings(sessionmaker) -> None:
    data = parse_lab_fixture(_FIXTURE.read_text())
    async with session_scope(sessionmaker) as s:
        result = await load_lab_fixture(s, data)

    assert result.hosts_loaded == 3
    assert result.assertions_run >= 1

    async with sessionmaker() as s:
        findings = (await s.execute(select(ReconciliationFinding))).scalars().all()
        kinds = Counter(f.kind for f in findings)

    # AC3: at least eleven day-one findings.
    assert len(findings) >= _MIN_FINDINGS
    # Every finding source the engine produces is represented.
    assert kinds[FindingKind.INVENTORY_GAP] >= 1
    assert kinds[FindingKind.STORAGE_PROVENANCE_DELTA] == 2  # forged WWN on lab-b + lab-c
    assert kinds[FindingKind.CONFIG_DRIFT] >= 1


async def test_lab_replay_is_idempotent(sessionmaker) -> None:
    data = parse_lab_fixture(_FIXTURE.read_text())
    async with session_scope(sessionmaker) as s:
        await load_lab_fixture(s, data)
    first = await _open_finding_count(sessionmaker)

    # AC4: re-running the same world neither duplicates hosts nor findings.
    async with session_scope(sessionmaker) as s:
        await load_lab_fixture(s, data)
    second = await _open_finding_count(sessionmaker)

    assert first == second
    async with sessionmaker() as s:
        assert (await s.execute(select(func.count(Host.id)))).scalar_one() == 3
