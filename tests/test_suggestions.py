"""Service suggestions (Phase 8.7) — idle capability and missing building blocks.

The matching is by name, which is the honest limit of what the harness knows,
so these tests pin both halves: that a plainly-present workload suppresses the
suggestion, and that a near-miss name does not.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from homelab_helper.db.base import Base
from homelab_helper.db.enums import FindingKind, FindingSeverity
from homelab_helper.db.models import (
    Cluster,
    Host,
    ReconciliationFinding,
    Service,
    VirtualMachine,
)
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.category_findings import category_of
from homelab_helper.engine.suggestions import (
    BUILDING_BLOCKS,
    building_block_issues,
    idle_gpu_issues,
    normalize,
    present_names,
)
from homelab_helper.engine.workloads import load_workload_library

LIBRARY = load_workload_library()
GPU_HOST = ("host-1", "bmax0", {"gpu_count": 1, "gpu_vendors": ["Intel"]})
PLAIN_HOST = ("host-2", "bmax1", {"gpu_count": 0})


# ---------------------------------------------------------------- normalising


def test_names_normalise_across_punctuation_and_case() -> None:
    assert normalize("Plex-Media_Server") == normalize("plex media server")
    assert normalize("  Jellyfin  ") == "jellyfin"


def test_present_names_merges_every_source() -> None:
    assert present_names(["Plex"], ["grafana"], ["", "  "]) == {"plex", "grafana"}


# ------------------------------------------------------------------ idle GPU


def test_a_gpu_with_nothing_using_it_is_suggested() -> None:
    issues = idle_gpu_issues([GPU_HOST], LIBRARY, present=set())
    assert len(issues) == 1
    assert issues[0].kind is FindingKind.SERVICE_SUGGESTION
    assert issues[0].severity is FindingSeverity.INFO
    assert issues[0].target_id == "host-1"
    assert "Intel" in issues[0].description


def test_the_suggestion_says_what_the_gpu_would_be_for() -> None:
    """The library carries gpu_purpose; a bare "it is idle" would be useless."""
    issue = idle_gpu_issues([GPU_HOST], LIBRARY, present=set())[0]
    assert "transcoding" in issue.description or "detection" in issue.description
    assert issue.evidence["candidates"]


def test_a_host_without_a_gpu_is_never_suggested() -> None:
    assert idle_gpu_issues([PLAIN_HOST], LIBRARY, present=set()) == []


def test_a_running_gpu_workload_silences_every_host() -> None:
    present = present_names(["jellyfin"])
    assert idle_gpu_issues([GPU_HOST], LIBRARY, present) == []


def test_a_guest_named_for_a_gpu_workload_counts_as_using_it() -> None:
    present = present_names(["media-plex-01"])
    assert idle_gpu_issues([GPU_HOST], LIBRARY, present) == []


def test_a_name_that_merely_contains_the_letters_does_not_count() -> None:
    """``lokiadmin`` is not loki; word-level containment, not substring."""
    present = present_names(["plexiglass-inventory"])
    assert idle_gpu_issues([GPU_HOST], LIBRARY, present) != []


def test_the_description_admits_it_matched_on_names() -> None:
    issue = idle_gpu_issues([GPU_HOST], LIBRARY, present=set())[0]
    assert "names only" in issue.description


# ------------------------------------------------------------ building blocks


def test_an_empty_lab_is_missing_every_block() -> None:
    issues = building_block_issues(LIBRARY, present=set())
    assert {i.target_id for i in issues} == set(BUILDING_BLOCKS)
    assert all(i.severity is FindingSeverity.INFO for i in issues)


def test_prometheus_satisfies_metrics() -> None:
    issues = building_block_issues(LIBRARY, present_names(["prometheus"]))
    assert "metrics" not in {i.target_id for i in issues}


def test_ollama_satisfies_the_local_model_block() -> None:
    issues = building_block_issues(LIBRARY, present_names(["ollama"]))
    assert "local-llm" not in {i.target_id for i in issues}


def test_grafana_counts_for_both_metrics_and_alerting() -> None:
    """It appears in both block lists on purpose; one install covers both."""
    issues = {i.target_id for i in building_block_issues(LIBRARY, present_names(["grafana"]))}
    assert issues == {"local-llm"}


def test_a_block_names_the_lightest_candidate_size() -> None:
    issue = next(
        i for i in building_block_issues(LIBRARY, present=set()) if i.target_id == "local-llm"
    )
    assert "MiB RAM" in issue.description
    assert "router" in issue.title


def test_each_block_keeps_its_own_fingerprint() -> None:
    issues = building_block_issues(LIBRARY, present=set())
    assert len({i.fingerprint for i in issues}) == len(issues)


# -------------------------------------------------------------- the pass


@pytest.fixture
async def suggestions_db():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield make_sessionmaker(engine)
    await engine.dispose()


async def test_the_pass_reads_guests_services_and_host_facts(suggestions_db) -> None:
    from homelab_helper import mcp_server as mcp_srv

    async with session_scope(suggestions_db) as session:
        session.add(Host(hostname="bmax0", capabilities={"gpu_count": 1, "gpu_vendors": ["Intel"]}))
        cluster = Cluster(name="homelab", kind="proxmox")
        session.add(cluster)
        await session.flush()
        session.add(VirtualMachine(cluster_id=cluster.id, name="prometheus", kind="lxc"))
        session.add(Service(name="grafana"))
        await session.flush()

        result = await mcp_srv._discover_suggestions(session)  # noqa: SLF001
        findings = (
            (
                await session.execute(
                    select(ReconciliationFinding).where(
                        ReconciliationFinding.kind == FindingKind.SERVICE_SUGGESTION
                    )
                )
            )
            .scalars()
            .all()
        )

    assert result["errors"] == {}
    blocks = {f.title for f in findings if category_of(f) == "building-block-missing"}
    assert any("local-llm" in t or "local model" in t for t in blocks)
    assert not any("metrics" in t for t in blocks), "prometheus is a guest here"
    assert any(category_of(f) == "capability-idle-gpu" for f in findings), (
        "a GPU host with no GPU workload named"
    )


async def test_the_pass_needs_no_adapter(suggestions_db) -> None:
    """It reads stored facts only, so both categories are always observed and
    a lab with nothing discovered yet still gets its suggestions."""
    from homelab_helper import mcp_server as mcp_srv

    async with session_scope(suggestions_db) as session:
        result = await mcp_srv._discover_suggestions(session)  # noqa: SLF001

    assert result["errors"] == {}
    assert result["findings"]["opened"] == len(BUILDING_BLOCKS)
