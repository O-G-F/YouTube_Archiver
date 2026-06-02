"""Schema creation and built-in data seeding.

- ``create_all`` builds the schema directly from the models. This is used for
  SQLite (local CLI / tests) and as a convenience; PostgreSQL deployments use
  Alembic migrations instead (``alembic upgrade head``).
- ``seed`` idempotently writes the built-in download profiles.
"""

from __future__ import annotations

from app.config import get_settings
from app.db import get_engine, session_scope
from app.logging_setup import get_logger
from app.models import Base
from app.services.profiles import seed_builtin_profiles

logger = get_logger(__name__)


def create_all() -> None:
    Base.metadata.create_all(get_engine())


def seed() -> int:
    with session_scope() as session:
        return seed_builtin_profiles(session)


def init(create_schema: bool = True) -> int:
    settings = get_settings()
    settings.ensure_dirs()
    if create_schema:
        create_all()
    written = seed()
    logger.info("init complete (profiles written/updated: %s)", written)
    return written


def startup_bootstrap() -> None:
    """Run at web startup: ensure dirs, create SQLite schema, seed profiles."""
    settings = get_settings()
    settings.ensure_dirs()
    if settings.database_url.startswith("sqlite"):
        create_all()
    try:
        seed()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "startup: profile seeding skipped (have migrations been run? %s)", exc
        )
