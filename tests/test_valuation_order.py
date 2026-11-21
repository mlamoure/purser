"""Of two valuations on one date, the later recorded wins (#565).

Same-date rows used to be ordered by their random UUID, so a loan whose feed
balance changed twice in a day kept an arbitrary one of the two. Every reader
now orders by (as_of, created_at, id) through networth.VALUATION_NEWEST_FIRST.
"""

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy.orm import Session

import purser.config
from tests.conftest import TEST_ENCRYPTION_KEY

TODAY = datetime.now(UTC).date()


def _loan(client: TestClient) -> str:
    resp = client.post(
        "/accounts",
        json={
            "name": "Synthetic Mortgage",
            "type": "loan",
            "path": f"liabilities.mortgage_{uuid.uuid4().hex[:8]}",
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _value(client: TestClient, account_id: str, value: str) -> None:
    resp = client.post(
        f"/accounts/{account_id}/valuations",
        json={"as_of": TODAY.isoformat(), "value": value},
    )
    assert resp.status_code == 201, resp.text


def _surfaces(client: TestClient, db: Session, account_id: str) -> list[Decimal]:
    """The balance every reader reports for the account today."""
    from purser import ledger as ledger_service
    from purser import networth as networth_service

    db.expire_all()
    account = ledger_service.get_account(db, uuid.UUID(account_id))
    series, _ = networth_service.account_balance_series(db, account, dates=[TODAY])
    (entry,) = [
        b
        for b in client.get("/networth", params={"as_of": TODAY.isoformat()}).json()["breakdown"]
        if b["account_id"] == account_id
    ]
    return [
        Decimal(client.get(f"/accounts/{account_id}").json()["balance"]),
        Decimal(client.get(f"/accounts/{account_id}/balance").json()["balance"]),
        Decimal(entry["value"]),
        series[TODAY],
        Decimal(client.get(f"/accounts/{account_id}/valuations").json()[0]["value"]),
    ]


def test_the_later_recorded_valuation_wins_on_every_surface(
    admin_client: TestClient, db_session: Session
) -> None:
    loan = _loan(admin_client)
    _value(admin_client, loan, "-1000.00")
    _value(admin_client, loan, "-900.00")
    assert _surfaces(admin_client, db_session, loan) == [Decimal("-900.00")] * 5


@settings(
    max_examples=15,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(st.lists(st.integers(min_value=1, max_value=10**8), min_size=2, max_size=6))
def test_any_sequence_on_one_date_answers_with_its_last_figure(
    admin_client: TestClient, db_session: Session, cents: list[int]
) -> None:
    loan = _loan(admin_client)
    figures = [-(Decimal(c) / 100) for c in cents]
    for figure in figures:
        _value(admin_client, loan, str(figure))
    assert _surfaces(admin_client, db_session, loan) == [figures[-1]] * 5


def test_rows_written_in_one_transaction_keep_their_order(db_session: Session) -> None:
    """created_at defaults to clock_timestamp(), not now(): now() is the
    transaction's start, which would tie every row of one transaction."""
    from purser import ledger as ledger_service
    from purser import networth as networth_service

    account = ledger_service.create_account(
        db_session,
        name="Synthetic Loan",
        type_="loan",
        path=f"liabilities.loan_{uuid.uuid4().hex[:8]}",
        actor_token_id=None,
    )
    for figure in ("-300.00", "-200.00", "-100.00"):
        networth_service.record_valuation(
            db_session,
            account_ref=str(account.id),
            as_of=TODAY,
            value=Decimal(figure),
            actor_token_id=None,
        )
    assert networth_service._latest_valuation(db_session, account.id, TODAY) == Decimal("-100.00")
    db_session.rollback()


# -- migration 0053 ----------------------------------------------------------

DB = "purser_valuation_order"


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
    command.upgrade(cfg, "0052")
    yield cfg, engine
    engine.dispose()
    purser.config.get_settings.cache_clear()


def test_migration_backfills_from_the_audit_row_else_the_date(
    scratch_db: tuple[Config, sa.Engine],
) -> None:
    cfg, engine = scratch_db
    audited_at = datetime(2026, 3, 4, 15, 30, tzinfo=UTC)
    day = TODAY - timedelta(days=3)
    with engine.begin() as conn:
        account = conn.execute(
            sa.text(
                "INSERT INTO accounts (name, type, path) "
                "VALUES ('Synthetic Loan', 'loan', 'liabilities.synthetic') RETURNING id"
            )
        ).scalar_one()
        manual, feed = (
            conn.execute(
                sa.text(
                    "INSERT INTO valuations (account_id, as_of, value, source) "
                    "VALUES (:a, :d, :v, :s) RETURNING id"
                ),
                {"a": account, "d": day, "v": value, "s": source},
            ).scalar_one()
            for value, source in (("-10.0000", "manual"), ("-20.0000", "api"))
        )
        conn.execute(
            sa.text(
                "INSERT INTO audit_log (at, action, entity_type, entity_id) "
                "VALUES (:at, 'valuation.create', 'valuation', :e)"
            ),
            {"at": audited_at, "e": manual},
        )
    command.upgrade(cfg, "0053")
    with engine.connect() as conn:
        stamps = {
            row.id: row.created_at
            for row in conn.execute(sa.text("SELECT id, created_at FROM valuations"))
        }
    assert stamps[manual] == audited_at
    assert stamps[feed] == datetime(day.year, day.month, day.day, tzinfo=UTC)
    command.downgrade(cfg, "0052")
