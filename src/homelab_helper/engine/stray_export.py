"""Stray-export detection — OMV exports with nothing behind them → STRAY_CONFIG.

The storage-side complement of ``stray_config`` (UniFi networks with no
clients). OpenMediaVault publishes shared folders over NFS and SMB; the
folders sit on mounted filesystems. Two things go stale in practice:

- An export still names a shared folder that was deleted (OMV keeps the
  export row; clients get a path that doesn't exist).
- A shared folder's backing filesystem is no longer mounted — the disk was
  pulled, reflashed, or moved to another box — so every export of it serves
  an empty mount.

Both are structural: no history needed, just a cross-reference of the four
reads the adapter already makes. "An export no client mounts" is the temporal
signal the roadmap defers to Phase 2.

Same fingerprint + reopen-on-recurrence lifecycle as every other finding
generator, keyed per export so a folder that is recreated and then loses its
disk hits the same row. Scope discipline (invariant #1): only exports the
appliance *reports* this run are judged; an export that vanished from the
config is left untouched, because absence never auto-resolves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from homelab_helper.db.enums import FindingKind, FindingSeverity, FindingStatus
from homelab_helper.db.models import ReconciliationFinding
from homelab_helper.engine.fingerprint import make_fingerprint

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_TARGET_TYPE = "export"
_ROOT_CAUSE = "unbacked"
NO_SHARED_FOLDER = "no-shared-folder"
NO_FILESYSTEM = "no-filesystem"


@dataclass(frozen=True)
class StrayExport:
    protocol: str
    label: str
    shared_folder: str | None
    reason: str
    detail: str

    @property
    def target_id(self) -> str:
        return f"{self.protocol}:{self.label}"

    @property
    def fingerprint(self) -> str:
        return make_fingerprint(
            FindingKind.STRAY_CONFIG.value, _TARGET_TYPE, self.target_id, _ROOT_CAUSE
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "export": self.label,
            "shared_folder": self.shared_folder,
            "reason": self.reason,
            "detail": self.detail,
            "fingerprint": self.fingerprint,
        }


@dataclass
class StrayExportResult:
    hits: list[StrayExport] = field(default_factory=list)
    opened: list[str] = field(default_factory=list)
    reopened: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    skipped_no_device: int = 0
    """Shared folders that report no backing device — unjudgeable, not stray."""

    @property
    def stray(self) -> int:
        return len(self.opened) + len(self.reopened) + len(self.updated)


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _export_label(export: dict[str, Any]) -> str:
    if export.get("protocol") == "smb":
        return str(export.get("name") or export.get("shared_folder") or "?")
    folder = str(export.get("shared_folder") or "?")
    client = export.get("client")
    return f"{folder}@{client}" if client else folder


def _folder_is_backed(folder: dict[str, Any], filesystems: list[dict[str, Any]]) -> bool | None:
    """True/False when the folder names a device; None when it can't be judged."""
    device = _norm(folder.get("device"))
    if not device:
        return None
    for fs in filesystems:
        if device in {_norm(fs.get("device")), _norm(fs.get("label"))}:
            return True
        if device in _norm(fs.get("mountpoint")):
            return True
    return False


def detect_stray_exports(
    filesystems: list[dict[str, Any]],
    shared_folders: list[dict[str, Any]],
    exports: list[dict[str, Any]],
) -> tuple[list[StrayExport], int]:
    """Pure cross-reference: ``(strays, folders skipped for lack of a device)``."""
    by_name = {_norm(f.get("name")): f for f in shared_folders if f.get("name")}
    by_uuid = {_norm(f.get("uuid")): f for f in shared_folders if f.get("uuid")}
    backing: dict[str, bool | None] = {}
    skipped = 0
    for folder in shared_folders:
        verdict = _folder_is_backed(folder, filesystems)
        if verdict is None:
            skipped += 1
        backing[_norm(folder.get("uuid"))] = verdict

    strays: list[StrayExport] = []
    for export in exports:
        protocol = str(export.get("protocol") or "?")
        label = _export_label(export)
        ref = _norm(export.get("shared_folder"))
        folder: dict[str, Any] | None = by_name.get(ref) or by_uuid.get(ref)
        if folder is None:
            strays.append(
                StrayExport(
                    protocol=protocol,
                    label=label,
                    shared_folder=export.get("shared_folder"),
                    reason=NO_SHARED_FOLDER,
                    detail=f"shared folder {export.get('shared_folder')!r} no longer exists",
                )
            )
            continue
        if backing.get(_norm(folder.get("uuid"))) is False:
            strays.append(
                StrayExport(
                    protocol=protocol,
                    label=label,
                    shared_folder=str(folder.get("name") or export.get("shared_folder")),
                    reason=NO_FILESYSTEM,
                    detail=(
                        f"shared folder {folder.get('name')!r} sits on device "
                        f"{folder.get('device')!r}, which is not mounted"
                    ),
                )
            )
    return strays, skipped


async def _find_by_fingerprint(
    session: AsyncSession, fingerprint: str
) -> ReconciliationFinding | None:
    return (
        await session.execute(
            select(ReconciliationFinding).where(ReconciliationFinding.fingerprint == fingerprint)
        )
    ).scalar_one_or_none()


async def reconcile_stray_exports(
    session: AsyncSession,
    filesystems: list[dict[str, Any]],
    shared_folders: list[dict[str, Any]],
    exports: list[dict[str, Any]],
    *,
    appliance: str = "openmediavault",
    when: datetime | None = None,
) -> StrayExportResult:
    """Open/resolve STRAY_CONFIG findings for exports with nothing behind them."""
    now_ts = when or datetime.now(UTC)
    strays, skipped = detect_stray_exports(filesystems, shared_folders, exports)
    result = StrayExportResult(hits=strays, skipped_no_device=skipped)
    stray_by_target = {s.target_id: s for s in strays}

    for export in exports:
        protocol = str(export.get("protocol") or "?")
        label = _export_label(export)
        target_id = f"{protocol}:{label}"
        probe = StrayExport(protocol, label, None, "", "")
        existing = await _find_by_fingerprint(session, probe.fingerprint)
        stray = stray_by_target.get(target_id)

        if stray is not None:
            title = f"Stray export: {protocol.upper()} {label}"
            description = (
                f"{appliance} exports {label!r} over {protocol.upper()}, but {stray.detail}. "
                "Clients get an empty or missing path; remove the export or restore what backs it."
            )
            affected = [{"target_type": _TARGET_TYPE, "target_id": target_id}]
            if existing is None:
                session.add(
                    ReconciliationFinding(
                        kind=FindingKind.STRAY_CONFIG,
                        severity=FindingSeverity.LOW,
                        fingerprint=stray.fingerprint,
                        title=title,
                        description=description,
                        affected=affected,
                        evidence_refs=[{"type": "omv_export", "id": target_id}],
                        proposed_actions=[
                            {
                                "summary": f"Remove the {protocol.upper()} export {label!r} from {appliance}"
                            },
                            {"summary": "Or restore the shared folder / remount its filesystem"},
                        ],
                        status=FindingStatus.OPEN,
                        first_seen=now_ts,
                        last_seen=now_ts,
                    )
                )
                result.opened.append(target_id)
            else:
                if existing.status == FindingStatus.RESOLVED:
                    existing.status = FindingStatus.OPEN
                    existing.resolved_at = None
                    existing.first_seen = now_ts
                    result.reopened.append(target_id)
                else:
                    result.updated.append(target_id)
                existing.last_seen = now_ts
                existing.title = title
                existing.description = description
                existing.affected = affected
        elif existing is not None and existing.status in {
            FindingStatus.OPEN,
            FindingStatus.ACKNOWLEDGED,
        }:
            existing.status = FindingStatus.RESOLVED
            existing.resolved_at = now_ts
            result.resolved.append(target_id)

    await session.flush()
    return result


__all__ = [
    "NO_FILESYSTEM",
    "NO_SHARED_FOLDER",
    "StrayExport",
    "StrayExportResult",
    "detect_stray_exports",
    "reconcile_stray_exports",
]
