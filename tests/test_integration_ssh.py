"""Opt-in SSH integration test against a real host.

To run, set:

    HOMELAB_TEST_SSH_HOST=bmax3
    HOMELAB_TEST_SSH_USER=root
    HOMELAB_TEST_SSH_KEY=~/.ssh/id_ed25519     # optional
    HOMELAB_TEST_SSH_PASSWORD=...              # optional

then:

    uv run pytest tests/test_integration_ssh.py -v

If ``HOMELAB_TEST_SSH_HOST`` is unset the tests are skipped.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import select

from homelab_helper.adapters.kernel_ssh import KernelSSHAdapter
from homelab_helper.db.base import Base
from homelab_helper.db.models import Host, Observation
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.runner import ProbeRunner
from homelab_helper.probes.base import AdapterRegistry, ProbeTarget
from homelab_helper.probes.host.identity import HostIdentityProbe

pytestmark = pytest.mark.skipif(
    not os.environ.get("HOMELAB_TEST_SSH_HOST"),
    reason="set HOMELAB_TEST_SSH_HOST to enable SSH integration tests",
)


def _target() -> ProbeTarget:
    key = os.environ.get("HOMELAB_TEST_SSH_KEY")
    return ProbeTarget(
        kind="host",
        hostname=os.environ["HOMELAB_TEST_SSH_HOST"],
        ssh_user=os.environ.get("HOMELAB_TEST_SSH_USER", "root"),
        ssh_key_path=str(Path(key).expanduser()) if key else None,
        ssh_password=os.environ.get("HOMELAB_TEST_SSH_PASSWORD"),
        ssh_port=int(os.environ.get("HOMELAB_TEST_SSH_PORT", "22")),
    )


async def test_kernel_ssh_health_check_reaches_host() -> None:
    """Adapter can reach the target and run a trivial command."""
    t = _target()
    adapter = KernelSSHAdapter()
    ok, err = await adapter.health_check(
        host=t.hostname or "",
        user=t.ssh_user or "",
        key_path=t.ssh_key_path,
        password=t.ssh_password,
        port=t.ssh_port,
    )
    assert ok, f"health check failed: {err}"


async def test_host_identity_probe_against_real_host() -> None:
    """The full pipe: ProbeRunner runs host.identity, observations land in DB."""
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = make_sessionmaker(engine)
        adapters = AdapterRegistry({"kernel-ssh": KernelSSHAdapter()})
        runner = ProbeRunner(adapters)
        async with session_scope(sm) as session:
            host = Host(hostname=_target().hostname)
            session.add(host)
            await session.flush()
            target = _target()
            target.host_id = str(host.id)
            run_row, result = await runner.run(
                HostIdentityProbe(), target, session, host_id=host.id
            )
        assert result.success, f"probe failed: {result.error}"
        assert run_row.observation_count >= 1
        async with sm() as session:
            obs = (
                (
                    await session.execute(
                        select(Observation).order_by(Observation.recorded_at.desc())
                    )
                )
                .scalars()
                .all()
            )
        keys = {o.key for o in obs}
        # hostname is the one mandatory fact
        assert "host.identity.hostname" in keys
    finally:
        await engine.dispose()
