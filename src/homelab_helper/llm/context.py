"""Lab context for LLM prompts — reconciled facts only, rendered compactly.

The trust boundary (``docs/architecture.md``) says agents read *synthesized
inventory views*, never raw tool output and never secrets. This module is that
view: a bounded plain-text fact sheet built from the harness DB — hosts,
clusters/VMs, services (with split-brain), and open findings with their stable
fingerprints — suitable for injection into a system prompt.

Bounded on purpose: past ``_MAX_ROWS`` per section it summarizes ("… and N
more") rather than growing the prompt without limit on a big fleet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from homelab_helper.db.enums import FindingStatus, ResolutionScope
from homelab_helper.db.models import (
    Host,
    ReconciliationFinding,
    Service,
    ServiceEndpoint,
    VirtualMachine,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_MAX_ROWS = 40


def _capped(lines: list[str], total: int) -> list[str]:
    if total <= _MAX_ROWS:
        return lines
    return [*lines[:_MAX_ROWS], f"  … and {total - _MAX_ROWS} more"]


async def build_lab_context(session: AsyncSession) -> str:
    """One plain-text fact sheet of the reconciled lab state."""
    sections: list[str] = []

    hosts = (await session.execute(select(Host).order_by(Host.hostname))).scalars().all()
    host_lines = [
        f"  - {h.hostname} ip={h.primary_ip or '?'} arch={h.arch.value} "
        f"source={h.discovery_source.value}"
        for h in hosts
    ]
    sections.append(f"HOSTS ({len(hosts)}):\n" + "\n".join(_capped(host_lines, len(hosts))))

    vms = (await session.execute(select(VirtualMachine))).scalars().all()
    if vms:
        running = sum(1 for v in vms if v.status == "running")
        vm_lines = [
            f"  - {v.name} kind={v.kind} node={v.node_name or '?'} status={v.status}"
            for v in sorted(vms, key=lambda v: v.name)
        ]
        sections.append(
            f"GUESTS ({len(vms)}, {running} running):\n" + "\n".join(_capped(vm_lines, len(vms)))
        )

    services = (await session.execute(select(Service).order_by(Service.name))).scalars().all()
    if services:
        endpoints = (await session.execute(select(ServiceEndpoint))).scalars().all()
        by_service: dict[object, list[ServiceEndpoint]] = {}
        for ep in endpoints:
            by_service.setdefault(ep.service_id, []).append(ep)
        svc_lines = []
        for svc in services:
            eps = by_service.get(svc.id, [])
            internal = sorted({e.ip for e in eps if e.scope == ResolutionScope.INTERNAL and e.ip})
            external = sorted({e.ip for e in eps if e.scope == ResolutionScope.EXTERNAL and e.ip})
            bits = [svc.name]
            if internal:
                bits.append(f"internal={','.join(internal)}")
            if external:
                bits.append(f"external={','.join(external)}")
            if internal and external and internal != external:
                bits.append("SPLIT-BRAIN")
            svc_lines.append("  - " + " ".join(bits))
        sections.append(
            f"SERVICES ({len(services)}):\n" + "\n".join(_capped(svc_lines, len(services)))
        )

    findings = (
        (
            await session.execute(
                select(ReconciliationFinding)
                .where(
                    ReconciliationFinding.status.in_(
                        [FindingStatus.OPEN, FindingStatus.ACKNOWLEDGED]
                    )
                )
                .order_by(ReconciliationFinding.severity, ReconciliationFinding.last_seen.desc())
            )
        )
        .scalars()
        .all()
    )
    if findings:
        finding_lines = [
            f"  - [{f.severity.value}] {f.kind.value} {f.fingerprint}: {f.title}" for f in findings
        ]
        sections.append(
            f"OPEN FINDINGS ({len(findings)}):\n" + "\n".join(_capped(finding_lines, len(findings)))
        )
    else:
        sections.append("OPEN FINDINGS (0): none")

    return "\n\n".join(sections)


__all__ = ["build_lab_context"]
