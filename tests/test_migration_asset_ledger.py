"""Migration 0054 (#566, decision 113): a property account's stored valuations
become entries against equity.revaluation, so its value on every valuation
date is what that valuation said; a closed account still reads 0 from its
close date on. Runs against a scratch database in the session's Postgres."""

import os
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

import purser.config
from tests.conftest import TEST_ENCRYPTION_KEY

DB = "purser_asset_ledger"
TODAY = datetime.now(UTC).date()


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
    command.upgrade(cfg, "0053")
    yield cfg, engine
    engine.dispose()
    purser.config.get_settings.cache_clear()


def _account(conn: sa.Connection, path: str, type_: str, virtual: bool = False) -> object:
    return conn.execute(
        sa.text(
            "INSERT INTO accounts (name, type, is_virtual, path) "
            "VALUES (:n, :t, :v, CAST(:p AS ltree)) RETURNING id"
        ),
        {"n": path, "t": type_, "v": virtual, "p": path},
    ).scalar_one()


def _tx(conn: sa.Connection, on: date, legs: list[tuple[object, str]]) -> None:
    tx = conn.execute(
        sa.text(
            "INSERT INTO transactions (date, status, origin) VALUES (:d, 'posted', 'human') "
            "RETURNING id"
        ),
        {"d": on},
    ).scalar_one()
    for account, amount in legs:
        conn.execute(
            sa.text(
                "INSERT INTO postings (transaction_id, account_id, amount, currency) "
                "VALUES (:t, :a, :x, 'USD')"
            ),
            {"t": tx, "a": account, "x": amount},
        )


def _valuation(conn: sa.Connection, account: object, on: date, value: str, at: datetime) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO valuations (account_id, as_of, value, source, created_at) "
            "VALUES (:a, :d, :v, 'manual', :c)"
        ),
        {"a": account, "d": on, "v": value, "c": at},
    )


def _balance(engine: sa.Engine, account: object, on: date) -> Decimal:
    with engine.connect() as conn:
        return Decimal(
            conn.execute(
                sa.text(
                    "SELECT COALESCE(SUM(p.amount), 0) FROM postings p "
                    "JOIN transactions t ON t.id = p.transaction_id "
                    "WHERE p.account_id = :a AND t.date <= :d "
                    "AND t.status IN ('pending', 'posted') "
                    "AND NOT EXISTS (SELECT 1 FROM transactions x WHERE x.reverses_id = t.id)"
                ),
                {"a": account, "d": on},
            ).scalar_one()
        )


def test_valuations_become_entries_and_the_downgrade_takes_them_back(
    scratch_db: tuple[Config, sa.Engine],
) -> None:
    cfg, engine = scratch_db
    d = lambda n: TODAY - timedelta(days=n)  # noqa: E731
    closed_at = datetime.combine(d(10), datetime.min.time(), tzinfo=UTC) + timedelta(hours=12)
    with engine.begin() as conn:
        checking = _account(conn, "assets.checking", "checking")
        house = _account(conn, "assets.house", "property")
        cabin = _account(conn, "assets.cabin", "property")
        # The down payment, a transfer into the house: 50,000 put in.
        _tx(conn, d(120), [(checking, "-50000.0000"), (house, "50000.0000")])
        _valuation(conn, house, d(90), "300000.0000", datetime(2026, 1, 1, tzinfo=UTC))
        # Two figures on one date: the later recorded is the one that counted.
        _valuation(conn, house, d(30), "310000.0000", datetime(2026, 1, 2, tzinfo=UTC))
        _valuation(conn, house, d(30), "320000.0000", datetime(2026, 1, 3, tzinfo=UTC))
        # A closed cabin: one valuation before the close, one after (never read).
        _valuation(conn, cabin, d(40), "90000.0000", datetime(2026, 1, 1, tzinfo=UTC))
        _valuation(conn, cabin, d(5), "95000.0000", datetime(2026, 1, 2, tzinfo=UTC))
        conn.execute(
            sa.text("UPDATE accounts SET closed_at = :c WHERE id = :a"),
            {"c": closed_at, "a": cabin},
        )
        # The store an instance would hold before the upgrade.
        from sqlalchemy.orm import Session

        from purser import balances

        session = Session(bind=conn)
        balances.rebuild_running(session)
        session.flush()

    command.upgrade(cfg, "0054")

    assert _balance(engine, house, d(100)) == Decimal("50000")  # before any valuation
    assert _balance(engine, house, d(90)) == Decimal("300000")
    assert _balance(engine, house, d(30)) == Decimal("320000")
    assert _balance(engine, house, TODAY) == Decimal("320000")
    assert _balance(engine, cabin, d(40)) == Decimal("90000")
    assert _balance(engine, cabin, d(11)) == Decimal("90000")
    assert _balance(engine, cabin, d(10)) == Decimal("0")  # its close date
    assert _balance(engine, cabin, TODAY) == Decimal("0")
    with engine.connect() as conn:
        reval = conn.execute(
            sa.text("SELECT id FROM accounts WHERE path = CAST('equity.revaluation' AS ltree)")
        ).scalar_one()
        unbalanced = conn.execute(
            sa.text(
                "SELECT transaction_id FROM postings GROUP BY transaction_id "
                "HAVING SUM(amount) <> 0"
            )
        ).all()
        stored = {
            account: conn.execute(
                sa.text(
                    "SELECT balance FROM account_running_balances WHERE account_id = :a "
                    "ORDER BY date DESC, created_at DESC, transaction_id DESC LIMIT 1"
                ),
                {"a": account},
            ).scalar_one()
            for account in (house, cabin)
        }
        rows = conn.execute(
            sa.text(
                "SELECT date, created_at, delta, balance FROM account_running_balances "
                "WHERE account_id = :a ORDER BY date, created_at, transaction_id"
            ),
            {"a": house},
        ).all()
        dirty = conn.execute(sa.text("SELECT networth_dirty_from FROM balance_watermark")).scalar()
    assert unbalanced == []
    assert _balance(engine, reval, TODAY) == Decimal("-270000")  # house only; cabin nets to 0
    assert Decimal(stored[house]) == Decimal("320000"), rows
    assert Decimal(stored[cabin]) == Decimal("0")
    assert dirty is not None and dirty <= d(90)

    command.downgrade(cfg, "0053")
    assert _balance(engine, house, TODAY) == Decimal("50000")
    assert _balance(engine, cabin, TODAY) == Decimal("0")
    with engine.connect() as conn:
        left = conn.execute(
            sa.text(
                "SELECT count(*) FROM accounts WHERE path = CAST('equity.revaluation' AS ltree)"
            )
        ).scalar_one()
    assert left == 0


def test_existing_assets_get_details_and_the_capital_account_appears(
    scratch_db: tuple[Config, sa.Engine],
) -> None:
    """Migration 0055 (#567): every property account gets a details row of
    kind 'other'; equity.capital is seeded; accounts take secured_by_id."""
    cfg, engine = scratch_db
    command.upgrade(cfg, "0054")
    with engine.begin() as conn:
        house = _account(conn, "assets.house", "property")
        loan = _account(conn, "liabilities.mortgage", "loan")
    command.upgrade(cfg, "0055")
    with engine.begin() as conn:
        kinds = conn.execute(sa.text("SELECT account_id, kind FROM asset_details")).all()
        capital = conn.execute(
            sa.text(
                "SELECT type, is_virtual FROM accounts WHERE path = CAST('equity.capital' AS ltree)"
            )
        ).one()
        conn.execute(
            sa.text("UPDATE accounts SET secured_by_id = :h WHERE id = :l"), {"h": house, "l": loan}
        )
    assert kinds == [(house, "other")]
    assert tuple(capital) == ("equity", True)
    command.downgrade(cfg, "0054")
    with engine.connect() as conn:
        gone = conn.execute(
            sa.text("SELECT count(*) FROM accounts WHERE path = CAST('equity.capital' AS ltree)")
        ).scalar_one()
    assert gone == 0
