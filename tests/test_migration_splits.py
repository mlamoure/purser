"""Migration 0061 (issue #624, decision 127): the ``split`` position event
kind, its ratio columns and ``in_lieu_of``.

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


@pytest.fixture
def scratch_db(database_url: str) -> Iterator[tuple[Config, sa.Engine]]:
    os.environ["PURSER_ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
    purser.config.get_settings.cache_clear()

    admin = sa.create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text("DROP DATABASE IF EXISTS purser_splits WITH (FORCE)"))
        conn.execute(sa.text("CREATE DATABASE purser_splits"))
    admin.dispose()

    scratch_url = database_url.rsplit("/", 1)[0] + "/purser_splits"
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", scratch_url)

    engine = sa.create_engine(scratch_url)
    yield cfg, engine
    engine.dispose()


def _position(conn: sa.Connection) -> str:
    account = conn.execute(
        sa.text(
            "INSERT INTO accounts (name, type, is_virtual, path, holdings_source)"
            " VALUES ('Broker', 'brokerage', false, 'assets.broker', 'manual') RETURNING id"
        )
    ).scalar_one()
    return str(
        conn.execute(
            sa.text(
                "INSERT INTO positions (account_id, symbol, opened_on)"
                " VALUES (:a, 'SYNT', '2026-01-02') RETURNING id"
            ),
            {"a": account},
        ).scalar_one()
    )


_EVENT = (
    "INSERT INTO position_events (position_id, occurred_on, kind, quantity, origin,"
    " split_numerator, split_denominator, price, in_lieu_of)"
    " VALUES (:p, '2026-02-02', :kind, :q, 'manual', :n, :d, :price, :lieu) RETURNING id"
)


def _insert(conn: sa.Connection, position: str, **kw: object) -> object:
    args = {"kind": "split", "q": 0, "n": 2, "d": 1, "price": None, "lieu": None, **kw}
    return conn.execute(sa.text(_EVENT), {"p": position, **args}).scalar_one()


def test_upgrade_shapes_and_downgrade(scratch_db: tuple[Config, sa.Engine]) -> None:
    cfg, engine = scratch_db
    command.upgrade(cfg, "0060")
    with engine.begin() as conn:
        position = _position(conn)
    command.upgrade(cfg, "0061")
    with engine.begin() as conn:
        split = _insert(conn, position)
        sale = _insert(conn, position, kind="sell", q=0.5, n=None, d=None, price=3, lieu=split)
        assert split and sale
    # The shape CHECKs: a split carries a ratio and nothing else.
    for bad in (
        {"n": 2, "d": 2},  # 1:1
        {"n": 0},
        {"d": 1_000_001},
        {"q": 1},  # a split holds no quantity
        {"price": 5},
        {"kind": "buy", "q": 1},  # a ratio off a split
        {"n": None},
    ):
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
            _insert(conn, position, **bad)
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
        # in_lieu_of names a split from a sell only, and once.
        _insert(conn, position, kind="sell", q=1, n=None, d=None, price=3, lieu=split)
    # The downgrade refuses while a split exists, then runs once none does.
    with pytest.raises(sa.exc.SQLAlchemyError, match="split events exist"):
        command.downgrade(cfg, "0060")
    with engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE position_events DISABLE TRIGGER USER"))
        conn.execute(sa.text("DELETE FROM position_events WHERE in_lieu_of IS NOT NULL"))
        conn.execute(sa.text("DELETE FROM position_events WHERE kind = 'split'"))
        conn.execute(sa.text("ALTER TABLE position_events ENABLE TRIGGER USER"))
    command.downgrade(cfg, "0060")
    with engine.begin() as conn:
        columns = {c["name"] for c in sa.inspect(conn).get_columns("position_events")}
        assert not columns & {"split_numerator", "split_denominator", "in_lieu_of"}
