"""Shared fixtures: a real Postgres via testcontainers, migrated to head.

Never SQLite — the schema depends on ltree, JSONB, triggers, and NUMERIC
semantics.
"""

import base64
import os
from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from hypothesis import strategies as st
from sqlalchemy import Engine
from sqlalchemy.orm import Session
from testcontainers.postgres import PostgresContainer

import purser.config
import purser.db

# Encryption at rest is required (issue #93): the key must exist before
# migrations run (0007 seeds the canary + encrypts the backfill with it).
# One fixed key for the whole suite; tests that exercise missing/rotated
# keys pop or swap it and restore this value. The key is derived from a
# readable 32-byte phrase rather than written out as a random-looking literal,
# so the source holds nothing a secret scanner mistakes for a real key.
TEST_ENCRYPTION_KEY = base64.urlsafe_b64encode(b"purser-test-suite-key-not-secret").decode()


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    with PostgresContainer("postgres:16", driver="psycopg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def migrated_engine(database_url: str) -> Iterator[Engine]:
    os.environ["PURSER_DATABASE_URL"] = database_url
    os.environ["PURSER_ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
    purser.config.get_settings.cache_clear()
    purser.db.get_engine.cache_clear()
    purser.db.get_sessionmaker.cache_clear()

    from purser.cli import run_migrations

    run_migrations()
    yield purser.db.get_engine()


@pytest.fixture
def db_session(migrated_engine: Engine) -> Iterator[Session]:
    with purser.db.get_sessionmaker()() as session:
        yield session
        session.rollback()


@pytest.fixture
def client(migrated_engine: Engine) -> Iterator[TestClient]:
    from purser.api.app import create_app

    with TestClient(create_app()) as c:
        yield c


# The first-run admin most API tests act as. Tests of the login flow itself
# need the raw credentials; everything else takes ``admin_client``.
ADMIN_CREDENTIALS = {"email": "admin@example.com", "password": "correct-horse-battery"}


# The tables reset_db truncates. CASCADE also empties every table holding a
# foreign key into this set; tests/test_reset_db.py accounts for the rest.
RESET_TABLES = (
    "users",
    "accounts",
    "payees",
    "events",
    "suggestions",
    "webhook_subscriptions",
    "app_settings",
    "net_worth_daily",
    # Global, no foreign key into the set above (#390).
    "security_prices",
    "price_answered",
)


def reset_db(engine: Engine) -> None:
    """Wipe identity + ledger tables back to a pristine instance.

    ``clean_db`` is this as a fixture. Hypothesis tests call it directly at
    the top of each example: a function-scoped fixture runs once per test,
    not once per example.

    connections/raw_fetches are deliberately left alone — raw_fetches is
    append-only (TRUNCATE is trigger-blocked), so feed tests create fresh
    rows instead of resetting. ``KEPT`` in tests/test_reset_db.py lists every
    table that survives, with the reason.
    """
    from sqlalchemy import text

    import purser.appsettings

    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(RESET_TABLES)} CASCADE"))
        # A pristine instance also has no dirty balance cursors (#146): the
        # post-write consumers may have materialized days for a PRIOR test's
        # ledger, and a stale cursor would smear into the next test.
        conn.execute(
            text(
                "UPDATE balance_watermark SET running_dirty_from = NULL, networth_dirty_from = NULL"
            )
        )
    # The overrides cache is process-global with a TTL; a stale entry would
    # leak one test's config into the next.
    purser.appsettings.invalidate()


@pytest.fixture
def clean_db(migrated_engine: Engine) -> None:
    """A pristine instance for this test (see ``reset_db``)."""
    reset_db(migrated_engine)


@pytest.fixture
def admin_client(clean_db: None, client: TestClient) -> TestClient:
    """A pristine instance whose first-run admin is signed in on ``client``."""
    assert client.post("/auth/setup", json=ADMIN_CREDENTIALS).status_code == 201
    return client


# Currencies for multi-currency ledger tests. The zero-sum invariant holds per
# (transaction, currency) in core (balance_by_currency) and in the DB trigger
# (GROUP BY transaction_id, currency); the service refuses mixed-currency
# transactions, but the invariant itself must hold for any mix.
LEDGER_CURRENCIES = ("USD", "EUR", "CAD", "JPY")

_leg_amounts = st.decimals(
    min_value=Decimal("-9999999.9999"),
    max_value=Decimal("9999999.9999"),
    allow_nan=False,
    allow_infinity=False,
    places=4,
).filter(lambda d: d != 0)


@st.composite
def balanced_multi_currency(draw: st.DrawFn) -> list[tuple[str, Decimal]]:
    """Legs ``(currency, amount)`` of one balanced transaction: 1-3 distinct
    currencies, each with 1-4 random nonzero legs closed by a balancing leg,
    the whole list shuffled. Every currency sums to zero; no leg is zero (when
    a currency's random legs already cancel, it needs no closing leg)."""
    currencies = draw(
        st.lists(st.sampled_from(LEDGER_CURRENCIES), min_size=1, max_size=3, unique=True)
    )
    legs: list[tuple[str, Decimal]] = []
    for currency in currencies:
        amounts = draw(st.lists(_leg_amounts, min_size=1, max_size=4))
        closing = -sum(amounts, Decimal(0))
        if closing != 0:
            amounts.append(closing)
        legs.extend((currency, amount) for amount in amounts)
    return list(draw(st.permutations(legs)))
