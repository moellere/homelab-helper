"""MCP server — the harness's query surface as Model Context Protocol tools.

Phase 4's first slice. Exposes what the CLI already answers (hosts, findings,
services, audit rollup) plus the management-plane discovery runs as MCP tools
over stdio, so Claude Desktop / Claude Code / Cursor can drive the harness in
natural language without shelling out.

The Phase-6 trust surface (``trust_status``, ``list_receipts``,
``pending_actions``) is **read-only on purpose**: a model may see what policy
allows and what has run, and has no tool to grant, elevate, override, roll
back, or open a window. Authority changes are operator gestures at the CLI.
Phase 7 added one *trigger*: ``execute_proposal`` asks the executor to run a
pending proposal, and the outcome is decided by ``decide()`` plus — at CONFIRM
— a human's tap on an approval channel. The agent never passes an override and
never authorizes anything; a cell at PROPOSE still executes nothing.

**Read-only against infrastructure (L1).** Query tools only read the harness
DB. ``run_discovery`` reads live sources (UniFi, Cloudflare, Argo CD, Proxmox,
K8s, OMV, Home Assistant, MikroTik — credentials from the same ``HOMELAB_HELPER_*`` env
vars the CLI uses) and persists into the harness DB — never a write to the lab
itself. The Phase-5 planners (placement, rebalance, bottlenecks, surplus,
network path) are exposed as deterministic reports; the client's own model
narrates them, so the harness never spends an LLM call on a tool's behalf.

Tools return plain dicts/lists (the SDK ships them as structured content).
Lookup misses return ``{"error": ...}`` rather than raising, so an LLM caller
gets a message it can act on instead of a protocol error.

The tools live in one module per domain (``inventory``, ``discovery``,
``planning``, ``trust``, ``proposals``, ``execution``); importing this package
registers all of them on ``server``.

Run it: ``helper mcp serve`` (stdio). Register in a client, e.g. Claude Code::

    claude mcp add homelab -- uv run --directory /path/to/homelab-helper helper mcp serve
"""

from __future__ import annotations

from homelab_helper.mcp_server._app import server
from homelab_helper.mcp_server.discovery import (
    console_snapshot,
    probe_host,
    probe_talos,
    probe_target_refusal,
    run_discovery,
    usage_summary,
)
from homelab_helper.mcp_server.execution import (
    execute_maintenance_run,
    execute_proposal,
    list_maintenance_runs,
    plan_maintenance_run,
    plan_talos_maintenance_run,
)
from homelab_helper.mcp_server.inventory import (
    ack_finding,
    audit_summary,
    config_status,
    diagram,
    get_finding,
    get_host,
    get_service,
    list_findings,
    list_hosts,
    list_services,
    resolve_finding,
    retire_host,
    suppress_finding,
)
from homelab_helper.mcp_server.planning import (
    analyze_bottlenecks,
    analyze_surplus,
    list_workloads,
    network_path,
    plan_rebalance,
    recommend_placement,
    rightsizing,
)
from homelab_helper.mcp_server.proposals import (
    draft_remediations,
    get_proposal,
    list_proposals,
    propose_action,
    propose_argocd_sync,
    propose_dns_record,
    propose_k8s_upgrade,
    propose_talos_upgrade,
    propose_workload_action,
)
from homelab_helper.mcp_server.trust import (
    list_receipts,
    pending_actions,
    trust_status,
)


def main() -> None:
    """Run the MCP server over stdio (blocking)."""
    server.run(transport="stdio")


__all__ = [
    "ack_finding",
    "analyze_bottlenecks",
    "analyze_surplus",
    "audit_summary",
    "config_status",
    "console_snapshot",
    "diagram",
    "draft_remediations",
    "execute_maintenance_run",
    "execute_proposal",
    "get_finding",
    "get_host",
    "get_proposal",
    "get_service",
    "list_findings",
    "list_hosts",
    "list_maintenance_runs",
    "list_proposals",
    "list_receipts",
    "list_services",
    "list_workloads",
    "main",
    "network_path",
    "pending_actions",
    "plan_maintenance_run",
    "plan_rebalance",
    "plan_talos_maintenance_run",
    "probe_host",
    "probe_talos",
    "probe_target_refusal",
    "propose_action",
    "propose_argocd_sync",
    "propose_dns_record",
    "propose_k8s_upgrade",
    "propose_talos_upgrade",
    "propose_workload_action",
    "recommend_placement",
    "resolve_finding",
    "retire_host",
    "rightsizing",
    "run_discovery",
    "server",
    "suppress_finding",
    "trust_status",
    "usage_summary",
]
