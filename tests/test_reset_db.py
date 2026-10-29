"""``reset_db`` (tests/conftest.py) wipes every table a test can fill.

A new table that the reset neither names nor reaches through TRUNCATE ...
CASCADE would carry one test's rows into the next, so every public table must
be either reset or listed in ``KEPT`` with the reason it survives.
"""

from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from tests.conftest import RESET_TABLES, reset_db

# Tables reset_db deliberately leaves alone.
KEPT = {
    "alembic_version": "migration state",
    "balance_watermark": "a singleton row: reset_db clears its cursors instead",
    "connections": "feed tests create fresh rows; raw_fetches references them",
    "encryption_canary": "seeded by migration 0007 to verify the encryption key",
    "encryption_keys": "the narrative-encryption keys the whole suite shares",
    "raw_fetches": "append-only: TRUNCATE is trigger-blocked",
}


def _public_tables(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return set(
            conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            ).scalars()
        )


def _cascade_closure(engine: Engine) -> set[str]:
    """RESET_TABLES plus every table TRUNCATE ... CASCADE adds: any table with
    a foreign key into one already in the set, to a fixpoint."""
    with engine.connect() as conn:
        edges = conn.execute(
            text(
                "SELECT conrelid::regclass::text, confrelid::regclass::text"
                " FROM pg_constraint WHERE contype = 'f'"
            )
        ).all()
    reset = set(RESET_TABLES)
    while added := {child for child, parent in edges if parent in reset} - reset:
        reset |= added
    return reset


def test_every_table_is_reset_or_deliberately_kept(migrated_engine: Engine) -> None:
    tables = _public_tables(migrated_engine)
    reset = _cascade_closure(migrated_engine)
    assert not reset & KEPT.keys(), "a KEPT table is reset after all; drop it from KEPT"
    assert tables - reset - KEPT.keys() == set(), (
        "reset_db misses these tables: add them to RESET_TABLES (or a foreign key"
        " into it), or to KEPT with the reason they must survive"
    )
    assert KEPT.keys() <= tables, "KEPT names a table that no longer exists"


def test_every_table_outside_kept_is_empty_afterwards(
    admin_client: TestClient, migrated_engine: Engine
) -> None:
    # A small world first, so "empty" is not just "never filled".
    assert admin_client.post(
        "/accounts", json={"name": "Checking", "type": "checking", "path": "assets.checking"}
    ).is_success
    assert admin_client.post(
        "/categories",
        json={"name": "Dining", "type": "category_expense", "path": "expenses.dining"},
    ).is_success
    assert admin_client.post(
        "/transactions",
        json={
            "account": "assets.checking",
            "category": "expenses.dining",
            "amount": "-14.00",
            "date": "2026-07-01",
            "status": "posted",
        },
    ).is_success
    assert admin_client.post("/tokens", json={"name": "agent", "scopes": ["read"]}).is_success

    reset_db(migrated_engine)
    with migrated_engine.connect() as conn:
        counts = {
            table: conn.execute(text(f'SELECT count(*) FROM "{table}"')).scalar_one()
            for table in sorted(_public_tables(migrated_engine) - KEPT.keys())
        }
    assert {table: n for table, n in counts.items() if n} == {}
