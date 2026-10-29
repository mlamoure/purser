"""Guarded `purser admin delete-account` (issue #124, DESIGN §3.4 decision 33).

Hard delete FREES the account's `qif:`-namespaced content hashes so a
mis-imported file can be re-imported onto the right account — the thing voiding
could never do. Service functions carry the logic (the CLI is a thin shell);
these tests drive the service directly inside one uncommitted db_session
transaction (rolled back at teardown), plus two CliRunner smokes.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from purser import ledger, matching, qif_import, review, transfers
from purser.admin import account_delete
from purser.admin.account_delete import DeleteRefused
from purser.core import ledger as core
from purser.models import (
    Account,
    ApiToken,
    AuditLog,
    Connection,
    Posting,
    RawFetch,
    Suggestion,
    Transaction,
    TransactionReview,
    User,
)

# A single-account QIF (parser's __default__ account); two plain rows.
SINGLE_QIF = """!Type:Bank
D01/15/2026
T-50.00
PGrocery Store
^
D01/16/2026
T-25.00
PCoffee Shop
^
"""


@pytest.fixture(autouse=True)
def _isolate(clean_db: None) -> None:
    """These service tests use fixed account paths and don't commit; truncate
    first so committed rows from earlier test files can't collide."""


def _commit_token(session: Session, suffix: str) -> ApiToken:
    user = User(email=f"del_{suffix}@example.com")
    session.add(user)
    session.flush()
    token = ApiToken(
        user_id=user.id, name=f"tok_{suffix}", token_hash=uuid.uuid4().hex, scopes=["write"]
    )
    session.add(token)
    session.flush()
    return token


def _account(session: Session, token: ApiToken, name: str, path: str) -> Account:
    return ledger.create_account(
        session, name=name, type_="checking", path=path, actor_token_id=token.id
    )


def test_preflight_dry_run_changes_nothing(db_session: Session) -> None:
    token = _commit_token(db_session, "dry")
    dupe = _account(db_session, token, "Dupe", "assets.dupe")
    qif_import.import_qif(
        db_session, content=SINGLE_QIF, account_map={"__default__": str(dupe.id)}, token=token
    )
    before = db_session.execute(select(func.count()).select_from(Transaction)).scalar_one()

    account = account_delete.resolve_and_check(db_session, "assets.dupe")
    plan = account_delete.preflight(db_session, account)
    assert plan["doomed_count"] == 2
    assert plan["status_counts"] == {"posted": 2}

    # preflight is read-only: nothing changed, account still present.
    after = db_session.execute(select(func.count()).select_from(Transaction)).scalar_one()
    assert after == before
    assert db_session.get(Account, dupe.id) is not None


def test_delete_frees_hashes_for_reimport(db_session: Session) -> None:
    token = _commit_token(db_session, "free")
    dupe = _account(db_session, token, "Dupe", "assets.dupe")
    other = _account(db_session, token, "Other", "assets.other")

    stats = qif_import.import_qif(
        db_session, content=SINGLE_QIF, account_map={"__default__": str(dupe.id)}, token=token
    )
    assert stats["imported"] == 2
    # QIF hashes are content-namespaced (qif:__default__) — re-importing the
    # same file into ANOTHER account is squatted (skipped) until dupe is gone.
    squatted = qif_import.import_qif(
        db_session, content=SINGLE_QIF, account_map={"__default__": str(other.id)}, token=token
    )
    assert squatted["imported"] == 0 and squatted["skipped"] == 2

    raw_before = db_session.execute(select(func.count()).select_from(RawFetch)).scalar_one()
    account = account_delete.resolve_and_check(db_session, str(dupe.id))
    summary = account_delete.execute_delete(db_session, account, actor_token_id=token.id)
    assert summary["transactions_deleted"] == 2

    assert db_session.get(Account, dupe.id) is None
    # raw_fetches is never touched.
    assert db_session.execute(select(func.count()).select_from(RawFetch)).scalar_one() == raw_before
    # Hashes freed: the same content now imports onto `other`.
    freed = qif_import.import_qif(
        db_session, content=SINGLE_QIF, account_map={"__default__": str(other.id)}, token=token
    )
    assert freed["imported"] == 2
    # The delete is audited (filter by entity so committed rows from other
    # tests can't perturb this).
    assert (
        db_session.execute(
            select(AuditLog).where(
                AuditLog.action == "account.delete", AuditLog.entity_id == dupe.id
            )
        ).scalar_one_or_none()
        is not None
    )


def test_delete_unlinks_merge_and_counterpart_survives(db_session: Session) -> None:
    token = _commit_token(db_session, "merge")
    dupe = _account(db_session, token, "Dupe", "assets.dupe")
    checking = _account(db_session, token, "Checking", "assets.checking")
    cat = ledger.ensure_category_path(db_session, "income.seedm", actor_token_id=token.id)

    outgoing = ledger.create_flat_transaction(
        db_session,
        account_ref=str(dupe.id),
        category_ref=str(cat.id),
        amount=Decimal("-100.00"),
        date=date.today(),
        description="transfer to checking",
        requested_status=core.Status.POSTED,
        origin=core.Origin.IMPORT,
        token=token,
        content_hash="hash-dupe",
    )
    incoming = ledger.create_flat_transaction(
        db_session,
        account_ref=str(checking.id),
        category_ref=str(cat.id),
        amount=Decimal("100.00"),
        date=date.today(),
        description="transfer from dupe",
        requested_status=core.Status.POSTED,
        origin=core.Origin.IMPORT,
        token=token,
        content_hash="hash-checking",
    )
    merged = matching.confirm_transfer(
        db_session, outgoing_id=outgoing.id, incoming_id=incoming.id, token=token
    )
    assert transfers.has_linked_originals(db_session, merged.id)

    account = account_delete.resolve_and_check(db_session, str(dupe.id))
    summary = account_delete.execute_delete(db_session, account, actor_token_id=token.id)
    assert summary["transfers_unlinked"] == 1

    assert db_session.get(Account, dupe.id) is None
    # dupe has no postings left at all.
    assert (
        db_session.execute(
            select(func.count()).select_from(Posting).where(Posting.account_id == dupe.id)
        ).scalar_one()
        == 0
    )
    # The counterpart clone survives on checking, LIVE, carrying its hash.
    survivor = (
        db_session.execute(
            select(Transaction)
            .join(Posting, Posting.transaction_id == Transaction.id)
            .where(
                Posting.account_id == checking.id,
                Transaction.status.in_(["pending", "posted"]),
                Transaction.content_hash == "hash-checking",
            )
        )
        .scalars()
        .first()
    )
    assert survivor is not None


def test_delete_unlinks_a_split_loan_payment_and_counterpart_survives(
    db_session: Session,
) -> None:
    """A split loan payment (decision 62) is a merge too: purging the paying
    account resurrects the loan's bank row and drops the interest posting."""
    token = _commit_token(db_session, "split")
    dupe = _account(db_session, token, "Dupe", "assets.dupe")
    loan = ledger.create_account(
        db_session,
        name="Car Loan",
        type_="loan",
        path="liabilities.carloan",
        actor_token_id=token.id,
    )
    cat = ledger.ensure_category_path(db_session, "income.seeds", actor_token_id=token.id)
    interest = ledger.ensure_category_path(
        db_session, "expenses.fees.interest", actor_token_id=token.id
    )
    rows = {}
    for account, amount, content_hash in (
        (dupe, "-612.40", "hash-split-dupe"),
        (loan, "540.15", "hash-split-loan"),
    ):
        rows[content_hash] = ledger.create_flat_transaction(
            db_session,
            account_ref=str(account.id),
            category_ref=str(cat.id),
            amount=Decimal(amount),
            date=date(2026, 7, 15),
            description="loan payment",
            requested_status=core.Status.POSTED,
            origin=core.Origin.IMPORT,
            token=token,
            content_hash=content_hash,
        )
    merged = matching.confirm_transfer(
        db_session,
        outgoing_id=rows["hash-split-dupe"].id,
        incoming_id=rows["hash-split-loan"].id,
        token=token,
        remainder_category_ref="expenses.fees.interest",
    )
    remainder = transfers.split_remainder(merged)
    assert remainder is not None and Decimal(remainder.amount) == Decimal("72.25")

    account = account_delete.resolve_and_check(db_session, str(dupe.id))
    assert account_delete.preflight(db_session, account)["merge_transfers"] == 1
    summary = account_delete.execute_delete(db_session, account, actor_token_id=token.id)
    assert summary["transfers_unlinked"] == 1

    # The loan's bank line survives LIVE on its own account, carrying its hash...
    survivor = (
        db_session.execute(
            select(Transaction)
            .join(Posting, Posting.transaction_id == Transaction.id)
            .where(
                Posting.account_id == loan.id,
                Transaction.status.in_(["pending", "posted"]),
                Transaction.content_hash == "hash-split-loan",
            )
        )
        .scalars()
        .first()
    )
    assert survivor is not None
    # ...and nothing books to the interest category any more.
    assert (
        db_session.execute(
            select(func.count()).select_from(Posting).where(Posting.account_id == interest.id)
        ).scalar_one()
        == 0
    )


def test_delete_takes_review_marks_with_the_rows(db_session: Session) -> None:
    """Migration 0021 backfilled a review mark onto every row that existed
    then, and its foreign key has no cascade: the guarded delete removes the
    marks with their rows instead of failing on them (#284)."""
    token = _commit_token(db_session, "marks")
    dupe = _account(db_session, token, "Dupe", "assets.dupe")
    qif_import.import_qif(
        db_session, content=SINGLE_QIF, account_map={"__default__": str(dupe.id)}, token=token
    )
    rows = list(
        db_session.execute(
            select(Transaction.id)
            .join(Posting, Posting.transaction_id == Transaction.id)
            .where(Posting.account_id == dupe.id)
        ).scalars()
    )
    for tx_id in rows:
        review.mark(db_session, tx_id=tx_id, token=None, via=review.VIA_BACKFILL)

    account = account_delete.resolve_and_check(db_session, str(dupe.id))
    summary = account_delete.execute_delete(db_session, account, actor_token_id=token.id)
    assert summary["transactions_deleted"] == 2
    assert summary["reviews_deleted"] == 2
    assert (
        db_session.execute(
            select(func.count())
            .select_from(TransactionReview)
            .where(TransactionReview.transaction_id.in_(rows))
        ).scalar_one()
        == 0
    )


def test_delete_takes_the_suggestions_naming_the_account(db_session: Session) -> None:
    """Suggestions reference accounts with no ON DELETE, so before #399 any
    suggestion naming the account (open or decided) failed the final account
    delete with a foreign-key violation. They go with the account now, and a
    suggestion naming only another account survives."""
    token = _commit_token(db_session, "sugg")
    dupe = _account(db_session, token, "Dupe", "assets.dupe")
    other = _account(db_session, token, "Other", "assets.other")
    for fingerprint, status, subject, counterparty in (
        ("t399:open", "open", dupe.id, None),
        ("t399:rejected", "rejected", other.id, dupe.id),
        ("t399:survivor", "open", other.id, None),
    ):
        db_session.add(
            Suggestion(
                kind="schedule",
                status=status,
                fingerprint=fingerprint,
                source="detected",
                subject_account_id=subject,
                counterparty_account_id=counterparty,
                draft={"name": "synthetic"},
            )
        )
    db_session.flush()

    account = account_delete.resolve_and_check(db_session, "assets.dupe")
    assert account_delete.preflight(db_session, account)["suggestions"] == 2
    summary = account_delete.execute_delete(db_session, account, actor_token_id=token.id)
    assert summary["suggestions_deleted"] == 2
    assert db_session.get(Account, dupe.id) is None
    left = db_session.execute(select(Suggestion.fingerprint)).scalars().all()
    assert left == ["t399:survivor"]


def test_delete_on_closed_account_works(db_session: Session) -> None:
    """A closed account (unmapped, so eligible) deletes: the temporary reopen
    lets the delete run without tripping the closed-account guards."""
    token = _commit_token(db_session, "closed")
    dupe = _account(db_session, token, "Dupe", "assets.dupe")
    qif_import.import_qif(
        db_session, content=SINGLE_QIF, account_map={"__default__": str(dupe.id)}, token=token
    )
    ledger.close_account(
        db_session,
        account_id=dupe.id,
        adjustment_category="expenses.adjustments",
        token=token,
    )
    account = account_delete.resolve_and_check(db_session, str(dupe.id))
    summary = account_delete.execute_delete(db_session, account, actor_token_id=token.id)
    assert summary["was_closed"] is True
    assert db_session.get(Account, dupe.id) is None


class TestRefusals:
    def test_not_found(self, db_session: Session) -> None:
        with pytest.raises(ledger.NotFoundError):
            account_delete.resolve_and_check(db_session, "assets.nope")

    def test_virtual_category(self, db_session: Session) -> None:
        token = _commit_token(db_session, "virt")
        cat = ledger.ensure_category_path(db_session, "expenses.things", actor_token_id=token.id)
        with pytest.raises(DeleteRefused, match="category"):
            account_delete.resolve_and_check(db_session, str(cat.id))

    def test_simplefin_attached(self, db_session: Session) -> None:
        token = _commit_token(db_session, "sf")
        conn = Connection()
        db_session.add(conn)
        db_session.flush()
        mapped = _account(db_session, token, "Mapped", "assets.mapped")
        mapped.connection_id = conn.id
        mapped.external_id = "ext-1"
        db_session.flush()
        with pytest.raises(DeleteRefused, match="SimpleFIN"):
            account_delete.resolve_and_check(db_session, str(mapped.id))

    def test_bare_external_id_is_not_feed_attachment(self, db_session: Session) -> None:
        """QIF-created accounts carry the file's account id with NO connection —
        exactly the accounts this tool exists to remove. Only a live
        connection mapping refuses (issue #124)."""
        token = _commit_token(db_session, "qif")
        orphan = _account(db_session, token, "QIF Import", "assets.qif_orphan")
        orphan.external_id = "A9UVQ5PQGR8SICQ"
        db_session.flush()
        resolved = account_delete.resolve_and_check(db_session, str(orphan.id))
        assert resolved.id == orphan.id

    def test_has_children(self, db_session: Session) -> None:
        token = _commit_token(db_session, "kids")
        parent = _account(db_session, token, "Parent", "assets.parent")
        ledger.create_account(
            db_session,
            name="Child",
            type_="checking",
            parent_id=parent.id,
            path="assets.parent.child",
            actor_token_id=token.id,
        )
        with pytest.raises(DeleteRefused, match="child"):
            account_delete.resolve_and_check(db_session, str(parent.id))


# --------------------------------------------------------------------------
# CLI smokes (typer command wiring)
# --------------------------------------------------------------------------


def test_cli_not_found_exits_1(migrated_engine: object) -> None:
    from purser.cli import app

    result = CliRunner().invoke(app, ["admin", "delete-account", "assets.no_such_account"])
    assert result.exit_code == 1
    assert "not found" in result.output.lower()


def test_cli_execute_deletes(db_session: Session) -> None:
    # A system-actor account (no token/user) so the CLI's committed delete
    # leaves no cross-test residue but its own actor-null audit rows.
    ledger.create_account(
        db_session, name="Smoke", type_="checking", path="assets.smoke_cli", actor_token_id=None
    )
    db_session.commit()  # the CLI opens its own session; it must see committed state

    from purser.cli import app

    result = CliRunner().invoke(app, ["admin", "delete-account", "assets.smoke_cli", "--yes"])
    assert result.exit_code == 0, result.output
    assert "pg_dump" in result.output  # the banner printed on execute
    assert "Deleted" in result.output
    db_session.expire_all()
    assert (
        db_session.execute(
            select(Account).where(Account.path == "assets.smoke_cli")
        ).scalar_one_or_none()
        is None
    )
