"""JetKVM source: adapter, console evidence, reconcile, run_discovery, `helper discover jetkvm`."""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from sqlalchemy import select
from typer.testing import CliRunner

import homelab_helper.cli.discover as cli_discover
import homelab_helper.engine.jetkvm as jk
import homelab_helper.mcp_server as mcp_srv
from homelab_helper.adapters.jetkvm import (
    JetKVMAdapter,
    JetKVMAPIError,
    JetKVMConfigError,
    JetKVMSnapshotUnavailable,
    grab_frame_webrtc,
)
from homelab_helper.cli.main import app
from homelab_helper.db.base import Base
from homelab_helper.db.enums import FindingKind, FindingStatus
from homelab_helper.db.models import Host, ReconciliationFinding, Service, ServiceEndpoint
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.secrets import forget_secrets

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

URL = "http://kvm.example.invalid"
PNG = b"\x89PNG\r\n\x1a\nfake"


class Device:
    """A fake JetKVM: open status, local-password login, a /device behind the cookie."""

    def __init__(self, password: str | None = "pw", *, up: bool = True) -> None:
        self.password = password
        self.up = up
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(f"{request.method} {request.url.path}")
        if not self.up:
            raise httpx.ConnectError("connection refused", request=request)
        if request.url.path == "/device/status":
            return httpx.Response(200, json={"isSetup": True})
        if request.url.path == "/auth/login-local":
            if b'"password":"pw"' not in request.content.replace(b" ", b""):
                return httpx.Response(401, json={"error": "invalid password"})
            return httpx.Response(
                200, json={"message": "ok"}, headers={"set-cookie": "authToken=tok; Path=/"}
            )
        if request.url.path == "/device":
            if self.password and "authToken=tok" not in request.headers.get("cookie", ""):
                return httpx.Response(401, json={"error": "unauthorized"})
            return httpx.Response(200, json={"authMode": "password", "deviceId": "dev-1"})
        return httpx.Response(404)


class Grabber:
    """Stands in for the WebRTC frame grab; records what it was handed."""

    def __init__(self, result: bytes | Exception = PNG) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, str], bool]] = []

    async def __call__(
        self,
        url: str,
        headers: dict[str, str],
        verify: bool,
        timeout: float,  # noqa: ASYNC109
    ) -> bytes:
        self.calls.append((url, headers, verify))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _adapter(
    device: Device, grabber: Grabber | None = None, *, url: str = URL, password: str | None = "pw"
) -> JetKVMAdapter:
    client = httpx.AsyncClient(base_url=url, transport=httpx.MockTransport(device))
    return JetKVMAdapter(url, password=password, client=client, frame_grabber=grabber or Grabber())


# --- adapter ---------------------------------------------------------------


async def test_status_is_open() -> None:
    dev = Device()
    assert await _adapter(dev).status() == {"isSetup": True}
    assert dev.requests == ["GET /device/status"]


async def test_device_logs_in_once_with_the_local_password() -> None:
    dev = Device()
    adapter = _adapter(dev)
    assert (await adapter.device())["deviceId"] == "dev-1"
    await adapter.device()
    assert dev.requests.count("POST /auth/login-local") == 1


async def test_a_wrong_password_is_refused() -> None:
    with pytest.raises(JetKVMAPIError, match="refused the local password"):
        await _adapter(Device(), password="nope").device()


async def test_device_without_a_password_says_what_to_set() -> None:
    with pytest.raises(JetKVMAPIError, match="HOMELAB_HELPER_JETKVM_PASSWORD"):
        await _adapter(Device(), password=None).device()


async def test_console_snapshot_hands_the_grabber_the_session() -> None:
    grabber = Grabber()
    adapter = _adapter(Device(), grabber, url="https://kvm.example.invalid")
    adapter.verify_ssl = False
    assert await adapter.console_snapshot() == PNG
    url, headers, verify = grabber.calls[0]
    assert url == "wss://kvm.example.invalid/webrtc/signaling/client"
    assert headers == {"Cookie": "authToken=tok"}
    assert verify is False


async def test_console_snapshot_in_no_password_mode_sends_no_cookie() -> None:
    grabber = Grabber()
    await _adapter(Device(password=None), grabber, password=None).console_snapshot()
    assert grabber.calls[0][0] == "ws://kvm.example.invalid/webrtc/signaling/client"
    assert grabber.calls[0][1] == {}


async def test_webrtc_without_the_extra_says_how_to_install_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "aiortc", None)
    with pytest.raises(JetKVMSnapshotUnavailable, match=r"homelab-helper\[jetkvm\]"):
        await grab_frame_webrtc("ws://kvm.example.invalid/x", {}, True, 1.0)


def test_adapter_has_no_input_or_power_method() -> None:
    """Console input is unbounded remote code execution; the adapter only watches."""
    names = {n.lower() for n in dir(JetKVMAdapter)}
    for bad in ("key", "type", "mouse", "click", "power", "media", "mount", "reboot", "paste"):
        assert not any(bad in n for n in names if not n.startswith("__")), bad


def test_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMELAB_HELPER_JETKVM_URL", raising=False)
    with pytest.raises(JetKVMConfigError):
        JetKVMAdapter.from_env()
    forget_secrets()
    monkeypatch.setenv("HOMELAB_HELPER_JETKVM_URL", URL + "/")
    monkeypatch.setenv("KVM_PW_FOR_TEST", "s3cret")
    monkeypatch.setenv("HOMELAB_HELPER_JETKVM_PASSWORD", "env:KVM_PW_FOR_TEST")
    monkeypatch.setenv("HOMELAB_HELPER_JETKVM_VERIFY_SSL", "false")
    adapter = JetKVMAdapter.from_env()
    assert adapter.url == URL
    assert adapter._password == "s3cret"
    assert adapter.verify_ssl is False
    forget_secrets()


def test_attached_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMELAB_HELPER_JETKVM_ATTACHED_TO", raising=False)
    monkeypatch.delenv("HOMELAB_HELPER_JETKVM_ATTACHED_PORT", raising=False)
    assert jk.attached_from_env() == (None, 22)
    monkeypatch.setenv("HOMELAB_HELPER_JETKVM_ATTACHED_TO", "node-a")
    monkeypatch.setenv("HOMELAB_HELPER_JETKVM_ATTACHED_PORT", "2222")
    assert jk.attached_from_env() == ("node-a", 2222)


# --- evidence --------------------------------------------------------------


async def test_tcp_reachable_against_a_loopback_server() -> None:
    async def _accept(_r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        w.close()

    server = await asyncio.start_server(_accept, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        assert await jk.tcp_reachable("127.0.0.1", port)
    finally:
        server.close()
        await server.wait_closed()
    assert not await jk.tcp_reachable("127.0.0.1", port, timeout=1.0)


def _names(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.glob("*.png"))


async def test_capture_console_saves_and_keeps_the_newest(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    adapter = _adapter(Device())
    start = datetime(2026, 1, 1, tzinfo=UTC)
    paths = []
    for i in range(jk.KEEP_SNAPSHOTS + 2):
        path, err = await jk.capture_console(
            adapter, tmp_path, "node-a", when=start + timedelta(minutes=i)
        )
        assert err is None
        paths.append(path)
    kept = _names(tmp_path)
    assert len(kept) == jk.KEEP_SNAPSHOTS
    assert paths[-1] is not None
    assert paths[-1].endswith(kept[-1])
    assert (tmp_path / kept[-1]).read_bytes() == PNG


async def test_capture_console_never_raises(tmp_path: Path) -> None:
    adapter = _adapter(Device(), Grabber(JetKVMSnapshotUnavailable("no video")))
    assert await jk.capture_console(adapter, tmp_path, "node-a") == (None, "no video")
    assert _names(tmp_path) == []


# --- reconcile -------------------------------------------------------------


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = make_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
def sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_sessionmaker(engine)


async def _findings(session: AsyncSession, kind: FindingKind) -> list[ReconciliationFinding]:
    rows = await session.execute(
        select(ReconciliationFinding).where(
            ReconciliationFinding.kind == kind,
            ReconciliationFinding.status == FindingStatus.OPEN,
        )
    )
    return list(rows.scalars().all())


def _evidence(row: ReconciliationFinding) -> dict[str, Any]:
    return next(r for r in row.evidence_refs if r.get("type") == "evidence")


@pytest.fixture
async def host(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with session_scope(sessionmaker) as s:
        s.add(Host(hostname="node-a", primary_ip="127.0.0.1"))


@pytest.fixture
def reachable(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    state = {"up": True}

    async def _fake(_address: str, _port: int, timeout: float = 5.0) -> bool:  # noqa: ASYNC109
        return state["up"]

    monkeypatch.setattr(jk, "tcp_reachable", _fake)
    return state


async def _observe(
    sessionmaker: async_sessionmaker[AsyncSession],
    adapter: JetKVMAdapter,
    tmp_path: Path,
    attached_to: str | None = "node-a",
) -> jk.JetKVMRun:
    async with session_scope(sessionmaker) as s:
        return await jk.observe_jetkvm(
            s, adapter, attached_to=attached_to, port=22, snapshot_dir=tmp_path
        )


@pytest.mark.usefixtures("host")
async def test_a_down_host_opens_with_its_console_then_resolves(
    sessionmaker: async_sessionmaker[AsyncSession], reachable: dict[str, bool], tmp_path: Path
) -> None:
    run = await _observe(sessionmaker, _adapter(Device()), tmp_path)
    assert run.attached is not None
    assert run.attached.reachable
    assert run.findings["opened"] == 0

    reachable["up"] = False
    run = await _observe(sessionmaker, _adapter(Device()), tmp_path)
    assert run.findings["opened"] == 1
    async with session_scope(sessionmaker) as s:
        [row] = await _findings(s, FindingKind.HOST_UNREACHABLE)
        ev = _evidence(row)
        assert ev["host"] == "node-a"
        assert ev["console_snapshot"].endswith(".png")
        assert ev["console_snapshot"] in row.description

    reachable["up"] = True
    run = await _observe(sessionmaker, _adapter(Device()), tmp_path)
    assert run.findings["resolved"] == 1


@pytest.mark.usefixtures("host")
async def test_a_failed_snapshot_is_absent_evidence(
    sessionmaker: async_sessionmaker[AsyncSession], reachable: dict[str, bool], tmp_path: Path
) -> None:
    reachable["up"] = False
    adapter = _adapter(Device(), Grabber(JetKVMSnapshotUnavailable("no video frame")))
    run = await _observe(sessionmaker, adapter, tmp_path)
    assert run.findings["opened"] == 1
    async with session_scope(sessionmaker) as s:
        [row] = await _findings(s, FindingKind.HOST_UNREACHABLE)
        assert _evidence(row)["console_snapshot"] is None
        assert _evidence(row)["snapshot_error"] == "no video frame"


@pytest.mark.usefixtures("host")
async def test_an_unreachable_kvm_still_checks_the_host(
    sessionmaker: async_sessionmaker[AsyncSession], reachable: dict[str, bool], tmp_path: Path
) -> None:
    reachable["up"] = False
    run = await _observe(sessionmaker, _adapter(Device(up=False)), tmp_path)
    assert run.status is None
    assert run.attached is not None
    assert run.attached.snapshot_error == "JetKVM unreachable"
    async with session_scope(sessionmaker) as s:
        assert len(await _findings(s, FindingKind.HOST_UNREACHABLE)) == 1
        [kvm] = await _findings(s, FindingKind.SERVICE_UNHEALTHY)
        assert kvm.title == "JetKVM is unreachable"

    reachable["up"] = True
    run = await _observe(sessionmaker, _adapter(Device()), tmp_path)
    assert run.findings["resolved"] == 2


@pytest.mark.usefixtures("host")
async def test_an_unchecked_host_keeps_its_finding(
    sessionmaker: async_sessionmaker[AsyncSession], reachable: dict[str, bool], tmp_path: Path
) -> None:
    """Invariant 1: a run that did not check the host observes nothing about it."""
    reachable["up"] = False
    await _observe(sessionmaker, _adapter(Device()), tmp_path)
    reachable["up"] = True
    run = await _observe(sessionmaker, _adapter(Device()), tmp_path, attached_to=None)
    assert run.attached is None
    assert run.findings["resolved"] == 0
    run = await _observe(sessionmaker, _adapter(Device()), tmp_path, attached_to="unknown-host")
    assert run.attached is None
    async with session_scope(sessionmaker) as s:
        assert len(await _findings(s, FindingKind.HOST_UNREACHABLE)) == 1


async def test_the_kvm_is_recorded_once_as_a_service(
    sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    first = await _observe(sessionmaker, _adapter(Device()), tmp_path, attached_to="node-a")
    again = await _observe(sessionmaker, _adapter(Device()), tmp_path, attached_to="node-a")
    assert first.created
    assert not again.created
    async with session_scope(sessionmaker) as s:
        [svc] = (await s.execute(select(Service))).scalars().all()
        assert svc.name == "jetkvm"
        assert svc.attributes["attached_to"] == "node-a"
        assert svc.attributes["device_id"] == "dev-1"
        assert svc.attributes["is_setup"] is True
        [ep] = (await s.execute(select(ServiceEndpoint))).scalars().all()
        assert ep.hostname == "kvm.example.invalid"


# --- run_discovery, console_snapshot, CLI ---------------------------------


@pytest.fixture
def db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    url = f"sqlite+aiosqlite:///{tmp_path / 'kvm.db'}"
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)
    monkeypatch.setenv("HOMELAB_HELPER_JETKVM_ATTACHED_TO", "node-a")

    async def _init() -> None:
        eng = make_engine(url)
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with session_scope(make_sessionmaker(eng)) as s:
            s.add(Host(hostname="node-a", primary_ip="127.0.0.1"))
        await eng.dispose()

    asyncio.run(_init())
    return url


def _loader(device: Device, grabber: Grabber | None = None) -> Callable[[], JetKVMAdapter]:
    return lambda: _adapter(device, grabber)


@pytest.mark.usefixtures("db_url")
def test_run_discovery_jetkvm(monkeypatch: pytest.MonkeyPatch, reachable: dict[str, bool]) -> None:
    reachable["up"] = False
    monkeypatch.setattr(mcp_srv, "_load_jetkvm_adapter", _loader(Device()))
    out = asyncio.run(mcp_srv.run_discovery("jetkvm"))
    assert out["reachable"]
    assert out["attached"]["host"] == "node-a"
    assert out["attached"]["console_snapshot"].endswith(".png")
    assert [i["category"] for i in out["issues"]] == ["host-unreachable"]
    assert out["findings"]["opened"] == 1


@pytest.mark.usefixtures("db_url")
def test_console_snapshot_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_srv, "_load_jetkvm_adapter", _loader(Device()))
    out = asyncio.run(mcp_srv.console_snapshot())
    assert out["attached_to"] == "node-a"
    assert out["path"].endswith(".png")

    monkeypatch.setattr(
        mcp_srv, "_load_jetkvm_adapter", _loader(Device(), Grabber(JetKVMSnapshotUnavailable("x")))
    )
    assert asyncio.run(mcp_srv.console_snapshot())["error"] == "x"


runner = CliRunner()


def test_cli_discover_jetkvm_reads_and_snapshots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli_discover, "_load_jetkvm_adapter", _loader(Device()))
    out = tmp_path / "shot.png"
    result = runner.invoke(app, ["discover", "jetkvm", "--snapshot", str(out)])
    assert result.exit_code == 0, result.stdout
    assert "set up=True" in result.stdout
    assert out.read_bytes() == PNG


@pytest.mark.usefixtures("db_url")
def test_cli_discover_jetkvm_persists(
    monkeypatch: pytest.MonkeyPatch, reachable: dict[str, bool]
) -> None:
    reachable["up"] = False
    monkeypatch.setattr(cli_discover, "_load_jetkvm_adapter", _loader(Device()))
    dry = runner.invoke(app, ["discover", "jetkvm", "--persist", "--dry-run"])
    assert dry.exit_code == 0, dry.stdout
    assert "rolled back" in dry.stdout
    result = runner.invoke(app, ["discover", "jetkvm", "--persist"])
    assert result.exit_code == 0, result.stdout
    assert "unreachable" in result.stdout
    assert "1 opened" in result.stdout

    monkeypatch.setattr(cli_discover, "_load_jetkvm_adapter", _loader(Device(up=False)))
    down = runner.invoke(app, ["discover", "jetkvm", "--persist"])
    assert down.exit_code == 1
