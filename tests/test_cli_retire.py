"""CLI tests for `helper host retire`, `helper part merge|show`, and `helper service ...`."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from sqlalchemy import select
from typer.testing import CliRunner

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

from homelab_helper.cli.main import app
from homelab_helper.db.base import Base
from homelab_helper.db.enums import DiscoverySource, PartKind, ResolutionScope
from homelab_helper.db.models import Host, PhysicalPart, Placement, Service, ServiceEndpoint
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope

runner = CliRunner()


def _db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    url = f"sqlite+aiosqlite:///{tmp_path / 'retire.db'}"
    monkeypatch.setenv("HOMELAB_HELPER_DATABASE_URL", url)
    monkeypatch.setenv("HOMELAB_HELPER_OPERATOR", "tester")

    async def _init() -> None:
        engine = make_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = make_sessionmaker(engine)
        async with session_scope(sm) as s:
            old = Host(hostname="pi-cp1", primary_ip="10.0.1.11")
            new = Host(hostname="wyhome")
            s.add_all([old, new])
            await s.flush()
            a = PhysicalPart(kind=PartKind.SSD, serial="SSD-A", model="A400")
            b = PhysicalPart(kind=PartKind.SSD, wwid="0xdeadbeef")
            s.add_all([a, b])
            await s.flush()
            s.add_all(
                [
                    Placement(part_id=a.id, host_id=old.id, slot="sda"),
                    Placement(part_id=b.id, host_id=new.id, slot="sdb"),
                ]
            )
            svc = Service(name="ha")
            s.add(svc)
            await s.flush()
            for resolver in ("unifi", "unifi:covington"):
                s.add(
                    ServiceEndpoint(
                        service_id=svc.id,
                        scope=ResolutionScope.INTERNAL,
                        hostname="ha.lan",
                        ip="10.0.1.50",
                        resolver=resolver,
                        discovery_source=DiscoverySource.UNIFI,
                    )
                )
        await engine.dispose()

    asyncio.run(_init())
    return url


async def _open_placements(url: str, hostname: str) -> int:
    engine = make_engine(url)
    try:
        sm = make_sessionmaker(engine)
        async with sm() as s:
            host = (await s.execute(select(Host).where(Host.hostname == hostname))).scalar_one()
            rows = (
                (
                    await s.execute(
                        select(Placement).where(
                            Placement.host_id == host.id, Placement.to_date.is_(None)
                        )
                    )
                )
                .scalars()
                .all()
            )
            return len(rows)
    finally:
        await engine.dispose()


def test_host_retire_requires_confirmation_then_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = _db(tmp_path, monkeypatch)
    aborted = runner.invoke(app, ["host", "retire", "pi-cp1"], input="n\n")
    assert aborted.exit_code == 1
    assert asyncio.run(_open_placements(url, "pi-cp1")) == 1

    result = runner.invoke(app, ["host", "retire", "pi-cp1", "--yes", "-r", "reflashed"])
    assert result.exit_code == 0, result.stdout
    assert "1 placement(s) closed" in result.stdout
    assert asyncio.run(_open_placements(url, "pi-cp1")) == 0
    assert asyncio.run(_open_placements(url, "wyhome")) == 1

    again = runner.invoke(app, ["host", "retire", "pi-cp1", "--yes"])
    assert again.exit_code == 0
    assert "already retired" in again.stdout

    shown = runner.invoke(app, ["host", "show", "pi-cp1"])
    assert shown.exit_code == 0
    assert "RETIRED" in shown.stdout

    missing = runner.invoke(app, ["host", "retire", "ghost", "--yes"])
    assert missing.exit_code == 2


def test_part_show_and_merge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _db(tmp_path, monkeypatch)
    shown = runner.invoke(app, ["part", "show", "SSD-A"])
    assert shown.exit_code == 0, shown.stdout
    assert "pi-cp1" in shown.stdout

    merged = runner.invoke(app, ["part", "merge", "0xdeadbeef", "--into", "SSD-A", "--yes"])
    assert merged.exit_code == 0, merged.stdout
    assert "1 placement(s) moved" in merged.stdout

    after = runner.invoke(app, ["part", "show", "SSD-A"])
    assert after.exit_code == 0
    assert "wyhome" in after.stdout
    assert "merged from" in after.stdout

    gone = runner.invoke(app, ["part", "show", "0xdeadbeef"])
    # the survivor now carries the wwid, so the reference resolves to it
    assert gone.exit_code == 0
    assert "pi-cp1" in gone.stdout

    unknown = runner.invoke(app, ["part", "merge", "nope", "--into", "SSD-A", "--yes"])
    assert unknown.exit_code == 2


def test_service_resolvers_retire_and_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _db(tmp_path, monkeypatch)
    listed = runner.invoke(app, ["service", "resolvers"])
    assert listed.exit_code == 0, listed.stdout
    assert "unifi:covington" in listed.stdout
    assert "2 slice(s)" in listed.stdout

    retired = runner.invoke(app, ["service", "retire-resolver", "unifi", "--yes"])
    assert retired.exit_code == 0, retired.stdout
    assert "1 endpoint(s) removed" in retired.stdout
    assert "0 empty service(s)" in retired.stdout

    bad_scope = runner.invoke(
        app, ["service", "retire-resolver", "unifi", "--scope", "sideways", "--yes"]
    )
    assert bad_scope.exit_code == 2

    monkeypatch.delenv("HOMELAB_HELPER_SERVICE_ALIASES", raising=False)
    none = runner.invoke(app, ["service", "aliases"])
    assert none.exit_code == 0
    assert "no alias map" in none.stdout

    f = tmp_path / "aliases.yaml"
    f.write_text("services:\n  home-assistant:\n    hostnames: [ha.lan, '*.ha.lan']\n")
    monkeypatch.setenv("HOMELAB_HELPER_SERVICE_ALIASES", str(f))
    shown = runner.invoke(app, ["service", "aliases"])
    assert shown.exit_code == 0, shown.stdout
    assert "home-assistant" in shown.stdout
    assert "glob" in shown.stdout
