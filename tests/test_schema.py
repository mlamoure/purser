"""The initial migration applies cleanly and the guard triggers hold."""

import uuid
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from purser.db import get_sessionmaker
from tests.conftest import LEDGER_CURRENCIES, balanced_multi_currency

EXPECTED_TABLES = {
    "users",
    "api_tokens",
    "connections",
    "raw_fetches",
    "accounts",
    "payees",
    "schedules",
    "transactions",
    "postings",
    "matches",
    "holdings_snapshots",
    "valuations",
    "events",
    "webhook_subscriptions",
    "audit_log",
    "app_settings",
    "encryption_keys",  # narrative-encryption DEKs (#137)
    "account_running_balances",  # stored running balances (#146, decision 37)
    "balance_watermark",  # dirty-watermark singleton (#145/#146, decision 38)
    "account_reconciliations",  # statement-balance claims (#268, decision 61)
    "instance_statements",  # card statements on payment instances (#351, decision 80)
    "positions",  # manually tracked holdings (#390, decision 88)
    "position_events",  # their append-only share-count history (#390)
    "security_prices",  # daily closes, manual and provider (#390, decision 89)
    "price_answered",  # the date ranges each provider answered for good (#390)
}


def test_all_tables_exist(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        ).scalars()
        tables = set(rows)
    assert tables >= EXPECTED_TABLES


def test_required_extensions_installed(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        exts = set(conn.execute(text("SELECT extname FROM pg_extension")).scalars())
    assert {"ltree", "pgcrypto"} <= exts


def _make_connection(session: Session) -> uuid.UUID:
    return session.execute(
        text("INSERT INTO connections (access_url_enc) VALUES (:enc) RETURNING id"),
        {"enc": b"test-encrypted-url"},
    ).scalar_one()


def _make_raw_fetch(session: Session, connection_id: uuid.UUID) -> uuid.UUID:
    from purser import crypto

    return session.execute(
        text(
            "INSERT INTO raw_fetches (connection_id, content_hash, payload_enc) "
            "VALUES (:cid, 'hash', :enc) RETURNING id"
        ),
        {"cid": connection_id, "enc": crypto.encrypt("{}")},
    ).scalar_one()


def _make_account(session: Session, name: str, path: str, type_: str = "checking") -> uuid.UUID:
    # Suffix keeps paths unique across the whole suite (accounts_path_uniq).
    unique_path = f"{path}_{uuid.uuid4().hex[:8]}"
    return session.execute(
        text("INSERT INTO accounts (name, type, path) VALUES (:name, :type, :path) RETURNING id"),
        {"name": name, "type": type_, "path": unique_path},
    ).scalar_one()


def _make_transaction(session: Session, status: str = "pending") -> uuid.UUID:
    return session.execute(
        text(
            "INSERT INTO transactions (date, status, origin) "
            "VALUES ('2026-07-01', :status, 'human') RETURNING id"
        ),
        {"status": status},
    ).scalar_one()


def _add_posting(
    session: Session,
    tx_id: uuid.UUID,
    account_id: uuid.UUID,
    amount: Decimal,
    currency: str = "USD",
) -> None:
    session.execute(
        text(
            "INSERT INTO postings (transaction_id, account_id, amount, currency)"
            " VALUES (:tx, :acct, :amount, :currency)"
        ),
        {"tx": tx_id, "acct": account_id, "amount": amount, "currency": currency},
    )


class TestRawFetchesAppendOnly:
    def test_update_rejected(self, db_session: Session) -> None:
        cid = _make_connection(db_session)
        fid = _make_raw_fetch(db_session, cid)
        with pytest.raises(DBAPIError, match="append-only"):
            db_session.execute(
                text("UPDATE raw_fetches SET content_hash = 'tampered' WHERE id = :id"),
                {"id": fid},
            )

    def test_delete_rejected(self, db_session: Session) -> None:
        cid = _make_connection(db_session)
        fid = _make_raw_fetch(db_session, cid)
        with pytest.raises(DBAPIError, match="append-only"):
            db_session.execute(text("DELETE FROM raw_fetches WHERE id = :id"), {"id": fid})


class TestZeroSumInvariant:
    def test_unbalanced_pending_rejected_at_commit(self, db_session: Session) -> None:
        acct = _make_account(db_session, "Checking", "assets.checking")
        tx = _make_transaction(db_session, status="pending")
        _add_posting(db_session, tx, acct, Decimal("-14.00"))
        with pytest.raises(DBAPIError, match="do not sum to zero"):
            db_session.commit()
        db_session.rollback()

    def test_balanced_pending_accepted(self, db_session: Session) -> None:
        acct = _make_account(db_session, "Checking2", "assets.checking2")
        cat = _make_account(db_session, "Dining", "expenses.dining", type_="category_expense")
        tx = _make_transaction(db_session, status="pending")
        _add_posting(db_session, tx, acct, Decimal("-14.00"))
        _add_posting(db_session, tx, cat, Decimal("14.00"))
        db_session.commit()

    def test_expected_transactions_exempt(self, db_session: Session) -> None:
        # The zero-sum invariant is scoped to pending/posted; expected rows are
        # estimates and may be one-sided until they materialize fully.
        acct = _make_account(db_session, "Checking3", "assets.checking3")
        tx = _make_transaction(db_session, status="expected")
        _add_posting(db_session, tx, acct, Decimal("-180.00"))
        db_session.commit()

    def test_moving_posting_between_transactions_checks_both_sides(
        self, db_session: Session
    ) -> None:
        # Moving a posting out of a balanced transaction must not leave the
        # source unbalanced, even if the destination still balances.
        acct = _make_account(db_session, "Checking5", "assets.checking5")
        cat = _make_account(db_session, "Cat5", "expenses.cat5", type_="category_expense")
        tx_a = _make_transaction(db_session, status="pending")
        _add_posting(db_session, tx_a, acct, Decimal("-10.00"))
        _add_posting(db_session, tx_a, cat, Decimal("10.00"))
        tx_b = _make_transaction(db_session, status="pending")
        _add_posting(db_session, tx_b, acct, Decimal("-5.00"))
        _add_posting(db_session, tx_b, cat, Decimal("5.00"))
        db_session.commit()
        # Move the +10 posting from A to B and compensate inside B so B stays
        # balanced; A is now one-sided and the commit must fail.
        db_session.execute(
            text(
                "UPDATE postings SET transaction_id = :b "
                "WHERE transaction_id = :a AND amount = 10.00"
            ),
            {"a": tx_a, "b": tx_b},
        )
        _add_posting(db_session, tx_b, acct, Decimal("-10.00"))
        with pytest.raises(DBAPIError, match="do not sum to zero"):
            db_session.commit()
        db_session.rollback()

    def test_status_change_to_pending_checks_balance(self, db_session: Session) -> None:
        acct = _make_account(db_session, "Checking4", "assets.checking4")
        tx = _make_transaction(db_session, status="expected")
        _add_posting(db_session, tx, acct, Decimal("-180.00"))
        db_session.commit()
        db_session.execute(
            text("UPDATE transactions SET status = 'pending' WHERE id = :id"), {"id": tx}
        )
        with pytest.raises(DBAPIError, match="do not sum to zero"):
            db_session.commit()
        db_session.rollback()

    @settings(max_examples=30, deadline=None)
    @given(legs=balanced_multi_currency(), data=st.data())
    def test_multi_currency_commits_iff_every_currency_sums_to_zero(
        self, migrated_engine: Engine, legs: list[tuple[str, Decimal]], data: st.DataObject
    ) -> None:
        """The trigger groups by (transaction, currency): a balanced mix of
        currencies commits; moving one leg to another currency keeps the
        currency-blind total at zero, yet the commit fails."""
        balanced = data.draw(st.booleans(), label="balanced")
        if not balanced:
            i = data.draw(st.integers(min_value=0, max_value=len(legs) - 1), label="moved leg")
            currency, amount = legs[i]
            other = data.draw(st.sampled_from([c for c in LEDGER_CURRENCIES if c != currency]))
            legs = [*legs[:i], (other, amount), *legs[i + 1 :]]
        # One session per example: a function-scoped db_session would span them all.
        with get_sessionmaker()() as session:
            acct = _make_account(session, "Multi", "assets.multi")
            cat = _make_account(session, "MultiCat", "expenses.multi", type_="category_expense")
            tx = _make_transaction(session, status="pending")
            for n, (currency, amount) in enumerate(legs):
                _add_posting(session, tx, acct if n % 2 else cat, amount, currency)
            if not balanced:
                with pytest.raises(DBAPIError, match="do not sum to zero"):
                    session.commit()
                session.rollback()
                return
            session.commit()
            sums = session.execute(
                text(
                    "SELECT currency, sum(amount) FROM postings"
                    " WHERE transaction_id = :tx GROUP BY currency"
                ),
                {"tx": tx},
            ).all()
        assert {currency for currency, _ in sums} == {currency for currency, _ in legs}
        assert all(total == 0 for _, total in sums)


class TestPostedImmutability:
    def _posted_tx(self, db_session: Session, suffix: str) -> uuid.UUID:
        acct = _make_account(db_session, f"Acct{suffix}", f"assets.acct{suffix}")
        cat = _make_account(
            db_session, f"Cat{suffix}", f"expenses.cat{suffix}", type_="category_expense"
        )
        tx = _make_transaction(db_session, status="posted")
        _add_posting(db_session, tx, acct, Decimal("-20.00"))
        _add_posting(db_session, tx, cat, Decimal("20.00"))
        db_session.commit()
        return tx

    def test_content_update_rejected(self, db_session: Session) -> None:
        # description is encrypted now (#137) → edit its BYTEA column; the
        # column-name-free posted guard still rejects any content change.
        tx = self._posted_tx(db_session, "a")
        with pytest.raises(DBAPIError, match="immutable"):
            db_session.execute(
                text(
                    "UPDATE transactions SET description_enc = decode('00', 'hex') WHERE id = :id"
                ),
                {"id": tx},
            )

    def test_delete_rejected(self, db_session: Session) -> None:
        tx = self._posted_tx(db_session, "b")
        with pytest.raises(DBAPIError, match="cannot be deleted"):
            db_session.execute(text("DELETE FROM transactions WHERE id = :id"), {"id": tx})

    def test_posting_edit_rejected(self, db_session: Session) -> None:
        tx = self._posted_tx(db_session, "c")
        with pytest.raises(DBAPIError, match="immutable"):
            db_session.execute(
                text("UPDATE postings SET amount = amount + 1 WHERE transaction_id = :id"),
                {"id": tx},
            )

    def test_void_with_link_allowed(self, db_session: Session) -> None:
        # Transfer merges void the one-sided posted originals with lineage links.
        tx = self._posted_tx(db_session, "d")
        merged = self._posted_tx(db_session, "e")
        db_session.execute(
            text("UPDATE transactions SET status = 'void', superseded_by = :m WHERE id = :id"),
            {"id": tx, "m": merged},
        )
        db_session.commit()

    def test_posted_at_creation_postings_allowed(self, db_session: Session) -> None:
        # Creating a transaction born posted attaches its postings in the same
        # DB transaction — the guard's carve-out (created_at = txn timestamp).
        tx = self._posted_tx(db_session, "f")
        count = db_session.execute(
            text("SELECT COUNT(*) FROM postings WHERE transaction_id = :id"), {"id": tx}
        ).scalar_one()
        assert count == 2

    def test_posting_insert_into_posted_rejected(self, db_session: Session) -> None:
        # Even a BALANCED pair added after the fact rewrites posted history.
        tx = self._posted_tx(db_session, "g")
        acct = _make_account(db_session, "AcctG2", "assets.acctg2")
        cat = _make_account(db_session, "CatG2", "expenses.catg2", type_="category_expense")
        with pytest.raises(DBAPIError, match="immutable"):
            db_session.execute(
                text(
                    "INSERT INTO postings (transaction_id, account_id, amount) VALUES "
                    "(:tx, :acct, -5.00), (:tx, :cat, 5.00)"
                ),
                {"tx": tx, "acct": acct, "cat": cat},
            )
        db_session.rollback()

    def test_repointing_posting_into_posted_rejected(self, db_session: Session) -> None:
        posted = self._posted_tx(db_session, "h")
        acct = _make_account(db_session, "AcctH2", "assets.accth2")
        cat = _make_account(db_session, "CatH2", "expenses.cath2", type_="category_expense")
        pending = _make_transaction(db_session, status="pending")
        _add_posting(db_session, pending, acct, Decimal("-5.00"))
        _add_posting(db_session, pending, cat, Decimal("5.00"))
        db_session.commit()
        with pytest.raises(DBAPIError, match="immutable"):
            db_session.execute(
                text("UPDATE postings SET transaction_id = :p WHERE transaction_id = :old"),
                {"p": posted, "old": pending},
            )
        db_session.rollback()

    def test_bare_void_of_posted_rejected(self, db_session: Session) -> None:
        # posted -> void without a superseded_by link erases balance effect
        # with no lineage (DESIGN §4).
        tx = self._posted_tx(db_session, "i")
        with pytest.raises(DBAPIError, match="superseded_by"):
            db_session.execute(
                text("UPDATE transactions SET status = 'void' WHERE id = :id"), {"id": tx}
            )
        db_session.rollback()

    def test_superseded_by_rewrite_on_live_posted_rejected(self, db_session: Session) -> None:
        tx = self._posted_tx(db_session, "j")
        other = self._posted_tx(db_session, "k")
        with pytest.raises(DBAPIError, match="immutable"):
            db_session.execute(
                text("UPDATE transactions SET superseded_by = :o WHERE id = :id"),
                {"id": tx, "o": other},
            )
        db_session.rollback()


class TestIntegrityIndexes:
    def test_second_reversal_of_same_transaction_rejected(self, db_session: Session) -> None:
        # transactions_reverses_id_uniq: a correction chain never forks.
        original = _make_transaction(db_session, status="pending")
        db_session.execute(
            text(
                "INSERT INTO transactions (date, status, origin, reverses_id) "
                "VALUES ('2026-07-01', 'pending', 'system', :r)"
            ),
            {"r": original},
        )
        with pytest.raises(DBAPIError, match="transactions_reverses_id_uniq"):
            db_session.execute(
                text(
                    "INSERT INTO transactions (date, status, origin, reverses_id) "
                    "VALUES ('2026-07-01', 'pending', 'system', :r)"
                ),
                {"r": original},
            )
        db_session.rollback()

    def test_second_live_match_on_posted_rejected(self, db_session: Session) -> None:
        posted = _make_transaction(db_session, status="pending")
        e1 = _make_transaction(db_session, status="expected")
        e2 = _make_transaction(db_session, status="expected")
        db_session.execute(
            text(
                "INSERT INTO matches (expected_id, posted_id, score, state) "
                "VALUES (:e, :p, 0.9, 'proposed')"
            ),
            {"e": e1, "p": posted},
        )
        with pytest.raises(DBAPIError, match="matches_posted_live_uniq"):
            db_session.execute(
                text(
                    "INSERT INTO matches (expected_id, posted_id, score, state) "
                    "VALUES (:e, :p, 0.9, 'proposed')"
                ),
                {"e": e2, "p": posted},
            )
        db_session.rollback()

    def test_rejected_matches_do_not_block(self, db_session: Session) -> None:
        posted = _make_transaction(db_session, status="pending")
        e1 = _make_transaction(db_session, status="expected")
        for state in ("rejected", "rejected", "proposed"):
            db_session.execute(
                text(
                    "INSERT INTO matches (expected_id, posted_id, score, state) "
                    "VALUES (:e, :p, 0.9, :s)"
                ),
                {"e": e1, "p": posted, "s": state},
            )
        db_session.commit()
