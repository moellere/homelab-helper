"""``Host.last_verified`` moves only when a probe run actually reached the host.

The reconciler replays stored observations on every run, so a host that had
been off for a month was stamped verified each time its login was refused.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import select

from homelab_helper.db.enums import IntentTargetType, PrivilegeLevel
from homelab_helper.db.models import Host
from homelab_helper.db.session import session_scope
from homelab_helper.engine.host_probe import HostProbeRequest, probe_host
from homelab_helper.engine.talos_probe import TalosProbeRequest, probe_talos
from homelab_helper.probes.base import ObservationData, Probe, ProbeContext, ProbeResult
from tests.test_host_probe_credentials import _FakeSSH
from tests.test_node_update import sessionmaker_  # noqa: F401 - fixture

_SEEN = datetime(2026, 1, 2, tzinfo=UTC)


def _verified(host: Host) -> datetime:
    """SQLite hands the column back naive, Postgres aware; compare as UTC."""
    assert host.last_verified is not None
    return host.last_verified.replace(tzinfo=UTC)


class _KernelProbe(Probe):
    name: ClassVar[str] = "test.kernel"
    version: ClassVar[str] = "0.0.1"
    required_privilege: ClassVar[PrivilegeLevel] = PrivilegeLevel.NONE
    target_kinds: ClassVar[list[str]] = ["host"]
    produces_keys: ClassVar[list[str]] = ["host.identity.kernel"]

    async def run(self, ctx: ProbeContext) -> ProbeResult:
        return ProbeResult(
            observations=[
                ObservationData(
                    key="host.identity.kernel",
                    value="6.8.0",
                    target_type=IntentTargetType.HOST,
                    target_id=ctx.target.host_id,
                )
            ]
        )


class _FailingProbe(Probe):
    name: ClassVar[str] = "test.failing"
    version: ClassVar[str] = "0.0.1"
    required_privilege: ClassVar[PrivilegeLevel] = PrivilegeLevel.NONE
    target_kinds: ClassVar[list[str]] = ["host"]

    async def run(self, ctx: ProbeContext) -> ProbeResult:
        return ProbeResult(success=False, error="command timed out")


async def _run(session: Any, probes: list[type[Probe]], *, refused: bool = False) -> Host:
    await probe_host(
        session,
        HostProbeRequest(name="pve9", ssh_user="root"),
        probe_classes=probes,
        ssh_adapter=_FakeSSH(fail=refused),  # type: ignore[arg-type]
    )
    return (await session.execute(select(Host).where(Host.hostname == "pve9"))).scalar_one()


async def _seen_once(session: Any) -> Host:
    host = await _run(session, [_KernelProbe])
    assert host.last_verified is not None
    host.last_verified = _SEEN
    await session.flush()
    return host


async def test_a_probe_that_answered_stamps_the_host(sessionmaker_) -> None:  # noqa: F811
    async with session_scope(sessionmaker_) as s:
        await _seen_once(s)
        host = await _run(s, [_FailingProbe, _KernelProbe])
        assert _verified(host) > _SEEN


async def test_a_refused_login_leaves_the_last_sighting_alone(sessionmaker_) -> None:  # noqa: F811
    async with session_scope(sessionmaker_) as s:
        await _seen_once(s)
        host = await _run(s, [_KernelProbe], refused=True)
        assert _verified(host) == _SEEN
        assert host.capabilities["kernel"] == "6.8.0"


async def test_a_login_where_every_probe_failed_is_no_sighting(sessionmaker_) -> None:  # noqa: F811
    async with session_scope(sessionmaker_) as s:
        await _seen_once(s)
        host = await _run(s, [_FailingProbe])
        assert _verified(host) == _SEEN


class _TalosAPI:
    def __init__(self, *, up: bool) -> None:
        self._up = up

    async def health_check(self, _node: str) -> tuple[bool, str | None]:
        return (True, None) if self._up else (False, "connection refused")


async def _run_talos(session: Any, *, up: bool) -> Host:
    await probe_talos(
        session,
        TalosProbeRequest(name="cp9", node="192.0.2.9"),
        probe_classes=[_KernelProbe],
        adapter=_TalosAPI(up=up),  # type: ignore[arg-type]
    )
    return (await session.execute(select(Host).where(Host.hostname == "cp9"))).scalar_one()


async def test_an_unreachable_talos_node_keeps_its_last_sighting(sessionmaker_) -> None:  # noqa: F811
    async with session_scope(sessionmaker_) as s:
        host = await _run_talos(s, up=True)
        assert host.last_verified is not None
        host.last_verified = _SEEN
        await s.flush()
        host = await _run_talos(s, up=False)
        assert _verified(host) == _SEEN
