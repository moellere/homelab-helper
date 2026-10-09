"""Phase 9.6 — per-probe and per-assertion cadences from one daemon, with no new state."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from homelab_helper.cli.main import app
from homelab_helper.db.base import Base
from homelab_helper.db.enums import (
    AssertionKind,
    AssertionScope,
    FindingSeverity,
    IntentTargetType,
    PrivilegeLevel,
)
from homelab_helper.db.models import AssertionRun, ConfigurationAssertion, DiscoveryRun, Host
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine import schedule as mod
from homelab_helper.engine.schedule import (
    ScheduleError,
    assertions_due,
    due_now,
    load_schedule,
    parse_interval,
    plan_assertions,
    plan_probes,
    run_due,
)
from homelab_helper.probes.base import ObservationData, Probe, ProbeContext, ProbeResult


class _FastProbe(Probe):
    name: ClassVar[str] = "host.fast"
    version: ClassVar[str] = "0.1.0"
    schema_version: ClassVar[int] = 1
    required_privilege: ClassVar[PrivilegeLevel] = PrivilegeLevel.USER
    target_kinds: ClassVar[list[str]] = ["host"]
    produces_keys: ClassVar[list[str]] = ["host.fast.tick"]
    description: ClassVar[str | None] = "fast"
    runs: ClassVar[list[str]] = []

    async def run(self, ctx: ProbeContext) -> ProbeResult:
        type(self).runs.append(ctx.target.hostname or "?")
        return ProbeResult(
            success=True,
            observations=[
                ObservationData(
                    key=self.produces_keys[0],
                    value=1,
                    target_type=IntentTargetType.HOST,
                    target_id=ctx.target.host_id or "h",
                )
            ],
        )


class _SlowProbe(_FastProbe):
    name: ClassVar[str] = "host.slow"
    produces_keys: ClassVar[list[str]] = ["host.slow.tick"]
    runs: ClassVar[list[str]] = []


FAKES: dict[str, type[Probe]] = {"host.fast": _FastProbe, "host.slow": _SlowProbe}


class _FakeSSH:
    """probe_host holds one shared session for the batch; the fake probes never use it."""

    def shared_session(self, *_: Any, **__: Any):
        class _Ctx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc: Any) -> bool:
                return False

        return _Ctx()


@pytest.fixture
def fakes(monkeypatch):
    monkeypatch.setattr(mod, "discover_probes", lambda: FAKES)
    _FastProbe.runs.clear()
    _SlowProbe.runs.clear()
    return FAKES


@pytest.fixture
async def sessionmaker():
    eng = make_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield make_sessionmaker(eng)
    await eng.dispose()


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "schedule.yaml"
    p.write_text(text)
    return p


SCHEDULE = """
defaults: {probes: 6h, assertions: 6h}
hosts:
  - name: nas0
    ssh_user: root
    primary_ip: 10.0.0.9
    probes: {host.fast: 15m}
assertions: {default: 1h, nightly: 1d}
"""


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("90s", 90), ("15m", 900), ("6h", 21600), ("1d", 86400), ("1w", 604800), (None, 21600)],
)
def test_parse_interval(text, seconds) -> None:
    assert parse_interval(text) == timedelta(seconds=seconds)


def test_cron_syntax_is_refused_not_guessed() -> None:
    with pytest.raises(ScheduleError, match="cron"):
        parse_interval("*/5 * * * *")


def test_load_schedule_validates_probe_names_and_env(tmp_path, monkeypatch, fakes) -> None:
    path = _write(tmp_path, SCHEDULE)
    monkeypatch.setenv("HOMELAB_HELPER_SCHEDULE", str(path))
    schedule = load_schedule()
    assert schedule.hosts[0].probes == {"host.fast": "15m"}
    with pytest.raises(ScheduleError, match="unknown probe"):
        load_schedule(_write(tmp_path, SCHEDULE.replace("host.fast", "host.nope")))
    with pytest.raises(ScheduleError, match="not an interval"):
        load_schedule(_write(tmp_path, SCHEDULE.replace("15m", "soon")))
    monkeypatch.delenv("HOMELAB_HELPER_SCHEDULE")
    with pytest.raises(ScheduleError, match="no schedule declared"):
        load_schedule()


async def test_two_probes_on_different_cadences_from_one_pass(
    tmp_path, sessionmaker, fakes
) -> None:
    """AC #6: host.fast every 15 min, host.slow at the 6 h default — run_due keeps them apart."""
    T0 = datetime.now(UTC)  # rows carry the wall clock, so the test clock starts here
    schedule = load_schedule(_write(tmp_path, SCHEDULE))
    async with session_scope(sessionmaker) as s:
        first = await run_due(s, schedule, now=T0, ssh_adapter=_FakeSSH(), assertions=False)
        assert first.probes_run == ["nas0: host.fast, host.slow"]  # nothing has ever run
        runs = (await s.execute(select(func.count()).select_from(DiscoveryRun))).scalar_one()
        assert runs == 2

        # Runs are stamped with the wall clock (≈ T0), so the clock below is relative to that.
        soon = await run_due(
            s, schedule, now=T0 + timedelta(minutes=10), ssh_adapter=_FakeSSH(), assertions=False
        )
        assert soon.probes_run == []  # nothing due: no connection is made
        assert soon.not_due == 2

        later = await run_due(
            s, schedule, now=T0 + timedelta(minutes=20), ssh_adapter=_FakeSSH(), assertions=False
        )
        assert later.probes_run == ["nas0: host.fast"]  # only the 15-minute probe is due
        assert later.not_due == 1

        plan = await plan_probes(s, schedule, now=T0 + timedelta(minutes=10))
        by_probe = {d.probe: d for d in plan}
        slow_wait = by_probe["host.slow"].due_in(T0 + timedelta(minutes=10))
        assert timedelta(hours=5, minutes=49) < slow_wait < timedelta(hours=5, minutes=51)
        assert len(due_now(plan, T0 + timedelta(hours=7))) == 2
    assert _FastProbe.runs == ["nas0", "nas0"]
    assert _SlowProbe.runs == ["nas0"]


async def test_an_assertion_runs_on_its_own_schedule(tmp_path, sessionmaker, fakes) -> None:
    T0 = datetime.now(UTC)  # rows carry the wall clock, so the test clock starts here
    schedule = load_schedule(_write(tmp_path, SCHEDULE))
    async with session_scope(sessionmaker) as s:
        host = Host(hostname="nas0", primary_ip="10.0.0.9", capabilities={"cpu_cores": 8})
        s.add(host)
        await s.flush()
        for name, own in (("hourly", None), ("nightly", None), ("pinned", "30m")):
            s.add(
                ConfigurationAssertion(
                    name=name,
                    scope=AssertionScope.HOST,
                    scope_target=str(host.id),
                    description=name,
                    kind=AssertionKind.OBSERVATION_PREDICATE,
                    verifier_spec={"key": "host.cpu.cores", "value": 8},
                    severity_on_fail=FindingSeverity.LOW,
                    schedule=own,
                )
            )
        await s.flush()
        plan = await plan_assertions(s, schedule, now=T0)
        intervals = {d.assertion.name: d.interval for d in plan}
        assert intervals == {
            "hourly": timedelta(hours=1),  # the file's default
            "nightly": timedelta(days=1),  # named in the file
            "pinned": timedelta(minutes=30),  # the row's own schedule column wins
        }
        first = await run_due(s, schedule, now=T0, probes=False)
        assert sorted(a.split(":")[0] for a in first.assertions_run) == [
            "hourly",
            "nightly",
            "pinned",
        ]
        assert (await s.execute(select(func.count()).select_from(AssertionRun))).scalar_one() == 3
        again = await run_due(s, schedule, now=T0 + timedelta(minutes=45), probes=False)
        assert [a.split(":")[0] for a in again.assertions_run] == ["pinned"]
        due = assertions_due(
            await plan_assertions(s, schedule, now=T0 + timedelta(hours=25)),
            T0 + timedelta(hours=25),
        )
        assert sorted(d.assertion.name for d in due) == ["hourly", "nightly", "pinned"]


async def test_an_unreachable_target_fails_alone(
    tmp_path, sessionmaker, fakes, monkeypatch
) -> None:
    T0 = datetime.now(UTC)  # rows carry the wall clock, so the test clock starts here
    text = SCHEDULE.replace(
        "    probes: {host.fast: 15m}\n",
        "    probes: {host.fast: 15m}\n  - {name: nas1, ssh_user: root, primary_ip: 10.0.0.10}\n",
    )
    schedule = load_schedule(_write(tmp_path, text))

    async def boom(session, request, **kw):
        if request.name == "nas1":
            raise OSError("connection refused")
        return await real(session, request, **kw)

    real = mod.probe_host
    monkeypatch.setattr(mod, "probe_host", boom)
    async with session_scope(sessionmaker) as s:
        r = await run_due(s, schedule, now=T0, ssh_adapter=_FakeSSH(), assertions=False)
    assert r.probes_run == ["nas0: host.fast, host.slow"]
    assert r.errors == ["nas1: connection refused"]


def test_daemon_once_runs_the_schedule_job_and_cli_shows_it(tmp_path, monkeypatch) -> None:
    from homelab_helper.cli import daemon as dmod

    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/d.db")
    path = _write(tmp_path, SCHEDULE)
    monkeypatch.setattr(mod, "discover_probes", lambda: FAKES)

    async def _init() -> None:
        eng = make_engine(f"sqlite+aiosqlite:///{tmp_path}/d.db")
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await eng.dispose()

    asyncio.run(_init())
    seen: list[str] = []

    async def fake_schedule(schedule_path, *, probes, assertions):
        seen.append(f"schedule:{Path(schedule_path).name}:{probes}:{assertions}")
        return {"probes_run": ["nas0: host.fast"], "not_due": 1}

    monkeypatch.setattr(dmod, "run_schedule_pass", fake_schedule)
    result = CliRunner().invoke(
        app,
        [
            "daemon",
            "run",
            "--once",
            "--sources",
            "",
            "--no-ask",
            "--no-playbooks",
            "--schedule",
            str(path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen == ["schedule:schedule.yaml:True:True"]
    assert "nas0: host.fast" in result.output

    # Without a file and without the env var the job is simply absent.
    monkeypatch.delenv("HOMELAB_HELPER_SCHEDULE", raising=False)
    result = CliRunner().invoke(
        app, ["daemon", "run", "--once", "--sources", "", "--no-ask", "--no-playbooks"]
    )
    assert result.exit_code == 1  # nothing enabled

    runner = CliRunner()
    result = runner.invoke(app, ["schedule", "--file", str(path)])
    assert result.exit_code == 0, result.output
    assert "host.fast" in result.output
    assert "15m" in result.output
    assert "never" in result.output
