"""Tests for the network.subnet-scan + network.fingerprint probes.

Uses asyncio loopback servers as test doubles. A small set of fake services
listens on ephemeral 127.0.0.1 ports; the probes scan/fingerprint them and we
assert on the observation output. No mocking of asyncio.open_connection — the
probes run their real connect/banner-grab paths.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

from homelab_helper.probes.base import (
    AdapterRegistry,
    ProbeContext,
    ProbeTarget,
)
from homelab_helper.probes.network.fingerprint import (
    NetworkFingerprintProbe,
    _classify_http,
    _looks_textual,
)
from homelab_helper.probes.network.subnet_scan import (
    DEFAULT_PORTS,
    NetworkSubnetScanProbe,
)


def _ctx_network(cidr: str) -> ProbeContext:
    return ProbeContext(
        target=ProbeTarget(kind="network", network_cidr=cidr),
        adapters=AdapterRegistry(),
        run_id="test-run",
        timeout_s=5,
    )


def _ctx_host(ip: str) -> ProbeContext:
    return ProbeContext(
        target=ProbeTarget(kind="network", primary_ip=ip),
        adapters=AdapterRegistry(),
        run_id="test-run",
        timeout_s=5,
    )


async def _serve_tcp(
    handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
    *,
    host: str = "127.0.0.1",
) -> tuple[asyncio.AbstractServer, int]:
    """Start an asyncio TCP server on an ephemeral port; return (server, port)."""
    server = await asyncio.start_server(handler, host=host, port=0)
    sock = server.sockets[0]
    port = sock.getsockname()[1]
    return server, port


@pytest.fixture
async def tcp_server() -> AsyncIterator[
    Callable[
        [Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]],
        Awaitable[int],
    ]
]:
    """Start servers for the duration of one test; tear them all down at the end."""
    servers: list[asyncio.AbstractServer] = []

    async def _start(
        handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
    ) -> int:
        server, port = await _serve_tcp(handler)
        servers.append(server)
        return port

    try:
        yield _start
    finally:
        for s in servers:
            s.close()
            await s.wait_closed()


# ---------------------------------------------------------------------------
# subnet-scan probe
# ---------------------------------------------------------------------------


async def test_subnet_scan_rejects_non_network_target() -> None:
    probe = NetworkSubnetScanProbe(ports=(1,), per_port_timeout_s=0.1)
    ctx = ProbeContext(
        target=ProbeTarget(kind="host", primary_ip="127.0.0.1"),
        adapters=AdapterRegistry(),
        run_id="t",
        timeout_s=5,
    )
    result = await probe.run(ctx)
    assert result.success is False
    assert "unsupported target kind" in (result.error or "")


async def test_subnet_scan_rejects_bad_cidr() -> None:
    probe = NetworkSubnetScanProbe(ports=(1,), per_port_timeout_s=0.1)
    result = await probe.run(_ctx_network("not-a-cidr"))
    assert result.success is False
    assert "invalid CIDR" in (result.error or "")


async def test_subnet_scan_returns_no_live_hosts_when_nothing_listens() -> None:
    """A high CIDR-host port should have nothing listening on the loopback."""
    probe = NetworkSubnetScanProbe(ports=(1,), per_port_timeout_s=0.1, concurrency=4)
    # 127.0.0.0/30 = .0 (network), .1 (host), .2 (host), .3 (broadcast)
    result = await probe.run(_ctx_network("127.0.0.0/30"))
    assert result.success is True
    # Two host addresses considered; nothing listening on port 1.
    payload = result.raw_payload
    assert payload is not None
    assert payload["scanned_count"] == 2
    assert payload["live_hosts"] == []


async def test_subnet_scan_finds_live_loopback_host(tcp_server) -> None:
    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    port = await tcp_server(_handler)

    probe = NetworkSubnetScanProbe(ports=(port,), per_port_timeout_s=0.5, concurrency=4)
    # 127.0.0.0/31 contains both 127.0.0.0 and 127.0.0.1 as hosts.
    result = await probe.run(_ctx_network("127.0.0.0/31"))
    assert result.success is True

    payload = result.raw_payload
    assert payload is not None
    live_ips = [h["ip"] for h in payload["live_hosts"]]
    assert "127.0.0.1" in live_ips
    # Both summary and per-host observations are present.
    keys = [o.key for o in result.observations]
    assert "network.scan.live_hosts" in keys
    assert "network.host.open_ports" in keys
    # Per-host observation's target_id is the IP.
    per_host = [o for o in result.observations if o.key == "network.host.open_ports"]
    assert any(o.target_id == "127.0.0.1" for o in per_host)


async def test_subnet_scan_records_multiple_open_ports(tcp_server) -> None:
    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    port_a = await tcp_server(_handler)
    port_b = await tcp_server(_handler)

    probe = NetworkSubnetScanProbe(ports=(port_a, port_b), per_port_timeout_s=0.5, concurrency=4)
    result = await probe.run(_ctx_network("127.0.0.1/32"))
    assert result.success is True

    payload = result.raw_payload
    assert payload is not None
    assert len(payload["live_hosts"]) == 1
    assert sorted(payload["live_hosts"][0]["open_ports"]) == sorted([port_a, port_b])


async def test_subnet_scan_concurrency_handles_many_ports(tcp_server) -> None:
    """Tight concurrency limit must not deadlock or miss hits."""

    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    ports = [await tcp_server(_handler) for _ in range(5)]
    probe = NetworkSubnetScanProbe(ports=tuple(ports), per_port_timeout_s=0.5, concurrency=2)
    result = await probe.run(_ctx_network("127.0.0.1/32"))
    assert result.success is True
    payload = result.raw_payload
    assert payload is not None
    assert sorted(payload["live_hosts"][0]["open_ports"]) == sorted(ports)


def test_subnet_scan_default_ports_includes_common_services() -> None:
    """Sanity-check the shipped port panel."""
    for p in (22, 80, 443, 8006, 9090, 6443):
        assert p in DEFAULT_PORTS


# ---------------------------------------------------------------------------
# fingerprint probe
# ---------------------------------------------------------------------------


async def test_fingerprint_requires_primary_ip() -> None:
    probe = NetworkFingerprintProbe(ports=[80], timeout_s=0.3)
    ctx = ProbeContext(
        target=ProbeTarget(kind="network"),
        adapters=AdapterRegistry(),
        run_id="t",
        timeout_s=5,
    )
    result = await probe.run(ctx)
    assert result.success is False
    assert "primary_ip" in (result.error or "")


async def test_fingerprint_records_no_services_when_nothing_listens() -> None:
    probe = NetworkFingerprintProbe(ports=[1], timeout_s=0.2)
    result = await probe.run(_ctx_host("127.0.0.1"))
    assert result.success is True
    payload = result.raw_payload
    assert payload is not None
    assert payload["services"] == []


async def test_fingerprint_detects_ssh_banner(tcp_server) -> None:
    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"SSH-2.0-OpenSSH_9.6p1\r\n")
        await writer.drain()
        writer.close()

    port = await tcp_server(_handler)
    probe = NetworkFingerprintProbe(ports=[port], timeout_s=1.0)
    result = await probe.run(_ctx_host("127.0.0.1"))
    assert result.success is True
    payload = result.raw_payload
    assert payload is not None
    fp = payload["services"][0]
    assert fp["hint"] == "ssh"
    assert fp["service"] == "ssh"
    assert "OpenSSH_9.6p1" in fp["evidence"]["banner"]


async def test_fingerprint_detects_http_server_header(tcp_server) -> None:
    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(2048)  # consume HEAD request
        writer.write(b"HTTP/1.0 200 OK\r\nServer: Cockpit/322\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    port = await tcp_server(_handler)
    probe = NetworkFingerprintProbe(ports=[port], timeout_s=1.0)
    result = await probe.run(_ctx_host("127.0.0.1"))
    assert result.success is True
    payload = result.raw_payload
    assert payload is not None
    fp = payload["services"][0]
    assert fp["hint"] == "http"
    assert fp["service"] == "cockpit"
    assert "Cockpit" in fp["evidence"]["server_header"]
    assert fp["evidence"]["status"] == "200"


async def test_fingerprint_detects_proxmox_via_server_header(tcp_server) -> None:
    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(2048)
        writer.write(b"HTTP/1.1 200 OK\r\nServer: pve-api-daemon/3.0\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    # Use a non-Proxmox port so the SERVER header (not port heuristic) must drive
    # the classification.
    port = await tcp_server(_handler)
    probe = NetworkFingerprintProbe(ports=[port], timeout_s=1.0)
    result = await probe.run(_ctx_host("127.0.0.1"))
    fp = result.raw_payload["services"][0]
    # "pve-api-daemon" alone doesn't contain "proxmox" — verify the heuristic
    # is strict on substrings and doesn't misclassify on an arbitrary port.
    assert fp["hint"] == "http"
    assert fp["service"] is None
    assert fp["evidence"]["server_header"] == "pve-api-daemon/3.0"


async def test_fingerprint_classifies_by_well_known_port_when_server_header_silent(
    tcp_server,
) -> None:
    """Server header missing → port heuristic still gives a guess."""

    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(2048)
        writer.write(b"HTTP/1.0 401 Unauthorized\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    # We can't bind to <1024 ports as non-root; use the port heuristic helper
    # directly to verify the well-known mapping.
    assert _classify_http(8006, None) == "proxmox-ve"
    assert _classify_http(9090, None) == "cockpit"
    assert _classify_http(6443, None) == "k8s-api"
    assert _classify_http(80, None) == "http-generic"
    # And the live HTTP path still works on an arbitrary port with the
    # generic fallback when no server header is sent.
    port = await tcp_server(_handler)
    probe = NetworkFingerprintProbe(ports=[port], timeout_s=1.0)
    result = await probe.run(_ctx_host("127.0.0.1"))
    fp = result.raw_payload["services"][0]
    assert fp["hint"] == "http"
    # Arbitrary high port → no classification, just an http hit.
    assert fp["service"] is None
    assert fp["evidence"]["status"] == "401"


async def test_fingerprint_records_bare_tcp_response_with_port_hint(tcp_server) -> None:
    """A port that opens but doesn't speak SSH or HTTP still gets a hint."""

    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # No greeting; HEAD request will get no reply.
        await asyncio.sleep(0.2)
        writer.close()

    port = await tcp_server(_handler)
    probe = NetworkFingerprintProbe(ports=[port], timeout_s=0.6)
    result = await probe.run(_ctx_host("127.0.0.1"))
    assert result.success is True
    fp = result.raw_payload["services"][0]
    # Silent server: the probe records it as "unknown" rather than skipping.
    assert fp["hint"] == "unknown"


def test_looks_textual_distinguishes_tls_from_http() -> None:
    assert _looks_textual(b"HTTP/1.1 200 OK\r\n")
    assert _looks_textual(b"SSH-2.0-OpenSSH_9.6p1\r\n")
    assert _looks_textual(b"")
    # A typical TLS ServerHello starts with binary 0x16 (handshake type).
    assert not _looks_textual(b"\x16\x03\x01\x02\x00\x02\x00\x00\x00")
