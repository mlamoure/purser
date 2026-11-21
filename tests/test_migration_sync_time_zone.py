"""Migration 0051 (issue #556, decision 111): the daily sync time stored in
UTC becomes the same instant as a household-zone clock time, so the time the
Settings page shows today does not change. Runs against a scratch database in
the session's Postgres container."""

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

import purser.config
from tests.conftest import TEST_ENCRYPTION_KEY

DB = "purser_sync_zone"


@pytest.fixture
def scratch_db(database_url: str) -> Iterator[tuple[Config, sa.Engine]]:
    os.environ["PURSER_ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
    purser.config.get_settings.cache_clear()
    admin = sa.create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)"))
        conn.execute(sa.text(f"CREATE DATABASE {DB}"))
    admin.dispose()
    scratch_url = database_url.rsplit("/", 1)[0] + f"/{DB}"
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", scratch_url)
    engine = sa.create_engine(scratch_url)
    command.upgrade(cfg, "0050")
    yield cfg, engine
    engine.dispose()
    purser.config.get_settings.cache_clear()


def _set(engine: sa.Engine, key: str, value: object) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO app_settings (key, value) VALUES (:k, CAST(:v AS jsonb))"),
            {"k": key, "v": json.dumps(value)},
        )


def _schedule(engine: sa.Engine) -> object:
    with engine.connect() as conn:
        return conn.execute(
            sa.text("SELECT value FROM app_settings WHERE key = 'sync_schedule'")
        ).scalar()


def _local(hour: int, minute: int, zone: str) -> tuple[int, int]:
    """Today's local clock reading of a UTC time of day."""
    today = datetime.now(UTC).date()
    at = datetime(today.year, today.month, today.day, hour, minute, tzinfo=UTC)
    moved = at.astimezone(ZoneInfo(zone))
    return moved.hour, moved.minute


def test_stored_daily_time_keeps_its_instant(scratch_db: tuple[Config, sa.Engine]) -> None:
    cfg, engine = scratch_db
    _set(engine, "timezone", "America/New_York")
    _set(engine, "sync_schedule", {"preset": "daily", "hour": 11, "minute": 30})
    command.upgrade(cfg, "0051")
    hour, minute = _local(11, 30, "America/New_York")
    assert _schedule(engine) == {"preset": "daily", "hour": hour, "minute": minute}
    command.downgrade(cfg, "0050")
    assert _schedule(engine) == {"preset": "daily", "hour": 11, "minute": 30}


def test_default_schedule_becomes_explicit_outside_utc(
    scratch_db: tuple[Config, sa.Engine], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, engine = scratch_db
    monkeypatch.setenv("PURSER_TIMEZONE", "Asia/Kolkata")  # the env zone, no override
    purser.config.get_settings.cache_clear()
    command.upgrade(cfg, "0051")
    assert _schedule(engine) == {"preset": "daily", "hour": 12, "minute": 0}  # 06:30 UTC


def test_utc_and_interval_presets_are_untouched(scratch_db: tuple[Config, sa.Engine]) -> None:
    cfg, engine = scratch_db
    command.upgrade(cfg, "0051")  # UTC household, no stored schedule
    assert _schedule(engine) is None
    command.downgrade(cfg, "0050")
    _set(engine, "timezone", "Europe/Berlin")
    _set(engine, "sync_schedule", {"preset": "every_6h"})
    command.upgrade(cfg, "0051")
    assert _schedule(engine) == {"preset": "every_6h"}
