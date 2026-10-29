"""Corrected bank history counts once and never settles bills with reversals (#419/#420)."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from purser import ledger, matching, schedules
from purser.core import ledger as core
from purser.models import Transaction

DAY = date(2026, 7, 15)


@pytest.fixture
def bank_row(clean_db: None, db_session: Session) -> Transaction:
    for name, type_, path in [
        ("Checking", "checking", "assets.checking"),
        ("Rent", "category_expense", "expenses.rent"),
    ]:
        ledger.create_account(db_session, name=name, type_=type_, path=path, actor_token_id=None)
    return ledger.create_flat_transaction(
        db_session,
        account_ref="assets.checking",
        category_ref="expenses.rent",
        amount=Decimal("-1500"),
        date=DAY,
        payee_name="Example Rentals",
        requested_status=core.Status.POSTED,
        origin=core.Origin.IMPORT,
        token=None,
    )


def correct(session: Session, tx: Transaction, origin: core.Origin) -> Transaction:
    return ledger.reverse_and_replace_with_specs(
        session,
        tx=tx,
        token=None,
        replacement_specs=ledger._specs_of(tx),
        description="Corrected description",
        origin=origin,
    )


@pytest.mark.parametrize("origin", [core.Origin.HUMAN, core.Origin.RULE, core.Origin.AGENT])
def test_recurrence_counts_only_latest_replacement(
    bank_row: Transaction, db_session: Session, origin: core.Origin
) -> None:
    replacement = correct(db_session, bank_row, origin)
    replacement = correct(db_session, replacement, origin)
    rows = schedules._history_rows(db_session, DAY)
    assert [row.id for row in rows] == [replacement.id]
    amount = sum(
        (Decimal(p.amount) for row in rows for p in row.postings if not p.account.is_virtual),
        Decimal("0"),
    )
    assert amount == Decimal("-1500")


@pytest.mark.parametrize("origin", [core.Origin.HUMAN, core.Origin.RULE, core.Origin.AGENT])
def test_match_candidates_follow_bank_corrections_but_exclude_reversals(
    bank_row: Transaction, db_session: Session, origin: core.Origin
) -> None:
    replacement = correct(db_session, bank_row, origin)
    replacement = correct(db_session, replacement, origin)
    account = ledger.resolve_account(db_session, "assets.checking")
    candidates = matching._candidates_for(db_session, account.id, DAY)
    assert [row.id for row in candidates] == [replacement.id]


def test_unrelated_system_entry_cannot_settle_a_bill(
    bank_row: Transaction, db_session: Session
) -> None:
    synthetic = ledger.create_flat_transaction(
        db_session,
        account_ref="assets.checking",
        category_ref="expenses.rent",
        amount=Decimal("-500"),
        date=DAY,
        requested_status=core.Status.POSTED,
        origin=core.Origin.SYSTEM,
        token=None,
    )
    account = ledger.resolve_account(db_session, "assets.checking")
    candidates = matching._candidates_for(db_session, account.id, DAY)
    assert synthetic.id not in {row.id for row in candidates}
    assert bank_row.id in {row.id for row in candidates}
