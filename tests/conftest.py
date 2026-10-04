"""Shared pytest fixtures for the homelab-helper test suite.

The suite runs hermetically: ``.env`` loading is disabled before any harness
module is imported. Without this a run on an operator's workstation reads that
operator's real credentials — tests would pass or fail depending on whose
machine they ran on, and a test that monkeypatches an adapter factory is
silently bypassed whenever the ambient config names something real to build.
"""

from __future__ import annotations

import os
import tempfile

from homelab_helper.config import HOME_VAR, NO_DOTENV_VAR

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
