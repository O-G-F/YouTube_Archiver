"""Test fixtures.

Tests run entirely on a temporary file-based SQLite DB and temp storage roots,
so no PostgreSQL or Redis is required.
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.db import get_session_factory, reset_engine_for_tests


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    logs = tmp_path / "logs"
    config = tmp_path / "config"
    db_file = tmp_path / "test.db"

    monkeypatch.setenv("ARCHIVE_ROOT", str(archive))
    monkeypatch.setenv("LOG_ROOT", str(logs))
    monkeypatch.setenv("CONFIG_ROOT", str(config))
    monkeypatch.setenv("TAKEOUT_IMPORT_ROOT", str(tmp_path / "takeout"))
    monkeypatch.setenv("OBSIDIAN_EXPORT_ROOT", str(tmp_path / "obsidian"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6399/0")  # intentionally dead
    monkeypatch.setenv("COOKIES_FILE", "")

    get_settings.cache_clear()
    s = get_settings()
    s.ensure_dirs()
    reset_engine_for_tests(s.database_url)

    from app.bootstrap import create_all

    create_all()

    yield s

    get_settings.cache_clear()


@pytest.fixture()
def session(settings):
    sess = get_session_factory()()
    try:
        yield sess
    finally:
        sess.rollback()
        sess.close()
