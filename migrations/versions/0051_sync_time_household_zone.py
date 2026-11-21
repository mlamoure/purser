"""The daily sync time is a household-zone clock time (#556, decision 111).

Until now ``app_settings.sync_schedule``'s daily ``hour``/``minute`` were a
UTC time, so a household on daylight-saving time saw the sync move by an hour
on the clock twice a year. From this revision they are a wall-clock time in
the household time zone and the scheduler reads the daily trigger in that
zone.

The upgrade converts the stored time with today's offset, so the time the
Settings page shows today does not change. An install with no stored
schedule ran at the default 06:30 UTC; when its household zone is not UTC it
gets that instant as an explicit local time, so its sync does not move
either. An install in UTC, or on an interval preset, is left as it is. The
downgrade converts back with the offset of the day it runs.

A setting written by a migration is not audited: audit rows record who
changed a value, and nobody did.
"""

import json
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None

KEY = "sync_schedule"
DEFAULT = {"preset": "daily", "hour": 6, "minute": 30}


def _household_zone(bind: sa.Connection) -> ZoneInfo:
    """The effective household zone: the runtime override, else the env."""
    row = bind.execute(sa.text("SELECT value FROM app_settings WHERE key = 'timezone'")).first()
    if row is not None and isinstance(row.value, str):
        return ZoneInfo(row.value)
    from purser.config import get_settings

    return ZoneInfo(get_settings().timezone)


def _stored(bind: sa.Connection) -> dict[str, object] | None:
    row = bind.execute(sa.text("SELECT value FROM app_settings WHERE key = :k"), {"k": KEY}).first()
    return None if row is None else dict(row.value)


def _write(bind: sa.Connection, value: dict[str, object]) -> None:
    bind.execute(
        sa.text(
            "INSERT INTO app_settings (key, value) VALUES (:k, CAST(:v AS jsonb))"
            " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"
        ),
        {"k": KEY, "v": json.dumps(value)},
    )


def _convert(value: dict[str, object], src: ZoneInfo, dst: ZoneInfo) -> dict[str, object]:
    """``value``'s daily time read in ``src`` on today's date, as ``dst`` reads it."""
    today = datetime.now(UTC).astimezone(src).date()
    at = time(int(value["hour"]), int(value["minute"]))  # type: ignore[call-overload]
    moved = datetime.combine(today, at, tzinfo=src).astimezone(dst)
    return {**value, "hour": moved.hour, "minute": moved.minute}


def upgrade() -> None:
    bind = op.get_bind()
    zone = _household_zone(bind)
    utc = ZoneInfo("UTC")
    stored = _stored(bind)
    if zone.key == "UTC":
        return
    if stored is None:
        _write(bind, _convert(DEFAULT, utc, zone))
    elif stored.get("preset") == "daily" and stored.get("hour") is not None:
        _write(bind, _convert(stored, utc, zone))


def downgrade() -> None:
    bind = op.get_bind()
    zone = _household_zone(bind)
    stored = _stored(bind)
    if zone.key == "UTC" or stored is None:
        return
    if stored.get("preset") == "daily" and stored.get("hour") is not None:
        _write(bind, _convert(stored, zone, ZoneInfo("UTC")))
