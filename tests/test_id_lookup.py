"""Rows are found by id prefix in the database, on SQLite and Postgres alike.

Postgres renders a uuid with hyphens and SQLite stores bare hex, so the
lookup strips hyphens on both sides; a prefix that cannot be part of an id
matches nothing rather than reaching the ``LIKE`` as a wildcard.
"""

from __future__ import annotations

import io
import uuid

import pytest
from rich.console import Console

from homelab_helper.cli._console import RedactingConsole
from homelab_helper.db.lookup import id_prefix, rows_by_id_prefix
from homelab_helper.db.models import Host
from homelab_helper.db.session import session_scope
from homelab_helper.secrets import forget_secrets, resolve_secret
from tests.test_node_update import sessionmaker_  # noqa: F401 - fixture


@pytest.mark.parametrize(
    ("text", "bare"),
    [
        ("0196F2AB", "0196f2ab"),
        ("0196f2ab-12", "0196f2ab12"),
        ("%", None),
        ("a_b", None),
        ("", ""),
    ],
)
def test_id_prefix_normalises_or_refuses(text: str, bare: str | None) -> None:
    assert id_prefix(text) == bare


async def test_a_prefix_finds_one_row_and_a_shared_one_finds_two(sessionmaker_) -> None:  # noqa: F811
    async with session_scope(sessionmaker_) as s:
        s.add_all(
            [
                Host(id=uuid.UUID("0196f2ab-1234-7000-8000-00000000000a"), hostname="a"),
                Host(id=uuid.UUID("0196f2ab-1299-7000-8000-00000000000b"), hostname="b"),
            ]
        )
        await s.flush()

        async def names(prefix: str) -> list[str]:
            return sorted(h.hostname for h in await rows_by_id_prefix(s, Host, prefix))

        assert await names("0196f2ab-1234-7000-8000-00000000000a") == ["a"]
        assert await names("0196F2AB-1234") == ["a"]  # crosses a hyphen, any case
        assert await names("0196f2ab") == ["a", "b"]  # ambiguous: the caller asks for more
        assert await names("%") == []
        assert await names("zzzz") == []


def test_the_cli_console_masks_resolved_secrets(monkeypatch) -> None:
    forget_secrets()
    monkeypatch.setenv("PVE_TOKEN", "s3cr3t-token-value")
    resolve_secret("env:PVE_TOKEN")
    out = io.StringIO()
    RedactingConsole(file=out, width=200).print("proxmox unreachable: 401 for s3cr3t-token-value")
    assert "s3cr3t" not in out.getvalue()
    assert "***" in out.getvalue()
    plain = io.StringIO()
    Console(file=plain, width=200).print("no secret here")
    assert plain.getvalue().strip() == "no secret here"
