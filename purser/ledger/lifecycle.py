"""Account close and reopen (DESIGN §3.4). Apart from accounts because a
close posts its balancing adjustment through tx_create."""

import uuid
from datetime import UTC, datetime
from datetime import date as date_type
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from purser import audit, balances, events, locks
from purser.core import ledger as core
from purser.ledger.accounts import ensure_category_path, get_account
from purser.ledger.errors import LedgerError
from purser.ledger.reporting import account_balances
from purser.ledger.tx_create import create_flat_transaction
from purser.models import Account, ApiToken, Posting, Schedule, Transaction


def _resolve_adjustment_category(
    session: Session, ref: str, *, actor_token_id: uuid.UUID | None
) -> Account:
    """A UUID resolves to an existing category; anything else is treated as an
    ltree path and auto-created if absent (the close flow's default is
    ``expenses.adjustments``)."""
    try:
        uuid.UUID(ref)
    except ValueError:
        category = ensure_category_path(session, ref, actor_token_id=actor_token_id)
    else:
        category = get_account(session, uuid.UUID(ref))
    if not category.is_virtual:
        raise LedgerError(f"{category.path} is not a category (virtual account)")
    return category


def _lock(session: Session, account_id: uuid.UUID) -> Account:
    """The account row, locked and read fresh (``purser.locks``)."""
    account = locks.lock_account(session, account_id)
    assert account is not None  # the caller just read it
    return account


def close_account(
    session: Session,
    *,
    account_id: uuid.UUID,
    adjustment_category: str | None,
    token: ApiToken,
) -> dict[str, Any]:
    """Close a REAL account (DESIGN §3.4): void its expected instances, post a
    guided balancing adjustment as an ordinary transaction, verify exact-zero,
    unmap any SimpleFIN link, then stamp ``closed_at`` (read-only thereafter).

    Preconditions refuse the close (LedgerError -> 422) rather than papering
    over live state: no active schedules target the account, and no live
    pending transactions touch it. Corrections after close require reopen.
    """
    account = get_account(session, account_id)
    if account.is_virtual:
        raise LedgerError(f"{account.path} is a category; categories are not closed")
    # Account before watermark (#403): the row is locked before the closing
    # adjustment marks the balance watermark, the order every account writer
    # (holdings, reconciliation, the importer) keeps, so none can deadlock it.
    account = _lock(session, account.id)
    if account.closed_at is not None:
        raise LedgerError(f"{account.path} is already closed")

    # (b) Preconditions.
    active = list(
        session.execute(
            select(Schedule).where(Schedule.account_id == account.id, Schedule.status == "active")
        ).scalars()
    )
    if active:
        names = ", ".join(sorted(s.name for s in active))
        raise LedgerError(
            f"{account.path} has active schedules targeting it ({names}); "
            "pause or delete them before closing"
        )
    pending = session.execute(
        select(func.count(func.distinct(Transaction.id)))
        .select_from(Transaction)
        .join(Posting, Posting.transaction_id == Transaction.id)
        .where(Posting.account_id == account.id, Transaction.status == "pending")
    ).scalar_one()
    if pending:
        raise LedgerError(
            f"{account.path} has {pending} live pending transaction(s); resolve them before closing"
        )

    # (c) Auto-void expected instances (expected -> void is a legal core
    # transition; done in-service, per-row audit in transition_status' shape).
    expected = list(
        session.execute(
            select(Transaction)
            .join(Posting, Posting.transaction_id == Transaction.id)
            .where(
                Posting.account_id == account.id,
                Transaction.status == core.Status.EXPECTED.value,
                Transaction.superseded_by.is_(None),
            )
            .distinct()
        ).scalars()
    )
    voided_ids: list[str] = []
    for tx in expected:
        tx.status = core.Status.VOID.value
        session.flush()
        audit.record(
            session,
            actor_token_id=token.id,
            action="transaction.void",
            entity_type="transaction",
            entity_id=tx.id,
            before={"status": core.Status.EXPECTED.value},
            after={"status": core.Status.VOID.value, "reason": "account.close"},
        )
        voided_ids.append(str(tx.id))

    # (d) Guided balance adjustment, posted BEFORE the account goes read-only.
    balance = account_balances(session, [account.id]).get(account.id, Decimal(0))
    adjustment_tx_id: uuid.UUID | None = None
    if balance != 0:
        if adjustment_category is None:
            raise LedgerError(
                f"{account.path} has a live balance of {balance}; provide "
                "adjustment_category to post a closing balance adjustment"
            )
        category = _resolve_adjustment_category(
            session, adjustment_category, actor_token_id=token.id
        )
        adjustment = create_flat_transaction(
            session,
            account_ref=str(account.id),
            category_ref=str(category.id),
            amount=-balance,
            date=date_type.today(),
            description="Closing adjustment",
            requested_status=core.Status.POSTED,
            token=token,
        )
        adjustment_tx_id = adjustment.id

    # (e) Exact-zero check.
    balance = account_balances(session, [account.id]).get(account.id, Decimal(0))
    if balance != Decimal("0"):
        raise LedgerError(
            f"{account.path} still has a live balance of {balance} after adjustment; cannot close"
        )

    # (f) Unmap any SimpleFIN link (the feed account reappears as unmapped in
    # discovery — honest); mirrors feed.service.map_account's holder clearing.
    unmapped = account.connection_id is not None or account.external_id is not None
    if unmapped:
        from purser.holdings import service as holdings_service

        # A manual period still waiting for the feed ends today (decision 88).
        holdings_service.end_period_unmapped(session, account, actor_token_id=token.id)
        before = {
            "external_id": account.external_id,
            "connection_id": str(account.connection_id) if account.connection_id else None,
        }
        account.external_id = None
        account.connection_id = None
        audit.record(
            session,
            actor_token_id=token.id,
            action="account.unmap",
            entity_type="account",
            entity_id=account.id,
            before=before,
            after={"external_id": None, "connection_id": None, "reason": "closed"},
        )

    # (g) Close.
    account.closed_at = datetime.now(UTC)
    session.flush()
    # Net-worth inclusion flips at the close date (valuations/holdings are
    # suppressed from it onward); postings were zeroed via the funnel-marked
    # adjustment, so only the networth cursor needs the close date.
    balances.mark_dirty(session, account.closed_at.date(), networth_only=True)
    audit.record(
        session,
        actor_token_id=token.id,
        action="account.close",
        entity_type="account",
        entity_id=account.id,
        after={
            "closed_at": account.closed_at.isoformat(),
            "adjustment_tx_id": str(adjustment_tx_id) if adjustment_tx_id else None,
            "voided_expected_ids": voided_ids,
            "unmapped": unmapped,
        },
    )
    events.emit(
        session,
        type_="account.closed",
        entity_type="account",
        entity_id=account.id,
        payload={"path": account.path, "closed_at": account.closed_at.isoformat()},
    )
    return {"closed_at": account.closed_at, "adjustment_tx_id": adjustment_tx_id}


def reopen_account(session: Session, *, account_id: uuid.UUID, token: ApiToken) -> Account:
    """Clear ``closed_at`` so the account accepts writes again (DESIGN §3.4)."""
    account = get_account(session, account_id)
    if account.is_virtual:
        raise LedgerError(f"{account.path} is a category; categories are not closed")
    if account.closed_at is None:
        raise LedgerError(f"{account.path} is not closed")
    before = {"closed_at": account.closed_at.isoformat()}
    reopened_from = account.closed_at.date()
    account.closed_at = None
    session.flush()
    # Valuations/holdings re-enter net worth from the old close date onward.
    balances.mark_dirty(session, reopened_from, networth_only=True)
    audit.record(
        session,
        actor_token_id=token.id,
        action="account.reopen",
        entity_type="account",
        entity_id=account.id,
        before=before,
        after={"closed_at": None},
    )
    events.emit(
        session,
        type_="account.reopened",
        entity_type="account",
        entity_id=account.id,
        payload={"path": account.path},
    )
    return account
