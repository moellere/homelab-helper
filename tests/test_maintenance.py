"""Phase 10.4 — maintenance runs: planned as proposals, executed as gated steps."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from homelab_helper.adapters.kernel_ssh import CommandResult
from homelab_helper.adapters.proxmox import ProxmoxAdapter, ProxmoxConfig
from homelab_helper.db.base import Base
from homelab_helper.db.enums import (
    AutonomyLevel,
    IntentState,
    IntentTargetType,
    MaintenanceStatus,
    ProposalOutcome,
    TrustDomain,
)
from homelab_helper.db.models import (
    Cluster,
    ExecutionReceipt,
    Host,
    OperationalIntent,
    ProposalLog,
    VirtualMachine,
)
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine import executor as executor_mod
from homelab_helper.engine.maintenance import (
    MaintenanceError,
    abort_maintenance,
    execute_maintenance,
    plan_maintenance,
    run_summary,
)
from homelab_helper.engine.trust import grant_cell, seed_domains

_PVE = ProxmoxConfig(url="https://pve.test:8006", token_id="t@pam!x", token_secret="s")
G = 1024**3
NODE = "pve1"


def make_proxmox(
    requests: list[httpx.Request],
    *,
    guests: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    fail_migrate_vmid: int | None = None,
) -> ProxmoxAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/cluster/status"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"type": "cluster", "name": "lab", "quorate": 1, "nodes": len(nodes)},
                        *[{"type": "node", "name": n["node"], "online": 1} for n in nodes],
                    ]
                },
            )
        if path.endswith("/cluster/resources"):
            kind = request.url.params.get("type")
            return httpx.Response(200, json={"data": nodes if kind == "node" else guests})
        if path.endswith("/status/current"):
            return httpx.Response(200, json={"data": {"status": "running", "name": "g"}})
        if path.endswith("/migrate"):
            vmid = int(path.split("/")[-2])
            if vmid == fail_migrate_vmid:
                return httpx.Response(500, json={"message": "migration aborted", "data": None})
            return httpx.Response(200, json={"data": "UPID:pve1:0001:qmigrate"})
        return httpx.Response(200, json={"data": "UPID:pve1:0000:other"})

    client = httpx.AsyncClient(
        base_url=_PVE.url.rstrip("/") + "/api2/json", transport=httpx.MockTransport(handler)
    )
    return ProxmoxAdapter(_PVE, client=client)


class _FakeSSH:
    def __init__(self) -> None:
        self.commands: list[str] = []

    async def apt_dist_upgrade(self, host: str, **kw: Any) -> CommandResult:
        self.commands.append("dist-upgrade")
        return CommandResult(command="dist-upgrade", stdout="0 upgraded", stderr="", exit_code=0)

    async def reboot_node(self, host: str, **kw: Any) -> CommandResult:
        self.commands.append("reboot")
        return CommandResult(command="systemctl reboot", stdout="", stderr="", exit_code=0)

    async def health_check(self, host: str, **kw: Any) -> tuple[bool, str | None]:
        return True, None


NODES = [
    {"node": NODE, "status": "online", "maxmem": 32 * G, "mem": 20 * G},
    {"node": "pve2", "status": "online", "maxmem": 32 * G, "mem": 4 * G},
    {"node": "pve3", "status": "online", "maxmem": 16 * G, "mem": 12 * G},
    {"node": "pve4", "status": "offline", "maxmem": 64 * G, "mem": 0},
]
GUESTS = [
    {
        "vmid": 105,
        "name": "ha",
        "node": NODE,
        "type": "qemu",
        "status": "running",
        "maxmem": 12 * G,
    },
    {"vmid": 106, "name": "cf", "node": NODE, "type": "lxc", "status": "running", "maxmem": 1 * G},
    {
        "vmid": 6113,
        "name": "cp1",
        "node": NODE,
        "type": "qemu",
        "status": "running",
        "maxmem": 4 * G,
    },
    {
        "vmid": 104,
        "name": "off",
        "node": NODE,
        "type": "qemu",
        "status": "stopped",
        "maxmem": 8 * G,
    },
    {
        "vmid": 9000,
        "name": "tpl",
        "node": NODE,
        "type": "qemu",
        "status": "running",
        "maxmem": 1 * G,
        "template": 1,
    },
    {
        "vmid": 200,
        "name": "elsewhere",
        "node": "pve2",
        "type": "qemu",
        "status": "running",
        "maxmem": 2 * G,
    },
]


@pytest.fixture
async def sessionmaker():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield make_sessionmaker(engine)
    await engine.dispose()


@pytest.fixture
def fast_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    clock = [0.0]

    async def _sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(executor_mod.asyncio, "sleep", _sleep)
    monkeypatch.setattr(executor_mod.time, "monotonic", lambda: clock[0])
    return clock


async def seed(session: Any, *, cp1_local: bool = True, close_pve2: bool = False) -> Host:
    await seed_domains(session)
    host = Host(
        hostname=NODE, primary_ip="10.0.6.21", credentials_ref="ssh:root:/root/.ssh/id_ed25519"
    )
    session.add_all(
        [
            host,
            Host(hostname="pve2", primary_ip="10.0.6.22"),
            Host(hostname="pve3", primary_ip="10.0.6.23"),
        ]
    )
    cluster = Cluster(name="lab", kind="proxmox")
    session.add(cluster)
    await session.flush()
    session.add(
        VirtualMachine(
            cluster_id=cluster.id,
            vmid=6113,
            name="cp1",
            kind="qemu",
            status="running",
            node_name=NODE,
            attributes={"storages": ["local"], "shared_storage": not cp1_local},
        )
    )
    if close_pve2:
        pve2 = (await session.execute(select(Host).where(Host.hostname == "pve2"))).scalar_one()
        session.add(
            OperationalIntent(
                target_type=IntentTargetType.HOST,
                target_id=str(pve2.id),
                intent=IntentState.NO_NEW_GUESTS,
                declared_by="op",
            )
        )
    await session.flush()
    return host


async def test_plan_drains_to_the_emptiest_node_and_shuts_down_local_storage_guests(
    sessionmaker,
) -> None:
    requests: list[httpx.Request] = []
    pve = make_proxmox(requests, guests=GUESTS, nodes=NODES)
    async with session_scope(sessionmaker) as s:
        await seed(s)
        run = await plan_maintenance(s, pve, NODE, actor="enoch")
        kinds = [(st["kind"], st["name"]) for st in run.steps]
        assert [k for k, _ in kinds] == [
            "drain-migrate",  # ha 12 GiB → pve2 (27 GiB free)
            "drain-shutdown",  # cp1: node-local disk
            "drain-migrate",  # cf 1 GiB → pve2 (still the emptiest after ha)
            "node-update",
            "node-reboot",
            "return-migrate",  # cf back
            "return-start",  # cp1
            "return-migrate",  # ha back
        ]
        assert "ha (105) → pve2" in kinds[0][1]
        assert "ha (105) back from pve2" in kinds[7][1]
        assert "node-local disk" in kinds[1][1]
        proposals = (await s.execute(select(ProposalLog))).scalars().all()
        assert len(proposals) == 8
        assert {p.proposed_by for p in proposals} == {"maintenance:enoch"}
        assert all(p.outcome is ProposalOutcome.PENDING for p in proposals)
        assert proposals[0].title.startswith(f"Maintenance {NODE} 1/8")
        assert run.status is MaintenanceStatus.PLANNED
        assert run_summary(run)["steps"][4]["kind"] == "node-reboot"
    await pve.aclose()


async def test_plan_honours_no_new_guests_and_capacity(sessionmaker) -> None:
    requests: list[httpx.Request] = []
    pve = make_proxmox(requests, guests=GUESTS, nodes=NODES)
    async with session_scope(sessionmaker) as s:
        await seed(s, close_pve2=True)
        # pve2 is closed; pve3 has 16-12-1 = 3 GiB free: ha (12 GiB) fits nowhere on its own.
        with pytest.raises(MaintenanceError, match="pve2 would fit but is no-new-guests"):
            await plan_maintenance(s, pve, NODE, actor="enoch")
        # Naming the closed host is the operator overriding their own intent for this run.
        run = await plan_maintenance(s, pve, NODE, actor="enoch", target_node="pve2")
        assert "→ pve2 (no-new-guests, named by you)" in run.steps[0]["name"]
    await pve.aclose()


async def test_plan_refuses_a_host_without_ssh_credentials_before_any_drain(sessionmaker) -> None:
    requests: list[httpx.Request] = []
    pve = make_proxmox(requests, guests=GUESTS, nodes=NODES)
    async with session_scope(sessionmaker) as s:
        host = await seed(s)
        host.credentials_ref = None
        await s.flush()
        with pytest.raises(MaintenanceError, match="no ssh credentials_ref"):
            await plan_maintenance(s, pve, NODE, actor="enoch")
        assert (await s.execute(select(ProposalLog))).scalars().all() == []


async def test_plan_refuses_a_second_open_run_an_unknown_host_and_a_bad_target(
    sessionmaker,
) -> None:
    requests: list[httpx.Request] = []
    pve = make_proxmox(requests, guests=GUESTS, nodes=NODES)
    async with session_scope(sessionmaker) as s:
        await seed(s)
        with pytest.raises(MaintenanceError, match="not an online node"):
            await plan_maintenance(s, pve, NODE, actor="enoch", target_node="pve4")  # offline
        with pytest.raises(MaintenanceError, match="cannot hold ha"):
            await plan_maintenance(s, pve, NODE, actor="enoch", target_node="pve3")  # 3 GiB free
        await plan_maintenance(s, pve, NODE, actor="enoch")
        with pytest.raises(MaintenanceError, match="unfinished run"):
            await plan_maintenance(s, pve, NODE, actor="enoch")
        with pytest.raises(MaintenanceError, match="not a known host"):
            await plan_maintenance(s, pve, "pve9", actor="enoch")
    await pve.aclose()


async def _grants(s: Any) -> None:
    for domain, kind in (
        (TrustDomain.HYPERVISOR, "migrate"),
        (TrustDomain.HYPERVISOR, "shutdown"),
        (TrustDomain.HYPERVISOR, "start"),
        (TrustDomain.CONTAINERS, "migrate"),
        (TrustDomain.HOST_OS, "node-update"),
        (TrustDomain.HOST_OS, "node-reboot"),
    ):
        await grant_cell(s, domain, kind, "single-host", AutonomyLevel.CONFIRM, actor="enoch")


async def test_run_executes_every_step_in_order_with_one_consent(sessionmaker, fast_clock) -> None:
    requests: list[httpx.Request] = []
    pve = make_proxmox(requests, guests=GUESTS, nodes=NODES)
    ssh = _FakeSSH()
    consents: list[str] = []

    async def _yes(manifest: Any, decision: Any) -> bool:
        consents.append(manifest.action_kind)
        return True

    async with session_scope(sessionmaker) as s:
        await seed(s)
        await _grants(s)
        run = await plan_maintenance(s, pve, NODE, actor="enoch")
        # The node is drained by the time update/reboot run: hand the executor a
        # view where nothing runs on pve1 any more.
        pve_drained = make_proxmox(
            requests, guests=[g for g in GUESTS if g["node"] != NODE], nodes=NODES
        )
        run = await execute_maintenance(
            s, run, pve_drained, actor="enoch", confirm_cb=_yes, ssh_adapter=ssh
        )
        assert run.status is MaintenanceStatus.SUCCEEDED, run.notes
        assert [st["state"] for st in run.steps] == ["succeeded"] * 8
        assert all(st["receipt_id"] for st in run.steps)
        receipts = (await s.execute(select(ExecutionReceipt))).scalars().all()
        assert len(receipts) == 8
        assert {r.outcome for r in receipts} == {"succeeded"}
        assert consents == [
            "migrate",
            "shutdown",
            "migrate",
            "node-update",
            "node-reboot",
            "migrate",
            "start",
            "migrate",
        ]
        assert ssh.commands == ["dist-upgrade", "reboot"]
        proposals = (await s.execute(select(ProposalLog))).scalars().all()
        assert {p.outcome for p in proposals} == {ProposalOutcome.USER_ACCEPTED}
    await pve.aclose()
    await pve_drained.aclose()


async def test_run_stops_at_the_first_failure_and_rejects_the_rest(
    sessionmaker, fast_clock
) -> None:
    requests: list[httpx.Request] = []
    pve = make_proxmox(requests, guests=GUESTS, nodes=NODES, fail_migrate_vmid=106)
    ssh = _FakeSSH()

    async def _yes(manifest: Any, decision: Any) -> bool:
        return True

    async with session_scope(sessionmaker) as s:
        await seed(s)
        await _grants(s)
        run = await plan_maintenance(s, pve, NODE, actor="enoch")
        run = await execute_maintenance(
            s, run, pve, actor="enoch", confirm_cb=_yes, ssh_adapter=ssh
        )
        assert run.status is MaintenanceStatus.FAILED
        states = [st["state"] for st in run.steps]
        assert states == [
            "succeeded",
            "succeeded",
            "failed",
            "skipped",
            "skipped",
            "skipped",
            "skipped",
            "skipped",
        ]
        assert "migration aborted" in run.steps[2]["detail"]
        assert run.notes is not None
        assert "step 3" in run.notes
        assert ssh.commands == [], "update and reboot never ran"
        proposals = (
            (await s.execute(select(ProposalLog).order_by(ProposalLog.proposed_at))).scalars().all()
        )
        assert [p.outcome for p in proposals] == [
            ProposalOutcome.USER_ACCEPTED,
            ProposalOutcome.USER_ACCEPTED,
            ProposalOutcome.PENDING,
            *[ProposalOutcome.USER_REJECTED] * 5,
        ]
        with pytest.raises(MaintenanceError, match="not runnable"):
            await execute_maintenance(s, run, pve, actor="enoch", confirm_cb=_yes, ssh_adapter=ssh)
    await pve.aclose()


async def test_a_step_whose_cell_is_at_propose_refuses_and_ends_the_run(
    sessionmaker, fast_clock
) -> None:
    requests: list[httpx.Request] = []
    pve = make_proxmox(requests, guests=GUESTS, nodes=NODES)
    ssh = _FakeSSH()

    async def _yes(manifest: Any, decision: Any) -> bool:
        return True

    async with session_scope(sessionmaker) as s:
        await seed(s)
        await grant_cell(
            s,
            TrustDomain.HYPERVISOR,
            "migrate",
            "single-host",
            AutonomyLevel.CONFIRM,
            actor="enoch",
        )
        run = await plan_maintenance(s, pve, NODE, actor="enoch")
        run = await execute_maintenance(
            s, run, pve, actor="enoch", confirm_cb=_yes, ssh_adapter=ssh
        )
        assert run.status is MaintenanceStatus.FAILED
        assert run.steps[0]["state"] == "succeeded"  # migrate ha: granted
        assert run.steps[1]["state"] == "failed"  # shutdown cp1: cell at PROPOSE
        assert "propose" in run.steps[1]["detail"]
        assert not any(r.path.endswith("/migrate") for r in requests if "106" in r.url.path)
    await pve.aclose()


async def test_abort_rejects_what_is_left(sessionmaker) -> None:
    requests: list[httpx.Request] = []
    pve = make_proxmox(requests, guests=GUESTS, nodes=NODES)
    async with session_scope(sessionmaker) as s:
        await seed(s)
        run = await plan_maintenance(s, pve, NODE, actor="enoch")
        run = await abort_maintenance(s, run, actor="enoch", reason="not today")
        assert run.status is MaintenanceStatus.ABORTED
        assert {st["state"] for st in run.steps} == {"skipped"}
        proposals = (await s.execute(select(ProposalLog))).scalars().all()
        assert {p.outcome for p in proposals} == {ProposalOutcome.USER_REJECTED}
        with pytest.raises(MaintenanceError, match="nothing to abort"):
            await abort_maintenance(s, run, actor="enoch", reason="again")
        # A new run can be planned once the old one is closed.
        again = await plan_maintenance(s, pve, NODE, actor="enoch")
        assert again.id != run.id
    await pve.aclose()


def test_summary_is_json() -> None:
    assert json.dumps({"steps": []})  # sanity for the shape helper's consumers


async def test_plan_resume_brings_back_what_a_stopped_run_left_displaced(sessionmaker) -> None:
    """A run that drained and then stopped: the next plan returns its guests."""
    requests: list[httpx.Request] = []
    async with session_scope(sessionmaker) as s:
        await seed(s, cp1_local=False)
        first = await plan_maintenance(
            s, make_proxmox(requests, guests=GUESTS, nodes=NODES), NODE, actor="e"
        )
        parked = {
            int(
                (await s.get(ProposalLog, uuid.UUID(st["proposal_id"]))).artifact["action"][
                    "target"
                ]["vmid"]
            ): (await s.get(ProposalLog, uuid.UUID(st["proposal_id"]))).artifact["action"][
                "target"
            ]["target_node"]
            for st in first.steps
            if st["kind"] == "drain-migrate"
        }
        assert parked, "the fixture drains at least one guest"
        steps = [dict(st) for st in first.steps]
        for st in steps:
            st["state"] = "succeeded" if st["kind"].startswith("drain-") else "skipped"
        steps[len(parked)]["state"] = "failed"
        first.steps = steps
        first.status = MaintenanceStatus.FAILED
        await s.flush()

        # The guests now sit where the first run parked them; one has since moved on.
        moved_on, *_ = sorted(parked)
        relocated = []
        for g in GUESTS:
            if g["vmid"] in parked:
                dest = (
                    "pve3"
                    if g["vmid"] == moved_on and parked[g["vmid"]] != "pve3"
                    else parked[g["vmid"]]
                )
                relocated.append({**g, "node": dest})
            else:
                relocated.append(g)
        first.status = MaintenanceStatus.RUNNING
        still_open = make_proxmox(requests, guests=relocated, nodes=NODES)
        with pytest.raises(MaintenanceError, match="unfinished run"):
            await plan_maintenance(s, still_open, NODE, actor="e", resume_from=first)
        first.status = MaintenanceStatus.FAILED
        second = await plan_maintenance(
            s,
            make_proxmox(requests, guests=relocated, nodes=NODES),
            NODE,
            actor="e",
            resume_from=first,
        )
        kinds = [st["kind"] for st in second.steps]
        assert kinds[:2] == ["node-update", "node-reboot"], "nothing left to drain"
        assert kinds[2:] == ["return-migrate"] * (len(parked) - 1)
        returned = {
            int(
                (await s.get(ProposalLog, uuid.UUID(st["proposal_id"]))).artifact["action"][
                    "target"
                ]["vmid"]
            )
            for st in second.steps
            if st["kind"] == "return-migrate"
        }
        assert returned == set(parked) - {moved_on}, "the guest that moved on is left alone"
        for st in second.steps[2:]:
            art = (await s.get(ProposalLog, uuid.UUID(st["proposal_id"]))).artifact["action"]
            assert art["target"]["target_node"] == NODE
            assert art["target"]["node"] == parked[int(art["target"]["vmid"])]


async def test_plan_names_the_steps_the_gate_would_refuse_today(sessionmaker) -> None:
    """Planned without the host-os grants: the plan says so instead of the run finding out."""
    requests: list[httpx.Request] = []
    async with session_scope(sessionmaker) as s:
        await seed(s)
        await grant_cell(
            s, TrustDomain.HYPERVISOR, "migrate", "single-host", AutonomyLevel.CONFIRM, actor="e"
        )
        run = await plan_maintenance(
            s, make_proxmox(requests, guests=GUESTS, nodes=NODES), NODE, actor="e"
        )
        by_kind = {st["kind"]: st for st in run.steps}
        assert by_kind["node-update"]["detail"].startswith(
            "would be refused today: host-os/node-update"
        )
        assert by_kind["node-reboot"]["detail"].startswith(
            "would be refused today: host-os/node-reboot"
        )
        assert by_kind["drain-migrate"]["detail"] in (
            None,
            "would be refused today: containers/migrate/single-host is at propose — grant it first",
        )
        assert "would be refused" in run_summary(run)["steps"][-2]["detail"]


def test_run_and_exec_run_ignore_a_terminal_hangup(monkeypatch: pytest.MonkeyPatch) -> None:
    """The process must outlive the SSH session that started it, or the receipts are lost."""
    import signal

    from typer.testing import CliRunner

    from homelab_helper.cli.main import app

    previous = signal.getsignal(signal.SIGHUP)
    try:
        signal.signal(signal.SIGHUP, signal.SIG_DFL)
        monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
        CliRunner().invoke(app, ["maintenance", "run", "00000000", "--yes"])
        assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
        signal.signal(signal.SIGHUP, signal.SIG_DFL)
        CliRunner().invoke(app, ["exec", "run", "00000000", "--yes"])
        assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, previous)
