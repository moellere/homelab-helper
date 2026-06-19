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
    assertions:                     # optional — same schema as the library loader
      - { name: lab-a.mem, host: lab-a, description: ..., verifier_spec: { ... } }
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import yaml
from sqlalchemy import select

from homelab_helper.db.enums import IntentTargetType, PrivilegeLevel
from homelab_helper.db.models import DiscoveryRun, Host, Observation, Probe
from homelab_helper.engine.assertion_library import load_library_entries
from homelab_helper.engine.assertions import AssertionEngine
from homelab_helper.engine.reconciler import Reconciler, normalize_arch

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_REPLAY_PROBE = "lab.replay"
_SUPPORTED_VERSION = 1


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

    assertions = data.get("assertions")
    if isinstance(assertions, list) and assertions:
        load = await load_library_entries(session, assertions)
        result.assertions_loaded = len(load.created) + len(load.updated) + len(load.unchanged)
        if run_assertions:
            runs = await AssertionEngine().run_all(session)
            result.assertions_run = len(runs)

    return result


__all__ = ["LabFixtureError", "ReplayResult", "load_lab_fixture", "parse_lab_fixture"]
