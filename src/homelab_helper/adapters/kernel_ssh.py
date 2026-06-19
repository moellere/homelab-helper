"""Kernel-SSH adapter — read-only SSH session wrapper for host probes.

Sessions are context-managed:

.. code-block:: python

    adapter = KernelSSHAdapter()
    async with adapter.session(host="node3", user="root", key_path=Path("~/.ssh/id_ed25519")) as ssh:
        result = await ssh.run("hostname")
        os_release = await ssh.cat("/etc/os-release")

For batched runs against the same host (e.g. five host probes in a row),
the caller can wrap the batch in :meth:`shared_session`. That pre-opens
one connection and ``session(...)`` then yields the cached session
without opening or closing per-call:

.. code-block:: python

    async with adapter.shared_session("node3", user="root", key_path=...):
        for probe in batch:
            await probe.run(...)        # each probe's adapter.session() reuses the same conn

Errors are returned as ``CommandResult`` objects with non-zero ``exit_code``
or raised as :class:`SSHConnectionError` / :class:`SSHAuthenticationError`
for connection-level failures.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import asyncssh
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class SSHConnectionError(RuntimeError):
    """Raised when the adapter cannot establish a connection."""


class SSHAuthenticationError(SSHConnectionError):
    """Raised when authentication fails."""


class CommandResult(BaseModel):
    """Outcome of running one command over SSH."""

    model_config = ConfigDict(frozen=True)

    command: str
    stdout: str
    stderr: str
    exit_code: int

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class SSHSession:
    """A live SSH connection. Created by :meth:`KernelSSHAdapter.session`."""

    def __init__(self, conn: asyncssh.SSHClientConnection, default_timeout: int) -> None:
        self._conn = conn
        self._default_timeout = default_timeout

    async def run(self, command: str, *, timeout: int | None = None) -> CommandResult:
        """Run ``command``; return a :class:`CommandResult`.

        Does NOT raise on non-zero exit — that's the caller's call. Raises
        :class:`SSHConnectionError` on connection-level failure.
        """
        t = timeout if timeout is not None else self._default_timeout
        try:
            async with asyncio.timeout(t):
                proc = await self._conn.run(command, check=False)
        except TimeoutError as exc:
            raise SSHConnectionError(f"command timed out after {t}s: {command!r}") from exc
        except asyncssh.Error as exc:
            raise SSHConnectionError(f"ssh error during {command!r}: {exc}") from exc
        return CommandResult(
            command=command,
            stdout=str(proc.stdout or ""),
            stderr=str(proc.stderr or ""),
            exit_code=int(proc.exit_status if proc.exit_status is not None else -1),
        )

    async def cat(self, path: str, *, timeout: int | None = None) -> str:
        """Convenience: ``cat <path>``; returns stdout or empty string on failure."""
        result = await self.run(f"cat {path}", timeout=timeout)
        return result.stdout if result.ok else ""

    async def exists(self, path: str, *, timeout: int | None = None) -> bool:
        """Convenience: returns True iff ``test -e <path>`` succeeds."""
        result = await self.run(f"test -e {path}", timeout=timeout)
        return result.ok


def _cache_key(host: str, user: str, port: int) -> tuple[str, str, int]:
    """Tuple identifying a unique cached connection."""
    return (host, user, port)


class KernelSSHAdapter:
    """The read-only SSH adapter used by host probes.

    Holds an optional shared-session cache: when :meth:`shared_session` is
    active for a given ``(host, user, port)``, calls to :meth:`session` with
    the same triple yield the cached :class:`SSHSession` instead of opening
    a new connection.
    """

    name = "kernel-ssh"

    def __init__(self, default_timeout: int = 30) -> None:
        self._default_timeout = default_timeout
        self._shared: dict[tuple[str, str, int], SSHSession] = {}

    @asynccontextmanager
    async def session(
        self,
        host: str,
        *,
        user: str,
        key_path: Path | str | None = None,
        password: str | None = None,
        port: int = 22,
        connect_timeout: int = 15,
        known_hosts: Path | str | None = None,
    ) -> AsyncIterator[SSHSession]:
        """Open an SSH connection; yields a :class:`SSHSession`.

        If a shared session matching ``(host, user, port)`` exists, yields
        that one without opening a new connection or closing on exit.
        Otherwise opens a fresh per-call session and closes it on exit.

        ``known_hosts=None`` accepts any host key — appropriate for a homelab
        discovery scenario, but production usage should pin a known-hosts file.
        """
        cached = self._shared.get(_cache_key(host, user, port))
        if cached is not None:
            yield cached
            return

        client_keys: list[str] | None = None
        if key_path is not None:
            client_keys = [str(Path(key_path).expanduser())]  # noqa: ASYNC240
        try:
            async with asyncio.timeout(connect_timeout):
                conn = await asyncssh.connect(
                    host,
                    port=port,
                    username=user,
                    client_keys=client_keys,
                    password=password,
                    known_hosts=str(known_hosts) if known_hosts else None,
                )
        except asyncssh.PermissionDenied as exc:
            raise SSHAuthenticationError(f"authentication failed for {user}@{host}") from exc
        except (asyncssh.Error, OSError, TimeoutError) as exc:
            raise SSHConnectionError(f"could not connect to {user}@{host}:{port}: {exc}") from exc

        try:
            yield SSHSession(conn, default_timeout=self._default_timeout)
        finally:
            conn.close()
            await conn.wait_closed()

    @asynccontextmanager
    async def shared_session(
        self,
        host: str,
        *,
        user: str,
        key_path: Path | str | None = None,
        password: str | None = None,
        port: int = 22,
        connect_timeout: int = 15,
        known_hosts: Path | str | None = None,
    ) -> AsyncIterator[SSHSession]:
        """Pre-open one connection and register it for reuse by :meth:`session`.

        The connection is opened on entry and closed on exit. Inside the
        ``async with``, any caller invoking ``session(host, user=..., port=...)``
        with matching parameters gets the same underlying connection.
        """
        client_keys: list[str] | None = None
        if key_path is not None:
            client_keys = [str(Path(key_path).expanduser())]  # noqa: ASYNC240
        try:
            async with asyncio.timeout(connect_timeout):
                conn = await asyncssh.connect(
                    host,
                    port=port,
                    username=user,
                    client_keys=client_keys,
                    password=password,
                    known_hosts=str(known_hosts) if known_hosts else None,
                )
        except asyncssh.PermissionDenied as exc:
            raise SSHAuthenticationError(f"authentication failed for {user}@{host}") from exc
        except (asyncssh.Error, OSError, TimeoutError) as exc:
            raise SSHConnectionError(f"could not connect to {user}@{host}:{port}: {exc}") from exc

        key = _cache_key(host, user, port)
        sess = SSHSession(conn, default_timeout=self._default_timeout)
        self._shared[key] = sess
        try:
            yield sess
        finally:
            self._shared.pop(key, None)
            conn.close()
            await conn.wait_closed()

    async def health_check(
        self,
        host: str,
        *,
        user: str,
        key_path: Path | str | None = None,
        password: str | None = None,
        port: int = 22,
    ) -> tuple[bool, str | None]:
        """Quick reachability + auth probe. Returns ``(ok, error_message)``."""
        try:
            async with self.session(
                host, user=user, key_path=key_path, password=password, port=port
            ) as ssh:
                result = await ssh.run("true", timeout=10)
                if not result.ok:
                    return False, f"'true' exited {result.exit_code}"
                return True, None
        except SSHConnectionError as exc:
            return False, str(exc)


__all__ = [
    "CommandResult",
    "KernelSSHAdapter",
    "SSHAuthenticationError",
    "SSHConnectionError",
    "SSHSession",
]
