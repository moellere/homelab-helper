"""Alembic configuration built from the installed package, not a checkout.

``helper db init`` used to walk up from this file looking for ``alembic.ini``,
which only exists in a source checkout — an installed wheel has no project
root, so the first command a fresh install ran failed. The migration scripts
now ship inside the package (``homelab_helper/migrations``) and the
:class:`alembic.config.Config` is assembled here in code. The root
``alembic.ini`` remains for developers who drive the ``alembic`` CLI directly;
it points at the same script directory.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from alembic.config import Config

from homelab_helper.config import database_url

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
"""The packaged Alembic script directory (``env.py`` + ``versions/``)."""


def alembic_config(url: str | None = None) -> Config:
    """An Alembic ``Config`` pointed at the packaged migrations.

    ``url`` defaults to :func:`database_url`; ``env.py`` reads it back from the
    ``sqlalchemy.url`` option, so callers never need an ini file on disk.
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("path_separator", "os")
    cfg.set_main_option("sqlalchemy.url", url or database_url())
    return cfg


def enable_alembic_logging(level: int = logging.INFO) -> None:
    """Send Alembic's progress lines to stderr, as the ini's logger block did."""
    logger = logging.getLogger("alembic")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)-5.5s [%(name)s] %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(level)


__all__ = ["MIGRATIONS_DIR", "alembic_config", "enable_alembic_logging"]
