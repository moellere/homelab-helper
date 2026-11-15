"""``--ssh-password-env`` reads the password through the secret resolver.

Read straight from the environment, a ``file:`` or ``env:`` reference was sent
to the host as the literal password, and the value was never registered for
masking, so an SSH error quoting it printed it in the clear.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

import homelab_helper.cli.discover as cli_discover
from homelab_helper.cli.main import app
from homelab_helper.engine.host_probe import HostProbeRequest, HostProbeResult
from homelab_helper.secrets import forget_secrets

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


@pytest.fixture
def seen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[HostProbeRequest]:
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'h.db'}")
    requests: list[HostProbeRequest] = []

    async def fake_probe_host(_session: Any, request: HostProbeRequest, **_: Any) -> Any:
        requests.append(request)
        return HostProbeResult(
            hostname=request.name,
            host_id="h1",
            failures=1,
            session_error=f"auth failed for password {request.ssh_password!r}",
        )

    monkeypatch.setattr(cli_discover, "probe_host", fake_probe_host)
    forget_secrets()
    return requests


def _discover(*extra: str) -> Any:
    return runner.invoke(
        app, ["discover", "host", "pve9", "--ssh-user", "root", "--no-sync", *extra]
    )


def test_a_reference_is_resolved_and_the_value_is_masked(
    seen: list[HostProbeRequest], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REAL_PW", "hunter2-not-a-real-one")
    monkeypatch.setenv("PVE_PW", "env:REAL_PW")

    result = _discover("--ssh-password-env", "PVE_PW")

    assert seen[0].ssh_password == "hunter2-not-a-real-one"
    assert "hunter2" not in result.output
    assert "***" in result.output


def test_a_reference_that_cannot_resolve_stops_before_any_login(
    seen: list[HostProbeRequest], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PVE_PW", "file:/nonexistent/secrets.yaml#pve")

    result = _discover("--ssh-password-env", "PVE_PW")

    assert result.exit_code == 2
    assert seen == []
