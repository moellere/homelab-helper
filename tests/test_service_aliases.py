"""Service alias map: parsing, resolution, and its effect on the DNS reconcile."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from homelab_helper.db.base import Base
from homelab_helper.db.models import Service, ServiceEndpoint
from homelab_helper.db.session import make_engine, make_sessionmaker, session_scope
from homelab_helper.engine.dns_reconcile import reconcile_internal_endpoints
from homelab_helper.engine.service_aliases import (
    AliasError,
    ServiceAliasMap,
    load_service_aliases,
    parse_aliases,
    service_key,
)

if TYPE_CHECKING:
    from pathlib import Path

_EXAMPLE = {
    "services": {
        "home-assistant": {"hostnames": ["ha.lan", "HA.example.com"]},
        "grafana-wyola": ["grafana.wyola.lan", "*.grafana.wyola.lan"],
    }
}


def test_parse_exact_and_glob_and_shorthand() -> None:
    aliases = parse_aliases(_EXAMPLE)
    assert aliases.exact == {
        "ha.lan": "home-assistant",
        "ha.example.com": "home-assistant",
        "grafana.wyola.lan": "grafana-wyola",
    }
    assert aliases.patterns == (("*.grafana.wyola.lan", "grafana-wyola"),)
    assert aliases.services == ["grafana-wyola", "home-assistant"]
    assert aliases.resolve("HA.LAN") == "home-assistant"
    assert aliases.resolve("api.grafana.wyola.lan") == "grafana-wyola"
    assert aliases.resolve("grafana.lan") is None


def test_parse_rejects_bad_shapes_and_conflicts() -> None:
    with pytest.raises(AliasError, match="services"):
        parse_aliases(["nope"])
    with pytest.raises(AliasError, match="non-empty list"):
        parse_aliases({"services": {"x": {"hostnames": []}}})
    with pytest.raises(AliasError, match="claimed by both"):
        parse_aliases({"services": {"a": ["h.lan"], "b": ["h.lan"]}})


def test_service_key_uses_aliases_then_leftmost_label() -> None:
    aliases = parse_aliases(_EXAMPLE)
    assert service_key("ha.example.com", aliases) == "home-assistant"
    assert service_key("nas.lan", aliases) == "nas"
    assert service_key("nas.lan") == "nas"
    assert service_key("api.grafana.wyola.lan", aliases) == "grafana-wyola"
    assert service_key("grafana.lan", aliases) == "grafana"


def test_load_from_env_and_none_when_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOMELAB_HELPER_SERVICE_ALIASES", raising=False)
    assert load_service_aliases() is None
    f = tmp_path / "aliases.yaml"
    f.write_text("services:\n  home-assistant:\n    hostnames: [ha.lan]\n")
    monkeypatch.setenv("HOMELAB_HELPER_SERVICE_ALIASES", str(f))
    loaded = load_service_aliases()
    assert loaded is not None
    assert loaded.source == f
    assert loaded.resolve("ha.lan") == "home-assistant"


def test_example_fixture_parses() -> None:
    from pathlib import Path as _P

    example = _P(__file__).resolve().parent.parent / "fixtures" / "service-aliases.example.yaml"
    loaded = load_service_aliases(example)
    assert loaded is not None
    assert loaded.resolve("ha.lan") == "home-assistant"


# ---------------------------------------------------------------------------
# reconcile integration
# ---------------------------------------------------------------------------


@pytest.fixture
async def engine():
    eng = make_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
def sessionmaker(engine):
    return make_sessionmaker(engine)


def _rec(hostname: str, ip: str) -> dict:
    return {"hostname": hostname, "value": ip, "record_type": "A", "enabled": True}


async def _services(s) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for svc in (await s.execute(select(Service))).scalars().all():
        eps = (
            (await s.execute(select(ServiceEndpoint).where(ServiceEndpoint.service_id == svc.id)))
            .scalars()
            .all()
        )
        out[svc.name] = {e.hostname for e in eps}
    return out


async def test_reconcile_names_services_through_the_alias_map(
    sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HOMELAB_HELPER_SERVICE_ALIASES", raising=False)
    aliases = parse_aliases(_EXAMPLE)
    async with session_scope(sessionmaker) as s:
        result = await reconcile_internal_endpoints(
            s, [_rec("ha.lan", "10.0.1.50"), _rec("nas.lan", "10.0.1.60")], aliases=aliases
        )
        assert sorted(result.created) == ["ha.lan", "nas.lan"]
        assert await _services(s) == {"home-assistant": {"ha.lan"}, "nas": {"nas.lan"}}


async def test_reconcile_repoints_existing_endpoints_when_the_map_changes(
    sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A map added after the fact moves the endpoint and cleans the orphaned service."""
    monkeypatch.delenv("HOMELAB_HELPER_SERVICE_ALIASES", raising=False)
    records = [_rec("ha.lan", "10.0.1.50"), _rec("nas.lan", "10.0.1.60")]
    async with session_scope(sessionmaker) as s:
        await reconcile_internal_endpoints(
            s, records, aliases=ServiceAliasMap(exact={}, patterns=())
        )
        assert await _services(s) == {"ha": {"ha.lan"}, "nas": {"nas.lan"}}

        result = await reconcile_internal_endpoints(s, records, aliases=parse_aliases(_EXAMPLE))
        assert result.moved == ["ha.lan"]
        assert result.unchanged == ["nas.lan"]
        assert result.changed == 1
        assert await _services(s) == {
            "home-assistant": {"ha.lan"},
            "nas": {"nas.lan"},
        }  # "ha" orphan removed


async def test_reconcile_reports_a_superseded_resolver_slice(
    sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Renaming a controller leaves the old slice behind; the new sync names it."""
    monkeypatch.delenv("HOMELAB_HELPER_SERVICE_ALIASES", raising=False)
    records = [_rec("ha.lan", "10.0.1.50"), _rec("nas.lan", "10.0.1.60")]
    async with session_scope(sessionmaker) as s:
        first = await reconcile_internal_endpoints(s, records, resolver="unifi")
        assert first.superseded_resolvers == []
        second = await reconcile_internal_endpoints(s, records, resolver="unifi:covington")
        assert second.superseded_resolvers == ["unifi"]
        # A different set at the same scope is not "superseded".
        third = await reconcile_internal_endpoints(
            s, [_rec("other.lan", "10.9.9.9")], resolver="unifi:wyola"
        )
        assert third.superseded_resolvers == []
