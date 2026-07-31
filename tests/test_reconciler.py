"""Unit + replay tests for the Reconciler.

Covers the host-row projection slices, the part-lineage slices (DIMM,
storage, NIC) including cross-host moves and cross-kind isolation, and
finding generation + auto-resolve — deterministic fingerprint dedup, per-
category scoping so an absent observation never silently auto-resolves, and
reopen-on-recurrence.

The replay-style tests materialize Observation rows directly from a fixture
dict (no probe runs, no SSH) and feed them through the reconciler — this is
the seed of the example replay-test fixture; it lives in-process today and
will migrate to a YAML fixture loader once the example integration lands.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from homelab_helper.db.base import Base
from homelab_helper.db.enums import (
    Architecture,
    Confidence,
    FindingKind,
    FindingSeverity,
    FindingStatus,
    IntentTargetType,
    PartKind,
    PrivilegeLevel,
)
from homelab_helper.db.models import (
    DiscoveryRun,
    Host,
    Observation,
    PhysicalPart,
    Placement,
    Probe,
    ReconciliationFinding,
)
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.reconciler import (
    HostProjectionRule,
    Reconciler,
    normalize_arch,
    normalize_wwn,
)


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


async def _seed_host(s, hostname: str = "node-test") -> Host:
    h = Host(hostname=hostname, primary_ip="10.0.6.99")
    s.add(h)
    await s.flush()
    return h


async def _seed_run(s, host_id: uuid.UUID, probe_name: str = "host.identity") -> DiscoveryRun:
    # Probe.name is unique — reuse the row if a prior _seed_run already inserted it.
    probe = (await s.execute(select(Probe).where(Probe.name == probe_name))).scalar_one_or_none()
    if probe is None:
        probe = Probe(
            name=probe_name,
            version="0.1.0",
            module_path=f"test:{probe_name}",
            required_privilege=PrivilegeLevel.USER,
            produces_keys=[],
        )
        s.add(probe)
        await s.flush()
    run = DiscoveryRun(
        host_id=host_id,
        probe_id=probe.id,
        probe_name=probe.name,
        probe_version=probe.version,
        privilege_level=PrivilegeLevel.USER,
    )
    s.add(run)
    await s.flush()
    return run


async def _record_observations(
    s,
    run_id: uuid.UUID,
    host_id: uuid.UUID,
    facts: dict[str, Any],
    *,
    recorded_at: datetime | None = None,
    confidence: Confidence | None = None,
) -> None:
    """Replay-style helper: write a key→value fixture into Observation rows."""
    for key, value in facts.items():
        obs = Observation(
            run_id=run_id,
            key=key,
            value=value,
            target_type=IntentTargetType.HOST,
            target_id=str(host_id),
        )
        if recorded_at is not None:
            obs.recorded_at = recorded_at
        if confidence is not None:
            obs.confidence = confidence
        s.add(obs)
    await s.flush()


# ---------------------------------------------------------------------------
# Core projection
# ---------------------------------------------------------------------------


async def test_projects_identity_observations_into_capabilities(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run = await _seed_run(s, host.id)
        await _record_observations(
            s,
            run.id,
            host.id,
            {
                "host.identity.hostname": "node-test",
                "host.identity.kernel": "6.8.0-49-generic",
                "host.identity.machine_id": "abc123def456",
                "host.identity.os_id": "ubuntu",
                "host.identity.os_pretty_name": "Ubuntu 24.04.1 LTS",
                "host.identity.boot_time_unix": 1_700_000_000,
            },
        )

        result = await Reconciler().reconcile_host(s, host.id)

    assert result.observations_seen == 6
    assert result.changes == {
        "capabilities.observed_hostname": "node-test",
        "capabilities.kernel": "6.8.0-49-generic",
        "capabilities.machine_id": "abc123def456",
        "capabilities.os_id": "ubuntu",
        "capabilities.os_pretty_name": "Ubuntu 24.04.1 LTS",
        "capabilities.boot_time_unix": 1_700_000_000,
    }

    async with sessionmaker() as s:
        h = (await s.execute(select(Host).where(Host.id == host.id))).scalar_one()
        assert h.capabilities["kernel"] == "6.8.0-49-generic"
        assert h.capabilities["os_id"] == "ubuntu"
        assert h.capabilities["boot_time_unix"] == 1_700_000_000
        assert h.discovery_last_run is not None
        assert h.last_verified is not None


async def test_reconcile_is_idempotent(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run = await _seed_run(s, host.id)
        await _record_observations(
            s,
            run.id,
            host.id,
            {"host.identity.kernel": "6.8.0-49-generic", "host.identity.os_id": "ubuntu"},
        )

        first = await Reconciler().reconcile_host(s, host.id)
        second = await Reconciler().reconcile_host(s, host.id)

    assert len(first.changes) == 2
    assert second.changes == {}
    assert second.observations_seen == 2


async def test_latest_observation_wins(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run = await _seed_run(s, host.id)

        older = datetime(2026, 1, 1, tzinfo=UTC)
        newer = older + timedelta(days=30)

        await _record_observations(
            s, run.id, host.id, {"host.identity.kernel": "6.5.0-old"}, recorded_at=older
        )
        await _record_observations(
            s, run.id, host.id, {"host.identity.kernel": "6.8.0-new"}, recorded_at=newer
        )

        result = await Reconciler().reconcile_host(s, host.id)

    assert result.observations_seen == 1
    assert result.changes == {"capabilities.kernel": "6.8.0-new"}


async def test_unknown_keys_are_ignored(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run = await _seed_run(s, host.id)
        await _record_observations(
            s,
            run.id,
            host.id,
            {
                "host.identity.kernel": "6.8.0-49-generic",
                # Future-slice keys the reconciler doesn't yet know about.
                "host.storage.devices[0].model": "Samsung 980",
                "host.network.interfaces[0].mac": "aa:bb:cc:dd:ee:ff",
            },
        )

        result = await Reconciler().reconcile_host(s, host.id)

    assert result.observations_seen == 1
    assert result.changes == {"capabilities.kernel": "6.8.0-49-generic"}


async def test_no_observations_means_no_changes_and_no_freshness_bump(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        assert host.discovery_last_run is None
        result = await Reconciler().reconcile_host(s, host.id)

    assert result.observations_seen == 0
    assert result.changes == {}

    async with sessionmaker() as s:
        h = (await s.execute(select(Host).where(Host.id == host.id))).scalar_one()
        # No observations ⇒ no probe ran ⇒ don't lie about freshness.
        assert h.discovery_last_run is None
        assert h.last_verified is None


async def test_missing_host_raises(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        with pytest.raises(ValueError, match="no Host"):
            await Reconciler().reconcile_host(s, uuid.uuid4())


# ---------------------------------------------------------------------------
# Rule registry sanity
# ---------------------------------------------------------------------------


def test_rule_requires_exactly_one_target() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        HostProjectionRule(key="x", attr="a", capability="c")
    with pytest.raises(ValueError, match="exactly one"):
        HostProjectionRule(key="x")


# ---------------------------------------------------------------------------
# Replay-style mini-fixture: a host walked through two probe runs
# ---------------------------------------------------------------------------


async def test_replay_two_runs_promotes_to_latest_state(sessionmaker) -> None:
    """A first probe run sets initial state; a later run updates kernel + boot_time.

    Asserts the reconciler tracks the freshest facts and only emits deltas for
    fields that actually changed.
    """
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s, hostname="node2")

        run1 = await _seed_run(s, host.id)
        t1 = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
        await _record_observations(
            s,
            run1.id,
            host.id,
            {
                "host.identity.hostname": "node2",
                "host.identity.kernel": "6.5.0-21-generic",
                "host.identity.os_id": "ubuntu",
                "host.identity.os_pretty_name": "Ubuntu 22.04.4 LTS",
                "host.identity.boot_time_unix": 1_714_500_000,
            },
            recorded_at=t1,
        )
        first = await Reconciler().reconcile_host(s, host.id)
        assert first.observations_seen == 5

        # 30 days later — kernel upgrade + reboot.
        run2 = await _seed_run(s, host.id)
        t2 = t1 + timedelta(days=30)
        await _record_observations(
            s,
            run2.id,
            host.id,
            {
                "host.identity.hostname": "node2",
                "host.identity.kernel": "6.8.0-49-generic",
                "host.identity.os_id": "ubuntu",
                "host.identity.os_pretty_name": "Ubuntu 24.04.1 LTS",
                "host.identity.boot_time_unix": 1_717_092_000,
            },
            recorded_at=t2,
        )
        second = await Reconciler().reconcile_host(s, host.id)

    # Only the three fields that actually changed should appear in deltas.
    assert second.changes == {
        "capabilities.kernel": "6.8.0-49-generic",
        "capabilities.os_pretty_name": "Ubuntu 24.04.1 LTS",
        "capabilities.boot_time_unix": 1_717_092_000,
    }


# ---------------------------------------------------------------------------
# host.cpu slice
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("x86_64", Architecture.AMD64),
        ("X86_64", Architecture.AMD64),
        ("amd64", Architecture.AMD64),
        ("x64", Architecture.AMD64),
        ("aarch64", Architecture.ARM64),
        ("arm64", Architecture.ARM64),
        ("arm", Architecture.ARM),
        ("armv?l", Architecture.ARM),
        ("riscv64", Architecture.OTHER),
        ("", Architecture.OTHER),
    ],
)
def test_normalize_arch_maps_common_spellings(raw: str, expected: Architecture) -> None:
    assert normalize_arch(raw) == expected


async def test_cpu_projection_sets_typed_arch_and_capability_bag(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run = await _seed_run(s, host.id, probe_name="host.cpu")
        await _record_observations(
            s,
            run.id,
            host.id,
            {
                "host.cpu.architecture": "x86_64",
                "host.cpu.model": "Example CPU",
                "host.cpu.vendor": "GenuineIntel",
                "host.cpu.sockets": 1,
                "host.cpu.cores": 4,
                "host.cpu.threads": 8,
                "host.cpu.threads_per_core": 2,
                "host.cpu.base_freq_mhz": 1600,
                "host.cpu.max_freq_mhz": 3900,
                "host.cpu.l1d_bytes": 131_072,
                "host.cpu.l1i_bytes": 131_072,
                "host.cpu.l2_bytes": 1_048_576,
                "host.cpu.l3_bytes": 6_291_456,
                "host.cpu.flags": ["avx", "avx2", "aes"],
                "host.cpu.interesting_flags": ["aes", "avx", "avx2"],
            },
        )

        result = await Reconciler().reconcile_host(s, host.id)

    assert result.observations_seen == 15
    # arch lands on the typed column, NOT in capabilities.
    assert result.changes["arch"] == Architecture.AMD64
    assert "capabilities.arch" not in result.changes
    # Spot-check a few capability projections.
    assert result.changes["capabilities.cpu_model"] == "Example CPU"
    assert result.changes["capabilities.cpu_cores"] == 4
    assert result.changes["capabilities.cpu_threads"] == 8
    assert result.changes["capabilities.cpu_l3_bytes"] == 6_291_456
    assert result.changes["capabilities.cpu_interesting_flags"] == ["aes", "avx", "avx2"]

    async with sessionmaker() as s:
        h = (await s.execute(select(Host).where(Host.id == host.id))).scalar_one()
        assert h.arch == Architecture.AMD64
        assert h.capabilities["cpu_vendor"] == "GenuineIntel"
        assert h.capabilities["cpu_sockets"] == 1


async def test_cpu_arch_transform_is_idempotent(sessionmaker) -> None:
    """Second reconcile with the same raw observation must not re-emit arch."""
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run = await _seed_run(s, host.id, probe_name="host.cpu")
        await _record_observations(s, run.id, host.id, {"host.cpu.architecture": "aarch64"})

        first = await Reconciler().reconcile_host(s, host.id)
        second = await Reconciler().reconcile_host(s, host.id)

    assert first.changes == {"arch": Architecture.ARM64}
    assert second.changes == {}


async def test_unknown_arch_falls_back_to_other(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run = await _seed_run(s, host.id, probe_name="host.cpu")
        await _record_observations(s, run.id, host.id, {"host.cpu.architecture": "riscv64"})

        result = await Reconciler().reconcile_host(s, host.id)

    # OTHER is the seeded default for a fresh Host — so the projection
    # decides "nothing to change," which is the right outcome for an
    # unrecognized arch. (A future finding-generation slice will surface
    # it as UNKNOWN_ARCH; the reconciler itself stays non-raising.)
    assert result.observations_seen == 1
    assert result.changes == {}

    async with sessionmaker() as s:
        h = (await s.execute(select(Host).where(Host.id == host.id))).scalar_one()
        assert h.arch == Architecture.OTHER


async def test_identity_and_cpu_reconcile_together(sessionmaker) -> None:
    """A real probe batch lands both namespaces; one reconcile applies both."""
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        identity_run = await _seed_run(s, host.id, probe_name="host.identity")
        await _record_observations(
            s,
            identity_run.id,
            host.id,
            {
                "host.identity.kernel": "6.8.0-49-generic",
                "host.identity.os_id": "ubuntu",
            },
        )
        cpu_run = await _seed_run(s, host.id, probe_name="host.cpu")
        await _record_observations(
            s,
            cpu_run.id,
            host.id,
            {
                "host.cpu.architecture": "aarch64",
                "host.cpu.model": "Cortex-A76",
                "host.cpu.cores": 4,
            },
        )

        result = await Reconciler().reconcile_host(s, host.id)

    assert result.observations_seen == 5
    assert result.changes == {
        "capabilities.kernel": "6.8.0-49-generic",
        "capabilities.os_id": "ubuntu",
        "arch": Architecture.ARM64,
        "capabilities.cpu_model": "Cortex-A76",
        "capabilities.cpu_cores": 4,
    }


# ---------------------------------------------------------------------------
# host.memory slice (totals only; DIMM lineage is a separate slice)
# ---------------------------------------------------------------------------


async def test_memory_totals_project_into_capabilities(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run = await _seed_run(s, host.id, probe_name="host.memory")
        await _record_observations(
            s,
            run.id,
            host.id,
            {
                "host.memory.mem_total_bytes": 16 * 1024**3,
                "host.memory.mem_available_bytes": 12 * 1024**3,
                "host.memory.mem_free_bytes": 6 * 1024**3,
                "host.memory.swap_total_bytes": 8 * 1024**3,
                "host.memory.swap_free_bytes": 8 * 1024**3,
                "host.memory.hugepages_total": 0,
                "host.memory.hugepages_free": 0,
                "host.memory.hugepagesize_bytes": 2 * 1024**2,
            },
        )

        result = await Reconciler().reconcile_host(s, host.id)

    assert result.observations_seen == 8
    assert result.changes == {
        "capabilities.mem_total_bytes": 16 * 1024**3,
        "capabilities.mem_available_bytes": 12 * 1024**3,
        "capabilities.mem_free_bytes": 6 * 1024**3,
        "capabilities.swap_total_bytes": 8 * 1024**3,
        "capabilities.swap_free_bytes": 8 * 1024**3,
        "capabilities.hugepages_total": 0,
        "capabilities.hugepages_free": 0,
        "capabilities.hugepagesize_bytes": 2 * 1024**2,
    }


async def test_memory_zero_values_round_trip_cleanly(sessionmaker) -> None:
    """Hugepage counts are routinely 0 — that's a real value, not 'no observation'.

    Idempotency must hold when the observed value is falsy: a second reconcile
    pass should report zero changes, not re-emit the zero as a new delta.
    """
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run = await _seed_run(s, host.id, probe_name="host.memory")
        await _record_observations(s, run.id, host.id, {"host.memory.hugepages_total": 0})

        first = await Reconciler().reconcile_host(s, host.id)
        second = await Reconciler().reconcile_host(s, host.id)

    assert first.changes == {"capabilities.hugepages_total": 0}
    assert second.changes == {}


async def test_all_three_namespaces_reconcile_in_one_call(sessionmaker) -> None:
    """A full warm probe batch hits identity + cpu + memory; reconcile applies all."""
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s, hostname="node0")

        identity_run = await _seed_run(s, host.id, probe_name="host.identity")
        await _record_observations(
            s,
            identity_run.id,
            host.id,
            {"host.identity.kernel": "6.8.0-49-generic"},
        )
        cpu_run = await _seed_run(s, host.id, probe_name="host.cpu")
        await _record_observations(
            s,
            cpu_run.id,
            host.id,
            {"host.cpu.architecture": "x86_64", "host.cpu.cores": 6},
        )
        mem_run = await _seed_run(s, host.id, probe_name="host.memory")
        await _record_observations(
            s,
            mem_run.id,
            host.id,
            {
                "host.memory.mem_total_bytes": 32 * 1024**3,
                "host.memory.swap_total_bytes": 4 * 1024**3,
            },
        )

        result = await Reconciler().reconcile_host(s, host.id)

    assert result.observations_seen == 5
    assert result.changes == {
        "capabilities.kernel": "6.8.0-49-generic",
        "arch": Architecture.AMD64,
        "capabilities.cpu_cores": 6,
        "capabilities.mem_total_bytes": 32 * 1024**3,
        "capabilities.swap_total_bytes": 4 * 1024**3,
    }


# ---------------------------------------------------------------------------
# DIMM PhysicalPart / Placement lineage
# ---------------------------------------------------------------------------


def _dimm(
    slot: str,
    serial: str | None = "S-DEFAULT",
    *,
    size_bytes: int | None = 16 * 1024**3,
    manufacturer: str | None = "Crucial",
    part_number: str | None = "CT16G4SFRA32A",
    speed_mts: int | None = 3200,
    dimm_type: str | None = "DDR4",
) -> dict[str, Any]:
    """Build a populated-slot dict matching the future dmidecode probe contract."""
    entry: dict[str, Any] = {"slot": slot}
    if serial is not None:
        entry["serial"] = serial
    if size_bytes is not None:
        entry["size_bytes"] = size_bytes
    if manufacturer is not None:
        entry["manufacturer"] = manufacturer
    if part_number is not None:
        entry["part_number"] = part_number
    if speed_mts is not None:
        entry["speed_mts"] = speed_mts
    if dimm_type is not None:
        entry["type"] = dimm_type
    return entry


async def _open_placements(s, host_id: uuid.UUID) -> list[Placement]:
    rows = (
        (
            await s.execute(
                select(Placement)
                .where(Placement.host_id == host_id, Placement.to_date.is_(None))
                .order_by(Placement.slot)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def test_first_dimm_observation_creates_part_and_placement(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run = await _seed_run(s, host.id, probe_name="host.memory.dimms")
        await _record_observations(
            s,
            run.id,
            host.id,
            {
                "host.memory.dimms": [
                    _dimm("DIMM_A1", serial="ABC123"),
                    _dimm("DIMM_A2", serial="ABC124"),
                ],
            },
        )

        result = await Reconciler().reconcile_host(s, host.id)

    assert result.observations_seen == 1
    assert result.parts_upserted == 2
    assert result.parts_skipped_no_identity == 0
    assert sorted(result.placements_opened) == [("ABC123", "DIMM_A1"), ("ABC124", "DIMM_A2")]
    assert result.placements_closed == []

    async with sessionmaker() as s:
        parts = (
            (await s.execute(select(PhysicalPart).where(PhysicalPart.kind == PartKind.DIMM)))
            .scalars()
            .all()
        )
        assert {p.serial for p in parts} == {"ABC123", "ABC124"}
        assert all(p.capacity_bytes == 16 * 1024**3 for p in parts)
        assert all(p.manufacturer == "Crucial" for p in parts)
        assert all(p.attributes.get("type") == "DDR4" for p in parts)
        open_p = await _open_placements(s, host.id)
        assert len(open_p) == 2
        assert {p.slot for p in open_p} == {"DIMM_A1", "DIMM_A2"}


async def test_dimm_lineage_is_idempotent(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run = await _seed_run(s, host.id, probe_name="host.memory.dimms")
        await _record_observations(
            s,
            run.id,
            host.id,
            {"host.memory.dimms": [_dimm("DIMM_A1", serial="ABC123")]},
        )

        first = await Reconciler().reconcile_host(s, host.id)
        second = await Reconciler().reconcile_host(s, host.id)

    assert first.parts_upserted == 1
    assert first.placements_opened == [("ABC123", "DIMM_A1")]
    assert second.parts_upserted == 0
    assert second.placements_opened == []
    assert second.placements_closed == []
    assert not second.touched_lineage

    async with sessionmaker() as s:
        # Re-run must not duplicate parts or placements.
        parts = (await s.execute(select(PhysicalPart))).scalars().all()
        placements = (await s.execute(select(Placement))).scalars().all()
        assert len(parts) == 1
        assert len(placements) == 1


async def test_dimm_removed_closes_placement(sessionmaker) -> None:
    """Probe run no longer reports a slot → its placement gets to_date set."""
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run1 = await _seed_run(s, host.id, probe_name="host.memory.dimms")
        await _record_observations(
            s,
            run1.id,
            host.id,
            {
                "host.memory.dimms": [
                    _dimm("DIMM_A1", serial="ABC123"),
                    _dimm("DIMM_A2", serial="ABC124"),
                ],
            },
            recorded_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        await Reconciler().reconcile_host(s, host.id)

        # Second run: A2 removed.
        run2 = await _seed_run(s, host.id, probe_name="host.memory.dimms")
        await _record_observations(
            s,
            run2.id,
            host.id,
            {"host.memory.dimms": [_dimm("DIMM_A1", serial="ABC123")]},
            recorded_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
        result = await Reconciler().reconcile_host(s, host.id)

    assert result.placements_opened == []
    assert result.placements_closed == [("ABC124", "DIMM_A2")]

    async with sessionmaker() as s:
        all_placements = (await s.execute(select(Placement))).scalars().all()
        assert len(all_placements) == 2  # history preserved
        closed = [p for p in all_placements if p.to_date is not None]
        assert len(closed) == 1
        assert closed[0].slot == "DIMM_A2"


async def test_dimm_moves_to_different_slot_same_host(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run1 = await _seed_run(s, host.id, probe_name="host.memory.dimms")
        await _record_observations(
            s,
            run1.id,
            host.id,
            {"host.memory.dimms": [_dimm("DIMM_A1", serial="ABC123")]},
            recorded_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        await Reconciler().reconcile_host(s, host.id)

        run2 = await _seed_run(s, host.id, probe_name="host.memory.dimms")
        await _record_observations(
            s,
            run2.id,
            host.id,
            {"host.memory.dimms": [_dimm("DIMM_B1", serial="ABC123")]},
            recorded_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
        result = await Reconciler().reconcile_host(s, host.id)

    assert result.parts_upserted == 0  # same part, same serial
    assert result.placements_opened == [("ABC123", "DIMM_B1")]
    assert result.placements_closed == [("ABC123", "DIMM_A1")]


async def test_dimm_moves_to_different_host(sessionmaker) -> None:
    """The lineage case the schema doc names — a DIMM crosses hosts."""
    async with session_scope(sessionmaker) as s:
        src = await _seed_host(s, hostname="node0")
        dst = await _seed_host(s, hostname="node1")

        run_src = await _seed_run(s, src.id, probe_name="host.memory.dimms")
        await _record_observations(
            s,
            run_src.id,
            src.id,
            {"host.memory.dimms": [_dimm("DIMM_A1", serial="ABC123")]},
            recorded_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        await Reconciler().reconcile_host(s, src.id)

        # Same serial now appears on dst.
        run_dst = await _seed_run(s, dst.id, probe_name="host.memory.dimms")
        await _record_observations(
            s,
            run_dst.id,
            dst.id,
            {"host.memory.dimms": [_dimm("DIMM_A1", serial="ABC123")]},
            recorded_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
        result = await Reconciler().reconcile_host(s, dst.id)

    assert result.parts_upserted == 0
    assert result.placements_opened == [("ABC123", "DIMM_A1")]
    # The prior placement on src — different host, same slot label — must close.
    assert result.placements_closed == [("ABC123", "DIMM_A1")]

    async with sessionmaker() as s:
        all_placements = (await s.execute(select(Placement))).scalars().all()
        assert len(all_placements) == 2  # history preserved
        open_rows = [p for p in all_placements if p.to_date is None]
        assert len(open_rows) == 1
        assert open_rows[0].host_id == dst.id


async def test_dimm_without_serial_is_skipped(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run = await _seed_run(s, host.id, probe_name="host.memory.dimms")
        await _record_observations(
            s,
            run.id,
            host.id,
            {
                "host.memory.dimms": [
                    _dimm("DIMM_A1", serial="ABC123"),
                    _dimm("DIMM_A2", serial=None),  # no serial
                ],
            },
        )

        result = await Reconciler().reconcile_host(s, host.id)

    assert result.parts_upserted == 1
    assert result.parts_skipped_no_identity == 1
    assert result.placements_opened == [("ABC123", "DIMM_A1")]

    async with sessionmaker() as s:
        parts = (await s.execute(select(PhysicalPart))).scalars().all()
        assert len(parts) == 1
        assert parts[0].serial == "ABC123"


async def test_dimm_part_enriched_on_later_observation(sessionmaker) -> None:
    """A part first seen with sparse metadata gets fields filled in later runs."""
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run1 = await _seed_run(s, host.id, probe_name="host.memory.dimms")
        await _record_observations(
            s,
            run1.id,
            host.id,
            {
                "host.memory.dimms": [
                    # First run: no manufacturer, no model.
                    _dimm(
                        "DIMM_A1",
                        serial="ABC123",
                        manufacturer=None,
                        part_number=None,
                    ),
                ],
            },
            recorded_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        await Reconciler().reconcile_host(s, host.id)

        run2 = await _seed_run(s, host.id, probe_name="host.memory.dimms")
        await _record_observations(
            s,
            run2.id,
            host.id,
            {"host.memory.dimms": [_dimm("DIMM_A1", serial="ABC123")]},
            recorded_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
        await Reconciler().reconcile_host(s, host.id)

    async with sessionmaker() as s:
        part = (await s.execute(select(PhysicalPart))).scalar_one()
        assert part.manufacturer == "Crucial"
        assert part.model == "CT16G4SFRA32A"


async def test_no_dimm_observation_does_not_close_placements(sessionmaker) -> None:
    """Reconcile without ever seeing a DIMM observation must not touch lineage.

    Otherwise re-running discovery for the cpu/identity slice alone would
    "remove" all DIMMs the framework has on record — a catastrophic surprise.
    """
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        # Seed a placement directly (simulates prior DIMM observation).
        part = PhysicalPart(kind=PartKind.DIMM, serial="ABC123", capacity_bytes=16 * 1024**3)
        s.add(part)
        await s.flush()
        s.add(Placement(part_id=part.id, host_id=host.id, slot="DIMM_A1"))
        await s.flush()

        # Only identity observation lands.
        run = await _seed_run(s, host.id, probe_name="host.identity")
        await _record_observations(s, run.id, host.id, {"host.identity.kernel": "6.8.0-49-generic"})

        result = await Reconciler().reconcile_host(s, host.id)

    assert result.placements_closed == []
    assert result.placements_opened == []
    assert result.parts_upserted == 0

    async with sessionmaker() as s:
        open_p = await _open_placements(s, host.id)
        assert len(open_p) == 1  # placement still open


# ---------------------------------------------------------------------------
# Storage PhysicalPart / Placement lineage
# ---------------------------------------------------------------------------


def _disk(
    name: str,
    *,
    wwn: str | None = "WWN-DEFAULT",
    serial: str | None = "SDISK-DEFAULT",
    size_bytes: int | None = 500 * 1024**3,
    vendor: str | None = "Samsung",
    model: str | None = "SSD 980",
    transport: str | None = "nvme",
    rotational: bool | None = False,
) -> dict[str, Any]:
    """Build a top-level disk dict matching the host.storage probe contract."""
    entry: dict[str, Any] = {"name": name, "type": "disk"}
    if wwn is not None:
        entry["wwn"] = wwn
    if serial is not None:
        entry["serial"] = serial
    if size_bytes is not None:
        entry["size_bytes"] = size_bytes
    if vendor is not None:
        entry["vendor"] = vendor
    if model is not None:
        entry["model"] = model
    if transport is not None:
        entry["transport"] = transport
    if rotational is not None:
        entry["rotational"] = rotational
    return entry


def _partition(name: str, parent: str) -> dict[str, Any]:
    """Non-disk entry — must be ignored by lineage."""
    return {"name": name, "type": "part", "parent": parent}


async def test_first_storage_observation_creates_part_and_placement(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run = await _seed_run(s, host.id, probe_name="host.storage")
        await _record_observations(
            s,
            run.id,
            host.id,
            {
                "host.storage.devices": [
                    _disk("nvme0n1", wwn="0025385191b1c4e2"),
                    _disk(
                        "sda", wwn=None, serial="SATA-SER-bbb", transport="sata", rotational=True
                    ),
                    _partition("nvme0n1p1", parent="nvme0n1"),  # not a part
                ],
            },
        )

        result = await Reconciler().reconcile_host(s, host.id)

    assert result.parts_upserted == 2
    assert result.parts_skipped_no_identity == 0
    assert sorted(result.placements_opened) == [
        ("0025385191b1c4e2", "/dev/nvme0n1"),
        ("SATA-SER-bbb", "/dev/sda"),
    ]
    assert result.placements_closed == []

    async with sessionmaker() as s:
        parts = (
            (await s.execute(select(PhysicalPart).where(PhysicalPart.kind != PartKind.DIMM)))
            .scalars()
            .all()
        )
        by_kind = {p.kind: p for p in parts}
        assert PartKind.NVME in by_kind  # transport=nvme
        assert PartKind.HDD in by_kind  # rotational=True
        assert by_kind[PartKind.NVME].wwid == "0025385191b1c4e2"
        assert by_kind[PartKind.HDD].serial == "SATA-SER-bbb"
        # WWN-keyed part still records serial if probe supplied one.
        assert by_kind[PartKind.NVME].serial == "SDISK-DEFAULT"


async def test_storage_lineage_is_idempotent(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run = await _seed_run(s, host.id, probe_name="host.storage")
        await _record_observations(
            s,
            run.id,
            host.id,
            {"host.storage.devices": [_disk("nvme0n1", wwn="0025385191b1c4e2")]},
        )

        first = await Reconciler().reconcile_host(s, host.id)
        second = await Reconciler().reconcile_host(s, host.id)

    assert first.parts_upserted == 1
    assert first.placements_opened == [("0025385191b1c4e2", "/dev/nvme0n1")]
    assert second.parts_upserted == 0
    assert second.placements_opened == []
    assert not second.touched_lineage


async def test_storage_disk_moves_to_different_host(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        src = await _seed_host(s, hostname="node0")
        dst = await _seed_host(s, hostname="node1")

        run_src = await _seed_run(s, src.id, probe_name="host.storage")
        await _record_observations(
            s,
            run_src.id,
            src.id,
            {"host.storage.devices": [_disk("nvme0n1", wwn="0025385191b1c4e2")]},
            recorded_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        await Reconciler().reconcile_host(s, src.id)

        # Same WWN appears on dst at a different slot label (renamed by kernel).
        run_dst = await _seed_run(s, dst.id, probe_name="host.storage")
        await _record_observations(
            s,
            run_dst.id,
            dst.id,
            {"host.storage.devices": [_disk("nvme1n1", wwn="0025385191b1c4e2")]},
            recorded_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
        result = await Reconciler().reconcile_host(s, dst.id)

    assert result.parts_upserted == 0
    assert result.placements_opened == [("0025385191b1c4e2", "/dev/nvme1n1")]
    assert result.placements_closed == [("0025385191b1c4e2", "/dev/nvme0n1")]

    async with sessionmaker() as s:
        all_placements = (await s.execute(select(Placement))).scalars().all()
        assert len(all_placements) == 2  # history preserved


async def test_storage_falls_back_to_serial_when_wwn_missing(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run = await _seed_run(s, host.id, probe_name="host.storage")
        await _record_observations(
            s,
            run.id,
            host.id,
            {"host.storage.devices": [_disk("sda", wwn=None, serial="serial-only")]},
        )

        result = await Reconciler().reconcile_host(s, host.id)

    assert result.placements_opened == [("serial-only", "/dev/sda")]
    async with sessionmaker() as s:
        part = (
            await s.execute(select(PhysicalPart).where(PhysicalPart.serial == "serial-only"))
        ).scalar_one()
        assert part.wwid is None  # nothing to record


async def test_forged_wwn_collision_keys_by_serial_and_flags(sessionmaker) -> None:
    """Distinct drives sharing a forged WWN stay distinct, with a finding each.

    Models the cp USB SSDs: identical ``naa.5000000000000099`` WWN, unique
    serials. Without the guard the reconciler would merge all three into one
    part and bounce its placement between hosts. With it, each drive is its own
    part keyed by serial, and every collider past the first raises a
    STORAGE_PROVENANCE_DELTA finding.
    """
    forged = "naa.5000000000000099"
    serials = ["SN0001", "SN0002", "SN0003"]
    async with session_scope(sessionmaker) as s:
        for i, serial in enumerate(serials):
            host = await _seed_host(s, hostname=f"cp{i + 1}")
            run = await _seed_run(s, host.id, probe_name="host.storage")
            await _record_observations(
                s,
                run.id,
                host.id,
                {
                    "host.storage.devices": [
                        _disk("sda", wwn=forged, serial=serial, transport="usb")
                    ]
                },
            )
            result = await Reconciler().reconcile_host(s, host.id)
            # No placement ever closes — the drives don't "move".
            assert result.placements_closed == []

    async with sessionmaker() as s:
        parts = (
            (await s.execute(select(PhysicalPart).where(PhysicalPart.kind != PartKind.DIMM)))
            .scalars()
            .all()
        )
        # Three distinct parts, one per serial — not one shared part.
        assert sorted(p.serial for p in parts) == sorted(serials)
        # Exactly one part owns the forged WWN (the first); the others are
        # serial-only so future WWN lookups stay unambiguous. Stored in the
        # canonical bare-hex form, not the notation the probe happened to use.
        assert [p.serial for p in parts if p.wwid == normalize_wwn(forged)] == ["SN0001"]
        assert {p.wwid for p in parts if p.serial in ("SN0002", "SN0003")} == {None}

        # Each of the three hosts keeps its own open placement (no bouncing).
        open_placements = (
            (await s.execute(select(Placement).where(Placement.to_date.is_(None)))).scalars().all()
        )
        assert len(open_placements) == 3
        assert len({pl.host_id for pl in open_placements}) == 3

        findings = await _open_findings(s)
        collisions = [f for f in findings if f.kind == FindingKind.STORAGE_PROVENANCE_DELTA]
        # Two colliders (cp2, cp3); cp1 established the WWN, so it isn't flagged.
        assert len(collisions) == 2
        assert all(normalize_wwn(forged) in f.title for f in collisions)


async def test_storage_disk_without_identity_is_skipped(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run = await _seed_run(s, host.id, probe_name="host.storage")
        await _record_observations(
            s,
            run.id,
            host.id,
            {
                "host.storage.devices": [
                    _disk("sda", wwn=None, serial=None),  # no identity at all
                    _disk("nvme0n1"),  # default WWN
                ],
            },
        )

        result = await Reconciler().reconcile_host(s, host.id)

    assert result.parts_upserted == 1
    assert result.parts_skipped_no_identity == 1
    assert result.placements_opened == [("WWN-DEFAULT", "/dev/nvme0n1")]


async def test_storage_partition_entries_are_ignored(sessionmaker) -> None:
    """type != 'disk' must never produce a PhysicalPart."""
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run = await _seed_run(s, host.id, probe_name="host.storage")
        await _record_observations(
            s,
            run.id,
            host.id,
            {
                "host.storage.devices": [
                    _partition("sda1", parent="sda"),
                    {"name": "vg-data", "type": "lvm"},
                    {"name": "md0", "type": "raid1"},
                ],
            },
        )

        result = await Reconciler().reconcile_host(s, host.id)

    assert result.parts_upserted == 0
    assert result.parts_skipped_no_identity == 0
    assert result.placements_opened == []


async def test_storage_part_kind_reclassifies_on_better_evidence(sessionmaker) -> None:
    """First run misses transport → OTHER; second run with transport → NVME."""
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run1 = await _seed_run(s, host.id, probe_name="host.storage")
        await _record_observations(
            s,
            run1.id,
            host.id,
            {
                "host.storage.devices": [
                    _disk(
                        "nvme0n1",
                        wwn="0025385191b1c4e2",
                        transport=None,
                        rotational=None,
                    ),
                ],
            },
            recorded_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        await Reconciler().reconcile_host(s, host.id)

        run2 = await _seed_run(s, host.id, probe_name="host.storage")
        await _record_observations(
            s,
            run2.id,
            host.id,
            {"host.storage.devices": [_disk("nvme0n1", wwn="0025385191b1c4e2")]},
            recorded_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
        await Reconciler().reconcile_host(s, host.id)

    async with sessionmaker() as s:
        part = (
            await s.execute(select(PhysicalPart).where(PhysicalPart.wwid == "0025385191b1c4e2"))
        ).scalar_one()
        assert part.kind == PartKind.NVME  # reclassified


async def test_no_storage_observation_does_not_close_storage_placements(sessionmaker) -> None:
    """Reconcile without a storage observation must not touch storage lineage."""
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        part = PhysicalPart(kind=PartKind.NVME, wwid="nvme-wwn-zzz")
        s.add(part)
        await s.flush()
        s.add(Placement(part_id=part.id, host_id=host.id, slot="/dev/nvme0n1"))
        await s.flush()

        # Only an identity observation lands — no storage probe data.
        run = await _seed_run(s, host.id, probe_name="host.identity")
        await _record_observations(s, run.id, host.id, {"host.identity.kernel": "6.8.0-49-generic"})

        result = await Reconciler().reconcile_host(s, host.id)

    assert result.placements_closed == []
    async with sessionmaker() as s:
        open_p = await _open_placements(s, host.id)
        assert len(open_p) == 1
        assert open_p[0].slot == "/dev/nvme0n1"


async def test_dimm_reconcile_does_not_touch_storage_placements(sessionmaker) -> None:
    """Regression: the close-on-this-host loop must filter by part kind.

    Without the filter, a DIMM-only observation would close every open
    placement on the host — including SSDs and NICs — because the active
    set for DIMMs doesn't contain them.
    """
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)

        # Seed a storage part + placement first.
        ssd = PhysicalPart(kind=PartKind.NVME, wwid="nvme-wwn-stable")
        s.add(ssd)
        await s.flush()
        s.add(Placement(part_id=ssd.id, host_id=host.id, slot="/dev/nvme0n1"))
        await s.flush()

        # Then a DIMM observation arrives — only DIMM data, no storage.
        run = await _seed_run(s, host.id, probe_name="host.memory.dimms")
        await _record_observations(
            s,
            run.id,
            host.id,
            {"host.memory.dimms": [_dimm("DIMM_A1", serial="DIMM-AAA")]},
        )

        result = await Reconciler().reconcile_host(s, host.id)

    # DIMM placement opened, storage placement untouched.
    assert ("DIMM-AAA", "DIMM_A1") in result.placements_opened
    assert result.placements_closed == []

    async with sessionmaker() as s:
        open_p = await _open_placements(s, host.id)
        slots = {p.slot for p in open_p}
        assert slots == {"DIMM_A1", "/dev/nvme0n1"}


async def test_storage_capability_scalars_project(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run = await _seed_run(s, host.id, probe_name="host.storage")
        await _record_observations(
            s,
            run.id,
            host.id,
            {
                "host.storage.disk_count": 2,
                "host.storage.disk_names": ["nvme0n1", "sda"],
                "host.storage.total_disk_bytes": 1_500 * 1024**3,
            },
        )

        result = await Reconciler().reconcile_host(s, host.id)

    assert result.changes == {
        "capabilities.disk_count": 2,
        "capabilities.disk_names": ["nvme0n1", "sda"],
        "capabilities.total_disk_bytes": 1_500 * 1024**3,
    }


# ---------------------------------------------------------------------------
# NIC PhysicalPart / Placement lineage
# ---------------------------------------------------------------------------


def _iface(
    name: str,
    *,
    mac: str | None = "aa:bb:cc:dd:ee:ff",
    link_type: str | None = "ether",
    mtu: int | None = 1500,
    speed_mbps: int | None = 1000,
    operstate: str | None = "UP",
    master: str | None = None,
) -> dict[str, Any]:
    """Build an interface dict matching the host.network probe contract."""
    entry: dict[str, Any] = {"name": name, "addresses": []}
    if mac is not None:
        entry["mac"] = mac
    if link_type is not None:
        entry["link_type"] = link_type
    if mtu is not None:
        entry["mtu"] = mtu
    if speed_mbps is not None:
        entry["speed_mbps"] = speed_mbps
    if operstate is not None:
        entry["operstate"] = operstate
    if master is not None:
        entry["master"] = master
    return entry


async def test_first_nic_observation_creates_part_and_placement(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run = await _seed_run(s, host.id, probe_name="host.network")
        await _record_observations(
            s,
            run.id,
            host.id,
            {
                "host.network.interfaces": [
                    _iface("enp1s0", mac="00:11:22:33:44:55"),
                    _iface("enp2s0", mac="00:11:22:33:44:56", speed_mbps=2500),
                ],
            },
        )

        result = await Reconciler().reconcile_host(s, host.id)

    assert result.parts_upserted == 2
    assert result.parts_skipped_no_identity == 0
    assert result.parts_skipped_filtered == 0
    assert sorted(result.placements_opened) == [
        ("00:11:22:33:44:55", "enp1s0"),
        ("00:11:22:33:44:56", "enp2s0"),
    ]

    async with sessionmaker() as s:
        parts = (
            (await s.execute(select(PhysicalPart).where(PhysicalPart.kind == PartKind.NIC)))
            .scalars()
            .all()
        )
        by_mac = {p.serial: p for p in parts}
        assert "00:11:22:33:44:55" in by_mac
        assert by_mac["00:11:22:33:44:56"].speed_mbps == 2500
        assert by_mac["00:11:22:33:44:55"].attributes.get("mtu") == 1500


async def test_nic_mac_is_normalized_to_lowercase(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run1 = await _seed_run(s, host.id, probe_name="host.network")
        await _record_observations(
            s,
            run1.id,
            host.id,
            {"host.network.interfaces": [_iface("eth0", mac="AA:BB:CC:DD:EE:FF")]},
            recorded_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        first = await Reconciler().reconcile_host(s, host.id)

        # Same MAC, different case — must not duplicate.
        run2 = await _seed_run(s, host.id, probe_name="host.network")
        await _record_observations(
            s,
            run2.id,
            host.id,
            {"host.network.interfaces": [_iface("eth0", mac="aa:bb:cc:dd:ee:ff")]},
            recorded_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
        second = await Reconciler().reconcile_host(s, host.id)

    assert first.parts_upserted == 1
    assert second.parts_upserted == 0
    assert not second.touched_lineage

    async with sessionmaker() as s:
        parts = (
            (await s.execute(select(PhysicalPart).where(PhysicalPart.kind == PartKind.NIC)))
            .scalars()
            .all()
        )
        assert len(parts) == 1
        assert parts[0].serial == "aa:bb:cc:dd:ee:ff"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0x5000000000000001", "5000000000000001"),
        ("naa.5000000000000001", "5000000000000001"),
        ("wwn-0x5000000000000001", "5000000000000001"),
        ("NAA.5000000000000001", "5000000000000001"),
        ("  0x5000000000000001  ", "5000000000000001"),
        ("nvme.eui.0025385191b1c4e2", "0025385191b1c4e2"),
        ("eui.0025385191b1c4e2", "0025385191b1c4e2"),
        ("5000-0000-0000-0001", "5000000000000001"),
        # Not hex once stripped — passed through untouched, never coerced.
        ("TPBF2209160050501345", "TPBF2209160050501345"),
        ("WWN-DEFAULT", "WWN-DEFAULT"),
        ("", ""),
    ],
)
def test_normalize_wwn(raw: str, expected: str) -> None:
    assert normalize_wwn(raw) == expected


async def test_wwn_notation_change_closes_prior_placement(sessionmaker) -> None:
    """One drive, two sources, two spellings — must be one part, not two.

    The real case: three Pis were reflashed from Talos to Ubuntu and renamed.
    Talos reported the USB SSD as ``naa.…``; lsblk reports the same drive as
    ``0x…``. Before normalization the disk placement stayed open on the old
    host while a duplicate part opened on the new one, so the drive appeared to
    be in two places at once.
    """
    async with session_scope(sessionmaker) as s:
        old_host = await _seed_host(s, hostname="pi-cp3")
        run = await _seed_run(s, old_host.id, probe_name="host.storage")
        await _record_observations(
            s,
            run.id,
            old_host.id,
            {"host.storage.devices": [_disk("sda", wwn="naa.5000000000000001", serial=None)]},
        )
        await Reconciler().reconcile_host(s, old_host.id)

        new_host = await _seed_host(s, hostname="wynode2")
        run2 = await _seed_run(s, new_host.id, probe_name="host.storage")
        await _record_observations(
            s,
            run2.id,
            new_host.id,
            {"host.storage.devices": [_disk("sda", wwn="0x5000000000000001", serial=None)]},
        )
        result = await Reconciler().reconcile_host(s, new_host.id)

        parts = (
            (await s.execute(select(PhysicalPart).where(PhysicalPart.wwid == "5000000000000001")))
            .scalars()
            .all()
        )
        open_placements = (
            (await s.execute(select(Placement).where(Placement.to_date.is_(None)))).scalars().all()
        )

    assert len(parts) == 1  # one drive, one part — not one per notation
    assert [p.host_id for p in open_placements] == [new_host.id]
    assert result.placements_closed == [("5000000000000001", "/dev/sda")]


async def test_virtual_interfaces_are_filtered_not_no_identity(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run = await _seed_run(s, host.id, probe_name="host.network")
        await _record_observations(
            s,
            run.id,
            host.id,
            {
                "host.network.interfaces": [
                    _iface("enp1s0", mac="aa:bb:cc:00:00:01"),  # real
                    _iface("br0", mac="aa:bb:cc:00:00:02"),  # bridge
                    _iface("docker0", mac="aa:bb:cc:00:00:03"),
                    _iface("veth1234", mac="aa:bb:cc:00:00:04"),
                    _iface("tailscale0", mac="aa:bb:cc:00:00:05"),
                    _iface("wg0", mac="aa:bb:cc:00:00:06"),
                    _iface("bond0", mac="aa:bb:cc:00:00:07"),
                    # Proxmox firewall plumbing — must be filtered, not parts.
                    _iface("fwbr100i0", mac="aa:bb:cc:00:00:08"),
                    _iface("fwpr100p0", mac="aa:bb:cc:00:00:09"),
                    _iface("fwln100i0", mac="aa:bb:cc:00:00:0a"),
                ],
            },
        )

        result = await Reconciler().reconcile_host(s, host.id)

    assert result.parts_upserted == 1
    assert result.parts_skipped_no_identity == 0
    # br0 docker0 veth1234 tailscale0 wg0 bond0 + fwbr/fwpr/fwln
    assert result.parts_skipped_filtered == 9
    assert result.placements_opened == [("aa:bb:cc:00:00:01", "enp1s0")]


async def test_loopback_and_other_link_types_are_filtered(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run = await _seed_run(s, host.id, probe_name="host.network")
        await _record_observations(
            s,
            run.id,
            host.id,
            {
                "host.network.interfaces": [
                    _iface("enp1s0", mac="aa:bb:cc:00:00:01"),
                    # link_type loopback — has a MAC (00:00:00:00:00:00) but
                    # _is_real_nic returns False on link_type != ether.
                    _iface("lo", mac="00:00:00:00:00:00", link_type="loopback"),
                ],
            },
        )

        result = await Reconciler().reconcile_host(s, host.id)

    assert result.parts_upserted == 1
    # Loopback skips via filter, not no-identity.
    assert result.parts_skipped_filtered == 1


async def test_nic_without_mac_is_skipped_no_identity(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run = await _seed_run(s, host.id, probe_name="host.network")
        await _record_observations(
            s,
            run.id,
            host.id,
            {
                "host.network.interfaces": [
                    _iface("enp1s0", mac="aa:bb:cc:00:00:01"),
                    _iface("enp2s0", mac=None),  # no MAC
                ],
            },
        )

        result = await Reconciler().reconcile_host(s, host.id)

    assert result.parts_upserted == 1
    assert result.parts_skipped_no_identity == 1
    assert result.parts_skipped_filtered == 0


async def test_nic_lineage_is_idempotent(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run = await _seed_run(s, host.id, probe_name="host.network")
        await _record_observations(
            s,
            run.id,
            host.id,
            {"host.network.interfaces": [_iface("eth0", mac="aa:bb:cc:dd:ee:ff")]},
        )

        first = await Reconciler().reconcile_host(s, host.id)
        second = await Reconciler().reconcile_host(s, host.id)

    assert first.parts_upserted == 1
    assert second.parts_upserted == 0
    assert second.placements_opened == []
    assert second.placements_closed == []


async def test_nic_moves_to_different_host(sessionmaker) -> None:
    """A whole NIC card moved between hosts — same MAC, new host."""
    async with session_scope(sessionmaker) as s:
        src = await _seed_host(s, hostname="node0")
        dst = await _seed_host(s, hostname="node1")

        run_src = await _seed_run(s, src.id, probe_name="host.network")
        await _record_observations(
            s,
            run_src.id,
            src.id,
            {"host.network.interfaces": [_iface("enp1s0", mac="aa:bb:cc:dd:ee:ff")]},
            recorded_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        await Reconciler().reconcile_host(s, src.id)

        run_dst = await _seed_run(s, dst.id, probe_name="host.network")
        await _record_observations(
            s,
            run_dst.id,
            dst.id,
            # Same MAC; new host may rename the slot.
            {"host.network.interfaces": [_iface("eno1", mac="aa:bb:cc:dd:ee:ff")]},
            recorded_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
        result = await Reconciler().reconcile_host(s, dst.id)

    assert result.parts_upserted == 0
    assert result.placements_opened == [("aa:bb:cc:dd:ee:ff", "eno1")]
    assert result.placements_closed == [("aa:bb:cc:dd:ee:ff", "enp1s0")]


async def test_nic_removed_closes_placement(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run1 = await _seed_run(s, host.id, probe_name="host.network")
        await _record_observations(
            s,
            run1.id,
            host.id,
            {
                "host.network.interfaces": [
                    _iface("enp1s0", mac="aa:bb:cc:00:00:01"),
                    _iface("enp2s0", mac="aa:bb:cc:00:00:02"),
                ],
            },
            recorded_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        await Reconciler().reconcile_host(s, host.id)

        run2 = await _seed_run(s, host.id, probe_name="host.network")
        await _record_observations(
            s,
            run2.id,
            host.id,
            {"host.network.interfaces": [_iface("enp1s0", mac="aa:bb:cc:00:00:01")]},
            recorded_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
        result = await Reconciler().reconcile_host(s, host.id)

    assert result.placements_opened == []
    assert result.placements_closed == [("aa:bb:cc:00:00:02", "enp2s0")]


async def test_no_network_observation_does_not_close_nic_placements(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        nic = PhysicalPart(kind=PartKind.NIC, serial="aa:bb:cc:dd:ee:ff")
        s.add(nic)
        await s.flush()
        s.add(Placement(part_id=nic.id, host_id=host.id, slot="enp1s0"))
        await s.flush()

        run = await _seed_run(s, host.id, probe_name="host.identity")
        await _record_observations(s, run.id, host.id, {"host.identity.kernel": "6.8.0-49-generic"})

        result = await Reconciler().reconcile_host(s, host.id)

    assert result.placements_closed == []
    async with sessionmaker() as s:
        open_p = await _open_placements(s, host.id)
        assert len(open_p) == 1
        assert open_p[0].slot == "enp1s0"


async def test_storage_reconcile_does_not_touch_nic_placements(sessionmaker) -> None:
    """Cross-kind regression: storage close-loop must not disturb NIC rows."""
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)

        nic = PhysicalPart(kind=PartKind.NIC, serial="aa:bb:cc:dd:ee:ff")
        s.add(nic)
        await s.flush()
        s.add(Placement(part_id=nic.id, host_id=host.id, slot="enp1s0"))
        await s.flush()

        run = await _seed_run(s, host.id, probe_name="host.storage")
        await _record_observations(
            s,
            run.id,
            host.id,
            {"host.storage.devices": [_disk("nvme0n1", wwn="0025385191b1c4e2")]},
        )
        result = await Reconciler().reconcile_host(s, host.id)

    assert result.placements_closed == []
    assert ("0025385191b1c4e2", "/dev/nvme0n1") in result.placements_opened

    async with sessionmaker() as s:
        open_p = await _open_placements(s, host.id)
        slots = {p.slot for p in open_p}
        assert slots == {"enp1s0", "/dev/nvme0n1"}


async def test_network_capability_scalars_project(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run = await _seed_run(s, host.id, probe_name="host.network")
        await _record_observations(
            s,
            run.id,
            host.id,
            {
                "host.network.interface_count": 3,
                "host.network.interface_names": ["enp1s0", "br0", "docker0"],
            },
        )

        result = await Reconciler().reconcile_host(s, host.id)

    assert result.changes == {
        "capabilities.network_interface_count": 3,
        "capabilities.network_interface_names": ["enp1s0", "br0", "docker0"],
    }


# ---------------------------------------------------------------------------
# Finding generation with deterministic fingerprint dedup + auto-resolve
# ---------------------------------------------------------------------------


async def _open_findings(s) -> list[ReconciliationFinding]:
    rows = (
        (
            await s.execute(
                select(ReconciliationFinding).where(
                    ReconciliationFinding.status == FindingStatus.OPEN
                )
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def test_dimm_without_serial_emits_inventory_gap_finding(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run = await _seed_run(s, host.id, probe_name="host.memory.dimms")
        await _record_observations(
            s,
            run.id,
            host.id,
            {"host.memory.dimms": [_dimm("DIMM_A1", serial=None)]},
        )

        result = await Reconciler().reconcile_host(s, host.id)

    assert result.parts_skipped_no_identity == 1
    assert len(result.findings_opened) == 1
    assert len(result.findings_resolved) == 0

    async with sessionmaker() as s:
        findings = await _open_findings(s)
        assert len(findings) == 1
        f = findings[0]
        assert f.kind == FindingKind.INVENTORY_GAP
        assert f.severity == FindingSeverity.LOW
        assert "DIMM_A1" in f.title
        # affected list carries both host scope and category marker.
        targets = {(a["target_type"], a["target_id"]) for a in f.affected}
        assert ("host", str(host.id)) in targets
        assert ("inventory-category", "dimm") in targets


async def test_finding_fingerprint_is_deterministic_and_dedups(sessionmaker) -> None:
    """Re-running the same no-serial observation must not duplicate findings."""
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run1 = await _seed_run(s, host.id, probe_name="host.memory.dimms")
        await _record_observations(
            s,
            run1.id,
            host.id,
            {"host.memory.dimms": [_dimm("DIMM_A1", serial=None)]},
            recorded_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        first = await Reconciler().reconcile_host(s, host.id)

        run2 = await _seed_run(s, host.id, probe_name="host.memory.dimms")
        await _record_observations(
            s,
            run2.id,
            host.id,
            {"host.memory.dimms": [_dimm("DIMM_A1", serial=None)]},
            recorded_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
        second = await Reconciler().reconcile_host(s, host.id)

    assert first.findings_opened == [first.findings_opened[0]]
    assert second.findings_opened == []
    assert second.findings_updated == first.findings_opened  # same fingerprint
    assert second.findings_resolved == []

    async with sessionmaker() as s:
        all_findings = (await s.execute(select(ReconciliationFinding))).scalars().all()
        assert len(all_findings) == 1  # no duplicate
        assert all_findings[0].last_seen > all_findings[0].first_seen


async def test_finding_auto_resolves_when_condition_clears(sessionmaker) -> None:
    """DIMM first seen with no serial; later seen WITH serial → finding resolved."""
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run1 = await _seed_run(s, host.id, probe_name="host.memory.dimms")
        await _record_observations(
            s,
            run1.id,
            host.id,
            {"host.memory.dimms": [_dimm("DIMM_A1", serial=None)]},
            recorded_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        await Reconciler().reconcile_host(s, host.id)

        run2 = await _seed_run(s, host.id, probe_name="host.memory.dimms")
        await _record_observations(
            s,
            run2.id,
            host.id,
            {"host.memory.dimms": [_dimm("DIMM_A1", serial="NOW-HAS-SERIAL")]},
            recorded_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
        result = await Reconciler().reconcile_host(s, host.id)

    assert len(result.findings_resolved) == 1
    assert result.findings_opened == []

    async with sessionmaker() as s:
        findings = (await s.execute(select(ReconciliationFinding))).scalars().all()
        assert len(findings) == 1  # the same row, now RESOLVED
        assert findings[0].status == FindingStatus.RESOLVED
        assert findings[0].resolved_at is not None


async def test_finding_reopens_when_condition_recurs(sessionmaker) -> None:
    """DIMM serial regression: gone → present → gone → present.

    Probe runs sometimes regress on whether they read serial (sudo level
    changed, dmidecode flaked, etc.). A finding that auto-resolved must be
    able to flip back to OPEN when the gap recurs, not silently stay closed.
    """
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)

        async def _record(serial: str | None, when: datetime) -> None:
            run = await _seed_run(s, host.id, probe_name="host.memory.dimms")
            await _record_observations(
                s,
                run.id,
                host.id,
                {"host.memory.dimms": [_dimm("DIMM_A1", serial=serial)]},
                recorded_at=when,
            )

        await _record(None, datetime(2026, 3, 1, tzinfo=UTC))
        r1 = await Reconciler().reconcile_host(s, host.id)
        assert len(r1.findings_opened) == 1
        first_fp = r1.findings_opened[0]

        await _record("ABC", datetime(2026, 4, 1, tzinfo=UTC))
        r2 = await Reconciler().reconcile_host(s, host.id)
        assert len(r2.findings_resolved) == 1

        await _record(None, datetime(2026, 5, 1, tzinfo=UTC))
        r3 = await Reconciler().reconcile_host(s, host.id)
        assert r3.findings_opened == [first_fp]  # same fingerprint — reopened
        assert r3.findings_resolved == []

    async with sessionmaker() as s:
        all_findings = (await s.execute(select(ReconciliationFinding))).scalars().all()
        assert len(all_findings) == 1  # still one row, status OPEN
        assert all_findings[0].status == FindingStatus.OPEN


async def test_distinct_slots_get_distinct_fingerprints(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run = await _seed_run(s, host.id, probe_name="host.memory.dimms")
        await _record_observations(
            s,
            run.id,
            host.id,
            {
                "host.memory.dimms": [
                    _dimm("DIMM_A1", serial=None),
                    _dimm("DIMM_A2", serial=None),
                ],
            },
        )

        result = await Reconciler().reconcile_host(s, host.id)

    assert len(result.findings_opened) == 2
    assert len(set(result.findings_opened)) == 2  # distinct fingerprints


async def test_absent_observation_does_not_auto_resolve(sessionmaker) -> None:
    """The critical safety property: if a category's observation is missing,
    findings of that category MUST NOT be auto-resolved — we have no evidence
    the condition is gone.
    """
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run1 = await _seed_run(s, host.id, probe_name="host.memory.dimms")
        await _record_observations(
            s,
            run1.id,
            host.id,
            {"host.memory.dimms": [_dimm("DIMM_A1", serial=None)]},
            recorded_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        await Reconciler().reconcile_host(s, host.id)

        # Second reconcile: NO DIMM observation at all (different category).
        run2 = await _seed_run(s, host.id, probe_name="host.identity")
        await _record_observations(s, run2.id, host.id, {"host.identity.kernel": "6.8.0"})
        result = await Reconciler().reconcile_host(s, host.id)

    assert result.findings_resolved == []  # absence is not evidence
    async with sessionmaker() as s:
        findings = await _open_findings(s)
        assert len(findings) == 1  # still open


async def test_storage_no_identity_emits_finding(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run = await _seed_run(s, host.id, probe_name="host.storage")
        await _record_observations(
            s,
            run.id,
            host.id,
            {
                "host.storage.devices": [
                    _disk("sda", wwn=None, serial=None),
                ],
            },
        )

        result = await Reconciler().reconcile_host(s, host.id)

    assert result.parts_skipped_no_identity == 1
    assert len(result.findings_opened) == 1
    async with sessionmaker() as s:
        findings = await _open_findings(s)
        assert len(findings) == 1
        assert "sda" in findings[0].title
        assert ("inventory-category", "storage") in {
            (a["target_type"], a["target_id"]) for a in findings[0].affected
        }


async def test_categories_are_independent_for_auto_resolve(sessionmaker) -> None:
    """A DIMM observation in run N+1 must not auto-resolve a storage finding."""
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        # Run 1: storage device with no identity → storage finding.
        run1 = await _seed_run(s, host.id, probe_name="host.storage")
        await _record_observations(
            s,
            run1.id,
            host.id,
            {"host.storage.devices": [_disk("sda", wwn=None, serial=None)]},
            recorded_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        await Reconciler().reconcile_host(s, host.id)

        # Run 2: only a DIMM observation (no storage) — storage finding
        # must stay open; the DIMM has a serial so no DIMM finding.
        run2 = await _seed_run(s, host.id, probe_name="host.memory.dimms")
        await _record_observations(
            s,
            run2.id,
            host.id,
            {"host.memory.dimms": [_dimm("DIMM_A1", serial="GOOD")]},
            recorded_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
        result = await Reconciler().reconcile_host(s, host.id)

    assert result.findings_resolved == []
    async with sessionmaker() as s:
        open_f = await _open_findings(s)
        assert len(open_f) == 1
        assert "sda" in open_f[0].title


async def test_no_findings_when_everything_is_clean(sessionmaker) -> None:
    """Baseline: a fully-identified probe batch produces no INVENTORY_GAP."""
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run = await _seed_run(s, host.id, probe_name="host.memory.dimms")
        await _record_observations(
            s,
            run.id,
            host.id,
            {
                "host.memory.dimms": [_dimm("DIMM_A1", serial="GOOD")],
                "host.storage.devices": [_disk("nvme0n1", wwn="wwn-good")],
            },
        )

        result = await Reconciler().reconcile_host(s, host.id)

    assert result.findings_opened == []
    assert result.findings_resolved == []
    assert not result.touched_findings
    async with sessionmaker() as s:
        assert len(await _open_findings(s)) == 0


async def test_finding_description_refreshes_on_re_run(sessionmaker) -> None:
    """If the human-facing wording changes (between code versions, say), an
    existing finding's description gets refreshed without losing identity.
    """
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run = await _seed_run(s, host.id, probe_name="host.memory.dimms")
        await _record_observations(
            s,
            run.id,
            host.id,
            {"host.memory.dimms": [_dimm("DIMM_A1", serial=None)]},
        )
        await Reconciler().reconcile_host(s, host.id)

        # Mutate the description in-place to simulate a stale wording.
        async with sessionmaker() as s2:
            f = (await s2.execute(select(ReconciliationFinding))).scalar_one()
            f.description = "stale wording from a previous code version"
            await s2.commit()

        await Reconciler().reconcile_host(s, host.id)

    async with sessionmaker() as s:
        f = (await s.execute(select(ReconciliationFinding))).scalar_one()
        assert "stale wording" not in f.description
        assert "DIMM" in f.description


# ---------------------------------------------------------------------------
# Multi-source precedence (confidence beats recency)
# ---------------------------------------------------------------------------


async def test_verified_beats_newer_inferred(sessionmaker) -> None:
    """A kernel-probe (VERIFIED) fact wins over a newer management-plane (INFERRED)."""
    older = datetime(2026, 1, 1, tzinfo=UTC)
    newer = datetime(2026, 6, 1, tzinfo=UTC)
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run = await _seed_run(s, host.id)
        await _record_observations(
            s,
            run.id,
            host.id,
            {"host.identity.kernel": "6.5.0-kernel"},
            recorded_at=older,
            confidence=Confidence.VERIFIED,
        )
        await _record_observations(
            s,
            run.id,
            host.id,
            {"host.identity.kernel": "9.9.9-mgmt"},
            recorded_at=newer,
            confidence=Confidence.INFERRED,
        )
        await Reconciler().reconcile_host(s, host.id)

    async with sessionmaker() as s:
        host = (await s.execute(select(Host).where(Host.hostname == host.hostname))).scalar_one()
        assert host.capabilities["kernel"] == "6.5.0-kernel"  # VERIFIED, despite being older


async def test_inferred_fills_key_kernel_never_reported(sessionmaker) -> None:
    """Management-plane data is used for keys no higher-confidence source covers."""
    async with session_scope(sessionmaker) as s:
        host = await _seed_host(s)
        run = await _seed_run(s, host.id)
        await _record_observations(
            s,
            run.id,
            host.id,
            {"host.identity.os_pretty_name": "Proxmox VE 8"},
            confidence=Confidence.INFERRED,
        )
        await Reconciler().reconcile_host(s, host.id)

    async with sessionmaker() as s:
        host = (await s.execute(select(Host).where(Host.hostname == host.hostname))).scalar_one()
        assert host.capabilities["os_pretty_name"] == "Proxmox VE 8"
