"""Migration 0046 (issue #393, decision 107): upgrade, a clean downgrade
while no lot data exists, and a refused downgrade once any would be lost.
Runs against a scratch database in the session's Postgres container so the
suite-wide migrated-to-head database is never touched. Synthetic data."""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

import purser.config
from tests.conftest import TEST_ENCRYPTION_KEY

DB = "purser_lots_migration"


@pytest.fixture
def scratch_db(database_url: str) -> Iterator[tuple[Config, sa.Engine]]:
    os.environ["PURSER_ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
    purser.config.get_settings.cache_clear()
    admin = sa.create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)"))
        conn.execute(sa.text(f"CREATE DATABASE {DB}"))
    admin.dispose()
    url = database_url.rsplit("/", 1)[0] + f"/{DB}"
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    engine = sa.create_engine(url)
    command.upgrade(cfg, "0046")
    yield cfg, engine
    engine.dispose()
    purser.config.get_settings.cache_clear()


def test_empty_round_trip(scratch_db: tuple[Config, sa.Engine]) -> None:
    cfg, engine = scratch_db
    command.downgrade(cfg, "0045")
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT to_regclass('lots')")).scalar() is None
    command.upgrade(cfg, "0046")


def test_downgrade_refuses_to_destroy_awards(scratch_db: tuple[Config, sa.Engine]) -> None:
    cfg, engine = scratch_db
    with engine.begin() as conn:
        account = conn.execute(
            sa.text(
                "INSERT INTO accounts (name, type, path) VALUES ('Plan', 'brokerage', "
                "CAST('assets.plan' AS ltree)) RETURNING id"
            )
        ).scalar()
        conn.execute(
            sa.text(
                "INSERT INTO awards (account_id, symbol, grant_ref_enc, grant_ref_bidx, "
                "granted_on, granted_quantity, origin) VALUES (:a, 'SYNT', '\\x00', 'b', "
                "'2025-01-01', 10, 'agent')"
            ),
            {"a": account},
        )
    with pytest.raises(Exception, match="would destroy them"):
        command.downgrade(cfg, "0045")
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM awards")).scalar() == 1


def test_all_withheld_vest_shape(scratch_db: tuple[Config, sa.Engine]) -> None:
    """A vested row with net 0 has no lot; one with net > 0 must name one."""
    _cfg, engine = scratch_db
    with engine.begin() as conn:
        account = conn.execute(
            sa.text(
                "INSERT INTO accounts (name, type, path) VALUES ('Plan', 'brokerage', "
                "CAST('assets.plan' AS ltree)) RETURNING id"
            )
        ).scalar()
        award = conn.execute(
            sa.text(
                "INSERT INTO awards (account_id, symbol, grant_ref_enc, grant_ref_bidx, "
                "granted_on, granted_quantity, origin) VALUES (:a, 'SYNT', '\\x00', 'b', "
                "'2025-01-01', 10, 'agent') RETURNING id"
            ),
            {"a": account},
        ).scalar()
        conn.execute(
            sa.text(
                "INSERT INTO award_vests (award_id, vest_date, quantity, status, vested_gross, "
                "vested_withheld, vested_net) VALUES (:w, '2025-06-01', 10, 'vested', 10, 10, 0)"
            ),
            {"w": award},
        )
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO award_vests (award_id, vest_date, quantity, status, vested_gross, "
                "vested_withheld, vested_net) VALUES (:w, '2025-07-01', 10, 'vested', 10, 4, 6)"
            ),
            {"w": award},
        )
