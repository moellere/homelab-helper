"""Smoke tests — the absolute minimum that proves the package installed.

These tests should never break. If they do, something is wrong with the
package layout, the build system, or the entry points — not with feature code.
"""

from __future__ import annotations

import subprocess
import sys


def test_package_importable() -> None:
    """The top-level package imports and exposes a version string."""

    import homelab_helper

    assert isinstance(homelab_helper.__version__, str)
    assert homelab_helper.__version__  # non-empty


def test_submodules_importable() -> None:
    """Every Phase-1 submodule imports without error.

    Catches missing __init__.py files, circular imports, and stray syntax
    errors in the scaffold before they bite us later.
    """

    import homelab_helper.adapters  # noqa: F401
    import homelab_helper.adapters.base  # noqa: F401
    import homelab_helper.api  # noqa: F401
    import homelab_helper.cli  # noqa: F401
    import homelab_helper.cli.main  # noqa: F401
    import homelab_helper.db  # noqa: F401
    import homelab_helper.db.base  # noqa: F401
    import homelab_helper.db.models  # noqa: F401
    import homelab_helper.db.session  # noqa: F401
    import homelab_helper.engine  # noqa: F401
    import homelab_helper.engine.fingerprint  # noqa: F401
    import homelab_helper.probes  # noqa: F401
    import homelab_helper.probes.base  # noqa: F401


def test_base_utilities() -> None:
    """``uuid7`` and ``now`` return values of the expected shape."""

    from datetime import datetime

    from homelab_helper.db.base import now, uuid7

    u = uuid7()
    assert u.version == 7
    # UUIDv7 is timestamp-prefixed; calling twice should produce different values.
    assert uuid7() != u

    t = now()
    assert isinstance(t, datetime)
    assert t.tzinfo is not None  # must be tz-aware UTC


def test_fingerprint_is_deterministic_and_short() -> None:
    """``make_fingerprint`` produces a stable 16-char hex string."""

    from homelab_helper.engine.fingerprint import make_fingerprint

    fp1 = make_fingerprint("config-drift", "host", "bmax0", "e1000e-offload-disabled")
    fp2 = make_fingerprint("config-drift", "host", "bmax0", "e1000e-offload-disabled")
    assert fp1 == fp2
    assert len(fp1) == 16
    assert all(c in "0123456789abcdef" for c in fp1)

    fp_other = make_fingerprint("config-drift", "host", "bmax1", "e1000e-offload-disabled")
    assert fp_other != fp1


def test_cli_version_runs() -> None:
    """``helper version`` exits 0 and prints something containing the version."""

    result = subprocess.run(
        [sys.executable, "-m", "homelab_helper.cli.main", "version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "homelab-helper" in result.stdout
