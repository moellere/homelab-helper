"""Talos node probe orchestration, shared by the CLI and the MCP surface.

``helper discover talos`` and the ``probe_talos`` MCP tool run the identical
sequence — resolve the Host row, health-check the machine API, run the talos
probes, persist observations, then reconcile — so it lives here, the same
shape as :mod:`host_probe`. Talos has no SSH: every read goes through the
operator's ``talosctl`` and its mTLS credentials, which is why the MCP tool
scopes its targets exactly as ``probe_host`` does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homelab_helper.adapters.talos import TalosAdapter
from homelab_helper.engine.host_probe import (
    HostProbeResult,
    ProbeOutcome,
    UnknownProbeError,
    mark_kernel_probed,
    resolve_host,
)
from homelab_helper.engine.reconciler import Reconciler
from homelab_helper.engine.runner import ProbeRunner
from homelab_helper.probes.base import AdapterRegistry, Probe, ProbeTarget
from homelab_helper.probes.registry import discover_probes

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class TalosProbeRequest:
    """Everything needed to probe one Talos node over its machine API."""

    name: str
    node: str | None = None
    """API endpoint (IP or name). Defaults to the host's recorded address, then ``name``."""
    talosconfig: str | None = None
    probe_names: tuple[str, ...] | None = None


def select_talos_probes(filter_names: tuple[str, ...] | list[str] | None) -> list[type[Probe]]:
    """Pick the probes to run — by name filter, or every talos-kind probe."""
    available = discover_probes()
    if filter_names:
        chosen: list[type[Probe]] = []
        for name in filter_names:
            if name not in available:
                raise UnknownProbeError(name)
            chosen.append(available[name])
        return chosen
    return [cls for cls in available.values() if "talos" in cls.target_kinds]


async def probe_talos(
    session: AsyncSession,
    request: TalosProbeRequest,
    *,
    probe_classes: list[type[Probe]] | None = None,
    adapter: TalosAdapter | None = None,
    on_probe: Any = None,
) -> HostProbeResult:
    """Run the talos probe batch against one node, then reconcile.

    An unreachable machine API is reported as ``session_error`` and counted as
    a failure; reconciliation still runs so the Host row's freshness markers
    reflect the attempt, exactly as the SSH path does.
    """
    classes = (
        probe_classes if probe_classes is not None else select_talos_probes(request.probe_names)
    )
    talos_adapter = adapter or TalosAdapter(talosconfig=request.talosconfig)
    runner = ProbeRunner(AdapterRegistry({"talos": talos_adapter}))

    host = await resolve_host(session, request.name, request.node)
    result = HostProbeResult(hostname=host.hostname, host_id=str(host.id))
    api_node = request.node or host.primary_ip or host.hostname or request.name

    ok, err = await talos_adapter.health_check(api_node)
    if not ok:
        result.session_error = f"talos node {api_node} unreachable: {err}"
        result.failures += 1
    else:
        target = ProbeTarget(
            kind="talos",
            host_id=str(host.id),
            hostname=host.hostname,
            primary_ip=host.primary_ip or api_node,
        )
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
        mark_kernel_probed(host, result.observations)

    result.reconcile = await Reconciler().reconcile_host(session, host.id)
    return result


__all__ = ["TalosProbeRequest", "probe_talos", "select_talos_probes"]
