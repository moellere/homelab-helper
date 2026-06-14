"""CLI test for ``helper discover network``.

Spins up an asyncio TCP listener on 127.0.0.1 inside the test, then invokes
the CLI command pointed at 127.0.0.1/31 with --ports set to the listener's
port. The whole thing happens against a temporary file-backed SQLite — same
pattern as the other CLI reader tests.
"""

from __future__ import annotations

import socket
import threading
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from homelab_helper.cli.main import app
from homelab_helper.db.base import Base
from homelab_helper.db.session import make_engine, make_sessionmaker

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


@pytest.fixture
async def fresh_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db_path = tmp_path / "discover-network.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    make_sessionmaker(engine)
    await engine.dispose()
    return url


def _start_blocking_tcp_server() -> tuple[socket.socket, int, threading.Thread]:
    """Threaded blocking TCP server on 127.0.0.1.

    Sends an HTTP/1.0 200 response *immediately* on accept (without waiting
    for a request) — many real homelab services (Cockpit, Proxmox UI) push
    their headers fast enough that this is a fair simulation, and it avoids
    racing with the async probe's banner-read timeout.
    """
    import contextlib

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.listen(16)
    sock.settimeout(2.0)

    def _serve() -> None:
        while True:
            try:
                conn, _ = sock.accept()
            except (OSError, TimeoutError):
                return
            try:
                with contextlib.suppress(OSError):
                    conn.sendall(
                        b"HTTP/1.0 200 OK\r\nServer: helper-test/0.1\r\nContent-Length: 0\r\n\r\n"
                    )
            finally:
                conn.close()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    return sock, port, t


@pytest.fixture
def tcp_listener():
    """Yield (port, stop_fn). Caller stops the listener at teardown."""
    sock, port, thread = _start_blocking_tcp_server()
    try:
        yield port
    finally:
        sock.close()
        thread.join(timeout=1.0)


def test_discover_network_finds_listener_and_fingerprints(
    fresh_db_url: str, tcp_listener: int
) -> None:
    port = tcp_listener
    result = runner.invoke(
        app,
        [
            "discover",
            "network",
            "127.0.0.0/31",
            "--ports",
            str(port),
            "--timeout",
            "1.0",
            "--concurrency",
            "8",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "live host(s)" in result.stdout
    # The discovered IP appears in the table.
    assert "127.0.0.1" in result.stdout
    # Fingerprint identified the Server header.
    assert "http" in result.stdout.lower()


def test_discover_network_no_fingerprint_flag_skips_fingerprint(
    fresh_db_url: str, tcp_listener: int
) -> None:
    port = tcp_listener
    result = runner.invoke(
        app,
        [
            "discover",
            "network",
            "127.0.0.0/31",
            "--ports",
            str(port),
            "--timeout",
            "1.0",
            "--no-fingerprint",
        ],
    )
    assert result.exit_code == 0
    assert "live host(s)" in result.stdout
    # Output should NOT mention "fingerprint" progress lines since we skipped them.
    assert "fingerprint" not in result.stdout.lower() or "no-fingerprint" in result.stdout.lower()


def test_discover_network_reports_zero_when_nothing_listens(fresh_db_url: str) -> None:
    # Use port 1 — unlikely to be open on the loopback.
    result = runner.invoke(
        app,
        [
            "discover",
            "network",
            "127.0.0.0/31",
            "--ports",
            "1",
            "--timeout",
            "0.3",
        ],
    )
    assert result.exit_code == 0
    assert "0" in result.stdout  # "0 live host(s)"


def test_discover_network_rejects_bad_ports(fresh_db_url: str) -> None:
    result = runner.invoke(
        app,
        ["discover", "network", "127.0.0.0/31", "--ports", "abc"],
    )
    assert result.exit_code != 0


def test_discover_network_help_loads() -> None:
    result = runner.invoke(app, ["discover", "network", "--help"])
    assert result.exit_code == 0
    assert "CIDR" in result.stdout.upper() or "network" in result.stdout.lower()
