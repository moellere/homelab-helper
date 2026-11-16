"""The MCP server object and the helpers the tool modules share.

Importing this builds ``server`` and loads ``.env``: an MCP client launches the
process with whatever environment it happens to have.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mcp.server.mcpserver import MCPServer
from sqlalchemy import select

from homelab_helper import __version__
from homelab_helper.config import load_env
from homelab_helper.db.enums import (
    FindingStatus,
    ResolutionScope,
)
from homelab_helper.db.models import (
    ReconciliationFinding,
    ServiceEndpoint,
)

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

server = MCPServer(
    "homelab-helper",
    version=__version__,
    instructions=(
        "Inventory, audit, and findings for a homelab. All tools are read-only "
        "against the infrastructure (L1: propose, never apply); run_discovery "
        "reads live sources and persists observations into the harness DB only. "
        "Fingerprints are stable finding identities — use them to reference a "
        "finding across calls."
    ),
)


# An MCP client launches this process with whatever environment it happens to
# have, so the .env is loaded here as well as in the CLI entry point.
load_env()


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _finding_dict(f: ReconciliationFinding) -> dict[str, Any]:
    return {
        "fingerprint": f.fingerprint,
        "kind": f.kind.value,
        "severity": f.severity.value,
        "status": f.status.value,
        "title": f.title,
        "description": f.description,
        "affected": f.affected,
        "first_seen": _iso(f.first_seen),
        "last_seen": _iso(f.last_seen),
    }


def _endpoint_dict(ep: ServiceEndpoint) -> dict[str, Any]:
    return {
        "scope": ep.scope.value,
        "resolver": ep.resolver,
        "hostname": ep.hostname,
        "ip": ep.ip,
        "tls_provider": ep.tls_provider,
    }


def _split_brain(endpoints: list[ServiceEndpoint]) -> dict[str, Any] | None:
    internal = sorted({e.ip for e in endpoints if e.scope == ResolutionScope.INTERNAL and e.ip})
    external = sorted({e.ip for e in endpoints if e.scope == ResolutionScope.EXTERNAL and e.ip})
    if internal and external and internal != external:
        return {"internal_ips": internal, "external_ips": external}
    return None


async def _open_findings(session: AsyncSession) -> list[ReconciliationFinding]:
    rows = await session.execute(
        select(ReconciliationFinding).where(
            ReconciliationFinding.status.in_([FindingStatus.OPEN, FindingStatus.ACKNOWLEDGED])
        )
    )
    return list(rows.scalars().all())
