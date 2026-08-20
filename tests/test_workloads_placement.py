"""Workload library + placement recommender tests (Phase 5, AC1/AC2)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from homelab_helper.db.base import Base
from homelab_helper.db.enums import Architecture, IntentState, IntentTargetType
from homelab_helper.db.models import Cluster, Host, OperationalIntent, VirtualMachine
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.placement import recommend_placement
from homelab_helper.engine.reconciler import _HOST_RULES
from homelab_helper.engine.workloads import (
    WorkloadLibraryError,
    WorkloadProfile,
    load_workload_library,
)

_GB = 1024**3


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


def _host(
    name: str,
    *,
    arch: Architecture = Architecture.AMD64,
    mem_gb: int | None = 32,
    threads: int | None = 16,
    disk_tb: float | None = None,
    gpus: int = 0,
) -> Host:
    caps: dict = {}
    if mem_gb is not None:
        caps["mem_total_bytes"] = mem_gb * _GB
    if threads is not None:
        caps["cpu_threads"] = threads
    if disk_tb is not None:
        caps["total_disk_bytes"] = int(disk_tb * 1024**4)
    if gpus:
        caps["gpu_count"] = gpus
    return Host(hostname=name, arch=arch, capabilities=caps)


# ---------------------------------------------------------------------------
# library (AC1)
# ---------------------------------------------------------------------------


def test_starter_library_has_at_least_50_entries() -> None:
    library = load_workload_library()
    assert len(library) >= 50


def test_starter_library_entries_are_valid() -> None:
    for name, p in load_workload_library().items():
        assert p.name == name
        assert p.cpu_cores > 0
        assert p.ram_mb > 0
        assert p.storage_gb > 0
        assert p.arch
        assert p.gpu in {"none", "optional", "required"}
        if p.gpu != "none":
            assert p.gpu_purpose, f"{name}: gpu={p.gpu} needs gpu_purpose"


def test_immich_profile_shape() -> None:
    """AC2's example workload has the fields the AC reasons about."""
    immich = load_workload_library()["immich"]
    assert immich.arch == ("amd64",)
    assert immich.gpu == "optional"
    assert "postgres" in immich.depends_on
    assert immich.data_gravity == "photo-library"


def test_operator_library_overlays_starter(tmp_path: Path, monkeypatch) -> None:
    override = tmp_path / "mine.yaml"
    override.write_text(
        "workloads:\n"
        "  immich:\n"
        "    category: media\n"
        "    description: my tuned immich\n"
        "    cpu_cores: 4\n"
        "    ram_mb: 8192\n"
        "    storage_gb: 20\n"
    )
    monkeypatch.setenv("HOMELAB_HELPER_WORKLOAD_LIBRARY", str(override))
    library = load_workload_library()
    assert library["immich"].ram_mb == 8192  # operator wins
    assert "plex" in library  # starters still present


def test_malformed_entry_names_the_problem(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("workloads:\n  broken:\n    category: misc\n")
    with pytest.raises(WorkloadLibraryError, match="broken"):
        load_workload_library(bad)


def test_gpu_facts_are_projected_to_capabilities() -> None:
    keys = {r.key for r in _HOST_RULES}
    assert "host.gpu.count" in keys
    assert "host.gpu.vendors" in keys


# ---------------------------------------------------------------------------
# placement (AC2)
# ---------------------------------------------------------------------------


def _immich() -> WorkloadProfile:
    return load_workload_library()["immich"]


async def test_arch_constraint_rejects_arm_host(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        s.add(_host("pi4", arch=Architecture.ARM64, mem_gb=8))
        s.add(_host("nuc", arch=Architecture.AMD64, mem_gb=32))
    async with sessionmaker() as s:
        report = await recommend_placement(s, _immich())
    assert [c.hostname for c in report.candidates] == ["nuc"]
    assert any(h == "pi4" and "arch" in why for h, why in report.rejected)


async def test_ram_constraint_rejects_small_host(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        s.add(_host("tiny", mem_gb=4))  # 4 GiB < immich 4096 MB + 1 GiB reserve
        s.add(_host("big", mem_gb=64))
    async with sessionmaker() as s:
        report = await recommend_placement(s, _immich())
    assert [c.hostname for c in report.candidates] == ["big"]
    assert any(h == "tiny" and "RAM" in why for h, why in report.rejected)


async def test_gpu_required_rejects_gpuless(sessionmaker) -> None:
    profile = WorkloadProfile(
        name="mlbox",
        category="misc",
        description="",
        cpu_cores=1,
        ram_mb=1024,
        storage_gb=1,
        gpu="required",
        gpu_purpose="inference",
    )
    async with session_scope(sessionmaker) as s:
        s.add(_host("cpuonly", mem_gb=32))
        s.add(_host("gpubox", mem_gb=32, gpus=1))
    async with sessionmaker() as s:
        report = await recommend_placement(s, profile)
    assert [c.hostname for c in report.candidates] == ["gpubox"]
    assert any("GPU" in why for _, why in report.rejected)


async def test_optional_gpu_and_storage_break_ram_ties(sessionmaker) -> None:
    """AC2's reasoning: GPU optionality + storage proximity influence rank."""
    async with session_scope(sessionmaker) as s:
        s.add(_host("plain", mem_gb=32))
        s.add(_host("gpu-nas", mem_gb=32, gpus=1, disk_tb=8))
    async with sessionmaker() as s:
        report = await recommend_placement(s, _immich())
    assert report.best is not None
    assert report.best.hostname == "gpu-nas"
    joined = " ".join(report.best.reasons)
    assert "GPU present" in joined
    assert "photo-library" in joined


async def test_unknown_ram_is_caveat_not_rejection(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        s.add(_host("mystery", mem_gb=None, threads=None))
    async with sessionmaker() as s:
        report = await recommend_placement(s, _immich())
    assert [c.hostname for c in report.candidates] == ["mystery"]
    assert any("RAM unknown" in c for c in report.candidates[0].caveats)


async def test_running_guests_penalize_score(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        busy = _host("busy", mem_gb=32)
        idle = _host("idle", mem_gb=32)
        s.add_all([busy, idle])
        await s.flush()
        cluster = Cluster(name="lab", kind="proxmox")
        s.add(cluster)
        await s.flush()
        for vmid in (100, 101, 102):
            s.add(
                VirtualMachine(
                    cluster_id=cluster.id,
                    vmid=vmid,
                    name=f"vm{vmid}",
                    kind="qemu",
                    status="running",
                    node_name="busy",
                    node_host_id=busy.id,
                )
            )
    async with sessionmaker() as s:
        report = await recommend_placement(s, _immich())
    assert report.best is not None
    assert report.best.hostname == "idle"


async def test_decommissioning_host_is_excluded(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        doomed = _host("doomed", mem_gb=64)
        s.add(doomed)
        await s.flush()
        s.add(
            OperationalIntent(
                target_type=IntentTargetType.HOST,
                target_id=str(doomed.id),
                intent=IntentState.DECOMMISSIONING,
                declared_by="test",
            )
        )
        s.add(_host("keeper", mem_gb=32))
    async with sessionmaker() as s:
        report = await recommend_placement(s, _immich())
    assert [c.hostname for c in report.candidates] == ["keeper"]
    assert any(h == "doomed" and "decommissioning" in why for h, why in report.rejected)
