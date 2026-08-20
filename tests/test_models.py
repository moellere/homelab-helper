"""Verify Slice 1 model layer end-to-end against an in-memory SQLite DB.

These tests don't go through Alembic — they use ``Base.metadata.create_all``
to materialize the schema directly. The Alembic round-trip is tested
separately in :mod:`test_alembic`.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from homelab_helper.db.base import Base
from homelab_helper.db.enums import (
    Architecture,
    AssertionKind,
    AssertionScope,
    AssertionStatus,
    FindingKind,
    FindingSeverity,
    IntentState,
    IntentTargetType,
    PartKind,
    PrivilegeLevel,
)
from homelab_helper.db.models import (
    AssertionRun,
    ConfigurationAssertion,
    DiscoveryRun,
    Host,
    Observation,
    OperationalIntent,
    PhysicalPart,
    Placement,
    Probe,
    ProposalLog,
    ReconciliationFinding,
)
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope


@pytest.fixture
async def engine():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessionmaker(engine):
    return make_sessionmaker(engine)


async def test_expected_tables_in_metadata() -> None:
    """Base.metadata holds the Slice 1 tables + the Phase-3 virtualization slice."""
    expected = {
        "host",
        "physical_part",
        "placement",
        "operational_intent",
        "probe",
        "discovery_run",
        "observation",
        "configuration_assertion",
        "assertion_run",
        "reconciliation_finding",
        "proposal_log",
        # Phase 3 virtualization slice
        "cluster",
        "virtual_machine",
        # Phase 3 service-endpoint slice
        "service",
        "service_endpoint",
        # Phase 4 skill inferer
        "skill_profile",
    }
    assert set(Base.metadata.tables.keys()) == expected


async def test_full_object_graph_round_trips(sessionmaker) -> None:
    """Insert one of each model and verify all rows persist + foreign keys hold."""
    async with session_scope(sessionmaker) as s:
        h = Host(
            hostname="node0",
            primary_ip="10.0.6.20",
            arch=Architecture.AMD64,
            capabilities={"cpu_cores": 4, "cpu_threads": 8},
        )
        s.add(h)
        await s.flush()

        part = PhysicalPart(
            kind=PartKind.DIMM,
            manufacturer="Crucial",
            serial="DIMM-SERIAL-1",
            capacity_bytes=16 * 1024**3,
            speed_mts=3200,
        )
        s.add(part)
        await s.flush()

        s.add(Placement(part_id=part.id, host_id=h.id, slot="DIMM_A1"))

        s.add(
            OperationalIntent(
                target_type=IntentTargetType.HOST,
                target_id=str(h.id),
                intent=IntentState.RUNNING,
                declared_by="enoch",
            )
        )

        probe = Probe(
            name="host.cpu",
            version="0.1.0",
            module_path="homelab_helper.probes.host.cpu:CPUProbe",
            required_privilege=PrivilegeLevel.USER,
            produces_keys=["host.cpu.model", "host.cpu.flags"],
        )
        s.add(probe)
        await s.flush()

        run = DiscoveryRun(
            host_id=h.id,
            probe_id=probe.id,
            probe_name=probe.name,
            probe_version=probe.version,
            privilege_level=PrivilegeLevel.USER,
        )
        s.add(run)
        await s.flush()

        s.add(
            Observation(
                run_id=run.id,
                key="host.cpu.model",
                value="Example CPU",
                target_type=IntentTargetType.HOST,
                target_id=str(h.id),
            )
        )

        assertion = ConfigurationAssertion(
            name="nas0.br6.mac_pinned",
            scope=AssertionScope.HOST,
            scope_target="nas0",
            description="bridge MAC must match NIC MAC",
            kind=AssertionKind.SSH_COMMAND,
            verifier_spec={"command": "ip link show br6"},
            severity_on_fail=FindingSeverity.HIGH,
        )
        s.add(assertion)
        await s.flush()

        s.add(AssertionRun(assertion_id=assertion.id, status=AssertionStatus.PASS))

        finding = ReconciliationFinding(
            kind=FindingKind.CEPH_BOTTLENECK,
            severity=FindingSeverity.HIGH,
            fingerprint="abc123def4567890",
            title="node0 ceph link",
            description="node0 has 1 GbE while peers have 2.5 GbE",
            affected=[{"target_type": "host", "target_id": "node0"}],
        )
        s.add(finding)
        await s.flush()

        s.add(
            ProposalLog(
                finding_id=finding.id,
                title="Add USB 2.5GbE NIC to node0",
                artifact={"kind": "shopping-list", "items": ["USB 2.5GbE NIC"]},
                blast_radius="single-host",
            )
        )

    # Round-trip read
    async with sessionmaker() as s:
        for cls in (
            Host,
            PhysicalPart,
            Placement,
            OperationalIntent,
            Probe,
            DiscoveryRun,
            Observation,
            ConfigurationAssertion,
            AssertionRun,
            ReconciliationFinding,
            ProposalLog,
        ):
            r = await s.execute(select(func.count()).select_from(cls))
            assert r.scalar() == 1, f"expected 1 row in {cls.__tablename__}"


async def test_placement_history_is_append_only(sessionmaker) -> None:
    """Closing a placement is a to_date write, not a delete."""
    async with session_scope(sessionmaker) as s:
        h1 = Host(hostname="node4")
        h2 = Host(hostname="node1")
        part = PhysicalPart(kind=PartKind.DIMM, serial="MIGRATED-DIMM")
        s.add_all([h1, h2, part])
        await s.flush()

        original = Placement(part_id=part.id, host_id=h1.id, slot="DIMM_A1")
        s.add(original)
        await s.flush()

    # Simulate the move: close old placement, open new one.
    from datetime import UTC, datetime

    async with session_scope(sessionmaker) as s:
        existing = (await s.execute(select(Placement))).scalar_one()
        existing.to_date = datetime.now(UTC)
        # Reload host ids
        h2 = (await s.execute(select(Host).where(Host.hostname == "node1"))).scalar_one()
        part = (await s.execute(select(PhysicalPart))).scalar_one()
        s.add(Placement(part_id=part.id, host_id=h2.id, slot="DIMM_A1"))

    async with sessionmaker() as s:
        rows = (await s.execute(select(Placement))).scalars().all()
        assert len(rows) == 2, "history is append-only"
        closed = [p for p in rows if p.to_date is not None]
        current = [p for p in rows if p.to_date is None]
        assert len(closed) == 1
        assert len(current) == 1
