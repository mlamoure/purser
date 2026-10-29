"""seed-demo guard (#217): existing user data blocks seeding unless --force.

The old guard checked only for a literal ``assets.checking`` account, so a
live install whose accounts used any other paths could be seeded over.
"""

from sqlalchemy import text
from sqlalchemy.orm import Session

from purser import ledger as ledger_service
from purser.seed import seed_demo


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
