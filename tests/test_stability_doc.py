"""docs/stability.md names every public surface — mechanically (Phase 9.4).

The page is the contract; this keeps its CLI and MCP tables from drifting
behind the code. A new verb or tool is not shipped until the page says what
is promised about it.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import click

from homelab_helper.cli.main import click_app
from homelab_helper.mcp_server import server

DOC = Path(__file__).resolve().parent.parent / "docs" / "stability.md"


def _cli_rows(text: str) -> dict[str, str]:
    """``{group: cell}`` from the CLI table — the row's second column."""
    return dict(re.findall(r"^\| `([a-z-]+)` \| (.*?) \|$", text, flags=re.MULTILINE))


def test_every_cli_group_and_verb_is_declared() -> None:
    text = DOC.read_text()
    rows = _cli_rows(text)
    for group, command in sorted(click_app.commands.items()):
        assert group in rows, f"CLI group {group!r} is missing from docs/stability.md"
        verbs = sorted(command.commands) if isinstance(command, click.Group) else []
        for verb in verbs:
            assert f"`{verb}`" in rows[group], f"{group} {verb} is missing from its row"
        if not verbs:
            assert rows[group].strip() == "—", f"{group} takes no verbs but its row says otherwise"


def test_every_mcp_tool_is_declared() -> None:
    text = DOC.read_text()
    names = sorted(t.name for t in asyncio.run(server.list_tools()))
    assert names, "no MCP tools enumerated"
    for name in names:
        assert f"`{name}`" in text, f"MCP tool {name!r} is missing from docs/stability.md"


def test_the_declared_python_versions_match_the_classifiers() -> None:
    text = DOC.read_text()
    pyproject = (DOC.parent.parent / "pyproject.toml").read_text()
    classified = set(re.findall(r"Programming Language :: Python :: (3\.\d+)", pyproject))
    declared = set(re.findall(r"\*\*(3\.\d+) and (3\.\d+)\.\*\*", text)[0])
    assert declared == classified
