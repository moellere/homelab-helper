"""Phase 9.5 — host.raid (mdraid composition + health) and host.shares (NFS/SMB + strays)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from homelab_helper.adapters.kernel_ssh import CommandResult
from homelab_helper.db.enums import FindingKind, FindingSeverity, FindingStatus
from homelab_helper.db.models import ReconciliationFinding
from homelab_helper.engine.raid_health import raid_issues
from homelab_helper.engine.reconciler import Reconciler
from homelab_helper.engine.stray_export import detect_stray_exports, host_exports
from homelab_helper.probes.base import AdapterRegistry, ProbeContext, ProbeTarget
from homelab_helper.probes.host.raid import (
    HostRaidProbe,
    array_summary,
    parse_mdadm_uuid,
    parse_mdstat,
)
from homelab_helper.probes.host.shares import (
    HostSharesProbe,
    backing_mountpoint,
    parse_findmnt,
    parse_fstab_mountpoints,
    parse_nfs_exports,
    parse_testparm,
    stray_inputs,
)
from tests.test_reconciler import (
    _record_observations,
    _seed_host,
    _seed_run,
    engine,  # noqa: F401 - fixture
    session_scope,
    sessionmaker,  # noqa: F401 - fixture
)

MDSTAT = """Personalities : [raid1] [raid6] [raid5] [raid4]
md0 : active raid1 sdb1[1] sda1[0]
      976630464 blocks super 1.2 [2/2] [UU]
      bitmap: 0/8 pages [0KB], 65536KB chunk

md1 : active raid5 sdd1[3] sdc1[1] sde1[0](F)
      1953260544 blocks super 1.2 level 5, 512k chunk, algorithm 2 [3/2] [UU_]

md2 : active raid1 sdg1[2] sdf1[0]
      488253440 blocks super 1.2 [2/1] [U_]
      [===>.................]  recovery = 17.4% (85123072/488253440) finish=42.1min speed=159456K/sec

md3 : inactive sdh1[0](S)
      976630464 blocks super 1.2

md4 : active raid1 sdj1[1] sdi1[0]
      100 blocks super 1.2 [2/2] [UU]
      [=>...................]  check =  6.2% (6/100) finish=1.0min speed=1K/sec

unused devices: <none>
"""

MDADM_DETAIL = """/dev/md0:
           Version : 1.2
        Raid Level : raid1
              UUID : 4E3A1B2C:5D6E7F80:91A2B3C4:D5E6F708
"""


def test_mdstat_composition_roles_and_sync() -> None:
    arrays = {a["name"]: a for a in parse_mdstat(MDSTAT)}
    assert list(arrays) == ["md0", "md1", "md2", "md3", "md4"]
    md0 = arrays["md0"]
    assert (md0["level"], md0["state"], md0["raid_disks"], md0["active_disks"]) == (
        "raid1",
        "active",
        2,
        2,
    )
    assert md0["size_bytes"] == 976630464 * 1024
    assert [m["device"] for m in md0["members"]] == ["sda1", "sdb1"]  # ordered by slot
    md1 = arrays["md1"]
    assert md1["degraded"] is True
    assert {m["device"]: m["state"] for m in md1["members"]}["sde1"] == "faulty"
    md2 = arrays["md2"]
    assert (md2["sync_action"], md2["sync_progress"]) == ("recovery", 17.4)
    md3 = arrays["md3"]
    assert md3["state"] == "inactive"
    assert md3["members"] == [{"device": "sdh1", "slot": 0, "state": "spare"}]
    assert arrays["md4"]["sync_action"] == "check"
    assert parse_mdstat("Personalities : \nunused devices: <none>\n") == []


def test_mdadm_uuid_and_summary() -> None:
    assert parse_mdadm_uuid(MDADM_DETAIL) == "4e3a1b2c:5d6e7f80:91a2b3c4:d5e6f708"
    assert parse_mdadm_uuid("") is None
    md1 = next(a for a in parse_mdstat(MDSTAT) if a["name"] == "md1")
    assert array_summary(md1) == {
        "name": "md1",
        "level": "raid5",
        "state": "active",
        "disks": "2/3",
        "degraded": True,
        "sync_action": None,
        "members": ["sde1", "sdc1", "sdd1"],
    }


def test_raid_issues_degraded_rebuilding_inactive_but_not_a_scrub() -> None:
    issues = {
        i.target_id.split("/")[-1]: i for i in raid_issues("h1", "nas0", parse_mdstat(MDSTAT))
    }
    assert set(issues) == {"md1", "md2", "md3"}  # md0 healthy, md4 is only a scrub
    assert (issues["md1"].category, issues["md1"].severity) == (
        "raid-degraded",
        FindingSeverity.HIGH,
    )
    assert "sde1" in issues["md1"].title
    assert (issues["md2"].category, issues["md2"].severity) == (
        "raid-rebuilding",
        FindingSeverity.MEDIUM,
    )
    assert "17.4%" in issues["md2"].title
    assert (issues["md3"].category, issues["md3"].severity) == (
        "raid-inactive",
        FindingSeverity.HIGH,
    )
    assert issues["md1"].also_affects == (("host", "h1"),)


# ------------------------------------------------------------------- probes


class _FakeSSH:
    def __init__(self, outputs: dict[str, tuple[int, str]]) -> None:
        self.outputs = outputs
        self.commands: list[str] = []

    async def run(self, command: str, **_: Any) -> CommandResult:
        self.commands.append(command)
        code, out = next((v for k, v in self.outputs.items() if k in command), (1, ""))
        return CommandResult(command=command, stdout=out, stderr="", exit_code=code)


class _FakeAdapter:
    def __init__(self, ssh: _FakeSSH) -> None:
        self._ssh = ssh

    def session(self, *_: Any, **__: Any):
        ssh = self._ssh

        class _Ctx:
            async def __aenter__(self):
                return ssh

            async def __aexit__(self, *exc: Any) -> bool:
                return False

        return _Ctx()


def _ctx(ssh: _FakeSSH, user: str = "admin") -> ProbeContext:
    return ProbeContext(
        target=ProbeTarget(kind="host", host_id="h1", hostname="nas0", ssh_user=user),
        adapters=AdapterRegistry({"kernel-ssh": _FakeAdapter(ssh)}),
        run_id="r1",
    )


async def test_raid_probe_emits_arrays_with_uuid_when_sudo_allows() -> None:
    ssh = _FakeSSH({"/proc/mdstat": (0, MDSTAT), "mdadm --detail /dev/md0": (0, MDADM_DETAIL)})
    result = await HostRaidProbe().run(_ctx(ssh))
    assert result.success, result.error
    obs = {o.key: o.value for o in result.observations}
    assert obs["host.raid.array_count"] == 5
    by_name = {a["name"]: a for a in obs["host.raid.arrays"]}
    assert by_name["md0"]["uuid"] == "4e3a1b2c:5d6e7f80:91a2b3c4:d5e6f708"
    assert by_name["md1"]["uuid"] is None  # mdadm refused: composition still reported
    assert all(c.startswith("sudo -n mdadm") for c in ssh.commands if "mdadm" in c)


async def test_raid_probe_reports_zero_arrays_without_the_md_driver() -> None:
    result = await HostRaidProbe().run(_ctx(_FakeSSH({}), user="root"))
    assert result.success
    obs = {o.key: o.value for o in result.observations}
    assert obs["host.raid.arrays"] == []  # an observation: lets a stale finding resolve


EXPORTFS = """/srv/nfs/media	192.168.1.0/24(sync,wdelay,hide,no_subtree_check,sec=sys,rw,secure,root_squash)
/srv/dev-disk-by-uuid-1234/backups
		10.0.0.5(sync,wdelay,hide,no_subtree_check,sec=sys,ro,secure,root_squash)
/srv/gone	*(ro)
"""

ETC_EXPORTS = """# comment
/srv/nfs/media 192.168.1.0/24(rw,sync) \\
    10.0.0.9(ro)
"""

TESTPARM = """[global]
	workgroup = WORKGROUP
[media]
	path = /srv/nfs/media
	read only = No
[homes]
	browseable = No
[printers]
	path = /var/spool/samba
	printable = Yes
"""

FSTAB = """UUID=abcd / ext4 defaults 0 1
UUID=1234 /srv/dev-disk-by-uuid-1234 ext4 defaults,nofail 0 2
UUID=5678 /srv/nfs ext4 defaults 0 2
/swapfile none swap sw 0 0
"""

FINDMNT = '{"filesystems":[{"source":"/dev/sda1","target":"/","fstype":"ext4"},{"source":"/dev/sdc1","target":"/srv/nfs","fstype":"ext4"},{"source":"tmpfs","target":"/run","fstype":"tmpfs"}]}'


def test_share_parsers() -> None:
    nfs = parse_nfs_exports(EXPORTFS)
    assert [(e["path"], e["client"]) for e in nfs] == [
        ("/srv/nfs/media", "192.168.1.0/24"),
        ("/srv/dev-disk-by-uuid-1234/backups", "10.0.0.5"),  # exportfs -v wrapped line
        ("/srv/gone", "*"),
    ]
    assert "ro" in nfs[1]["options"]
    declared = parse_nfs_exports(ETC_EXPORTS)
    assert [(e["path"], e["client"]) for e in declared] == [
        ("/srv/nfs/media", "192.168.1.0/24"),
        ("/srv/nfs/media", "10.0.0.9"),  # backslash continuation
    ]
    assert parse_testparm(TESTPARM) == [
        {"protocol": "smb", "name": "media", "path": "/srv/nfs/media", "read_only": False}
    ]
    points = parse_fstab_mountpoints(FSTAB)
    assert points == ["/", "/srv/dev-disk-by-uuid-1234", "/srv/nfs"]
    omv = parse_fstab_mountpoints(
        "/srv/dev-disk-by-uuid-1234/Backup/ /export/Backup none bind,nofail 0 0\n"
        "proc /proc proc defaults 0 0\n"
    )
    assert omv == ["/export/Backup"]  # a bind is a promise; /proc is not
    assert backing_mountpoint("/srv/nfs/media", points) == "/srv/nfs"
    assert backing_mountpoint("/home/x", points) is None  # only "/" — cannot judge
    assert [f["mountpoint"] for f in parse_findmnt(FINDMNT)] == ["/", "/srv/nfs"]


def test_unbacked_and_missing_exports_are_strays_and_root_paths_are_skipped() -> None:
    exports = parse_nfs_exports(EXPORTFS) + parse_testparm(TESTPARM)
    existing = {"/srv/nfs/media", "/srv/dev-disk-by-uuid-1234/backups"}
    filesystems, folders = stray_inputs(
        exports, existing, parse_fstab_mountpoints(FSTAB), parse_findmnt(FINDMNT)
    )
    strays, skipped = detect_stray_exports(
        filesystems, folders, host_exports(exports), scope="host:nas0"
    )
    by_label = {s.label: s.reason for s in strays}
    assert (
        by_label
        == {
            "host:nas0//srv/dev-disk-by-uuid-1234/backups@10.0.0.5": "no-filesystem",  # its disk isn't mounted
            "host:nas0//srv/gone@*": "no-shared-folder",  # the path is gone
        }
    )
    assert skipped == 0


async def test_shares_probe_falls_back_to_etc_exports_and_tolerates_no_samba() -> None:
    ssh = _FakeSSH(
        {
            "exportfs -v": (1, ""),
            "cat /etc/exports": (0, ETC_EXPORTS),
            "cat /etc/fstab": (0, FSTAB),
            "findmnt": (0, FINDMNT),
            "test -d /srv/nfs/media": (0, ""),
        }
    )
    result = await HostSharesProbe().run(_ctx(ssh))
    assert result.success, result.error
    obs = {o.key: o.value for o in result.observations}
    assert obs["host.shares.export_count"] == 2
    assert obs["host.shares.folders"] == [
        {"name": "/srv/nfs/media", "uuid": "/srv/nfs/media", "device": "/srv/nfs"}
    ]


# ------------------------------------------------------------ reconciler path


async def test_reconciler_opens_raid_findings_per_host_and_resolves_on_repair(sessionmaker) -> None:  # noqa: F811
    now = datetime.now(UTC)
    async with session_scope(sessionmaker) as s:
        nas = await _seed_host(s, "nas0")
        other = await _seed_host(s, "nas1")
        run = await _seed_run(s, nas.id, "host.raid")
        arrays = parse_mdstat(MDSTAT)
        await _record_observations(
            s,
            run.id,
            nas.id,
            {"host.raid.arrays": arrays, "host.raid.summary": [array_summary(a) for a in arrays]},
        )
        result = await Reconciler().reconcile_host(s, nas.id)
        assert len(result.findings_opened) == 3
        assert {r["name"] for r in nas.capabilities["raid"]} == {"md0", "md1", "md2", "md3", "md4"}

        # A host whose raid probe never ran resolves nothing.
        await Reconciler().reconcile_host(s, other.id)
        open_rows = (
            (
                await s.execute(
                    select(ReconciliationFinding).where(
                        ReconciliationFinding.kind == FindingKind.STORAGE_HEALTH,
                        ReconciliationFinding.status == FindingStatus.OPEN,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(open_rows) == 3

        # md1 repaired, md2 finished rebuilding, md3 gone: a fresh observation resolves all three.
        healthy = [a for a in arrays if a["name"] in ("md0", "md4")]
        run2 = await _seed_run(s, nas.id, "host.raid")
        await _record_observations(
            s,
            run2.id,
            nas.id,
            {"host.raid.arrays": healthy},
            recorded_at=now + timedelta(minutes=5),
        )
        repaired = await Reconciler().reconcile_host(s, nas.id)
        assert len(repaired.findings_resolved) == 3


async def test_reconciler_runs_the_stray_export_check_for_host_shares(sessionmaker) -> None:  # noqa: F811
    exports = parse_nfs_exports(EXPORTFS)
    filesystems, folders = stray_inputs(
        exports,
        {"/srv/nfs/media", "/srv/dev-disk-by-uuid-1234/backups"},
        parse_fstab_mountpoints(FSTAB),
        parse_findmnt(FINDMNT),
    )
    async with session_scope(sessionmaker) as s:
        nas = await _seed_host(s, "nas0")
        run = await _seed_run(s, nas.id, "host.shares")
        await _record_observations(
            s,
            run.id,
            nas.id,
            {
                "host.shares.exports": exports,
                "host.shares.folders": folders,
                "host.shares.filesystems": filesystems,
            },
        )
        result = await Reconciler().reconcile_host(s, nas.id)
        rows = (
            (
                await s.execute(
                    select(ReconciliationFinding).where(
                        ReconciliationFinding.kind == FindingKind.STRAY_CONFIG
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(result.findings_opened) == 2
    assert {r.affected[0]["target_id"] for r in rows} == {
        "nfs:host:nas0//srv/dev-disk-by-uuid-1234/backups@10.0.0.5",
        "nfs:host:nas0//srv/gone@*",
    }
    assert all(r.evidence_refs[0]["type"] == "host_export" for r in rows)


def test_an_omv_style_bind_export_is_judged_and_flagged_when_its_bind_is_missing() -> None:
    fstab = "/srv/dev-disk-by-uuid-1234/Backup/ /export/Backup none bind,nofail 0 0\n"
    exports = parse_nfs_exports("/export/Backup\t10.0.0.0/16(rw)\n")
    points = parse_fstab_mountpoints(fstab)
    mounted_ok = parse_findmnt(
        '{"filesystems":[{"source":"/dev/sdd2[/Backup]","target":"/export/Backup","fstype":"ext4"}]}'
    )
    fs, folders = stray_inputs(exports, {"/export/Backup"}, points, mounted_ok)
    assert folders == [
        {"name": "/export/Backup", "uuid": "/export/Backup", "device": "/export/Backup"}
    ]
    assert detect_stray_exports(fs, folders, host_exports(exports), scope="host:nas")[0] == []
    fs, folders = stray_inputs(exports, {"/export/Backup"}, points, [])  # disk gone: no bind
    strays, _ = detect_stray_exports(fs, folders, host_exports(exports), scope="host:nas")
    assert [s.reason for s in strays] == ["no-filesystem"]
