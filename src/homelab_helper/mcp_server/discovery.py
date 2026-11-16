"""Discovery tools: run a source, probe a host or Talos node, console snapshot, usage history."""

from __future__ import annotations

import os
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any

from sqlalchemy import or_, select

from homelab_helper.adapters import factories
from homelab_helper.config import PROBE_ALLOW_VAR, database_url, probe_allow_patterns
from homelab_helper.db.models import (
    Host,
)
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.discoverers import run_discovery as _run_discovery
from homelab_helper.engine.host_probe import HostProbeRequest, UnknownProbeError
from homelab_helper.engine.host_probe import probe_host as _probe_host
from homelab_helper.engine.jetkvm import (
    attached_from_env,
    capture_console,
    snapshot_dir,
)
from homelab_helper.engine.talos_probe import TalosProbeRequest
from homelab_helper.engine.talos_probe import probe_talos as _probe_talos
from homelab_helper.engine.usage import usage_overview
from homelab_helper.secrets import redact

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from homelab_helper.mcp_server._app import (
    server,
)


@server.tool()
async def run_discovery(source: str) -> dict[str, Any]:
    """Run a management-plane discovery and persist into the harness DB.
    Source must be one of: unifi, cloudflare, argocd, proxmox, k8s, omv, hass, mikrotik,
    printwarden (print-warden reachability and printer health from its /healthz),
    jetkvm (the KVM's status and its attached host's reachability, with a console
    snapshot as evidence when that host is down),
    versions (Phase 8.1: package lag, mixed versions, OS end of life, HA updates),
    backups (Phase 8.2: uncovered/stale/unverified guests, orphaned backups, capacity),
    usage (Phase 8.3: hourly/daily usage rollups from Proxmox RRD, backfilled, pruned),
    storage, suggestions, capacity (Phase 10.2: filesystem, memory and CPU trends),
    anomalies (Phase 10.3: guests and nodes outside their own usage baseline).
    Reads the live source (credentials from HOMELAB_HELPER_* env vars); never
    writes to the infrastructure itself."""
    return await _run_discovery(source)


@server.tool()
async def console_snapshot() -> dict[str, Any]:
    """Capture one frame of the console behind the configured JetKVM (read-only).

    Saves a PNG under the data directory and returns its path and the host
    the KVM is declared attached to (HOMELAB_HELPER_JETKVM_ATTACHED_TO). Nothing is
    typed or clicked: the KVM is only watched. Needs the optional jetkvm extra; a
    failure is returned as ``{"error": ...}``."""
    try:
        adapter = factories.jetkvm()
    except Exception as exc:
        return {"error": redact(str(exc))}
    attached_to, _ = attached_from_env()
    try:
        path, err = await capture_console(adapter, snapshot_dir(), attached_to or "console")
    finally:
        await adapter.aclose()
    if path is None:
        return {"error": redact(err or "snapshot failed"), "attached_to": attached_to}
    return {"path": path, "attached_to": attached_to}


@server.tool()
async def usage_summary(subject: str | None = None, days: int = 30) -> list[dict[str, Any]]:
    """What hosts and guests actually used over the last ``days`` (Phase 8.3).

    Per subject: CPU p95 and peak (fraction of allocated CPUs), memory p95 and
    peak (bytes) against the allocation, and how many hourly buckets back it.
    ``subject`` filters by node name, guest name, or vmid; omit for everything.
    Read-only; history comes from ``run_discovery("usage")``."""
    engine = make_engine(database_url())
    try:
        async with session_scope(make_sessionmaker(engine)) as session:
            return await usage_overview(session, subject=subject, days=days)
    finally:
        await engine.dispose()


def _matches_any(value: str | None, patterns: tuple[str, ...]) -> bool:
    return value is not None and any(fnmatchcase(value.lower(), p.lower()) for p in patterns)


def _known_host_refusal(
    hostname: str, primary_ip: str | None, known: Host, patterns: tuple[str, ...]
) -> str | None:
    if primary_ip and known.primary_ip and primary_ip != known.primary_ip:
        return (
            f"{hostname!r} is recorded at {known.primary_ip}; refusing to probe it at "
            f"{primary_ip}. If the host moved, update it from the CLI first."
        )
    if primary_ip and not known.primary_ip and not _matches_any(primary_ip, patterns):
        return (
            f"{hostname!r} has no recorded address and {primary_ip!r} matches no pattern "
            f"in {PROBE_ALLOW_VAR}; omit primary_ip to connect by name, or allow the address."
        )
    return None


def _unknown_host_refusal(
    hostname: str, primary_ip: str | None, patterns: tuple[str, ...]
) -> str | None:
    if not patterns:
        return (
            f"{hostname!r} is not a known host and {PROBE_ALLOW_VAR} is unset: the MCP surface "
            "only probes hosts already in the harness DB. Add it with `helper discover host` "
            f"or `helper onboard`, or set {PROBE_ALLOW_VAR} to comma-separated hostname/IP globs."
        )
    if not _matches_any(hostname, patterns):
        return f"{hostname!r} matches no pattern in {PROBE_ALLOW_VAR}"
    if primary_ip and not _matches_any(primary_ip, patterns):
        return (
            f"{primary_ip!r} matches no pattern in {PROBE_ALLOW_VAR}; an unknown host must "
            "connect by a name or address the allow list covers"
        )
    return None


def probe_target_refusal(
    hostname: str,
    primary_ip: str | None,
    known: Host | None,
    patterns: tuple[str, ...],
) -> str | None:
    """Why an MCP caller may not probe this target, or ``None`` when it may.

    The tool authenticates with the operator's SSH key, so the set of targets
    it can be steered at is the whole attack surface. A known host is always
    probeable, but only at its recorded address — a caller can't aim a trusted
    hostname at some other IP. An unknown host (or a caller-supplied address for
    a host that has none recorded) must match a glob in
    ``HOMELAB_HELPER_MCP_PROBE_ALLOW``; with the variable unset, only known
    hosts probe. Pure, so the policy is unit-testable without a server.
    """
    if known is not None:
        return _known_host_refusal(hostname, primary_ip, known, patterns)
    return _unknown_host_refusal(hostname, primary_ip, patterns)


async def _known_host(session: AsyncSession, hostname: str, primary_ip: str | None) -> Host | None:
    """The row ``resolve_host`` would reuse for this request, if any."""
    conditions = [Host.hostname == hostname]
    if primary_ip:
        conditions.append(Host.primary_ip == primary_ip)
    row: Host | None = (
        (await session.execute(select(Host).where(or_(*conditions)).order_by(Host.created_at)))
        .scalars()
        .first()
    )
    return row


@server.tool()
async def probe_host(
    hostname: str,
    ssh_user: str,
    ssh_key_path: str | None = None,
    primary_ip: str | None = None,
    ssh_port: int = 22,
    probes: list[str] | None = None,
) -> dict[str, Any]:
    """Deep-probe one Linux host over SSH (CPU, memory, DIMMs, storage, NICs,
    PCI, GPU, SMART, services), persist the observations, and reconcile.

    Complements run_discovery, which only reads management planes — this is the
    kernel-level source. Reads the host and writes only to the harness DB.

    Authenticates by key: pass ssh_key_path, or set HOMELAB_HELPER_SSH_KEY.
    Passwords are deliberately not accepted here. A full suite takes ~30s.

    Scoped: a host already in the harness DB is probed at its recorded address
    (a different primary_ip is refused). A host the harness doesn't know is
    refused unless its name — and primary_ip, when given — match a glob in
    HOMELAB_HELPER_MCP_PROBE_ALLOW (e.g. "*.lan,10.0.1.*"). Add new hosts from
    the CLI (`helper discover host`, `helper onboard`) or widen the allow list.
    """
    key_path = ssh_key_path or os.environ.get("HOMELAB_HELPER_SSH_KEY")
    if not key_path:
        return {"error": "an SSH key is required: pass ssh_key_path or set HOMELAB_HELPER_SSH_KEY"}
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as session:
            known = await _known_host(session, hostname, primary_ip)
            refusal = probe_target_refusal(hostname, primary_ip, known, probe_allow_patterns())
            if refusal is not None:
                return {"hostname": hostname, "error": refusal}
            result = await _probe_host(
                session,
                HostProbeRequest(
                    name=hostname,
                    ssh_user=ssh_user,
                    ssh_key_path=key_path,
                    primary_ip=primary_ip,
                    ssh_port=ssh_port,
                    probe_names=tuple(probes) if probes else None,
                ),
            )
            return result.as_dict()
    except UnknownProbeError as exc:
        return {"error": f"unknown probe {exc.args[0]!r}"}
    except Exception as exc:
        return {"hostname": hostname, "error": redact(str(exc))}
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Phase 5 planners — deterministic reports; the MCP client's own model narrates
# ---------------------------------------------------------------------------


@server.tool()
async def probe_talos(
    hostname: str,
    node: str | None = None,
    talosconfig: str | None = None,
    probes: list[str] | None = None,
) -> dict[str, Any]:
    """Discover a Talos Linux node over its machine API (no SSH) via the
    operator's talosctl credentials, persist the observations, and reconcile.
    ``node`` is the API endpoint when it differs from the host's recorded
    address.

    Scoped like probe_host: a known host is probed at its recorded address (a
    different ``node`` is refused); an unknown host must match a glob in
    HOMELAB_HELPER_MCP_PROBE_ALLOW.
    """
    engine = make_engine(database_url())
    try:
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as session:
            known = await _known_host(session, hostname, node)
            refusal = probe_target_refusal(hostname, node, known, probe_allow_patterns())
            if refusal is not None:
                return {"hostname": hostname, "error": refusal}
            result = await _probe_talos(
                session,
                TalosProbeRequest(
                    name=hostname,
                    node=node,
                    talosconfig=talosconfig,
                    probe_names=tuple(probes) if probes else None,
                ),
            )
            return result.as_dict()
    except UnknownProbeError as exc:
        return {"error": f"unknown probe {exc.args[0]!r}"}
    except Exception as exc:
        return {"hostname": hostname, "error": redact(str(exc))}
    finally:
        await engine.dispose()


# --------------------------------------------------------- trust surface (L2)
#
# The trust gradient's whole premise is that an LLM is never in the path that
# authorizes execution, so this surface lets a model *see* the policy — what
# is allowed, what ran, what is pending — and gives it no way to change any
# of it. There is no MCP tool to grant a cell, open a window, override, or
# roll back; those are operator gestures at the CLI. A mechanical test
# enforces the absence. Phase 7's `execute_proposal` is a *trigger*, not an
# authority: it runs the same executor the CLI does, with no override, so
# `decide()` and the approval channel (a human on another device) still
# decide; a second mechanical test pins that it can never pass an override.
