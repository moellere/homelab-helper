"""Deep-host probe orchestration, shared by the CLI and the MCP surface.

``helper discover host`` and the ``probe_host`` MCP tool run the identical
sequence — resolve the Host row, open one SSH session for the whole probe batch,
persist observations, then reconcile — so it lives here rather than inside a
Typer command. The CLI renders the returned structure with Rich; the MCP tool
ships it as structured content.

Nothing here prints or raises for control flow: a failed probe or a refused SSH
session is reported in the result so a partial run still reconciles whatever
landed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import or_, select

from homelab_helper.adapters.kernel_ssh import KernelSSHAdapter
from homelab_helper.db.enums import DiscoverySource
from homelab_helper.db.models import Host
from homelab_helper.engine.reconciler import Reconciler, ReconcileResult
from homelab_helper.engine.runner import ProbeRunner
from homelab_helper.probes.base import AdapterRegistry, Probe, ProbeTarget
from homelab_helper.probes.registry import discover_probes

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class UnknownProbeError(ValueError):
    """A requested probe name isn't registered."""


@dataclass(frozen=True)
class HostProbeRequest:
    """Everything needed to probe one host over SSH.

    ``ssh_password`` is accepted but never sourced from a caller-supplied
    literal in either surface — the CLI reads it from a named env var and the
    MCP tool doesn't expose it at all.
    """

    name: str
    ssh_user: str
    ssh_key_path: str | None = None
    ssh_password: str | None = None
    primary_ip: str | None = None
    ssh_port: int = 22
    probe_names: tuple[str, ...] | None = None


@dataclass
class ProbeOutcome:
    probe: str
    version: str
    success: bool
    observations: int = 0
    error: str | None = None


@dataclass
class HostProbeResult:
    hostname: str
    host_id: str
    observations: int = 0
    failures: int = 0
    probes: list[ProbeOutcome] = field(default_factory=list)
    session_error: str | None = None
    reconcile: ReconcileResult | None = None

    def as_dict(self) -> dict[str, Any]:
        """Flatten for the MCP surface (structured content must be JSON-safe)."""
        summary = self.reconcile
        return {
            "hostname": self.hostname,
            "host_id": self.host_id,
            "observations": self.observations,
            "failures": self.failures,
            "session_error": self.session_error,
            "probes": [
                {
                    "probe": p.probe,
                    "version": p.version,
                    "success": p.success,
                    "observations": p.observations,
                    "error": p.error,
                }
                for p in self.probes
            ],
            "reconcile": None
            if summary is None
            else {
                "observations_seen": summary.observations_seen,
                "changes": {k: str(v) for k, v in summary.changes.items()},
                "parts_upserted": summary.parts_upserted,
                "parts_skipped_no_identity": summary.parts_skipped_no_identity,
                "parts_skipped_filtered": summary.parts_skipped_filtered,
                "placements_opened": [list(p) for p in summary.placements_opened],
                "placements_closed": [list(p) for p in summary.placements_closed],
                "findings_opened": len(summary.findings_opened),
                "findings_resolved": len(summary.findings_resolved),
                "findings_updated": len(summary.findings_updated),
            },
        }


async def resolve_host(session: AsyncSession, name: str, primary_ip: str | None) -> Host:
    """Find an existing Host by hostname *or* primary_ip; create one if missing.

    Matching by IP as well as name keeps a probe run (which may pass a short
    name) from duplicating a row another source created under a different label
    — e.g. the scan importer storing an FQDN. First-created row wins on ties.
    """
    conditions = [Host.hostname == name]
    if primary_ip:
        conditions.append(Host.primary_ip == primary_ip)
    existing: Host | None = (
        (await session.execute(select(Host).where(or_(*conditions)).order_by(Host.created_at)))
        .scalars()
        .first()
    )
    if existing is not None:
        if primary_ip and not existing.primary_ip:
            existing.primary_ip = primary_ip
        return existing
    host = Host(hostname=name, primary_ip=primary_ip)
    session.add(host)
    await session.flush()
    return host


def select_host_probes(filter_names: tuple[str, ...] | list[str] | None) -> list[type[Probe]]:
    """Pick the probes to run — by name filter, or every host-kind probe."""
    available = discover_probes()
    if filter_names:
        chosen: list[type[Probe]] = []
        for name in filter_names:
            if name not in available:
                raise UnknownProbeError(name)
            chosen.append(available[name])
        return chosen
    return [cls for cls in available.values() if "host" in cls.target_kinds]


def mark_kernel_probed(host: Host, observations: int) -> None:
    """Flag a directly-probed host so the scan importer treats it as covered."""
    if observations > 0:
        host.discovery_source = DiscoverySource.KERNEL_PROBE


async def probe_host(
    session: AsyncSession,
    request: HostProbeRequest,
    *,
    probe_classes: list[type[Probe]] | None = None,
    ssh_adapter: KernelSSHAdapter | None = None,
    on_probe: Any = None,
) -> HostProbeResult:
    """Run the host probe batch against one target, then reconcile.

    ``on_probe`` is called with each :class:`ProbeOutcome` as it completes so the
    CLI can stream progress; the MCP tool leaves it unset. Reconciliation runs
    even when probes failed — partial observations are still worth applying, and
    ``reconcile_host`` is idempotent.
    """
    classes = (
        probe_classes if probe_classes is not None else select_host_probes(request.probe_names)
    )
    adapter = ssh_adapter or KernelSSHAdapter()
    runner = ProbeRunner(AdapterRegistry({"kernel-ssh": adapter}))

    host = await resolve_host(session, request.name, request.primary_ip)
    result = HostProbeResult(hostname=host.hostname, host_id=str(host.id))
    connect_host = host.primary_ip or host.hostname or request.name
    target = ProbeTarget(
        kind="host",
        host_id=str(host.id),
        hostname=host.hostname,
        primary_ip=host.primary_ip,
        ssh_user=request.ssh_user,
        ssh_key_path=request.ssh_key_path,
        ssh_password=request.ssh_password,
        ssh_port=request.ssh_port,
    )

    # One SSH connection shared across the whole probe batch.
    try:
        async with adapter.shared_session(
            connect_host,
            user=request.ssh_user,
            key_path=request.ssh_key_path,
            password=request.ssh_password,
            port=request.ssh_port,
        ):
            for cls in classes:
                probe = cls()
                _run_row, probe_result = await runner.run(
                    probe, target, session, host_id=host.id, triggered_by="manual"
                )
                outcome = ProbeOutcome(
                    probe=probe.name,
                    version=probe.version,
                    success=probe_result.success,
                    observations=len(probe_result.observations) if probe_result.success else 0,
                    error=None if probe_result.success else probe_result.error,
                )
                if outcome.success:
                    result.observations += outcome.observations
                else:
                    result.failures += 1
                result.probes.append(outcome)
                if on_probe is not None:
                    on_probe(outcome)
    except Exception as exc:
        result.session_error = str(exc)
        result.failures += 1

    mark_kernel_probed(host, result.observations)
    result.reconcile = await Reconciler().reconcile_host(session, host.id)
    return result


__all__ = [
    "HostProbeRequest",
    "HostProbeResult",
    "ProbeOutcome",
    "UnknownProbeError",
    "mark_kernel_probed",
    "probe_host",
    "resolve_host",
    "select_host_probes",
]
