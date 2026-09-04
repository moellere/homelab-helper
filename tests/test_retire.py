"""Host retire, part merge, and resolver-slice retire — the cleanup verbs."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from homelab_helper.db.base import Base
from homelab_helper.db.enums import (
    DiscoverySource,
    FindingKind,
    FindingSeverity,
    FindingStatus,
    IntentState,
    PartKind,
    PowerState,
    ResolutionScope,
)
from homelab_helper.db.models import (
    Host,
    OperationalIntent,
    PhysicalPart,
    Placement,
    ReconciliationFinding,
    Service,
    ServiceEndpoint,
)
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.placement import recommend_placement
from homelab_helper.engine.rebalance import plan_rebalance
from homelab_helper.engine.retire import (
    PartLookupError,
    find_part,
    is_retired,
    list_resolver_slices,
    merge_parts,
    retire_host,
    retire_resolver_slice,
    retired_host_ids,
)
from homelab_helper.engine.workloads import load_workload_library
from homelab_helper.llm.context import build_lab_context


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; compare in UTC."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


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


async def _seed(s) -> tuple[Host, Host]:
    old = Host(
        hostname="pi-cp1", primary_ip="10.0.1.11", capabilities={"mem_total_bytes": 8 * 1024**3}
    )
    new = Host(hostname="wyhome", primary_ip="10.254.0.11")
    s.add_all([old, new])
    await s.flush()
    ssd = PhysicalPart(kind=PartKind.SSD, serial="SSD-A", model="Kingston A400")
    nic = PhysicalPart(kind=PartKind.NIC, serial="aa:bb:cc:dd:ee:01")
    other = PhysicalPart(kind=PartKind.SSD, serial="SSD-Z")
    s.add_all([ssd, nic, other])
    await s.flush()
    s.add_all(
        [
            Placement(part_id=ssd.id, host_id=old.id, slot="sda"),
            Placement(part_id=nic.id, host_id=old.id, slot="eth0"),
            Placement(part_id=other.id, host_id=new.id, slot="sda"),
        ]
    )
    s.add_all(
        [
            ReconciliationFinding(
                kind=FindingKind.INVENTORY_GAP,
                severity=FindingSeverity.LOW,
                fingerprint="f-old-1",
                title="gap on pi-cp1",
                description="",
                affected=[{"target_type": "host", "target_id": str(old.id)}],
            ),
            ReconciliationFinding(
                kind=FindingKind.INVENTORY_GAP,
                severity=FindingSeverity.LOW,
                fingerprint="f-new-1",
                title="gap on wyhome",
                description="",
                affected=[{"target_type": "host", "target_id": str(new.id)}],
            ),
            ReconciliationFinding(
                kind=FindingKind.INVENTORY_GAP,
                severity=FindingSeverity.LOW,
                fingerprint="f-old-resolved",
                title="already resolved",
                description="",
                status=FindingStatus.RESOLVED,
                affected=[{"target_type": "host", "target_id": str(old.id)}],
            ),
        ]
    )
    await s.flush()
    return old, new


async def test_retire_closes_placements_resolves_findings_records_intent(sessionmaker) -> None:
    when = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    async with session_scope(sessionmaker) as s:
        old, new = await _seed(s)
        result = await retire_host(
            s, old, declared_by="enoch", rationale="reflashed as wyhome", when=when
        )
        assert result.already_retired is False
        assert sorted(result.placements_closed) == [("SSD-A", "sda"), ("aa:bb:cc:dd:ee:01", "eth0")]
        assert result.findings_resolved == ["f-old-1"]

        placements = (await s.execute(select(Placement))).scalars().all()
        by_host = {}
        for p in placements:
            by_host.setdefault(p.host_id, []).append(p)
        assert all(_aware(p.to_date) == when for p in by_host[old.id])
        assert all(p.to_date is None for p in by_host[new.id])  # other host untouched
        assert "retired" in (by_host[old.id][0].notes or "")

        f_old = (
            await s.execute(
                select(ReconciliationFinding).where(ReconciliationFinding.fingerprint == "f-old-1")
            )
        ).scalar_one()
        f_new = (
            await s.execute(
                select(ReconciliationFinding).where(ReconciliationFinding.fingerprint == "f-new-1")
            )
        ).scalar_one()
        assert f_old.status is FindingStatus.RESOLVED
        assert _aware(f_old.resolved_at) == when
        assert f_new.status is FindingStatus.OPEN

        intents = (await s.execute(select(OperationalIntent))).scalars().all()
        assert len(intents) == 1
        assert intents[0].intent is IntentState.DECOMMISSIONING
        assert intents[0].target_id == str(old.id)
        assert intents[0].declared_by == "enoch"
        assert intents[0].rationale == "reflashed as wyhome"
        assert old.expected_power_state is PowerState.OFF
        assert await is_retired(s, old.id)
        assert not await is_retired(s, new.id)


async def test_retire_is_idempotent(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        old, _ = await _seed(s)
        first = await retire_host(s, old, declared_by="enoch")
        second = await retire_host(s, old, declared_by="enoch")
        assert first.already_retired is False
        assert second.already_retired is True
        assert second.placements_closed == []
        assert second.findings_resolved == []
        assert len((await s.execute(select(OperationalIntent))).scalars().all()) == 1


async def test_retired_hosts_leave_the_planners_and_are_tagged_in_chat(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        old, _new = await _seed(s)
        await retire_host(s, old, declared_by="enoch")
        assert await retired_host_ids(s) == {old.id}

        report = await recommend_placement(s, load_workload_library()["immich"])
        rejected = dict(report.rejected)
        assert "decommissioning" in rejected.get("pi-cp1", "")

        rebalance = await plan_rebalance(s)
        assert "pi-cp1" not in {h.hostname for h in rebalance.hosts} | set(rebalance.unknown_hosts)

        context = await build_lab_context(s)
        assert "pi-cp1" in context
        assert "RETIRED" in context.split("pi-cp1", 1)[1].split("\n", 1)[0]


# ---------------------------------------------------------------------------
# part merge
# ---------------------------------------------------------------------------


async def test_merge_moves_placements_fills_gaps_and_deletes_duplicate(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        _old, new = await _seed(s)
        # The same drive, reported by the new era under a different identity.
        dup = PhysicalPart(
            kind=PartKind.SSD,
            wwid="0x5000c500deadbeef",
            capacity_bytes=480 * 10**9,
            attributes={"firmware": "SBFK"},
        )
        s.add(dup)
        await s.flush()
        s.add(Placement(part_id=dup.id, host_id=new.id, slot="sdb"))
        await s.flush()
        survivor = await find_part(s, "SSD-A")

        result = await merge_parts(s, dup, survivor)
        assert result.placements_moved == 1
        assert result.fields_filled == ["wwid", "capacity_bytes"]

        assert (
            await s.execute(select(PhysicalPart).where(PhysicalPart.id == dup.id))
        ).scalar_one_or_none() is None
        assert survivor.wwid == "0x5000c500deadbeef"
        assert survivor.model == "Kingston A400"  # survivor's own field wins
        assert survivor.attributes["firmware"] == "SBFK"
        assert survivor.attributes["merged_from"][0]["wwid"] == "0x5000c500deadbeef"
        slots = sorted(
            p.slot
            for p in (await s.execute(select(Placement).where(Placement.part_id == survivor.id)))
            .scalars()
            .all()
        )
        assert slots == ["sda", "sdb"]


async def test_merge_refuses_kind_mismatch_and_self(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await _seed(s)
        ssd = await find_part(s, "SSD-A")
        nic = await find_part(s, "aa:bb:cc:dd:ee:01")
        with pytest.raises(ValueError, match="kind mismatch"):
            await merge_parts(s, nic, ssd)
        with pytest.raises(ValueError, match="itself"):
            await merge_parts(s, ssd, ssd)


async def test_find_part_by_prefix_serial_wwid_and_ambiguity(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await _seed(s)
        ssd = await find_part(s, "ssd-a")  # case-insensitive serial
        assert (
            await find_part(s, str(ssd.id)[:20])
        ).id == ssd.id  # uuid7: the first 12 chars are the millisecond
        with pytest.raises(PartLookupError, match="no part"):
            await find_part(s, "nope")
        with pytest.raises(PartLookupError, match="empty"):
            await find_part(s, "")
        s.add(PhysicalPart(kind=PartKind.HDD, serial="SSD-A"))
        await s.flush()
        with pytest.raises(PartLookupError, match="ambiguous"):
            await find_part(s, "SSD-A")


# ---------------------------------------------------------------------------
# resolver slices
# ---------------------------------------------------------------------------


async def _seed_slices(s) -> None:
    svc_ha = Service(name="ha")
    svc_nas = Service(name="nas")
    s.add_all([svc_ha, svc_nas])
    await s.flush()
    for resolver in ("unifi", "unifi:covington"):
        s.add(
            ServiceEndpoint(
                service_id=svc_ha.id,
                scope=ResolutionScope.INTERNAL,
                hostname="ha.lan",
                ip="10.0.1.50",
                resolver=resolver,
                discovery_source=DiscoverySource.UNIFI,
            )
        )
    s.add(
        ServiceEndpoint(
            service_id=svc_nas.id,
            scope=ResolutionScope.INTERNAL,
            hostname="nas.lan",
            ip="10.0.1.60",
            resolver="unifi",
            discovery_source=DiscoverySource.UNIFI,
        )
    )
    s.add(
        ServiceEndpoint(
            service_id=svc_ha.id,
            scope=ResolutionScope.EXTERNAL,
            hostname="ha.example.com",
            ip="203.0.113.9",
            resolver="cloudflare",
            discovery_source=DiscoverySource.CLOUDFLARE,
        )
    )
    await s.flush()


async def test_list_and_retire_resolver_slice(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await _seed_slices(s)
        slices = {(x.scope, x.resolver): x.endpoints for x in await list_resolver_slices(s)}
        assert slices == {
            ("internal", "unifi"): 2,
            ("internal", "unifi:covington"): 1,
            ("external", "cloudflare"): 1,
        }

        result = await retire_resolver_slice(s, "unifi")
        assert result.endpoints_removed == 2
        assert result.services_removed == ["nas"]  # ha survives on the renamed slice + cloudflare
        remaining = {
            (e.resolver, e.hostname)
            for e in (await s.execute(select(ServiceEndpoint))).scalars().all()
        }
        assert remaining == {("unifi:covington", "ha.lan"), ("cloudflare", "ha.example.com")}
        assert {x.name for x in (await s.execute(select(Service))).scalars().all()} == {"ha"}


async def test_retire_resolver_slice_scoped(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await _seed_slices(s)
        result = await retire_resolver_slice(s, "unifi", scope=ResolutionScope.EXTERNAL)
        assert result.endpoints_removed == 0
        assert result.scope == "external"
