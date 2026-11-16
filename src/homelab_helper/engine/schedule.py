"""Per-probe and per-assertion cadences (Phase 9.6) — the Phase-2 tail.

One discovery interval for everything was the Phase 7 shortcut; "continuous"
means each probe on its own cadence. The operator declares the cadences in a
YAML file (``HOMELAB_HELPER_SCHEDULE``; see ``fixtures/schedule.example.yaml``):

.. code-block:: yaml

    defaults: {probes: 6h, assertions: 6h}
    hosts:
      - name: nas0
        ssh_user: root
        primary_ip: 10.0.6.29
        probes: {host.raid: 15m, host.smart: 1d}     # others at the default
    talos:
      - {name: cp1, node: 10.0.6.13}
    assertions: {default: 6h}

There is **no new state**. What ran and when is already recorded: every probe
run is a ``DiscoveryRun`` row keyed by host and probe name, every assertion run
an ``AssertionRun`` row, so "due" is just "no run newer than the interval". That
is what lets ``helper daemon run --once`` from a cron tick honour cadences from
15 minutes to a week without a scheduler process of its own.

An assertion's own ``schedule`` column (``"6h"``, ``"1d"``) wins over the
default. Only interval syntax is accepted — ``90s``, ``15m``, ``6h``, ``1d``,
``1w`` — because it is what the due computation can answer from a timestamp
alone; a cron expression is reported as an error rather than guessed at.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select

from homelab_helper.db.enums import FindingKind, FindingSeverity
from homelab_helper.db.models import (
    AssertionRun,
    ConfigurationAssertion,
    DiscoveryRun,
    Host,
    SourceRun,
)
from homelab_helper.db.session import session_scope
from homelab_helper.engine.category_findings import (
    CategoryIssue,
    CategoryResult,
    reconcile_category_findings,
)
from homelab_helper.engine.host_probe import HostProbeRequest, probe_host
from homelab_helper.engine.talos_probe import TalosProbeRequest, probe_talos
from homelab_helper.probes.registry import discover_probes
from homelab_helper.secrets import redact

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from homelab_helper.engine.assertions import AssertionEngine

SCHEDULE_ENV_VAR = "HOMELAB_HELPER_SCHEDULE"
DEFAULT_INTERVAL = "6h"
SCHEDULE_PROBES_LOCK_KEY = 0x484C5350
SCHEDULE_ASSERTIONS_LOCK_KEY = 0x484C5341
"""Advisory-lock keys: one pass at a time per half, so two daemons sharing a
database never run the same probe batch or the same assertion twice at once."""
PROBE_FAILING_AFTER = 3
"""Consecutive failed runs of one probe on one host before it becomes a finding."""
PROBE_FAILING_WINDOW = timedelta(days=7)
"""How far back failure history is read, at least; slower probes look further."""
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_INTERVAL = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$")


class ScheduleError(ValueError):
    """The schedule file is missing, unreadable, or names something that does not exist."""


def parse_interval(text: str | None, *, default: str = DEFAULT_INTERVAL) -> timedelta:
    """``"15m"`` → 15 minutes; ``None`` → the default. Cron syntax is refused."""
    raw = text if text is not None else default
    m = _INTERVAL.match(str(raw))
    if not m:
        raise ScheduleError(
            f"{raw!r} is not an interval (use e.g. 90s, 15m, 6h, 1d, 1w; cron syntax is not supported)"
        )
    return timedelta(seconds=int(m.group(1)) * _UNITS[m.group(2)])


class HostSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    ssh_user: str = Field(min_length=1)
    primary_ip: str | None = None
    ssh_key_path: str | None = None
    ssh_port: int = 22
    probes: dict[str, str] = Field(default_factory=dict)
    """Probe name → interval; ``default`` sets the host's own default."""


class TalosSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    node: str | None = None
    talosconfig: str | None = None
    probes: dict[str, str] = Field(default_factory=dict)


class Defaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    probes: str = DEFAULT_INTERVAL
    assertions: str = DEFAULT_INTERVAL


class Schedule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    defaults: Defaults = Field(default_factory=Defaults)
    hosts: list[HostSpec] = Field(default_factory=list)
    talos: list[TalosSpec] = Field(default_factory=list)
    assertions: dict[str, str] = Field(default_factory=dict)
    """Assertion name → interval; ``default`` overrides ``defaults.assertions``."""
    sources: dict[str, str] = Field(default_factory=dict)
    """Discovery source → interval; ``default`` covers the rest. A source with
    neither runs on every discovery pass, as it does with no block at all."""

    def source_interval(self, source: str) -> timedelta | None:
        raw = self.sources.get(source) or self.sources.get("default")
        return parse_interval(raw) if raw else None

    def probe_interval(self, spec: HostSpec | TalosSpec, probe: str) -> timedelta:
        return parse_interval(
            spec.probes.get(probe) or spec.probes.get("default") or self.defaults.probes
        )

    def assertion_interval(self, assertion: ConfigurationAssertion) -> timedelta:
        own = assertion.schedule
        named = self.assertions.get(assertion.name)
        return parse_interval(
            own or named or self.assertions.get("default") or self.defaults.assertions
        )


def load_schedule(path: str | Path | None = None) -> Schedule:
    """``path`` overrides (tests); otherwise ``HOMELAB_HELPER_SCHEDULE``."""
    target = Path(path) if path else None
    if target is None:
        env = os.environ.get(SCHEDULE_ENV_VAR)
        if not env:
            raise ScheduleError(
                f"no schedule declared — set {SCHEDULE_ENV_VAR} to a schedule file "
                "(see fixtures/schedule.example.yaml)"
            )
        target = Path(env).expanduser()
    try:
        raw = yaml.safe_load(target.read_text()) or {}
    except FileNotFoundError as exc:
        raise ScheduleError(f"schedule file not found: {target}") from exc
    except yaml.YAMLError as exc:
        raise ScheduleError(f"schedule file is not valid YAML: {exc}") from exc
    try:
        schedule = Schedule.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"]) or "schedule"
        raise ScheduleError(f"{target}: {loc}: {first['msg']}") from exc
    known = set(discover_probes())
    specs: list[HostSpec | TalosSpec] = [*schedule.hosts, *schedule.talos]
    for spec in specs:
        for name in spec.probes:
            if name != "default" and name not in known:
                raise ScheduleError(f"{target}: {spec.name}: unknown probe {name!r}")
        for name in spec.probes.values():
            parse_interval(name)
    for name in [*schedule.assertions.values(), *schedule.sources.values()]:
        parse_interval(name)
    parse_interval(schedule.defaults.probes)
    parse_interval(schedule.defaults.assertions)
    return schedule


@dataclass(frozen=True)
class DueProbe:
    target: str
    kind: str  # "host" | "talos"
    probe: str
    interval: timedelta
    last_run: datetime | None

    def due_in(self, now: datetime) -> timedelta:
        if self.last_run is None:
            return timedelta(0)
        return max(timedelta(0), self.last_run + self.interval - now)

    @property
    def due(self) -> bool:
        return self.last_run is None or self.due_in(datetime.now(UTC)) <= timedelta(0)


@dataclass(frozen=True)
class DueAssertion:
    assertion: ConfigurationAssertion
    interval: timedelta
    last_run: datetime | None


def _probe_names_for(kind: str) -> list[str]:
    return sorted(name for name, cls in discover_probes().items() if kind in cls.target_kinds)


def _aware(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


async def _last_runs(
    session: AsyncSession, hostnames: list[str]
) -> dict[tuple[str, str], datetime]:
    """``(hostname, probe) → newest DiscoveryRun.started_at`` for the named hosts."""
    if not hostnames:
        return {}
    rows = (
        await session.execute(
            select(Host.hostname, DiscoveryRun.probe_name, func.max(DiscoveryRun.started_at))
            .join(Host, Host.id == DiscoveryRun.host_id)
            .where(Host.hostname.in_(hostnames))
            .group_by(Host.hostname, DiscoveryRun.probe_name)
        )
    ).all()
    return {(str(h), str(p)): _aware(ts) for h, p, ts in rows if ts is not None}  # type: ignore[misc]


async def plan_probes(
    session: AsyncSession, schedule: Schedule, *, now: datetime | None = None
) -> list[DueProbe]:
    """Every (target, probe) the schedule covers, with its interval and last run."""
    current = now or datetime.now(UTC)
    specs: list[tuple[str, HostSpec | TalosSpec]] = [("host", h) for h in schedule.hosts] + [
        ("talos", t) for t in schedule.talos
    ]
    last = await _last_runs(session, [s.name for _, s in specs])
    out: list[DueProbe] = []
    for kind, spec in specs:
        names = _probe_names_for(kind)
        for probe in names:
            out.append(
                DueProbe(
                    target=spec.name,
                    kind=kind,
                    probe=probe,
                    interval=schedule.probe_interval(spec, probe),
                    last_run=last.get((spec.name, probe)),
                )
            )
    return sorted(out, key=lambda d: (d.due_in(current), d.target, d.probe))


def due_now(plan: list[DueProbe], now: datetime) -> list[DueProbe]:
    return [d for d in plan if d.due_in(now) <= timedelta(0)]


async def plan_assertions(
    session: AsyncSession, schedule: Schedule, *, now: datetime | None = None
) -> list[DueAssertion]:
    rows = (
        (
            await session.execute(
                select(ConfigurationAssertion).where(ConfigurationAssertion.enabled.is_(True))
            )
        )
        .scalars()
        .all()
    )
    last = {
        aid: _aware(ts)
        for aid, ts in (
            await session.execute(
                select(AssertionRun.assertion_id, func.max(AssertionRun.ran_at)).group_by(
                    AssertionRun.assertion_id
                )
            )
        ).all()
    }
    return [
        DueAssertion(assertion=a, interval=schedule.assertion_interval(a), last_run=last.get(a.id))
        for a in rows
    ]


def assertions_due(plan: list[DueAssertion], now: datetime) -> list[DueAssertion]:
    return [d for d in plan if d.last_run is None or d.last_run + d.interval <= now]


@dataclass(frozen=True)
class DueSource:
    source: str
    interval: timedelta | None
    """None: no cadence in the file, so the source runs on every discovery pass."""
    last_run: datetime | None

    def due_in(self, now: datetime) -> timedelta:
        if self.interval is None or self.last_run is None:
            return timedelta(0)
        return self.last_run + self.interval - now


async def plan_sources(
    session: AsyncSession, schedule: Schedule, sources: list[str]
) -> list[DueSource]:
    """Each source with its cadence and when it last ran (a failed run counts, as for probes)."""
    last = {
        str(name): _aware(ts)
        for name, ts in (
            await session.execute(
                select(SourceRun.source, func.max(SourceRun.started_at))
                .where(SourceRun.source.in_(sources))
                .group_by(SourceRun.source)
            )
        ).all()
        if ts is not None
    }
    return [DueSource(src, schedule.source_interval(src), last.get(src)) for src in sources]


@dataclass
class ScheduleRun:
    probes_run: list[str] = field(default_factory=list)
    """``"<target>: <probe>, <probe>"`` per target touched."""
    probe_failures: list[str] = field(default_factory=list)
    assertions_run: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    not_due: int = 0
    probe_findings: dict[str, int] = field(default_factory=dict)
    """``PROBE_FAILING`` activity this pass, zero counts dropped."""


async def reconcile_probe_failures(session: AsyncSession, schedule: Schedule) -> CategoryResult:
    """Turn a scheduled probe's repeated failure into a finding; its next success resolves it.

    A failed run still counts as a run for cadence, so without this a probe that always
    fails looks merely not due. A pair with too few runs to judge, or a mixed recent
    history, is left out of ``observed`` and any finding on it stays as it is.
    """
    plan = await plan_probes(session, schedule)
    pairs = {(d.target, d.probe) for d in plan}
    # Only as far back as the slowest probe needs to show its last few runs.
    slowest = max((d.interval for d in plan), default=timedelta(0))
    since = datetime.now(UTC) - max(PROBE_FAILING_WINDOW, slowest * PROBE_FAILING_AFTER * 2)
    ranked = (
        select(
            Host.hostname.label("hostname"),
            DiscoveryRun.probe_name.label("probe"),
            DiscoveryRun.success.label("success"),
            DiscoveryRun.error.label("error"),
            DiscoveryRun.started_at.label("started_at"),
            func.row_number()
            .over(
                partition_by=(Host.hostname, DiscoveryRun.probe_name),
                order_by=DiscoveryRun.started_at.desc(),
            )
            .label("rank"),
        )
        .join(Host, Host.id == DiscoveryRun.host_id)
        .where(
            Host.hostname.in_({target for target, _ in pairs}),
            DiscoveryRun.started_at >= since,
        )
        .subquery()
    )
    rows = (
        await session.execute(
            select(ranked)
            .where(ranked.c.rank <= PROBE_FAILING_AFTER)
            .order_by(ranked.c.hostname, ranked.c.probe, ranked.c.rank)
        )
    ).all()
    recent: dict[tuple[str, str], list[Any]] = {}
    for row in rows:
        if (row.hostname, row.probe) in pairs:
            recent.setdefault((row.hostname, row.probe), []).append(row)

    issues: list[CategoryIssue] = []
    observed: set[str] = set()
    targets: set[tuple[str, str]] = set()
    for (hostname, probe), runs in recent.items():
        failing = len(runs) == PROBE_FAILING_AFTER and not any(r.success for r in runs)
        if not (failing or runs[0].success):
            continue
        category = f"probe-failing:{probe}"
        observed.add(category)
        targets.add(("host", hostname))
        if failing:
            error = redact(str(runs[0].error or "no error recorded"))[:500]
            issues.append(
                CategoryIssue(
                    kind=FindingKind.PROBE_FAILING,
                    category=category,
                    target_type="host",
                    target_id=hostname,
                    severity=FindingSeverity.MEDIUM,
                    title=f"{probe} is failing on {hostname}",
                    description=(
                        f"The last {PROBE_FAILING_AFTER} runs of {probe} on {hostname} failed, "
                        f"so its facts are no longer refreshing. Latest error: {error}"
                    ),
                    evidence={"probe": probe, "last_error": error},
                )
            )
    return await reconcile_category_findings(
        session, FindingKind.PROBE_FAILING, issues, observed, observed_targets=targets
    )


async def _probe_target(
    session: AsyncSession,
    spec: HostSpec | TalosSpec,
    names: list[str],
    classes: list[type[Any]],
    *,
    ssh_adapter: Any,
    talos_adapter: Any,
) -> Any:
    if isinstance(spec, HostSpec):
        return await probe_host(
            session,
            HostProbeRequest(
                name=spec.name,
                ssh_user=spec.ssh_user,
                ssh_key_path=spec.ssh_key_path,
                primary_ip=spec.primary_ip,
                ssh_port=spec.ssh_port,
                probe_names=tuple(names),
            ),
            probe_classes=classes,
            ssh_adapter=ssh_adapter,
        )
    return await probe_talos(
        session,
        TalosProbeRequest(
            name=spec.name, node=spec.node, talosconfig=spec.talosconfig, probe_names=tuple(names)
        ),
        probe_classes=classes,
        adapter=talos_adapter,
    )


async def _due_probes(
    session: AsyncSession,
    schedule: Schedule,
    current: datetime,
    result: ScheduleRun,
    *,
    ssh_adapter: Any,
    talos_adapter: Any,
    sessionmaker: async_sessionmaker[AsyncSession] | None,
    concurrency: int,
) -> None:
    plan = await plan_probes(session, schedule, now=current)
    due = due_now(plan, current)
    result.not_due += len(plan) - len(due)
    by_target: dict[tuple[str, str], list[str]] = {}
    for d in due:
        by_target.setdefault((d.kind, d.target), []).append(d.probe)
    specs = {("host", h.name): h for h in schedule.hosts} | {
        ("talos", t.name): t for t in schedule.talos
    }
    available = discover_probes()
    await session.commit()  # end the planning read before any target writes
    gate = asyncio.Semaphore(max(1, concurrency) if sessionmaker is not None else 1)

    async def one(kind: str, target: str, names: list[str]) -> Any:
        spec = specs[kind, target]
        classes = [available[n] for n in names if n in available]
        kw = {"ssh_adapter": ssh_adapter, "talos_adapter": talos_adapter}
        async with gate:
            if sessionmaker is not None:
                try:
                    async with session_scope(sessionmaker) as own:
                        return await _probe_target(own, spec, names, classes, **kw)
                except Exception as exc:  # one dead target must not stop the others
                    return exc
            try:
                outcome = await _probe_target(session, spec, names, classes, **kw)
                await session.commit()
                return outcome
            except Exception as exc:
                await session.rollback()
                return exc

    targets = list(by_target.items())
    outcomes = await asyncio.gather(*(one(k, t, n) for (k, t), n in targets))
    for ((_kind, target), names), outcome in zip(targets, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            result.errors.append(redact(f"{target}: {outcome}"))
            continue
        session_error = getattr(outcome, "session_error", None)
        if session_error:
            result.errors.append(redact(f"{target}: {session_error}"))
            continue
        result.probes_run.append(f"{target}: {', '.join(names)}")
        for p in getattr(outcome, "probes", []) or []:
            if not getattr(p, "success", True):
                result.probe_failures.append(redact(f"{target}/{p.probe}: {p.error}"))
    counts = (await reconcile_probe_failures(session, schedule)).counts()
    result.probe_findings = {k: v for k, v in counts.items() if v}
    await session.commit()


async def _due_assertions(
    session: AsyncSession,
    schedule: Schedule,
    current: datetime,
    result: ScheduleRun,
    assertion_engine: AssertionEngine | None,
) -> None:
    from homelab_helper.engine.assertions import AssertionEngine as _Engine  # noqa: PLC0415

    engine = assertion_engine or _Engine()
    for item in assertions_due(await plan_assertions(session, schedule, now=current), current):
        name = item.assertion.name
        try:
            run = await engine.run_assertion(session, item.assertion)
            await session.commit()
        except Exception as exc:
            await session.rollback()
            result.errors.append(redact(f"assertion {name}: {exc}"))
            continue
        status = getattr(getattr(run, "status", None), "value", "ran")
        result.assertions_run.append(f"{name}: {status}")


async def run_due(
    session: AsyncSession,
    schedule: Schedule,
    *,
    now: datetime | None = None,
    probes: bool = True,
    assertions: bool = True,
    ssh_adapter: Any = None,
    talos_adapter: Any = None,
    assertion_engine: AssertionEngine | None = None,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    concurrency: int = 1,
) -> ScheduleRun:
    """Run whatever is due: one probe batch per target, then each due assertion.

    Each half runs only where the file asks for it: probes for the targets it
    lists, assertions only when it has an ``assertions:`` block. A daemon whose
    file lists hosts alone leaves assertions to the daemon whose file names them.

    A target with nothing due costs no connection. Probe failures are reported
    per probe (the batch still reconciles what succeeded, as ``discover host``
    does); an unreachable target is an error for that target only.

    Each target and each assertion is its own transaction: what finished is
    kept if the pass is interrupted, a target that raises rolls back alone,
    and no transaction stays open across more than one target's network I/O.
    With a ``sessionmaker`` each target gets its own session, and up to
    ``concurrency`` targets are probed at once; without one, ``session`` is
    reused and targets run one after another.
    """
    current = now or datetime.now(UTC)
    result = ScheduleRun()
    if probes:
        await _due_probes(
            session,
            schedule,
            current,
            result,
            ssh_adapter=ssh_adapter,
            talos_adapter=talos_adapter,
            sessionmaker=sessionmaker,
            concurrency=concurrency,
        )
    if assertions and schedule.assertions:
        await _due_assertions(session, schedule, current, result, assertion_engine)
    return result


__all__ = [
    "DEFAULT_INTERVAL",
    "PROBE_FAILING_AFTER",
    "SCHEDULE_ASSERTIONS_LOCK_KEY",
    "SCHEDULE_ENV_VAR",
    "SCHEDULE_PROBES_LOCK_KEY",
    "DueAssertion",
    "DueProbe",
    "DueSource",
    "HostSpec",
    "Schedule",
    "ScheduleError",
    "ScheduleRun",
    "TalosSpec",
    "assertions_due",
    "due_now",
    "load_schedule",
    "parse_interval",
    "plan_assertions",
    "plan_probes",
    "plan_sources",
    "reconcile_probe_failures",
    "run_due",
]
