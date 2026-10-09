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

import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select

from homelab_helper.db.models import AssertionRun, ConfigurationAssertion, DiscoveryRun, Host
from homelab_helper.engine.host_probe import HostProbeRequest, probe_host
from homelab_helper.engine.talos_probe import TalosProbeRequest, probe_talos
from homelab_helper.probes.registry import discover_probes

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from homelab_helper.engine.assertions import AssertionEngine

SCHEDULE_ENV_VAR = "HOMELAB_HELPER_SCHEDULE"
DEFAULT_INTERVAL = "6h"
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
    for name in schedule.assertions.values():
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


@dataclass
class ScheduleRun:
    probes_run: list[str] = field(default_factory=list)
    """``"<target>: <probe>, <probe>"`` per target touched."""
    probe_failures: list[str] = field(default_factory=list)
    assertions_run: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    not_due: int = 0


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
) -> ScheduleRun:
    """Run whatever is due: one probe batch per target, then each due assertion.

    A target with nothing due costs no connection. Probe failures are reported
    per probe (the batch still reconciles what succeeded, as ``discover host``
    does); an unreachable target is an error for that target only.
    """
    current = now or datetime.now(UTC)
    result = ScheduleRun()
    if probes:
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
        for (kind, target), names in by_target.items():
            spec = specs[kind, target]
            classes = [available[n] for n in names if n in available]
            try:
                if kind == "host":
                    assert isinstance(spec, HostSpec)
                    outcome = await probe_host(
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
                else:
                    assert isinstance(spec, TalosSpec)
                    outcome = await probe_talos(
                        session,
                        TalosProbeRequest(
                            name=spec.name,
                            node=spec.node,
                            talosconfig=spec.talosconfig,
                            probe_names=tuple(names),
                        ),
                        probe_classes=classes,
                        adapter=talos_adapter,
                    )
            except Exception as exc:  # one dead target must not stop the others
                result.errors.append(f"{target}: {exc}")
                continue
            session_error = getattr(outcome, "session_error", None)
            if session_error:
                result.errors.append(f"{target}: {session_error}")
                continue
            result.probes_run.append(f"{target}: {', '.join(names)}")
            for p in getattr(outcome, "probes", []) or []:
                if not getattr(p, "success", True):
                    result.probe_failures.append(f"{target}/{p.probe}: {p.error}")
    if assertions:
        from homelab_helper.engine.assertions import AssertionEngine as _Engine  # noqa: PLC0415

        engine = assertion_engine or _Engine()
        for item in assertions_due(await plan_assertions(session, schedule, now=current), current):
            try:
                run = await engine.run_assertion(session, item.assertion)
            except Exception as exc:
                result.errors.append(f"assertion {item.assertion.name}: {exc}")
                continue
            status = getattr(getattr(run, "status", None), "value", "ran")
            result.assertions_run.append(f"{item.assertion.name}: {status}")
    return result


__all__ = [
    "DEFAULT_INTERVAL",
    "SCHEDULE_ENV_VAR",
    "DueAssertion",
    "DueProbe",
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
    "run_due",
]
