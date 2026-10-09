"""Lab replay — seed Host rows + Observations from a YAML fixture, then reconcile.

Turns a committed, synthetic fixture into a fully-populated harness DB with no
live SSH/Talos access: the day-one audit becomes a deterministic CI integration
test. The fixture mirrors the in-process replay shape used in the reconciler
unit tests — a list of hosts, each with its raw ``host.*`` observations — plus
an optional bundled assertion library that's loaded and run after reconcile.

Fixture schema (v1)::

    version: 1
    hosts:
      - hostname: lab-a
        primary_ip: 192.0.2.10
        arch: amd64                 # optional; else inferred from host.cpu.architecture
        observations:
          - { key: host.cpu.cores, value: 4 }
          - { key: host.storage.devices, value: [ ... ] }
    clusters:                       # optional — Cluster + VirtualMachine rows
      - name: lab-ceph
        kind: proxmox
        guests:
          - { name: vm-100, vmid: 100, node: lab-a, status: running,
              memory_bytes: 4294967296 }
    assertions:                     # optional — same schema as the library loader
      - { name: lab-a.mem, host: lab-a, description: ..., verifier_spec: { ... } }

A ``clusters`` block is what lets the fleet-shape analysers (``helper
bottlenecks``, ``helper plan rebalance``) run with no live management plane:
those patterns read cluster membership off the guest rows, so a node with no
guest is not a cluster member as far as they are concerned.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import yaml
from sqlalchemy import select

from homelab_helper.db.enums import DiscoverySource, IntentTargetType, PrivilegeLevel
from homelab_helper.db.models import (
    Cluster,
    DiscoveryRun,
    Host,
    Observation,
    Probe,
    VirtualMachine,
)
from homelab_helper.engine.assertion_library import load_library_entries
from homelab_helper.engine.assertions import AssertionEngine
from homelab_helper.engine.reconciler import Reconciler, normalize_arch

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_REPLAY_PROBE = "lab.replay"
_SUPPORTED_VERSION = 1
_CLUSTER_SOURCES = {"kubernetes": DiscoverySource.K8S}
"""A replayed cluster's kind decides its discovery source; Proxmox is the default."""


class LabFixtureError(ValueError):
    """Raised when the fixture is malformed."""


def parse_lab_fixture(text: str) -> dict[str, Any]:
    """Parse + validate the fixture YAML into a dict."""
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise LabFixtureError("fixture must be a mapping")
    version = data.get("version")
    if version != _SUPPORTED_VERSION:
        raise LabFixtureError(
            f"unsupported fixture version {version!r} (expected {_SUPPORTED_VERSION})"
        )
    if not isinstance(data.get("hosts"), list):
        raise LabFixtureError("fixture must have a 'hosts' list")
    return data


@dataclass
class ReplayResult:
    hosts_loaded: int = 0
    observations_loaded: int = 0
    assertions_loaded: int = 0
    assertions_run: int = 0
    clusters_loaded: int = 0
    guests_loaded: int = 0


async def _ensure_replay_probe(session: AsyncSession) -> Probe:
    probe = (
        await session.execute(select(Probe).where(Probe.name == _REPLAY_PROBE))
    ).scalar_one_or_none()
    if probe is not None:
        return probe
    probe = Probe(
        name=_REPLAY_PROBE,
        version="0.1.0",
        module_path="lab:replay",
        required_privilege=PrivilegeLevel.NONE,
        produces_keys=[],
        description="Synthetic observations replayed from a lab fixture.",
    )
    session.add(probe)
    await session.flush()
    return probe


async def _resolve_host(session: AsyncSession, hostname: str, primary_ip: str | None) -> Host:
    existing = (
        await session.execute(select(Host).where(Host.hostname == hostname))
    ).scalar_one_or_none()
    if existing is not None:
        if primary_ip and not existing.primary_ip:
            existing.primary_ip = primary_ip
        return existing
    host = Host(hostname=hostname, primary_ip=primary_ip)
    session.add(host)
    await session.flush()
    return host


async def _resolve_cluster(session: AsyncSession, entry: dict[str, Any]) -> Cluster:
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise LabFixtureError("each cluster needs a 'name'")
    existing = (
        await session.execute(select(Cluster).where(Cluster.name == name))
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    kind = entry.get("kind", "proxmox")
    cluster = Cluster(
        name=name,
        kind=kind,
        discovery_source=_CLUSTER_SOURCES.get(kind, DiscoverySource.PROXMOX),
        quorate=entry.get("quorate"),
    )
    session.add(cluster)
    await session.flush()
    return cluster


async def _resolve_guest(session: AsyncSession, cluster: Cluster, entry: dict[str, Any]) -> bool:
    """Upsert a guest by ``(cluster, vmid)`` — or by name when the fixture gives no vmid.

    Returns whether the row is new, so a re-replay reports nothing loaded.
    """
    name = entry.get("name")
    if not isinstance(name, str) or not name:
        raise LabFixtureError(f"each guest of cluster {cluster.name!r} needs a 'name'")
    vmid = entry.get("vmid")
    key = VirtualMachine.vmid == vmid if isinstance(vmid, int) else VirtualMachine.name == name
    existing = (
        await session.execute(
            select(VirtualMachine).where(VirtualMachine.cluster_id == cluster.id, key)
        )
    ).scalar_one_or_none()
    vm = existing or VirtualMachine(cluster_id=cluster.id, vmid=vmid, name=name)
    vm.name = name
    vm.kind = entry.get("kind", "qemu")
    vm.status = entry.get("status", "running")
    vm.template = bool(entry.get("template", False))
    vm.vcpus = entry.get("vcpus")
    vm.memory_bytes = entry.get("memory_bytes")
    vm.disk_bytes = entry.get("disk_bytes")
    vm.discovery_source = _CLUSTER_SOURCES.get(cluster.kind, DiscoverySource.PROXMOX)
    node = entry.get("node")
    if isinstance(node, str) and node:
        vm.node_name = node
        host = (
            await session.execute(select(Host).where(Host.hostname == node))
        ).scalar_one_or_none()
        if host is None:
            raise LabFixtureError(
                f"guest {name!r} names node {node!r}, which is not a host in this fixture"
            )
        vm.node_host_id = host.id
    if existing is None:
        session.add(vm)
    await session.flush()
    return existing is None


async def _load_clusters(session: AsyncSession, data: dict[str, Any]) -> tuple[int, int]:
    clusters = data.get("clusters")
    if not isinstance(clusters, list):
        return 0, 0
    n_clusters = n_guests = 0
    for entry in clusters:
        cluster = await _resolve_cluster(session, entry)
        nodes: set[str] = set()
        for guest in entry.get("guests", []):
            n_guests += int(await _resolve_guest(session, cluster, guest))
            if isinstance(guest.get("node"), str):
                nodes.add(guest["node"])
        cluster.node_count = len(nodes) or cluster.node_count
        n_clusters += 1
    return n_clusters, n_guests


async def load_lab_fixture(
    session: AsyncSession,
    data: dict[str, Any],
    *,
    run_assertions: bool = True,
) -> ReplayResult:
    """Seed hosts + observations from ``data``, reconcile each, run bundled assertions."""
    result = ReplayResult()
    probe = await _ensure_replay_probe(session)

    for entry in data.get("hosts", []):
        hostname = entry.get("hostname")
        if not isinstance(hostname, str) or not hostname:
            raise LabFixtureError("each host needs a 'hostname'")
        host = await _resolve_host(session, hostname, entry.get("primary_ip"))
        if entry.get("arch"):
            host.arch = normalize_arch(entry["arch"])

        run = DiscoveryRun(
            host_id=host.id,
            probe_id=probe.id,
            probe_name=_REPLAY_PROBE,
            probe_version="0.1.0",
            privilege_level=PrivilegeLevel.NONE,
            triggered_by="replay",
        )
        session.add(run)
        await session.flush()

        for obs in entry.get("observations", []):
            session.add(
                Observation(
                    run_id=run.id,
                    key=obs["key"],
                    value=obs["value"],
                    target_type=IntentTargetType.HOST,
                    target_id=str(host.id),
                )
            )
            result.observations_loaded += 1
        await session.flush()

        await Reconciler().reconcile_host(session, host.id)
        result.hosts_loaded += 1

    result.clusters_loaded, result.guests_loaded = await _load_clusters(session, data)

    assertions = data.get("assertions")
    if isinstance(assertions, list) and assertions:
        load = await load_library_entries(session, assertions)
        result.assertions_loaded = len(load.created) + len(load.updated) + len(load.unchanged)
        if run_assertions:
            runs = await AssertionEngine().run_all(session)
            result.assertions_run = len(runs)

    return result


__all__ = ["LabFixtureError", "ReplayResult", "load_lab_fixture", "parse_lab_fixture"]
