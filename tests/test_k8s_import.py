"""Tests for K8s node discovery + multi-source precedence on shared hosts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from homelab_helper.db.base import Base
from homelab_helper.db.enums import Confidence, IntentTargetType, PrivilegeLevel
from homelab_helper.db.models import DiscoveryRun, Host, Observation, Probe
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.k8s_import import discover_k8s_nodes

_WHEN = datetime(2026, 6, 20, tzinfo=UTC)


def _node(name: str, **over: Any) -> dict[str, Any]:
    base = {
        "name": name,
        "internal_ip": "192.0.2.50",
        "arch": "amd64",
        "kernel": "6.18-k8s",
        "os_image": "Talos",
        "machine_id": "abc",
        "cpu_capacity": 4,
        "memory_bytes": 8 * 1024**3,
        "kubelet_version": "v1.35.2",
        "container_runtime": "containerd://2.1.6",
        "roles": ["control-plane"],
    }
    base.update(over)
    return base


class _FakeK8s:
    def __init__(self, nodes: list[dict[str, Any]]) -> None:
        self._nodes = nodes

    async def list_nodes(self) -> list[dict[str, Any]]:
        return self._nodes


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


async def test_discover_creates_host_and_projects_k8s_facts(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        result = await discover_k8s_nodes(s, _FakeK8s([_node("knode")]), when=_WHEN)

    assert result.nodes_seen == 1
    assert result.hosts_created == ["knode"]
    async with sessionmaker() as s:
        host = (await s.execute(select(Host).where(Host.hostname == "knode"))).scalar_one()
        # K8s-only facts land in capabilities; INFERRED kernel used (no VERIFIED present).
        assert host.capabilities["k8s_kubelet_version"] == "v1.35.2"
        assert host.capabilities["k8s_roles"] == ["control-plane"]
        assert host.capabilities["kernel"] == "6.18-k8s"


async def _seed_verified_kernel(s, host: Host, value: str) -> None:
    probe = Probe(
        name="host.identity",
        version="0.1.0",
        module_path="x",
        required_privilege=PrivilegeLevel.USER,
        produces_keys=[],
    )
    s.add(probe)
    await s.flush()
    run = DiscoveryRun(
        host_id=host.id,
        probe_id=probe.id,
        probe_name="host.identity",
        probe_version="0.1.0",
        privilege_level=PrivilegeLevel.USER,
    )
    s.add(run)
    await s.flush()
    s.add(
        Observation(
            run_id=run.id,
            key="host.identity.kernel",
            value=value,
            confidence=Confidence.VERIFIED,
            target_type=IntentTargetType.HOST,
            target_id=str(host.id),
        )
    )
    await s.flush()


async def test_k8s_inferred_does_not_override_kernel_verified(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        host = Host(hostname="knode", primary_ip="192.0.2.50")
        s.add(host)
        await s.flush()
        await _seed_verified_kernel(s, host, "6.5-VERIFIED")

    # K8s reports a different kernel at INFERRED — must NOT win.
    async with session_scope(sessionmaker) as s:
        await discover_k8s_nodes(s, _FakeK8s([_node("knode", kernel="6.18-k8s")]), when=_WHEN)

    async with sessionmaker() as s:
        host = (await s.execute(select(Host).where(Host.hostname == "knode"))).scalar_one()
        assert host.capabilities["kernel"] == "6.5-VERIFIED"  # kernel ground truth held
        assert host.capabilities["k8s_kubelet_version"] == "v1.35.2"  # K8s still enriched


async def test_discover_matches_existing_host_by_ip(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        s.add(Host(hostname="real-name", primary_ip="192.0.2.50"))  # different name, same IP
    async with session_scope(sessionmaker) as s:
        result = await discover_k8s_nodes(s, _FakeK8s([_node("knode")]), when=_WHEN)
    assert result.hosts_matched == ["knode"]
    assert result.hosts_created == []
    async with sessionmaker() as s:
        assert len((await s.execute(select(Host))).scalars().all()) == 1  # no duplicate
