"""JetKVM → inventory, ``HOST_UNREACHABLE`` findings with console evidence.

One read of the KVM (``adapters/jetkvm.py``) does three things:

- **Inventory.** One ``Service`` row named ``jetkvm`` carries the device's facts (set up, auth mode, device id) and
  the operator-declared ``attached_to`` host in its ``attributes``, with one internal ``ServiceEndpoint``. The KVM
  cannot know what it is plugged into, so ``HOMELAB_HELPER_JETKVM_ATTACHED_TO`` (a harness hostname) says.
- **Reachability of the attached host.** A TCP connect to its ``primary_ip`` on ``HOMELAB_HELPER_JETKVM_ATTACHED_PORT``
  (default 22). A host that does not answer raises ``host-unreachable``; the finding carries a console snapshot
  saved under the data directory, so the reason (a panic, a stuck boot menu, a BIOS prompt) is on the finding.
- **The KVM itself.** A KVM whose status cannot be read raises ``jetkvm-unreachable`` (``SERVICE_UNHEALTHY``).

Invariant 1: each category resolves only when it was observed. ``jetkvm-unreachable`` is observed on every run;
``host-unreachable`` only when the attached host resolved to an address and the connect was attempted, and then only
for that host. A snapshot never decides anything: one that fails is absent evidence, and nothing here raises into the
caller because of it. Neither kind maps to a playbook; these findings report, they never draft a remediation.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import func, select

from homelab_helper.adapters.jetkvm import JetKVMAPIError
from homelab_helper.config import data_dir
from homelab_helper.db.enums import DiscoverySource, FindingKind, FindingSeverity, ResolutionScope
from homelab_helper.db.models import Host, Service, ServiceEndpoint
from homelab_helper.engine.category_findings import CategoryIssue, reconcile_category_findings

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession

    from homelab_helper.adapters.jetkvm import JetKVMAdapter

log = logging.getLogger(__name__)

SERVICE_NAME = "jetkvm"
RESOLVER = "jetkvm"
KVM_UNREACHABLE = "jetkvm-unreachable"
HOST_UNREACHABLE = "host-unreachable"
DEFAULT_PORT = 22
CONNECT_TIMEOUT_S = 5.0
KEEP_SNAPSHOTS = 5


def attached_from_env() -> tuple[str | None, int]:
    """``(HOMELAB_HELPER_JETKVM_ATTACHED_TO, HOMELAB_HELPER_JETKVM_ATTACHED_PORT or 22)``."""
    port = os.environ.get("HOMELAB_HELPER_JETKVM_ATTACHED_PORT", "").strip()
    return os.environ.get("HOMELAB_HELPER_JETKVM_ATTACHED_TO") or None, int(port or DEFAULT_PORT)


def snapshot_dir() -> Path:
    """Where console snapshots are kept: ``<data dir>/console``."""
    return data_dir() / "console"


@dataclass(frozen=True)
class AttachedCheck:
    """The attached host's reachability this run, and the console evidence if it was down."""

    host_id: str
    hostname: str
    address: str
    port: int
    reachable: bool
    snapshot: str | None = None
    snapshot_error: str | None = None


async def resolve_attached(session: AsyncSession, hostname: str | None) -> Host | None:
    """The harness host named ``hostname`` (case-insensitive), when it has an address."""
    if not hostname:
        return None
    host = (
        (
            await session.execute(
                select(Host).where(func.lower(Host.hostname) == hostname.lower()).limit(1)
            )
        )
        .scalars()
        .first()
    )
    return host if host is not None and host.primary_ip else None


async def tcp_reachable(address: str, port: int, timeout: float = CONNECT_TIMEOUT_S) -> bool:  # noqa: ASYNC109
    """True when a TCP connect to ``address:port`` completes within ``timeout``."""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(address, port), timeout)
    except (OSError, TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return True


def _save_snapshot(dest: Path, hostname: str, png: bytes, now: datetime) -> str:
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"{hostname}-{now.strftime('%Y%m%dT%H%M%SZ')}.png"
    path.write_bytes(png)
    for old in sorted(dest.glob(f"{hostname}-*.png"), reverse=True)[KEEP_SNAPSHOTS:]:
        old.unlink(missing_ok=True)
    return str(path)


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


async def snapshot_to_file(adapter: JetKVMAdapter, path: Path) -> str | None:
    """Write one console frame to ``path``; the error text when it could not, else None. Never raises."""
    try:
        _write(path, await adapter.console_snapshot())
    except Exception as exc:
        return (str(exc) or type(exc).__name__)[:300]
    return None


async def capture_console(
    adapter: JetKVMAdapter, dest: Path, hostname: str, *, when: datetime | None = None
) -> tuple[str | None, str | None]:
    """Save one console frame as ``<dest>/<hostname>-<UTC stamp>.png``; ``(path, None)`` or ``(None, error)``.

    Keeps the newest ``KEEP_SNAPSHOTS`` per host. Never raises: a failed snapshot is absent evidence.
    """
    now = when or datetime.now(UTC)
    try:
        png = await adapter.console_snapshot()
        return _save_snapshot(dest, hostname, png, now), None
    except Exception as exc:
        log.debug("console snapshot failed: %s", exc)
        return None, (str(exc) or type(exc).__name__)[:300]


async def import_jetkvm(
    session: AsyncSession,
    *,
    url: str,
    status: dict[str, Any] | None,
    device: dict[str, Any] | None,
    attached_to: str | None,
    when: datetime | None = None,
) -> bool:
    """Upsert the ``jetkvm`` Service and its endpoint; True when the row was created."""
    now = when or datetime.now(UTC)
    svc = (
        await session.execute(select(Service).where(Service.name == SERVICE_NAME))
    ).scalar_one_or_none()
    created = svc is None
    if svc is None:
        svc = Service(name=SERVICE_NAME)
        session.add(svc)
        await session.flush()
    attributes = dict(svc.attributes or {})
    attributes.update(
        {
            "kind": SERVICE_NAME,
            "url": url,
            "attached_to": attached_to,
            "reachable": status is not None,
        }
    )
    if status is not None:
        attributes["is_setup"] = status.get("isSetup")
        attributes["last_seen"] = now.isoformat()
    if device is not None:
        attributes["auth_mode"] = device.get("authMode")
        attributes["device_id"] = device.get("deviceId") or device.get("deviceID")
    svc.attributes = attributes

    hostname = urlparse(url).hostname
    if hostname:
        try:
            ip: str | None = str(ipaddress.ip_address(hostname))
        except ValueError:
            ip, hostname = None, hostname.lower()
        ep = (
            await session.execute(
                select(ServiceEndpoint).where(
                    ServiceEndpoint.service_id == svc.id,
                    ServiceEndpoint.scope == ResolutionScope.INTERNAL,
                    ServiceEndpoint.resolver == RESOLVER,
                )
            )
        ).scalar_one_or_none()
        if ep is None:
            ep = ServiceEndpoint(
                service_id=svc.id,
                scope=ResolutionScope.INTERNAL,
                hostname=hostname,
                ip=ip,
                resolver=RESOLVER,
                discovery_source=DiscoverySource.JETKVM,
            )
            session.add(ep)
        else:
            ep.hostname = hostname
            ep.ip = ip
        ep.attributes = {"url": url}
    await session.flush()
    return created


def kvm_unreachable_issue(url: str, error: str) -> CategoryIssue:
    return CategoryIssue(
        kind=FindingKind.SERVICE_UNHEALTHY,
        category=KVM_UNREACHABLE,
        target_type="service",
        target_id=SERVICE_NAME,
        severity=FindingSeverity.LOW,
        title="JetKVM is unreachable",
        description=(
            f"GET {url}/device/status failed: {error}. The attached host's console cannot be "
            "captured as evidence until it is back."
        ),
        evidence={"url": url, "error": error[:300]},
    )


def host_unreachable_issue(url: str, check: AttachedCheck) -> CategoryIssue:
    where = (
        f"Its console was captured to {check.snapshot}."
        if check.snapshot
        else f"No console snapshot: {check.snapshot_error or 'not attempted'}."
    )
    return CategoryIssue(
        kind=FindingKind.HOST_UNREACHABLE,
        category=HOST_UNREACHABLE,
        target_type="host",
        target_id=check.host_id,
        severity=FindingSeverity.HIGH,
        title=f"{check.hostname} does not answer on {check.address}:{check.port}",
        description=(
            f"A TCP connect to {check.hostname} ({check.address}:{check.port}) failed. The JetKVM on "
            f"its console shows what the machine is doing. {where}"
        ),
        evidence={
            "host": check.hostname,
            "address": check.address,
            "port": check.port,
            "kvm": url,
            "console_snapshot": check.snapshot,
            "snapshot_error": check.snapshot_error,
        },
    )


async def reconcile_jetkvm(
    session: AsyncSession,
    url: str,
    status: dict[str, Any] | None,
    error: str | None,
    attached: AttachedCheck | None,
    *,
    when: datetime | None = None,
) -> tuple[list[CategoryIssue], dict[str, int]]:
    """Reconcile one read: the KVM's own reachability, and the attached host's when it was checked."""
    now = when or datetime.now(UTC)
    kvm_issues = [] if status is not None else [kvm_unreachable_issue(url, error or "unknown")]
    kvm = await reconcile_category_findings(
        session, FindingKind.SERVICE_UNHEALTHY, kvm_issues, {KVM_UNREACHABLE}, when=now
    )
    counts = kvm.counts()
    issues = list(kvm_issues)
    if attached is not None:
        host_issues = [] if attached.reachable else [host_unreachable_issue(url, attached)]
        result = await reconcile_category_findings(
            session,
            FindingKind.HOST_UNREACHABLE,
            host_issues,
            {HOST_UNREACHABLE},
            when=now,
            observed_targets={("host", attached.host_id)},
        )
        for k, v in result.counts().items():
            counts[k] = counts.get(k, 0) + v
        issues += host_issues
    return issues, counts


@dataclass
class JetKVMRun:
    """What one ``observe_jetkvm`` pass saw and did."""

    url: str
    status: dict[str, Any] | None
    error: str | None
    attached_to: str | None
    attached: AttachedCheck | None
    created: bool
    issues: list[CategoryIssue] = field(default_factory=list)
    findings: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        a = self.attached
        return {
            "reachable": self.status is not None,
            "error": self.error,
            "is_setup": (self.status or {}).get("isSetup"),
            "attached_to": self.attached_to,
            "attached": None
            if a is None
            else {
                "host": a.hostname,
                "address": a.address,
                "port": a.port,
                "reachable": a.reachable,
                "console_snapshot": a.snapshot,
                "snapshot_error": a.snapshot_error,
            },
            "issues": [
                {"category": i.category, "severity": i.severity.value, "title": i.title}
                for i in self.issues
            ],
            "findings": self.findings,
        }


async def observe_jetkvm(
    session: AsyncSession,
    adapter: JetKVMAdapter,
    *,
    attached_to: str | None,
    port: int,
    snapshot_dir: Path,
    when: datetime | None = None,
) -> JetKVMRun:
    """Read the KVM, check its attached host, capture evidence if that host is down, and reconcile."""
    now = when or datetime.now(UTC)
    status: dict[str, Any] | None = None
    device: dict[str, Any] | None = None
    error: str | None = None
    try:
        status = await adapter.status()
    except (httpx.HTTPError, JetKVMAPIError) as exc:
        error = str(exc) or type(exc).__name__
    if status is not None:
        try:
            device = await adapter.device()
        except (httpx.HTTPError, JetKVMAPIError) as exc:
            log.debug("jetkvm /device not read: %s", exc)
    created = await import_jetkvm(
        session, url=adapter.url, status=status, device=device, attached_to=attached_to, when=now
    )
    attached: AttachedCheck | None = None
    host = await resolve_attached(session, attached_to)
    if host is not None and host.primary_ip:
        reachable = await tcp_reachable(host.primary_ip, port)
        snapshot = snapshot_error = None
        if not reachable:
            if status is None:
                snapshot_error = "JetKVM unreachable"
            else:
                snapshot, snapshot_error = await capture_console(
                    adapter, snapshot_dir, host.hostname, when=now
                )
        attached = AttachedCheck(
            host_id=str(host.id),
            hostname=host.hostname,
            address=host.primary_ip,
            port=port,
            reachable=reachable,
            snapshot=snapshot,
            snapshot_error=snapshot_error,
        )
    issues, counts = await reconcile_jetkvm(session, adapter.url, status, error, attached, when=now)
    return JetKVMRun(
        url=adapter.url,
        status=status,
        error=error,
        attached_to=attached_to,
        attached=attached,
        created=created,
        issues=issues,
        findings=counts,
    )


__all__ = [
    "DEFAULT_PORT",
    "HOST_UNREACHABLE",
    "KVM_UNREACHABLE",
    "SERVICE_NAME",
    "AttachedCheck",
    "JetKVMRun",
    "attached_from_env",
    "capture_console",
    "host_unreachable_issue",
    "import_jetkvm",
    "kvm_unreachable_issue",
    "observe_jetkvm",
    "reconcile_jetkvm",
    "resolve_attached",
    "snapshot_dir",
    "snapshot_to_file",
    "tcp_reachable",
]
