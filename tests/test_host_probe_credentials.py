"""A host probe that logged in records the login, so node actions can use it.

``helper exec`` and maintenance runs read ``Host.credentials_ref`` for the SSH
user and key; before this, nothing wrote it, so the remedy the executor named
("re-run `helper discover host`") changed nothing.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from homelab_helper.db.models import Host
from homelab_helper.db.session import session_scope
from homelab_helper.engine.host_probe import HostProbeRequest, probe_host
from tests.test_node_update import sessionmaker_  # noqa: F401 - fixture


class _FakeSSH:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail

    def shared_session(self, *_: Any, **__: Any) -> Any:
        fail = self._fail

        class _Ctx:
            async def __aenter__(self) -> _Ctx:
                if fail:
                    raise OSError("connection refused")
                return self

            async def __aexit__(self, *exc: Any) -> bool:
                return False

        return _Ctx()


async def _probe(session: Any, request: HostProbeRequest, ssh: _FakeSSH) -> Host:
    await probe_host(session, request, probe_classes=[], ssh_adapter=ssh)  # type: ignore[arg-type]
    return (await session.execute(select(Host).where(Host.hostname == request.name))).scalar_one()


async def test_a_successful_probe_records_the_login(sessionmaker_) -> None:  # noqa: F811
    async with session_scope(sessionmaker_) as s:
        host = await _probe(
            s,
            HostProbeRequest(name="pve9", ssh_user="root", ssh_key_path="/home/op/.ssh/id_ed25519"),
            _FakeSSH(),
        )
        assert host.credentials_ref == "ssh:root:/home/op/.ssh/id_ed25519"


async def test_a_refused_session_or_a_password_login_records_nothing(sessionmaker_) -> None:  # noqa: F811
    async with session_scope(sessionmaker_) as s:
        refused = await _probe(
            s, HostProbeRequest(name="pve8", ssh_user="root"), _FakeSSH(fail=True)
        )
        assert refused.credentials_ref is None
        by_password = await _probe(
            s, HostProbeRequest(name="pve7", ssh_user="root", ssh_password="pw"), _FakeSSH()
        )
        assert by_password.credentials_ref is None
