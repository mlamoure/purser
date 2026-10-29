"""Guarded `purser admin purge-retired-rows` (issue #284, decision 72).

Rows a retired ingest path wrote outlive their archive: `origin='import'`
with `source_fetch_id` NULL. `purge-feed-rows` selects by fetch and cannot
reach them, so this command removes them by CREATION BATCH — every row one
import committed shares that transaction's `created_at`. Batches must be
written in separate committed transactions to be told apart, so these tests
commit (clean_db / an explicit reset wipes the ledger between tests).

All data is synthetic.
"""

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from purser import audit, balances, ledger, matching, qif_import, review
from purser.admin import retired_rows
from purser.admin.feed_repair import RepairRefused
from purser.core import ledger as core
from purser.db import get_sessionmaker
from purser.models import (
    Account,
    ApiToken,
    AuditLog,
    Event,
    Match,
    Posting,
    Transaction,
    TransactionReview,
    User,
)
from tests.conftest import reset_db

from .test_feed import FeedHarness, payload_of

CARD = "liabilities.card_x"
SAVINGS = "assets.savings_x"
ANCHOR = date(2026, 6, 15)  # fixed: nothing here depends on today's date

SINGLE_QIF = """!Type:Bank
D06/01/2026
T-12.00
PSynthetic Grocer
^
"""


# --------------------------------------------------------------------------
# World builders (each helper commits: one call = one creation batch)
# --------------------------------------------------------------------------


def _accounts() -> None:
    with get_sessionmaker()() as session:
        ledger.create_account(
            session, name="Card X", type_="credit_card", path=CARD, actor_token_id=None
        )
        ledger.create_account(
            session, name="Savings X", type_="savings", path=SAVINGS, actor_token_id=None
        )
        session.commit()


def _commit_token() -> uuid.UUID:
    with get_sessionmaker()() as session:
        user = User(email=f"retired_{uuid.uuid4().hex[:8]}@example.com")
        session.add(user)
        session.flush()
        token = ApiToken(
            user_id=user.id, name="op", token_hash=uuid.uuid4().hex, scopes=["read", "write"]
        )
        session.add(token)
        session.commit()
        return token.id


def _token(session: Session, token_id: uuid.UUID) -> ApiToken:
    token = session.get(ApiToken, token_id)
    assert token is not None
    return token


def _retired_batch(
    rows: list[tuple[str, str]],
    *,
    account: str = CARD,
    provenance: str | None = "connection.ingest_direct",
    reviewed: bool = False,
    void_last: bool = False,
) -> tuple[datetime, list[uuid.UUID]]:
    """Write rows the way the removed ingest did — origin import, a content
    hash, NO source fetch — in one committed transaction. ``rows`` is
    (amount, status) pairs; ``reviewed`` adds migration 0021's backfill mark;
    ``void_last`` voids the last (pending) row in the same transaction."""
    with get_sessionmaker()() as session:
        target = ledger.account_by_path(session, account)
        category = ledger.ensure_category_path(session, "income.uncategorized", actor_token_id=None)
        ids: list[uuid.UUID] = []
        for n, (amount, status) in enumerate(rows):
            tx = ledger.create_flat_transaction(
                session,
                account_ref=str(target.id),
                category_ref=str(category.id),
                amount=Decimal(amount),
                date=ANCHOR + timedelta(days=n),
                description=f"SYNTHETIC RETIRED {n}",
                requested_status=core.Status(status),
                origin=core.Origin.IMPORT,
                token=None,
                content_hash=f"retired-{uuid.uuid4().hex}",
            )
            if reviewed:
                review.mark(session, tx_id=tx.id, token=None, via=review.VIA_BACKFILL)
            ids.append(tx.id)
        if void_last:
            ledger.transition_status(
                session, tx_id=ids[-1], new_status=core.Status.VOID, token=None
            )
        if provenance is not None:
            audit.record(
                session,
                actor_token_id=None,
                action=provenance,
                entity_type="connection",
                entity_id=uuid.uuid4(),
            )
        session.commit()
        stamp = session.get(Transaction, ids[0])
        assert stamp is not None
        return stamp.created_at, ids


def _qif_batch(account: str = CARD) -> datetime:
    with get_sessionmaker()() as session:
        qif_import.import_qif(
            session, content=SINGLE_QIF, account_map={"__default__": account}, token=None
        )
        session.commit()
        return session.execute(
            select(func.max(Transaction.created_at)).where(Transaction.origin == "import")
        ).scalar_one()


def _plan(batches: list[datetime], account: str = CARD) -> retired_rows.RetiredPlan:
    with get_sessionmaker()() as session:
        acct = retired_rows.resolve_account(session, account)
        return retired_rows.plan_purge(session, acct, batches)


def _purge(batches: list[datetime], *, unlink: bool = False, account: str = CARD) -> dict:
    with get_sessionmaker()() as session:
        acct = retired_rows.resolve_account(session, account)
        summary = retired_rows.execute_purge(
            session, acct, batches, unlink=unlink, actor_token_id=None
        )
        session.commit()
        return summary


def _live_balance(account: str) -> Decimal:
    with get_sessionmaker()() as session:
        acct = ledger.account_by_path(session, account)
        return ledger.account_balances(session, [acct.id]).get(acct.id, Decimal("0"))


def _status(tx_id: uuid.UUID) -> str | None:
    with get_sessionmaker()() as session:
        tx = session.get(Transaction, tx_id)
        return tx.status if tx is not None else None


@pytest.fixture
def world(clean_db: None) -> None:
    _accounts()


# --------------------------------------------------------------------------
# Listing
# --------------------------------------------------------------------------


class TestListing:
    def test_batches_group_by_creation_with_audit_provenance(
        self, clean_db: None, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Feed rows (they HAVE a fetch) never list; a manual import and a
        retired batch list separately, each labelled by its import's audit
        row."""
        harness = FeedHarness(client, monkeypatch)
        harness.use_payload(
            payload_of(
                [{"id": "f1", "posted": 1_780_000_000, "amount": "-5.00", "description": "x"}]
            )
        )
        connection_id = harness.connect()
        harness.map_new(connection_id, "Demo Checking", "assets.checking_x")
        assert harness.sync(connection_id)["imported"] == 1

        qif_at = _qif_batch("assets.checking_x")
        retired_at, _ids = _retired_batch(
            [("50.00", "posted"), ("7.25", "pending")], account="assets.checking_x"
        )
        with get_sessionmaker()() as session:
            acct = retired_rows.resolve_account(session, "assets.checking_x")
            batches = retired_rows.list_batches(session, acct)
        assert [b.created_at for b in batches] == [qif_at, retired_at]
        qif, retired = batches
        assert qif.rows == 1 and "qif.import" in qif.provenance
        assert retired.status_counts == {"posted": 1, "pending": 1}
        assert "connection.ingest_direct" in retired.provenance
        assert "qif.import" not in retired.provenance
        assert (retired.first_date, retired.last_date) == (ANCHOR, ANCHOR + timedelta(days=1))

    def test_cli_lists_without_a_batch(self, world: None) -> None:
        retired_at, _ids = _retired_batch([("50.00", "posted")])
        from purser.cli import app

        result = CliRunner().invoke(app, ["admin", "purge-retired-rows", CARD])
        assert result.exit_code == 0, result.output
        assert retired_at.isoformat() in result.output
        assert "connection.ingest_direct" in result.output

    def test_cli_apply_needs_a_batch(self, world: None) -> None:
        from purser.cli import app

        result = CliRunner().invoke(app, ["admin", "purge-retired-rows", CARD, "--apply"])
        assert result.exit_code == 1
        assert "--batch" in result.output


# --------------------------------------------------------------------------
# Dry run == apply
# --------------------------------------------------------------------------


class TestPurge:
    def test_dry_run_reports_and_changes_nothing(self, world: None) -> None:
        retired_at, _ids = _retired_batch(
            [("50.00", "posted"), ("20.00", "posted"), ("7.25", "pending"), ("3.00", "pending")],
            void_last=True,
        )
        plan = _plan([retired_at])
        assert plan.counts() == {
            "rows": 4,
            "transactions_voided": 1,
            "transactions_deleted": 2,
            "left_void": 1,
            "matches_rejected": 0,
            "matches_unmatched": 0,
            "transfers_unlinked": 0,
        }
        assert plan.refusal(unlink=False) is None
        before = _live_balance(CARD)

        from purser.cli import app

        result = CliRunner().invoke(
            app, ["admin", "purge-retired-rows", CARD, "--batch", retired_at.isoformat()]
        )
        assert result.exit_code == 0, result.output
        assert "Dry run" in result.output
        assert "SYNTHETIC RETIRED 0" in result.output  # the sample
        assert _live_balance(CARD) == before
        with get_sessionmaker()() as session:
            assert (
                session.execute(
                    select(AuditLog).where(AuditLog.action == "account.purge_retired_rows")
                ).first()
                is None
            )

    def test_apply_voids_pending_deletes_posted_and_spares_the_rest(self, world: None) -> None:
        qif_at = _qif_batch()
        retired_at, (posted_a, posted_b, pending, voided) = _retired_batch(
            [("50.00", "posted"), ("20.00", "posted"), ("7.25", "pending"), ("3.00", "pending")],
            reviewed=True,  # every pre-0021 row carries a backfill mark
            void_last=True,
        )
        other_at, (other,) = _retired_batch([("99.00", "posted")])
        before = _live_balance(CARD)
        plan = _plan([retired_at])

        summary = _purge([retired_at])

        assert {k: summary[k] for k in plan.counts()} == plan.counts()
        assert summary["reviews_deleted"] == 2
        assert _status(posted_a) is None and _status(posted_b) is None  # hard-deleted
        assert _status(pending) == "void"  # the normal void path
        assert _status(voided) == "void"  # already void: lineage, untouched
        assert _status(other) == "posted"  # another batch
        # Exactly the batch's live amounts left the account; the QIF row stays.
        assert _live_balance(CARD) == before - Decimal("77.25")
        with get_sessionmaker()() as session:
            # The voided rows stay listed as void lineage; nothing else moved.
            remaining = retired_rows.list_batches(
                session, retired_rows.resolve_account(session, CARD)
            )
            assert [(b.created_at, b.status_counts) for b in remaining] == [
                (qif_at, {"posted": 1}),
                (retired_at, {"void": 2}),
                (other_at, {"posted": 1}),
            ]
            # Stored running balances agree with the ledger after the commit hook.
            acct = ledger.account_by_path(session, CARD)
            newest = session.execute(
                text(
                    "SELECT balance FROM account_running_balances WHERE account_id = :a "
                    f"ORDER BY {balances.REGISTER_ORDER_DESC_SQL} LIMIT 1"
                ),
                {"a": acct.id},
            ).scalar_one()
            assert Decimal(newest) == _live_balance(CARD)
            row = session.execute(
                select(AuditLog).where(AuditLog.action == "account.purge_retired_rows")
            ).scalar_one()
            assert row.before_after["before"]["transactions_deleted"] == 2
            assert set(row.before_after["before"]["deleted_ids"]) == {str(posted_a), str(posted_b)}
            assert (
                session.execute(select(Event).where(Event.type == "account.retired_rows_purged"))
                .scalar_one()
                .payload["transactions_voided"]
                == 1
            )
            assert session.execute(
                select(AuditLog).where(
                    AuditLog.action == "transaction.void", AuditLog.entity_id == pending
                )
            ).first()

    def test_cli_apply_runs_the_previewed_plan(self, world: None) -> None:
        retired_at, (row,) = _retired_batch([("50.00", "posted")])
        from purser.cli import app

        result = CliRunner().invoke(
            app,
            ["admin", "purge-retired-rows", CARD, "--batch", retired_at.isoformat(), "--apply"],
        )
        assert result.exit_code == 0, result.output
        assert "pg_dump" in result.output
        assert "1 deleted" in result.output
        assert _status(row) is None

    def test_rows_of_other_origins_are_never_selected(self, world: None) -> None:
        """Only import rows can lose a source: a human row created in the
        same transaction as a retired batch is not part of it."""
        token_id = _commit_token()
        with get_sessionmaker()() as session:
            card = ledger.account_by_path(session, CARD)
            category = ledger.ensure_category_path(
                session, "expenses.synthetic", actor_token_id=None
            )
            human = ledger.create_flat_transaction(
                session,
                account_ref=str(card.id),
                category_ref=str(category.id),
                amount=Decimal("-4.00"),
                date=ANCHOR,
                description="hand entry",
                requested_status=core.Status.POSTED,
                token=_token(session, token_id),
            )
            imported = ledger.create_flat_transaction(
                session,
                account_ref=str(card.id),
                category_ref=str(category.id),
                amount=Decimal("-6.00"),
                date=ANCHOR,
                description="retired",
                requested_status=core.Status.POSTED,
                origin=core.Origin.IMPORT,
                token=None,
                content_hash="retired-mixed",
            )
            session.commit()
            stamp, human_id, imported_id = imported.created_at, human.id, imported.id
        assert _plan([stamp]).counts()["rows"] == 1
        _purge([stamp])
        assert _status(human_id) == "posted"
        assert _status(imported_id) is None


# --------------------------------------------------------------------------
# Links: refused unless --unlink
# --------------------------------------------------------------------------


class TestLinks:
    def test_confirmed_match_refuses_then_unmatches_and_the_bill_returns(self, world: None) -> None:
        token_id = _commit_token()
        with get_sessionmaker()() as session:
            card = ledger.account_by_path(session, CARD)
            bills = ledger.ensure_category_path(session, "expenses.bills", actor_token_id=None)
            bill = ledger.create_flat_transaction(
                session,
                account_ref=str(card.id),
                category_ref=str(bills.id),
                amount=Decimal("-30.00"),
                date=ANCHOR,
                description="synthetic bill",
                requested_status=core.Status.EXPECTED,
                token=_token(session, token_id),
            )
            session.commit()
            bill_id = bill.id
        retired_at, (row,) = _retired_batch([("-30.00", "posted")])
        with get_sessionmaker()() as session:
            matching.manual_match(
                session, expected_id=bill_id, posted_id=row, token=_token(session, token_id)
            )
            session.commit()

        plan = _plan([retired_at])
        assert plan.counts()["matches_unmatched"] == 1
        assert "--unlink" in (plan.refusal(unlink=False) or "")
        with pytest.raises(RepairRefused, match="--unlink"):
            _purge([retired_at])
        assert _status(row) == "posted"  # refused before any write

        summary = _purge([retired_at], unlink=True)
        assert summary["matches_unmatched"] == 1 and summary["transactions_deleted"] == 1
        with get_sessionmaker()() as session:
            # The bill the retired row settled is expected again.
            recreated = session.execute(
                select(Transaction)
                .join(Posting, Posting.transaction_id == Transaction.id)
                .where(
                    Posting.account_id == ledger.account_by_path(session, CARD).id,
                    Transaction.status == "expected",
                )
            ).scalar_one()
            assert recreated.description == "synthetic bill"
            assert session.execute(select(func.count()).select_from(Match)).scalar_one() == 0

    def test_merged_transfer_unlinks_and_the_counterpart_survives(self, world: None) -> None:
        token_id = _commit_token()
        retired_at, (outgoing,) = _retired_batch([("-100.00", "posted")])
        with get_sessionmaker()() as session:
            savings = ledger.account_by_path(session, SAVINGS)
            category = ledger.ensure_category_path(
                session, "income.uncategorized", actor_token_id=None
            )
            incoming = ledger.create_flat_transaction(
                session,
                account_ref=str(savings.id),
                category_ref=str(category.id),
                amount=Decimal("100.00"),
                date=ANCHOR,
                description="synthetic deposit",
                requested_status=core.Status.POSTED,
                origin=core.Origin.IMPORT,
                token=None,
                content_hash="counterpart-hash",
            )
            matching.confirm_transfer(
                session,
                outgoing_id=outgoing,
                incoming_id=incoming.id,
                token=_token(session, token_id),
            )
            session.commit()
        savings_before = _live_balance(SAVINGS)
        card_before = _live_balance(CARD)

        plan = _plan([retired_at])
        assert plan.counts()["transfers_unlinked"] == 1
        assert plan.resurrected_delete == 1
        assert plan.refusal(unlink=False) is not None

        summary = _purge([retired_at], unlink=True)
        assert summary["transfers_unlinked"] == 1 and summary["transactions_deleted"] == 1
        # The counterpart's bank line is live again on its own account,
        # carrying its hash; the card lost exactly the retired row.
        with get_sessionmaker()() as session:
            survivor = session.execute(
                select(Transaction).where(Transaction.content_hash == "counterpart-hash")
            ).scalar_one()
            assert survivor.status == "posted"
        assert _live_balance(SAVINGS) == savings_before
        assert _live_balance(CARD) == card_before + Decimal("100.00")
        assert _status(outgoing) == "void"  # the merged original stays as lineage

    def test_corrected_rows_refuse_even_with_unlink(self, world: None) -> None:
        token_id = _commit_token()
        retired_at, (row,) = _retired_batch([("50.00", "posted")])
        with get_sessionmaker()() as session:
            ledger.ensure_category_path(session, "income.refunds", actor_token_id=None)
            ledger.update_transaction(
                session,
                tx_id=row,
                token=_token(session, token_id),
                category_ref="income.refunds",
            )
            session.commit()
        plan = _plan([retired_at])
        assert plan.blocked and "corrected" in plan.blocked[0]
        with pytest.raises(RepairRefused, match="corrected"):
            _purge([retired_at], unlink=True)
        assert _status(row) == "posted"

    def test_rows_posting_to_another_account_refuse(self, world: None) -> None:
        token_id = _commit_token()
        retired_at, (row,) = _retired_batch([("-40.00", "pending")])
        with get_sessionmaker()() as session:
            ledger.update_transaction(
                session, tx_id=row, token=_token(session, token_id), category_ref=f"[{SAVINGS}]"
            )
            session.commit()
        plan = _plan([retired_at])
        assert plan.blocked and "another real account" in plan.blocked[0]
        with pytest.raises(RepairRefused, match="another real account"):
            _purge([retired_at], unlink=True)


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


class TestRefusals:
    def test_unknown_batch(self, world: None) -> None:
        _retired_batch([("50.00", "posted")])
        stray = datetime.fromisoformat("2026-01-01T00:00:00+00:00")
        assert "no retired-origin rows" in (_plan([stray]).refusal(unlink=True) or "")
        with pytest.raises(RepairRefused, match="no retired-origin rows"):
            _purge([stray])

    def test_category_and_closed_accounts(self, world: None) -> None:
        token_id = _commit_token()
        with get_sessionmaker()() as session:
            ledger.ensure_category_path(session, "expenses.synthetic", actor_token_id=None)
            session.commit()
            with pytest.raises(RepairRefused, match="category"):
                retired_rows.resolve_account(session, "expenses.synthetic")
            ledger.close_account(
                session,
                account_id=ledger.account_by_path(session, SAVINGS).id,
                adjustment_category=None,
                token=_token(session, token_id),
            )
            session.commit()
            with pytest.raises(RepairRefused, match="closed"):
                retired_rows.resolve_account(session, SAVINGS)

    def test_batch_timestamps_need_their_offset(self) -> None:
        with pytest.raises(RepairRefused, match="UTC offset"):
            retired_rows.parse_batch("2026-07-08T14:03:11.123456")
        with pytest.raises(RepairRefused, match="ISO 8601"):
            retired_rows.parse_batch("last tuesday")
        stamp = retired_rows.parse_batch("2026-07-08T14:03:11.123456+00:00")
        assert stamp.microsecond == 123456


# --------------------------------------------------------------------------
# Property: the purge removes exactly the batch's live footprint
# --------------------------------------------------------------------------

_amounts = st.decimals(
    min_value=Decimal("-999.99"),
    max_value=Decimal("999.99"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
).filter(lambda d: d != 0)
_rows = st.lists(
    st.tuples(_amounts, st.sampled_from(["pending", "posted"])), min_size=1, max_size=6
)


class TestProperty:
    @settings(
        max_examples=10,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(doomed=_rows, kept=_rows, elsewhere=_rows)
    def test_purge_removes_exactly_the_batch(
        self,
        migrated_engine: Engine,
        doomed: list[tuple[Decimal, str]],
        kept: list[tuple[Decimal, str]],
        elsewhere: list[tuple[Decimal, str]],
    ) -> None:
        """Whatever the batch holds: the account loses exactly its live
        amounts, another batch on it and every other account are untouched,
        the stored running balances agree with the ledger, and the executed
        counts equal the preview."""
        reset_db(migrated_engine)
        _accounts()
        as_rows = [(str(a), s) for a, s in doomed]
        batch_at, _ = _retired_batch(as_rows, reviewed=True)
        _retired_batch([(str(a), s) for a, s in kept])
        _retired_batch([(str(a), s) for a, s in elsewhere], account=SAVINGS)
        card_before, savings_before = _live_balance(CARD), _live_balance(SAVINGS)

        plan = _plan([batch_at])
        summary = _purge([batch_at])

        assert {k: summary[k] for k in plan.counts()} == plan.counts()
        assert summary["transactions_voided"] == sum(1 for _a, s in doomed if s == "pending")
        assert summary["transactions_deleted"] == sum(1 for _a, s in doomed if s == "posted")
        assert _live_balance(CARD) == card_before - sum((a for a, _s in doomed), Decimal("0"))
        assert _live_balance(SAVINGS) == savings_before
        with get_sessionmaker()() as session:
            assert (
                session.execute(
                    select(func.count())
                    .select_from(TransactionReview)
                    .where(TransactionReview.transaction_id.not_in(select(Transaction.id)))
                ).scalar_one()
                == 0
            )
            for path in (CARD, SAVINGS):
                acct = session.execute(select(Account).where(Account.path == path)).scalar_one()
                newest = session.execute(
                    text(
                        "SELECT balance FROM account_running_balances WHERE account_id = :a "
                        f"ORDER BY {balances.REGISTER_ORDER_DESC_SQL} LIMIT 1"
                    ),
                    {"a": acct.id},
                ).scalar_one_or_none()
                assert Decimal(newest or 0) == _live_balance(path)
