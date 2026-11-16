"""The CLI starts without importing the MCP server.

``mcp_server`` builds its server object and loads ``.env`` at import; before
discovery moved to ``engine/discoverers.py`` every ``helper`` command paid for
that, ``helper --help`` included. Only ``helper mcp ...`` should.
"""

from __future__ import annotations

import subprocess
import sys


def test_importing_the_cli_does_not_import_the_mcp_server() -> None:
    code = (
        "import sys, homelab_helper.cli.main\n"
        "loaded = sorted(m for m in sys.modules if m == 'homelab_helper.mcp_server' or m == 'mcp')\n"
        "print(','.join(loaded))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == ""


def test_the_engine_never_imports_an_interface() -> None:
    code = (
        "import sys, homelab_helper.engine.discoverers, homelab_helper.adapters.factories\n"
        "bad = sorted(m for m in sys.modules if m.startswith(('homelab_helper.cli', "
        "'homelab_helper.mcp_server')))\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == ""
