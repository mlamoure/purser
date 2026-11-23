"""Migration 0058 (issue #591, decision 119): the balance true-up switch
(on for every feed-holdings brokerage account, CHECKed to them), the
``brokerage_true_ups`` table, the ``trade_rule`` suggestion kind and ``mark``
source, and the two new income categories, seeded once and alone.

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

NEW = ("income.gifts", "income.employer_contributions")


@pytest.fixture
def scratch_db(database_url: str) -> Iterator[tuple[Config, sa.Engine]]:
    os.environ["PURSER_ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
    purser.config.get_settings.cache_clear()

    admin = sa.create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text("DROP DATABASE IF EXISTS purser_brokerage_trades WITH (FORCE)"))
        conn.execute(sa.text("CREATE DATABASE purser_brokerage_trades"))
    admin.dispose()

    scratch_url = database_url.rsplit("/", 1)[0] + "/purser_brokerage_trades"
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", scratch_url)

    engine = sa.create_engine(scratch_url)
    yield cfg, engine
    engine.dispose()


def _paths(conn: sa.Connection) -> set[str]:
    return {str(p) for p in conn.execute(sa.text("SELECT path::text FROM accounts")).scalars()}


def test_upgrade_and_downgrade(scratch_db: tuple[Config, sa.Engine]) -> None:
    cfg, engine = scratch_db
    command.upgrade(cfg, "0057")
    with engine.begin() as conn:
        # An install from before 0058: neither new category exists (0012 seeds
        # today's whole default set on a fresh database), and the person
        # deleted a default category of their own.
        conn.execute(
            sa.text("DELETE FROM accounts WHERE path::text IN :paths").bindparams(
                sa.bindparam("paths", expanding=True)
            ),
            {"paths": [*NEW, "income.unemployment"]},
        )

        def account(name: str, type_: str, path: str, source: str = "feed") -> None:
            conn.execute(
                sa.text(
                    "INSERT INTO accounts (name, type, path, holdings_source)"
                    " VALUES (:n, :t, CAST(:p AS ltree), :s)"
                ),
                {"n": name, "t": type_, "p": path, "s": source},
            )

        account("Feed broker", "brokerage", "assets.feed_broker")
        account("Hand broker", "brokerage", "assets.hand_broker", "manual")
        account("Checking", "checking", "assets.checking")

    command.upgrade(cfg, "0058")
    with engine.begin() as conn:
        flags = dict(
            conn.execute(
                sa.text("SELECT path::text, balance_true_up FROM accounts WHERE NOT is_virtual")
            )
            .tuples()
            .all()
        )
        assert flags == {
            "assets.feed_broker": True,
            "assets.hand_broker": False,
            "assets.checking": False,
        }
        paths = _paths(conn)
        assert set(NEW) <= paths
        assert "income.unemployment" not in paths  # a person's deletion sticks
        parent = conn.execute(
            sa.text(
                "SELECT p.path::text FROM accounts c JOIN accounts p ON p.id = c.parent_id"
                " WHERE c.path = 'income.gifts'"
            )
        ).scalar_one()
        assert parent == "income"
        # The CHECK keeps the switch on feed-holdings brokerage accounts.
        with pytest.raises(sa.exc.IntegrityError), conn.begin_nested():
            conn.execute(
                sa.text("UPDATE accounts SET balance_true_up = true WHERE path = 'assets.checking'")
            )
        with pytest.raises(sa.exc.IntegrityError), conn.begin_nested():
            conn.execute(
                sa.text(
                    "UPDATE accounts SET holdings_source = 'manual'"
                    " WHERE path = 'assets.feed_broker'"
                )
            )
        conn.execute(
            sa.text(
                "INSERT INTO suggestions (kind, fingerprint, source, draft_enc)"
                " VALUES ('trade_rule', 'fp-trade', 'mark', '\\x00')"
            )
        )
        tables = set(sa.inspect(conn).get_table_names())
        assert "brokerage_true_ups" in tables
        # One category in use survives the downgrade.
        gifts = conn.execute(
            sa.text("SELECT id FROM accounts WHERE path = 'income.gifts'")
        ).scalar_one()
        conn.execute(
            sa.text(
                "INSERT INTO rules (type, category_id, match_field, match_mode, match_text_enc)"
                " VALUES ('categorize', :c, 'payee', 'equals', '\\x00')"
            ),
            {"c": gifts},
        )

    command.downgrade(cfg, "0057")
    with engine.begin() as conn:
        columns = {c["name"] for c in sa.inspect(conn).get_columns("accounts")}
        assert "balance_true_up" not in columns
        assert "brokerage_true_ups" not in set(sa.inspect(conn).get_table_names())
        assert conn.execute(sa.text("SELECT count(*) FROM suggestions")).scalar_one() == 0
        paths = _paths(conn)
        assert "income.gifts" in paths  # a rule books to it
        assert "income.employer_contributions" not in paths

    command.upgrade(cfg, "head")
    with engine.begin() as conn:
        assert set(NEW) <= _paths(conn)
