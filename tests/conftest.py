"""Shared pytest fixtures for the homelab-helper test suite.

The suite runs hermetically: ``.env`` loading is disabled before any harness
module is imported. Without this a run on an operator's workstation reads that
operator's real credentials — tests would pass or fail depending on whose
machine they ran on, and a test that monkeypatches an adapter factory is
silently bypassed whenever the ambient config names something real to build.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import TYPE_CHECKING

import pytest

from homelab_helper.config import HOME_VAR, NO_DOTENV_VAR

if TYPE_CHECKING:
    from collections.abc import Iterator

# The operator's shell exports HOMELAB_HELPER_* from ~/.env; a test that builds
# an adapter from the environment would then reach a real controller or NAS.
# Only the two variables this file sets survive. The Anthropic key is also read
# under its SDK name, so it goes too.
for _name in [k for k in os.environ if k.startswith("HOMELAB_HELPER_")]:
    if _name not in {HOME_VAR, NO_DOTENV_VAR}:
        del os.environ[_name]
os.environ.pop("ANTHROPIC_API_KEY", None)

# Set before the CLI/MCP entry points call load_env(); pytest imports conftest
# ahead of the test modules, so this lands first.
os.environ[NO_DOTENV_VAR] = "1"

# Point the per-user data/config directories at a throwaway location so a test
# that never sets HOMELAB_HELPER_DATABASE_URL can't touch the operator's real
# database or config file.
os.environ[HOME_VAR] = tempfile.mkdtemp(prefix="homelab-helper-tests-")


class _NoImplicitLoopPolicy(asyncio.DefaultEventLoopPolicy):
    """``get_event_loop()`` never conjures a loop.

    pytest-asyncio remembers the "old" loop before installing its runner by
    calling ``asyncio.get_event_loop()``. After a CliRunner test, ``asyncio.run``
    has left the main thread with no loop, and Python 3.12's default policy
    answers by *creating* one — which pytest-asyncio stores, restores and never
    closes. Under ``filterwarnings = error`` that unclosed loop (and its
    self-pipe sockets) surfaces as a random test error. Raising here takes the
    ``except RuntimeError: old_loop = None`` branch instead.
    """

    def get_event_loop(self) -> asyncio.AbstractEventLoop:
        loop = self._local._loop  # type: ignore[attr-defined]
        if loop is None:
            raise RuntimeError("no current event loop (tests never create one implicitly)")
        return loop


@pytest.fixture(scope="session")
def event_loop_policy() -> Iterator[asyncio.AbstractEventLoopPolicy]:
    policy = _NoImplicitLoopPolicy()
    previous = asyncio.get_event_loop_policy()
    asyncio.set_event_loop_policy(policy)
    try:
        yield policy
    finally:
        asyncio.set_event_loop_policy(previous)
