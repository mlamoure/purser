"""One ledger day: the household time zone setting (issue #471, decision 105).

The clock is pinned at 04:30 UTC on 2026-11-09, which is 23:30 on 2026-11-08
in New York: the evening hour where a host-local "today" and a UTC "today"
used to disagree. With ``timezone = America/New_York`` every surface must
answer for 11-08; with the default (UTC) every surface must answer for 11-09,
exactly as before.
"""

import os
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

import purser.config
from purser import appsettings, clock, crypto, digest, matching
from purser.cli import _record_env_timezone
from purser.db import get_sessionmaker

INSTANT = datetime(2026, 11, 9, 4, 30, tzinfo=UTC)
NY_DAY = date(2026, 11, 8)
UTC_DAY = date(2026, 11, 9)
NEW_YORK = "America/New_York"


@pytest.fixture(autouse=True)
def _zone_left_alone(migrated_engine: Engine) -> Iterator[None]:
    """Tests elsewhere share the database without a reset (``db_session``):
    a time zone override left behind would move their "today"."""
    yield
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM app_settings WHERE key = 'timezone'"))
    appsettings.invalidate()


@pytest.fixture
def frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clock, "_utc_now", lambda: INSTANT)


@pytest.fixture
def world(admin_client: TestClient, frozen: None) -> TestClient:
    for name, type_, path in [
        ("Checking", "checking", "assets.checking"),
        ("Housing", "category_expense", "expenses.housing"),
    ]:
        route = "/categories" if type_.startswith("category") else "/accounts"
        resp = admin_client.post(route, json={"name": name, "type": type_, "path": path})
        assert resp.status_code == 201, resp.text
    resp = admin_client.post(
        "/transactions",
        json={
            "account": "assets.checking",
            "category": "expenses.housing",
            "amount": "500.00",
            "date": "2026-10-01",
            "payee": "Synthetic Employer",
            "status": "posted",
        },
    )
    assert resp.status_code == 201, resp.text
    return admin_client


def _set_zone(client: TestClient, zone: str) -> None:
    resp = client.put("/config", json={"timezone": zone})
    assert resp.status_code == 200, resp.text
    assert resp.json()["values"]["timezone"] == zone


def _expected_bill(client: TestClient, on: date) -> str:
    resp = client.post(
        "/transactions",
        json={
            "account": "assets.checking",
            "category": "expenses.housing",
            "amount": "-75.00",
            "date": on.isoformat(),
            "payee": "Synthetic Utility",
            "status": "expected",
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _recurring(engine: Engine, day: date) -> None:
    """Three monthly import rows on the 15th before ``day`` (detector food)."""
    with engine.begin() as conn:
        checking, housing = (
            conn.execute(text("SELECT id FROM accounts WHERE path = :p"), {"p": p}).scalar_one()
            for p in ("assets.checking", "expenses.housing")
        )
        payee = conn.execute(
            text("INSERT INTO payees (name_enc, name_bidx) VALUES (:ne, :nb) RETURNING id"),
            {
                "ne": crypto.encrypt("Synthetic Stream"),
                "nb": crypto.blind_index("synthetic stream"),
            },
        ).scalar_one()
        for months_ago in (3, 2, 1):
            m = day.month - months_ago
            y = day.year + (m - 1) // 12
            m = (m - 1) % 12 + 1
            tx = conn.execute(
                text(
                    "INSERT INTO transactions (date, payee_id, description_enc, status, origin)"
                    " VALUES (:d, :p, :desc, 'posted', 'import') RETURNING id"
                ),
                {"d": date(y, m, 15), "p": payee, "desc": crypto.encrypt("SYNTHETIC STREAM")},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO postings (transaction_id, account_id, amount)"
                    " VALUES (:tx, :a, -9.99), (:tx, :c, 9.99)"
                ),
                {"tx": tx, "a": checking, "c": housing},
            )


@pytest.mark.parametrize(
    ("zone", "ledger_day"),
    [(NEW_YORK, NY_DAY), ("UTC", UTC_DAY)],
    ids=["new-york-evening", "utc-default"],
)
def test_every_surface_agrees_on_the_ledger_day(
    world: TestClient, migrated_engine: Engine, zone: str, ledger_day: date
) -> None:
    client = world
    if zone != "UTC":
        _set_zone(client, zone)
    else:
        # The default needs no setting: an untouched install keeps the UTC day.
        assert client.get("/config").json()["values"]["timezone"] == "UTC"
    assert clock.ledger_today() == ledger_day

    # Schedules: the default anchor and the next due date are the ledger day.
    resp = client.post(
        "/schedules",
        json={
            "name": "Synthetic Daily",
            "account": "assets.checking",
            "rrule": "FREQ=DAILY",
            "amount_type": "fixed",
            "amount": "-1.00",
            "category": "expenses.housing",
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["dtstart"] == ledger_day.isoformat()
    assert resp.json()["next_due"] == ledger_day.isoformat()

    # Matching: a bill expected on 11-08 is due today in New York, a day late in UTC.
    bill_id = _expected_bill(client, NY_DAY)
    with get_sessionmaker()() as session:
        rows = {
            str(r["transaction_id"]): r
            for r in matching.list_unmatched(session, direction="expected")
        }
        assert rows[bill_id]["aging_days"] == (ledger_day - NY_DAY).days

        # Digest: the week's bills start today, so the 11-08 bill is still to come
        # in New York and already past in UTC.
        upcoming = digest.whats_new(session)["upcoming"]
        listed = {str(item["transaction_id"]) for item in upcoming["items"]}
        assert (bill_id in listed) is (ledger_day == NY_DAY)

    # Net worth: "today" is the ledger day.
    worth = client.get("/networth")
    assert worth.status_code == 200, worth.text
    assert worth.json()["as_of"] == ledger_day.isoformat()

    # Reconciliation: 11-09 is the future in New York, today in UTC.
    checking = client.get("/accounts").json()
    checking_id = next(a["id"] for a in checking if a["path"] == "assets.checking")
    resp = client.post(
        f"/accounts/{checking_id}/reconcile",
        json={"as_of": UTC_DAY.isoformat(), "statement_balance": "0.00", "dry_run": True},
    )
    if ledger_day == NY_DAY:
        assert resp.status_code == 422
        assert "future" in resp.json()["detail"]
    else:
        assert resp.status_code == 200, resp.text

    # Suggestions: a default snooze runs 30 ledger days.
    _recurring(migrated_engine, ledger_day)
    detected = client.post("/schedules/detect")
    assert detected.status_code == 200, detected.text
    sid = detected.json()["suggestions"][0]["suggestion_id"]
    snoozed = client.post(f"/suggestions/{sid}/decide", json={"decision": "snooze"})
    assert snoozed.status_code == 200, snoozed.text
    until = date.fromisoformat(snoozed.json()["suggestion"]["snooze_until"])
    assert until == ledger_day + timedelta(days=30)


def test_put_config_rejects_an_unknown_zone(admin_client: TestClient) -> None:
    for bad in ("Mars/Olympus_Mons", "EST5EDT-nope", "", "new york"):
        resp = admin_client.put("/config", json={"timezone": bad})
        assert resp.status_code == 422, (bad, resp.text)
    resp = admin_client.put("/config", json={"timezone": None})
    assert resp.status_code == 422
    assert admin_client.get("/config").json()["values"]["timezone"] == "UTC"


def test_the_setting_moves_the_ledger_day_without_a_restart(
    admin_client: TestClient, frozen: None, migrated_engine: Engine
) -> None:
    assert clock.ledger_today() == UTC_DAY
    _set_zone(admin_client, NEW_YORK)
    assert clock.ledger_today() == NY_DAY
    assert "timezone" in admin_client.get("/config").json()["overridden"]
    _set_zone(admin_client, "Asia/Tokyo")
    assert clock.ledger_today() == date(2026, 11, 9)
    assert clock.ledger_now().hour == 13  # 04:30 UTC is 13:30 in Tokyo
    reset = admin_client.delete("/config/timezone")
    assert reset.status_code == 200, reset.text
    assert reset.json()["values"]["timezone"] == "UTC"
    assert clock.ledger_today() == UTC_DAY
    # Audited through the config path like every other setting.
    with migrated_engine.connect() as conn:
        actions = conn.execute(
            text(
                "SELECT action FROM audit_log WHERE entity_type = 'config'"
                " AND kind = 'change' ORDER BY at, id"
            )
        ).scalars()
        assert list(actions) == ["config.update", "config.update", "config.reset"]


def test_an_instant_lands_on_its_household_day(admin_client: TestClient) -> None:
    _set_zone(admin_client, NEW_YORK)
    assert clock.ledger_date(INSTANT) == NY_DAY
    assert clock.ledger_date(INSTANT.replace(tzinfo=None)) == NY_DAY  # naive = UTC
    appsettings.invalidate()


def test_readmodel_ledger_day_follows_the_setting(
    admin_client: TestClient, migrated_engine: Engine
) -> None:
    def sql_day(conn: Any, zone: str) -> tuple[date, date]:
        got = conn.execute(text("SELECT readmodel.ledger_today()")).scalar_one()
        want = conn.execute(text("SELECT (now() AT TIME ZONE :z)::date"), {"z": zone}).scalar_one()
        return got, want

    with migrated_engine.connect() as conn:
        got, want = sql_day(conn, "UTC")
        assert got == want
    _set_zone(admin_client, "Pacific/Kiritimati")  # UTC+14: always a day ahead of UTC
    admin_client.post(
        "/accounts", json={"name": "Checking", "type": "checking", "path": "assets.checking"}
    )
    with migrated_engine.connect() as conn:
        got, want = sql_day(conn, "Pacific/Kiritimati")
        assert got == want
        first = conn.execute(text("SELECT min(day) FROM readmodel.v1_forecast_daily")).scalar_one()
        assert first == want


@pytest.fixture
def env_zone(monkeypatch: pytest.MonkeyPatch, migrated_engine: Engine) -> Iterator[str]:
    """PURSER_TIMEZONE set for this test, mirrored as a startup would."""
    monkeypatch.setenv("PURSER_TIMEZONE", "Pacific/Kiritimati")
    purser.config.get_settings.cache_clear()
    appsettings.invalidate()
    with migrated_engine.connect() as conn:
        _record_env_timezone(conn)
        conn.commit()
    yield "Pacific/Kiritimati"
    monkeypatch.delenv("PURSER_TIMEZONE")
    purser.config.get_settings.cache_clear()
    appsettings.invalidate()
    with migrated_engine.connect() as conn:
        _record_env_timezone(conn)
        conn.commit()
        assert conn.execute(text("SELECT env_timezone FROM ledger_clock")).scalar_one() == "UTC"


def test_env_default_reaches_app_and_readmodel(
    admin_client: TestClient, frozen: None, env_zone: str, migrated_engine: Engine
) -> None:
    assert os.environ["PURSER_TIMEZONE"] == env_zone
    assert admin_client.get("/config").json()["values"]["timezone"] == env_zone
    assert clock.ledger_today() == date(2026, 11, 9)  # 18:30 on 11-09 at UTC+14
    with migrated_engine.connect() as conn:
        got = conn.execute(text("SELECT readmodel.ledger_today()")).scalar_one()
        want = conn.execute(text("SELECT (now() AT TIME ZONE :z)::date"), {"z": env_zone})
        assert got == want.scalar_one()
    # An override still wins over the env default, in the app and in SQL.
    _set_zone(admin_client, "UTC")
    assert clock.ledger_today() == UTC_DAY
    with migrated_engine.connect() as conn:
        got = conn.execute(text("SELECT readmodel.ledger_today()")).scalar_one()
        assert got == conn.execute(text("SELECT (now() AT TIME ZONE 'UTC')::date")).scalar_one()


def test_an_invalid_env_zone_fails_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PURSER_TIMEZONE", "Not/AZone")
    with pytest.raises(ValueError, match="IANA"):
        purser.config.Settings()


def test_migration_0044_round_trips(database_url: str) -> None:
    """0044 swaps CURRENT_DATE for readmodel.ledger_today() and back, on a
    scratch database so the suite's migrated database is never touched."""
    import sqlalchemy as sa
    from alembic import command
    from alembic.config import Config

    from tests.conftest import TEST_ENCRYPTION_KEY

    os.environ["PURSER_ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
    purser.config.get_settings.cache_clear()
    admin = sa.create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text("DROP DATABASE IF EXISTS purser_ledger_day WITH (FORCE)"))
        conn.execute(sa.text("CREATE DATABASE purser_ledger_day"))
    admin.dispose()
    scratch_url = database_url.rsplit("/", 1)[0] + "/purser_ledger_day"
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", scratch_url)
    engine = sa.create_engine(scratch_url)

    def view_sql() -> str:
        with engine.connect() as conn:
            return str(
                conn.execute(
                    sa.text("SELECT pg_get_viewdef('readmodel.v1_forecast_daily'::regclass)")
                ).scalar_one()
            )

    try:
        command.upgrade(cfg, "0044")
        assert "ledger_today()" in view_sql()
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT env_timezone FROM ledger_clock")).scalar_one() == (
                "UTC"
            )
        command.downgrade(cfg, "0043")
        assert "CURRENT_DATE" in view_sql()
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT to_regclass('ledger_clock')")).scalar_one() is None
        command.upgrade(cfg, "0044")
        assert "ledger_today()" in view_sql()
    finally:
        engine.dispose()


def test_a_read_token_reads_the_ledger_day(
    admin_client: TestClient, frozen: None, migrated_engine: Engine
) -> None:
    """GET /clock (#496 review): a client that dates a write needs the
    server's "today" — the Positions form refused its own default date every
    evening west of UTC when it used the UTC date. Read scope is enough."""
    token = admin_client.post("/tokens", json={"name": "r", "scopes": ["read"]}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert admin_client.get("/clock", headers=headers).json() == {
        "ledger_today": UTC_DAY.isoformat(),
        "timezone": "UTC",
    }
    _set_zone(admin_client, NEW_YORK)
    assert admin_client.get("/clock", headers=headers).json() == {
        "ledger_today": NY_DAY.isoformat(),
        "timezone": NEW_YORK,
    }
