"""Migration 0059 (issue #585, decision 123): the system account
``equity.opening_balances`` and the ``account_starting_balances`` table.

Runs against a scratch database in the session's Postgres container so the
suite-wide migrated-to-head database is never touched.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

import purser.config
from tests.conftest import TEST_ENCRYPTION_KEY

OPENING = "equity.opening_balances"


@pytest.fixture
def scratch_db(database_url: str) -> Iterator[tuple[Config, sa.Engine]]:
    os.environ["PURSER_ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
    purser.config.get_settings.cache_clear()

    admin = sa.create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text("DROP DATABASE IF EXISTS purser_starting_balances WITH (FORCE)"))
        conn.execute(sa.text("CREATE DATABASE purser_starting_balances"))
    admin.dispose()

    scratch_url = database_url.rsplit("/", 1)[0] + "/purser_starting_balances"
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", scratch_url)

    engine = sa.create_engine(scratch_url)
    yield cfg, engine
    engine.dispose()


def _opening(conn: sa.Connection) -> list[tuple[str, bool]]:
    return [
        (str(type_), bool(virtual))
        for type_, virtual in conn.execute(
            sa.text("SELECT type, is_virtual FROM accounts WHERE path = CAST(:p AS ltree)"),
            {"p": OPENING},
        )
    ]


def test_upgrade_and_downgrade(scratch_db: tuple[Config, sa.Engine]) -> None:
    cfg, engine = scratch_db
    command.upgrade(cfg, "0058")
    with engine.begin() as conn:
        assert _opening(conn) == []
        conn.execute(
            sa.text(
                "INSERT INTO accounts (name, type, is_virtual, path)"
                " VALUES ('Checking', 'checking', false, 'assets.checking')"
            )
        )

    command.upgrade(cfg, "0059")
    with engine.begin() as conn:
        assert _opening(conn) == [("equity", True)]
        assert "account_starting_balances" in set(sa.inspect(conn).get_table_names())
        account = conn.execute(
            sa.text("SELECT id FROM accounts WHERE path = 'assets.checking'")
        ).scalar_one()
        connection = conn.execute(
            sa.text("INSERT INTO connections (provider) VALUES ('simplefin') RETURNING id")
        ).scalar_one()
        fetch = conn.execute(
            sa.text(
                "INSERT INTO raw_fetches (connection_id, content_hash, payload_enc)"
                " VALUES (:c, 'synthetic', '\\x00') RETURNING id"
            ),
            {"c": connection},
        ).scalar_one()
        row = {"a": account, "f": fetch}
        insert = (
            "INSERT INTO account_starting_balances (account_id, raw_fetch_id, reported,"
            " posted_before, pending_before, basis, horizon, amount)"
            " VALUES (:a, :f, 100, 10, 10, {basis}, DATE '2026-01-01', 90)"
        )
        # The basis is one of the drift check's two.
        with pytest.raises(sa.exc.IntegrityError), conn.begin_nested():
            conn.execute(sa.text(insert.format(basis="'posted'")), row)
        conn.execute(sa.text(insert.format(basis="'pending_inclusive'")), row)
        # One per account.
        with pytest.raises(sa.exc.IntegrityError), conn.begin_nested():
            conn.execute(sa.text(insert.format(basis="'posted_only'")), row)

    # Running the upgrade's seed twice never duplicates the system account.
    command.downgrade(cfg, "0058")
    with engine.begin() as conn:
        assert "account_starting_balances" not in set(sa.inspect(conn).get_table_names())
        assert _opening(conn) == []  # nothing was posted to it
    command.upgrade(cfg, "0059")
    with engine.begin() as conn:
        assert _opening(conn) == [("equity", True)]


def test_downgrade_keeps_a_system_account_that_holds_postings(
    scratch_db: tuple[Config, sa.Engine],
) -> None:
    cfg, engine = scratch_db
    command.upgrade(cfg, "0059")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO accounts (name, type, is_virtual, path)"
                " VALUES ('Checking', 'checking', false, 'assets.checking')"
            )
        )
        tx = conn.execute(
            sa.text(
                "INSERT INTO transactions (date, status, origin)"
                " VALUES (DATE '2026-01-01', 'posted', 'system') RETURNING id"
            )
        ).scalar_one()
        conn.execute(
            sa.text(
                "INSERT INTO postings (transaction_id, account_id, amount, currency)"
                " SELECT :t, id, CASE WHEN is_virtual THEN -90 ELSE 90 END, 'USD'"
                " FROM accounts WHERE path IN ('assets.checking', CAST(:p AS ltree))"
            ),
            {"t": tx, "p": OPENING},
        )

    command.downgrade(cfg, "0058")
    with engine.begin() as conn:
        assert _opening(conn) == [("equity", True)]
