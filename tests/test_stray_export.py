"""OMV stray-export detection: pure cross-reference + finding lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from homelab_helper.db.base import Base
from homelab_helper.db.enums import FindingKind, FindingStatus
from homelab_helper.db.models import ReconciliationFinding
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.stray_export import (
    NO_FILESYSTEM,
    NO_SHARED_FOLDER,
    detect_stray_exports,
    reconcile_stray_exports,
)

FILESYSTEMS = [
    {"device": "/dev/sda1", "mountpoint": "/srv/dev-disk-by-uuid-AAAA-1111", "label": "data"},
    {"device": "/dev/sdb1", "mountpoint": "/srv/dev-disk-by-label-backup", "label": "backup"},
]
FOLDERS = [
    {"uuid": "u-media", "name": "media", "path": "media/", "device": "/dev/sda1"},
    {
        "uuid": "u-photos",
        "name": "photos",
        "path": "photos/",
        "device": "aaaa-1111",
    },  # by mountpoint uuid
    {"uuid": "u-bak", "name": "backup", "path": "/", "device": "backup"},  # by label
    {"uuid": "u-gone", "name": "scratch", "path": "scratch/", "device": "/dev/sdz9"},  # disk pulled
    {"uuid": "u-nodev", "name": "mystery", "path": "m/", "device": None},  # unjudgeable
]
EXPORTS = [
    {"protocol": "nfs", "uuid": "n1", "shared_folder": "media", "client": "10.0.1.0/24"},
    {"protocol": "nfs", "uuid": "n2", "shared_folder": "u-photos", "client": None},  # by ref
    {"protocol": "smb", "uuid": "s1", "name": "Backups", "shared_folder": "backup"},
    {"protocol": "smb", "uuid": "s2", "name": "Scratch", "shared_folder": "scratch"},
    {"protocol": "nfs", "uuid": "n3", "shared_folder": "deleted-folder", "client": "*"},
    {"protocol": "smb", "uuid": "s3", "name": "Mystery", "shared_folder": "mystery"},
]


def test_detect_cross_references_folders_and_filesystems() -> None:
    strays, skipped = detect_stray_exports(FILESYSTEMS, FOLDERS, EXPORTS)
    assert skipped == 1  # "mystery" has no device: not judged, not stray
    by_target = {s.target_id: s for s in strays}
    assert set(by_target) == {"smb:Scratch", "nfs:deleted-folder@*"}
    assert by_target["smb:Scratch"].reason == NO_FILESYSTEM
    assert "/dev/sdz9" in by_target["smb:Scratch"].detail
    assert by_target["nfs:deleted-folder@*"].reason == NO_SHARED_FOLDER
    # Labels: NFS keeps the client, SMB uses the share name.
    assert by_target["nfs:deleted-folder@*"].label == "deleted-folder@*"


def test_detect_matches_device_by_path_mountpoint_uuid_and_label() -> None:
    healthy = [e for e in EXPORTS if e["uuid"] in {"n1", "n2", "s1"}]
    strays, _ = detect_stray_exports(FILESYSTEMS, FOLDERS, healthy)
    assert strays == []


def test_fingerprint_is_stable_per_export_regardless_of_reason() -> None:
    a, _ = detect_stray_exports(
        [],
        [{"uuid": "u", "name": "x", "device": "/dev/nope"}],
        [{"protocol": "smb", "name": "X", "shared_folder": "x"}],
    )
    b, _ = detect_stray_exports([], [], [{"protocol": "smb", "name": "X", "shared_folder": "x"}])
    assert a[0].reason == NO_FILESYSTEM
    assert b[0].reason == NO_SHARED_FOLDER
    assert a[0].fingerprint == b[0].fingerprint


@pytest.fixture
async def engine():
    eng = make_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
def sessionmaker(engine):
    return make_sessionmaker(engine)


async def _findings(s) -> dict[str, ReconciliationFinding]:
    rows = (await s.execute(select(ReconciliationFinding))).scalars().all()
    return {f.affected[0]["target_id"]: f for f in rows}


async def test_lifecycle_open_update_resolve_reopen(sessionmaker) -> None:
    t0 = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    async with session_scope(sessionmaker) as s:
        first = await reconcile_stray_exports(s, FILESYSTEMS, FOLDERS, EXPORTS, when=t0)
        assert sorted(first.opened) == ["nfs:deleted-folder@*", "smb:Scratch"]
        assert first.stray == 2
        assert first.skipped_no_device == 1
        found = await _findings(s)
        assert found["smb:Scratch"].kind is FindingKind.STRAY_CONFIG
        assert found["smb:Scratch"].status is FindingStatus.OPEN
        assert "not mounted" in found["smb:Scratch"].description
        assert (
            found["smb:Scratch"].proposed_actions[0]["summary"].startswith("Remove the SMB export")
        )

        # Same state again: re-seen, nothing new.
        second = await reconcile_stray_exports(
            s, FILESYSTEMS, FOLDERS, EXPORTS, when=t0 + timedelta(hours=1)
        )
        assert second.opened == []
        assert sorted(second.updated) == ["nfs:deleted-folder@*", "smb:Scratch"]

        # The scratch disk comes back: its finding resolves; the deleted folder's stays.
        with_disk = [*FILESYSTEMS, {"device": "/dev/sdz9", "mountpoint": "/srv/z", "label": None}]
        third = await reconcile_stray_exports(
            s, with_disk, FOLDERS, EXPORTS, when=t0 + timedelta(hours=2)
        )
        assert third.resolved == ["smb:Scratch"]
        assert third.updated == ["nfs:deleted-folder@*"]
        found = await _findings(s)
        assert found["smb:Scratch"].status is FindingStatus.RESOLVED

        # Disk pulled again: the same row reopens.
        fourth = await reconcile_stray_exports(
            s, FILESYSTEMS, FOLDERS, EXPORTS, when=t0 + timedelta(hours=3)
        )
        assert fourth.reopened == ["smb:Scratch"]
        assert len(await _findings(s)) == 2


async def test_absent_export_never_auto_resolves(sessionmaker) -> None:
    """Invariant #1: an export that vanished from the config is left untouched."""
    async with session_scope(sessionmaker) as s:
        await reconcile_stray_exports(s, FILESYSTEMS, FOLDERS, EXPORTS)
        without_scratch = [e for e in EXPORTS if e["uuid"] != "s2"]
        result = await reconcile_stray_exports(s, FILESYSTEMS, FOLDERS, without_scratch)
        assert result.resolved == []
        found = await _findings(s)
        assert found["smb:Scratch"].status is FindingStatus.OPEN


async def test_acknowledged_finding_resolves_when_condition_clears(sessionmaker) -> None:
    async with session_scope(sessionmaker) as s:
        await reconcile_stray_exports(s, FILESYSTEMS, FOLDERS, EXPORTS)
        found = await _findings(s)
        found["nfs:deleted-folder@*"].status = FindingStatus.ACKNOWLEDGED
        await s.flush()
        restored = [
            *FOLDERS,
            {"uuid": "u-new", "name": "deleted-folder", "path": "d/", "device": "/dev/sda1"},
        ]
        result = await reconcile_stray_exports(s, FILESYSTEMS, restored, EXPORTS)
        assert result.resolved == ["nfs:deleted-folder@*"]
