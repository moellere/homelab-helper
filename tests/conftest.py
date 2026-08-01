"""Shared pytest fixtures for the homelab-helper test suite.

The suite runs hermetically: ``.env`` loading is disabled before any harness
module is imported. Without this a run on an operator's workstation reads that
operator's real credentials — tests would pass or fail depending on whose
machine they ran on, and a test that monkeypatches an adapter factory is
silently bypassed whenever the ambient config names something real to build.
"""

from __future__ import annotations

import os

from homelab_helper.config import NO_DOTENV_VAR

# Set before the CLI/MCP entry points call load_env(); pytest imports conftest
# ahead of the test modules, so this lands first.
os.environ[NO_DOTENV_VAR] = "1"
