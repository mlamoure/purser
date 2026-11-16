"""seed-demo guard (#217): existing user data blocks seeding unless --force.

The old guard checked only for a literal ``assets.checking`` account, so a
live install whose accounts used any other paths could be seeded over.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.orm import Session

import purser.config
from purser import ledger as ledger_service
from purser import seed
from purser.models import Account
from purser.seed import seed_demo
from tests.conftest import TEST_ENCRYPTION_KEY


@pytest.fixture
def fresh_db(database_url: str, migrated_engine: sa.Engine) -> Iterator[sa.Engine]:
    """A database migrated from nothing, in the session's Postgres container:
    exactly what the migrations seed (the category tree, equity.securities)
    and nothing else. clean_db's TRUNCATE takes those seeds away too, so it
    cannot show what a fresh install holds. (The suite's database stays the
    global engine's: seeding reads runtime settings through it.)"""
    os.environ["PURSER_ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
    purser.config.get_settings.cache_clear()
    admin = sa.create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text("DROP DATABASE IF EXISTS purser_seed_fresh WITH (FORCE)"))
        conn.execute(sa.text("CREATE DATABASE purser_seed_fresh"))
    admin.dispose()
    url = database_url.rsplit("/", 1)[0] + "/purser_seed_fresh"
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    engine = sa.create_engine(url)
    yield engine
    engine.dispose()


def test_a_freshly_migrated_database_seeds(fresh_db: sa.Engine) -> None:
    """The migration-seeded system account (equity.securities, 0043) is not
    user data: `purser seed-demo` runs on a fresh install."""
    with Session(fresh_db) as session:
        securities = session.execute(
            select(Account).where(Account.path == ledger_service.SECURITIES_PATH)
        ).scalar_one()
        assert ledger_service.is_system_account(securities)
        assert seed._has_user_data(session) is False
        stats = seed_demo(session)
        assert "skipped" not in stats and stats["accounts"] >= 3
        session.rollback()


class TestSeedGuard:
    def test_clean_db_seeds(self, clean_db: None, db_session: Session) -> None:
        stats = seed_demo(db_session)
        assert "skipped" not in stats
        assert stats["accounts"] >= 3

    def test_reseed_is_skipped(self, clean_db: None, db_session: Session) -> None:
        seed_demo(db_session)
        assert seed_demo(db_session) == {"skipped": 1}

    def test_any_real_account_blocks_seed(self, clean_db: None, db_session: Session) -> None:
        # No assets.checking anywhere — the old guard would have seeded here.
        ledger_service.create_account(
            db_session, name="Main", type_="checking", path="assets.main", actor_token_id=None
        )
        assert seed_demo(db_session) == {"skipped": 1}

    def test_migration_category_tree_alone_does_not_block(
        self, clean_db: None, db_session: Session
    ) -> None:
        # Categories ARE accounts (§3.2); the ~90 rows migration 0012 ships
        # must not read as user data or a fresh install could never seed.
        ledger_service.ensure_category_path(db_session, "expenses.preexisting", actor_token_id=None)
        stats = seed_demo(db_session)
        assert "skipped" not in stats

    def test_force_seeds_over_user_data_and_audits(
        self, clean_db: None, db_session: Session
    ) -> None:
        ledger_service.create_account(
            db_session, name="Main", type_="checking", path="assets.main", actor_token_id=None
        )
        stats = seed_demo(db_session, force=True)
        assert "skipped" not in stats
        marker = db_session.execute(
            text("SELECT COUNT(*) FROM audit_log WHERE action = 'seed.force'")
        ).scalar_one()
        assert marker == 1
