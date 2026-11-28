"""Migration 0065 (issue #694, decision 141): review marks go, a mark on a
reconciliation adjustment becomes its acceptance, and the removed settings'
rows are deleted.

Runs against a scratch database in the session's Postgres container so the
suite-wide migrated-to-head database is never touched. Synthetic data only.
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


@pytest.fixture
def scratch_db(database_url: str) -> Iterator[tuple[Config, sa.Engine]]:
    os.environ["PURSER_ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
    purser.config.get_settings.cache_clear()

    admin = sa.create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text("DROP DATABASE IF EXISTS purser_review_reasons WITH (FORCE)"))
        conn.execute(sa.text("CREATE DATABASE purser_review_reasons"))
    admin.dispose()

    scratch_url = database_url.rsplit("/", 1)[0] + "/purser_review_reasons"
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", scratch_url)

    engine = sa.create_engine(scratch_url)
    yield cfg, engine
    engine.dispose()


def _tx(conn: sa.Connection, *, reverses: object = None) -> object:
    return conn.execute(
        sa.text(
            "INSERT INTO transactions (date, status, origin, reverses_id)"
            " VALUES (DATE '2026-01-31', 'posted', 'system', :r) RETURNING id"
        ),
        {"r": reverses},
    ).scalar_one()


def _post(conn: sa.Connection, tx: object, account: object, category: object, amount: str) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO postings (transaction_id, account_id, amount, currency)"
            " VALUES (:t, :a, :x, 'USD'), (:t, :c, -CAST(:x AS NUMERIC), 'USD')"
        ),
        {"t": tx, "a": account, "c": category, "x": amount},
    )


def _account(conn: sa.Connection, path: str, *, virtual: bool) -> object:
    type_ = "category_expense" if virtual else "checking"
    return conn.execute(
        sa.text(
            "INSERT INTO accounts (name, type, is_virtual, path)"
            " VALUES (:n, :t, :v, CAST(:p AS ltree)) RETURNING id"
        ),
        {"n": path, "t": type_, "v": virtual, "p": path},
    ).scalar_one()


def _token(conn: sa.Connection) -> object:
    user = conn.execute(
        sa.text("INSERT INTO users (email) VALUES ('reviewer@example.com') RETURNING id")
    ).scalar_one()
    return conn.execute(
        sa.text(
            "INSERT INTO api_tokens (user_id, name, token_hash, scopes)"
            " VALUES (:u, 'reviewer', 'synthetic-hash', ARRAY['read','write']) RETURNING id"
        ),
        {"u": user},
    ).scalar_one()


def _reconciliation(conn: sa.Connection, account: object, as_of: str, tx: object) -> object:
    return conn.execute(
        sa.text(
            "INSERT INTO account_reconciliations"
            " (account_id, as_of, basis, target_balance, adjustment, adjustment_tx_id)"
            " VALUES (:a, CAST(:d AS date), 'posted', 10, 10, :t) RETURNING id"
        ),
        {"a": account, "d": as_of, "t": tx},
    ).scalar_one()


def test_a_mark_on_an_adjustment_becomes_its_acceptance(
    scratch_db: tuple[Config, sa.Engine],
) -> None:
    cfg, engine = scratch_db
    command.upgrade(cfg, "0064")
    with engine.begin() as conn:
        checking = _account(conn, "assets.checking", virtual=False)
        plug = _account(conn, "expenses.reconciliation_adjustments", virtual=True)
        dining = _account(conn, "expenses.dining", virtual=True)
        token = _token(conn)

        # 1: the adjustment row itself carries the mark.
        marked = _tx(conn)
        _post(conn, marked, checking, plug, "10")
        first = _reconciliation(conn, checking, "2026-01-01", marked)
        # 2: corrected (original <- reversal <- replacement); the head's mark
        # wins over the original's.
        original = _tx(conn)
        _post(conn, original, checking, plug, "10")
        reversal = _tx(conn, reverses=original)
        _post(conn, reversal, checking, plug, "-10")
        head = _tx(conn, reverses=reversal)
        _post(conn, head, checking, dining, "10")
        second = _reconciliation(conn, checking, "2026-01-02", original)
        # 3: never marked.
        unmarked = _tx(conn)
        _post(conn, unmarked, checking, plug, "10")
        third = _reconciliation(conn, checking, "2026-01-03", unmarked)

        conn.execute(
            sa.text(
                "INSERT INTO transaction_reviews (transaction_id, reviewed_at, reviewed_by, via)"
                " VALUES (:m, TIMESTAMPTZ '2026-02-01 12:00+00', :tok, 'explicit'),"
                " (:o, TIMESTAMPTZ '2026-02-02 12:00+00', NULL, 'aged'),"
                " (:h, TIMESTAMPTZ '2026-02-03 12:00+00', :tok, 'update')"
            ),
            {"m": marked, "o": original, "h": head, "tok": token},
        )
        for key in ("ai_review", "review_max_age_days", "review_guidance", "timezone"):
            conn.execute(
                sa.text("INSERT INTO app_settings (key, value) VALUES (:k, CAST(:v AS jsonb))"),
                {"k": key, "v": '"UTC"' if key == "timezone" else "true"},
            )

    command.upgrade(cfg, "0065")
    with engine.begin() as conn:
        tables = set(sa.inspect(conn).get_table_names())
        assert "transaction_reviews" not in tables
        accepted = {
            row.id: (row.at, row.by)
            for row in conn.execute(
                sa.text(
                    "SELECT id, adjustment_accepted_at AS at, adjustment_accepted_by AS by"
                    " FROM account_reconciliations"
                )
            )
        }
        assert accepted[first][1] == token
        assert accepted[first][0].isoformat().startswith("2026-02-01")
        assert accepted[second][1] == token  # the head's mark, not the original's
        assert accepted[second][0].isoformat().startswith("2026-02-03")
        assert accepted[third] == (None, None)
        keys = set(conn.execute(sa.text("SELECT key FROM app_settings")).scalars())
        assert keys == {"timezone"}
        indexes = set(
            conn.execute(
                sa.text("SELECT indexname FROM pg_indexes WHERE tablename = 'transactions'")
            ).scalars()
        )
        assert "transactions_review_scope" not in indexes
        assert "transactions_superseded_by" in indexes

    # The downgrade starts the old table silent and gives an acceptance back
    # as a mark.
    command.downgrade(cfg, "0064")
    with engine.begin() as conn:
        marks = {
            row.transaction_id: (row.via, row.reviewed_by)
            for row in conn.execute(
                sa.text("SELECT transaction_id, via, reviewed_by FROM transaction_reviews")
            )
        }
        assert marks[marked] == ("explicit", token)
        assert marks[unmarked] == ("backfill", None)
        assert len(marks) == 5  # every transaction
        columns = {c["name"] for c in sa.inspect(conn).get_columns("account_reconciliations")}
        assert "adjustment_accepted_at" not in columns
    command.upgrade(cfg, "0065")
