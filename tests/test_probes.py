"""Unit tests for the Probe SDK, ProbeRunner, and host.identity probe."""

from __future__ import annotations

from typing import ClassVar

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from homelab_helper.db.base import Base
from homelab_helper.db.enums import IntentTargetType, PrivilegeLevel
from homelab_helper.db.models import Host, Observation
from homelab_helper.db.models import Probe as ProbeRow
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.runner import ProbeRunner
from homelab_helper.probes.base import (
    AdapterRegistry,
    ObservationData,
    Probe,
    ProbeContext,
    ProbeResult,
    ProbeTarget,
)
from homelab_helper.probes.host.identity import (
    HostIdentityOutput,
    HostIdentityProbe,
    parse_os_release,
    parse_uptime,
)
from homelab_helper.probes.registry import discover_probes

# ---------------------------------------------------------------------------
# parse_os_release / parse_uptime
# ---------------------------------------------------------------------------


def test_parse_os_release_quoted_values() -> None:
    raw = """NAME="Ubuntu"
ID=ubuntu
PRETTY_NAME="Ubuntu 22.04.5 LTS"
VERSION_ID="22.04"
HOME_URL="https://www.ubuntu.com/"
"""
    parsed = parse_os_release(raw)
    assert parsed["NAME"] == "Ubuntu"
    assert parsed["ID"] == "ubuntu"
    assert parsed["PRETTY_NAME"] == "Ubuntu 22.04.5 LTS"
    assert parsed["VERSION_ID"] == "22.04"


def test_parse_os_release_tolerates_comments_and_blanks() -> None:
    raw = """# This is a comment
ID=debian

PRETTY_NAME=Debian
no_equals_line
=missing_key
"""
    parsed = parse_os_release(raw)
    assert parsed == {"ID": "debian", "PRETTY_NAME": "Debian"}


def test_parse_uptime_returns_float() -> None:
    assert parse_uptime("12345.67 8901.23") == 12345.67
    assert parse_uptime("") is None
    assert parse_uptime("garbage") is None


# ---------------------------------------------------------------------------
# AdapterRegistry
# ---------------------------------------------------------------------------


def test_adapter_registry_basic() -> None:
    reg = AdapterRegistry()
    assert not reg.has("foo")
    reg.register("foo", object())
    assert reg.has("foo")
    assert reg.get("foo") is not None
    with pytest.raises(KeyError):
        reg.get("missing")


# ---------------------------------------------------------------------------
# Probe SDK discovery
# ---------------------------------------------------------------------------


def test_discover_probes_finds_host_identity() -> None:
    probes = discover_probes()
    assert "host.identity" in probes
    assert probes["host.identity"] is HostIdentityProbe


# ---------------------------------------------------------------------------
# HostIdentityProbe — failure modes (no SSH attempted)
# ---------------------------------------------------------------------------


async def test_host_identity_rejects_bad_target() -> None:
    probe = HostIdentityProbe()
    bad = ProbeTarget(kind="network", network_cidr="10.0.0.0/24")
    ctx = ProbeContext(target=bad, adapters=AdapterRegistry(), run_id="run-1")
    result = await probe.run(ctx)
    assert result.success is False
    assert "unsupported target kind" in (result.error or "")


async def test_host_identity_requires_ssh_user() -> None:
    probe = HostIdentityProbe()
    target = ProbeTarget(kind="host", hostname="node3")
    ctx = ProbeContext(target=target, adapters=AdapterRegistry(), run_id="run-1")
    result = await probe.run(ctx)
    assert result.success is False
    assert "ssh_user" in (result.error or "")


async def test_host_identity_requires_kernel_ssh_adapter() -> None:
    probe = HostIdentityProbe()
    target = ProbeTarget(kind="host", hostname="node3", ssh_user="root")
    ctx = ProbeContext(target=target, adapters=AdapterRegistry(), run_id="run-1")
    result = await probe.run(ctx)
    assert result.success is False
    assert "kernel-ssh" in (result.error or "")


# ---------------------------------------------------------------------------
# ProbeRunner — happy path with a fake probe
# ---------------------------------------------------------------------------


class FakeProbeOutput(BaseModel):
    answer: int


class FakeProbe(Probe):
    name: ClassVar[str] = "test.fake"
    version: ClassVar[str] = "0.0.1"
    required_privilege: ClassVar[PrivilegeLevel] = PrivilegeLevel.NONE
    target_kinds: ClassVar[list[str]] = ["host"]
    produces_keys: ClassVar[list[str]] = ["test.answer"]
    output_schema: ClassVar[type[BaseModel] | None] = FakeProbeOutput

    async def run(self, ctx: ProbeContext) -> ProbeResult:
        return ProbeResult(
            observations=[
                ObservationData(
                    key="test.answer",
                    value=42,
                    target_type=IntentTargetType.HOST,
                    target_id="node3",
                )
            ],
            success=True,
            raw_payload={"answer": 42},
        )


class CrashingProbe(Probe):
    name: ClassVar[str] = "test.crash"
    version: ClassVar[str] = "0.0.1"
    required_privilege: ClassVar[PrivilegeLevel] = PrivilegeLevel.NONE
    target_kinds: ClassVar[list[str]] = ["host"]

    async def run(self, ctx: ProbeContext) -> ProbeResult:
        raise RuntimeError("boom")


class BadSchemaProbe(Probe):
    """Returns a raw_payload that violates its declared schema."""

    name: ClassVar[str] = "test.bad-schema"
    version: ClassVar[str] = "0.0.1"
    required_privilege: ClassVar[PrivilegeLevel] = PrivilegeLevel.NONE
    target_kinds: ClassVar[list[str]] = ["host"]
    output_schema: ClassVar[type[BaseModel] | None] = FakeProbeOutput

    async def run(self, ctx: ProbeContext) -> ProbeResult:
        return ProbeResult(
            observations=[
                ObservationData(
                    key="test.bad", value=1, target_id="x", target_type=IntentTargetType.HOST
                )
            ],
            success=True,
            raw_payload={"wrong_key": "not an int"},
        )


@pytest.fixture
async def engine():
    e = make_engine("sqlite+aiosqlite:///:memory:")
    async with e.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield e
    await e.dispose()


@pytest.fixture
def sessionmaker(engine):
    return make_sessionmaker(engine)


async def test_runner_success_path(sessionmaker) -> None:
    runner = ProbeRunner(AdapterRegistry())
    target = ProbeTarget(kind="host", hostname="node3")
    async with session_scope(sessionmaker) as session:
        host = Host(hostname="node3")
        session.add(host)
        await session.flush()
        run_row, result = await runner.run(FakeProbe(), target, session, host_id=host.id)
        assert result.success
        assert run_row.success is True
        assert run_row.observation_count == 1
        # Probe row was auto-created
        probe_row = (
            await session.execute(select(ProbeRow).where(ProbeRow.name == "test.fake"))
        ).scalar_one()
        assert probe_row.version == "0.0.1"

    async with sessionmaker() as session:
        obs = (await session.execute(select(Observation))).scalars().all()
        assert len(obs) == 1
        assert obs[0].key == "test.answer"
        assert obs[0].value == 42


async def test_runner_records_crash_as_failure(sessionmaker) -> None:
    runner = ProbeRunner(AdapterRegistry())
    target = ProbeTarget(kind="host", hostname="node3")
    async with session_scope(sessionmaker) as session:
        host = Host(hostname="node3")
        session.add(host)
        await session.flush()
        run_row, result = await runner.run(CrashingProbe(), target, session, host_id=host.id)
        assert not result.success
        assert "boom" in (result.error or "")
        assert run_row.success is False
        assert run_row.observation_count == 0


async def test_runner_validates_output_schema(sessionmaker) -> None:
    """Bad raw_payload -> result.success flips to False, no observations persisted."""
    runner = ProbeRunner(AdapterRegistry())
    target = ProbeTarget(kind="host", hostname="node3")
    async with session_scope(sessionmaker) as session:
        host = Host(hostname="node3")
        session.add(host)
        await session.flush()
        run_row, result = await runner.run(BadSchemaProbe(), target, session, host_id=host.id)
        assert not result.success
        assert "output_schema" in (result.error or "")
        assert run_row.observation_count == 0


# ---------------------------------------------------------------------------
# HostIdentityOutput model itself
# ---------------------------------------------------------------------------


def test_host_identity_output_round_trip() -> None:
    out = HostIdentityOutput(
        hostname="node3",
        kernel="6.1.0-23-amd64",
        machine_id="abc123",
        os_id="debian",
        os_pretty_name="Debian GNU/Linux 12 (bookworm)",
        boot_time_unix=1700000000,
    )
    dumped = out.model_dump()
    assert dumped["hostname"] == "node3"
    assert HostIdentityOutput.model_validate(dumped).hostname == "node3"
