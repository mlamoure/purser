"""An account's starting balance from the bank (issue #585, DESIGN §7.5,
decision 123).

A bank feed delivers about three months of transactions and no balance from
before them, so a newly mapped account reads far from what the bank reports.
The starting balance is ONE system entry that stands for everything before
the first transaction Purser holds: the account against
``equity.opening_balances`` (neither income nor spending), dated the day
before the account's earliest row, so every balance from that row onward
agrees with the bank.

Purser derives it; nobody passes a date, a sign or a basis:

    amount = reported - ledger(basis) - older

- ``reported``: the bank's balance in the latest sync's fetch, in the feed's
  own sign (a card is negative when owed, as in the ledger).
- ``ledger(basis)``: the account's sum over ALL recorded dates on the drift
  check's basis (§7.3) at the moment it was set, so rows the bank dates ahead
  of the ledger day are counted and there is no day to wait for.
- ``older``: every row that has since arrived dated before the ``horizon``,
  the earliest row the account held when it was set. The starting balance
  stood for exactly those rows, so it gives way by their sum and moves to
  before the new earliest one: importing older history never changes a
  balance on or after the horizon.

What it is not: a reconciliation (§3.7). It records no claim and moves no
marker; a person's "the statement said X on D" stays an explicit write. It is
refused where it would contradict one (a reconciled account that does not
already agree with the bank), and it follows the bank's learned basis only
while no reconciliation exists.

:func:`reseat` recomputes the entry from the stored facts and replaces it
through the ledger's correction pair when it differs. It runs after every
sync, backfill, refetch, archive replay, manual import and mapping, each
account in its own savepoint: like the drift check and the brokerage true-up,
a failure never fails the import.
"""

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Any

import structlog
from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session, aliased

from purser import audit, clock, events, ledger
from purser.core import ledger as core
from purser.feed import drift
from purser.models import (
    Account,
    AccountStartingBalance,
    Posting,
    Transaction,
)

log = structlog.get_logger()

PAYEE = "Starting balance"
DESCRIPTION = "Balance before the first transaction, from the bank's reported balance"
REMOVED_DESCRIPTION = "Starting balance removed"

# Why a starting balance was written, on the audit row and the event.
SET = "set"
OLDER_HISTORY = "older_history"
BASIS = "basis"
REMOVED = "removed"


class StartingBalanceError(ledger.LedgerError):
    """The account cannot take a starting balance from the bank (422).
    ``code`` names the reason for a caller that branches on it."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Outcome:
    """What a call did, or on a dry run would do. Amounts are in ledger sign
    (a card is negative when owed)."""

    account: Account
    status: str  # set | unchanged | removed | none
    amount: Decimal
    entry_date: date | None  # None when nothing is posted
    basis: str | None
    reported: Decimal | None  # the bank's balance it was derived from
    balance_date: date | None
    transaction_id: uuid.UUID | None
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "account_id": str(self.account.id),
            "status": self.status,
            "amount": str(self.amount),
            "entry_date": self.entry_date.isoformat() if self.entry_date else None,
            "basis": self.basis,
            "bank_balance": str(self.reported) if self.reported is not None else None,
            "bank_balance_date": self.balance_date.isoformat() if self.balance_date else None,
            "transaction_id": str(self.transaction_id) if self.transaction_id else None,
            "dry_run": self.dry_run,
        }


def _q(value: Decimal) -> Decimal:
    quantized = core.quantize(value)
    return quantized if quantized != 0 else core.quantize(Decimal(0))


def _lock(session: Session, account_id: uuid.UUID, *, skip_locked: bool = False) -> Account | None:
    """The account row, locked and read fresh (account before watermark,
    decision 88). An automatic pass never waits for another writer's lock: it
    may already hold the balance watermark that writer needs next, so a busy
    account is skipped and picked up by the next pass."""
    return session.execute(
        select(Account)
        .where(Account.id == account_id)
        .with_for_update(key_share=True, skip_locked=skip_locked)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _own_rows() -> Any:
    """Transactions that carry a leg on equity.opening_balances: the starting
    balance and every link of its correction chain."""
    opening = aliased(Account)
    leg = aliased(Posting)
    return (
        select(leg.transaction_id)
        .join(opening, opening.id == leg.account_id)
        .where(opening.path == ledger.OPENING_PATH)
    )


def _history(
    session: Session, account_id: uuid.UUID, *, before: date | None = None
) -> tuple[Decimal, date | None]:
    """(sum, earliest date) of the account's live rows other than its starting
    balance: chain heads, pending or posted. ``before`` restricts them to
    rows dated before that day."""
    reversal = aliased(Transaction)
    query = (
        select(func.coalesce(func.sum(Posting.amount), 0), func.min(Transaction.date))
        .join(Transaction, Transaction.id == Posting.transaction_id)
        .where(
            Posting.account_id == account_id,
            Transaction.status.in_(["pending", "posted"]),
            ~exists().where(reversal.reverses_id == Transaction.id),
            Transaction.id.not_in(_own_rows()),
        )
    )
    if before is not None:
        query = query.where(Transaction.date < before)
    total, earliest = session.execute(query).one()
    return _q(Decimal(total)), earliest


def get(session: Session, account_id: uuid.UUID) -> AccountStartingBalance | None:
    return session.get(AccountStartingBalance, account_id)


def entry_ids(session: Session, tx_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """Which of ``tx_ids`` are a starting balance as it stands now (one
    query, for the register's label)."""
    if not tx_ids:
        return set()
    found = session.execute(
        select(AccountStartingBalance.transaction_id).where(
            AccountStartingBalance.transaction_id.in_(tx_ids)
        )
    ).scalars()
    return {tx_id for tx_id in found if tx_id is not None}


def _entry_date(row: AccountStartingBalance, earliest: date | None) -> date:
    first = min(row.horizon, earliest) if earliest is not None else row.horizon
    return first - timedelta(days=1)


def _amount(row: AccountStartingBalance, basis: str, older: Decimal) -> Decimal:
    before = row.posted_before if basis == drift.POSTED_ONLY else row.pending_before
    return _q(Decimal(row.reported) - Decimal(before) - older)


def _describe(session: Session, account: Account, row: AccountStartingBalance) -> Outcome:
    tx = session.get(Transaction, row.transaction_id) if row.transaction_id else None
    return Outcome(
        account=account,
        status="unchanged",
        amount=_q(Decimal(row.amount)),
        entry_date=tx.date if tx is not None else None,
        basis=row.basis,
        reported=_q(Decimal(row.reported)),
        balance_date=row.balance_date,
        transaction_id=row.transaction_id,
    )


def _write(
    session: Session,
    account: Account,
    row: AccountStartingBalance,
    *,
    amount: Decimal,
    on: date,
    actor_token_id: uuid.UUID | None,
    reason: str,
) -> Transaction | None:
    """Make the ledger entry say ``amount`` on ``on``: post it when there is
    none yet, replace it through the correction pair otherwise (a posted row
    is corrected, never edited; zero keeps the chain with a zero head)."""
    previous = _q(Decimal(row.amount))
    opening = ledger.opening_balances_account(session)
    currency = account.currency.strip()
    tx: Transaction | None
    if row.transaction_id is None:
        if amount == 0:
            tx = None
        else:
            tx = ledger.create_flat_transaction(
                session,
                account_ref=str(account.id),
                category_ref=str(opening.id),
                amount=amount,
                date=on,
                payee_name=PAYEE,
                description=DESCRIPTION,
                requested_status=core.Status.POSTED,
                origin=core.Origin.SYSTEM,
                token=None,
                system_leg=True,
            )
    else:
        head = ledger.lock_transaction(session, row.transaction_id)
        tx = ledger.reverse_and_replace_with_specs(
            session,
            tx=head,
            token=None,
            replacement_specs=[
                core.PostingSpec(account_id=account.id, amount=amount, currency=currency),
                core.PostingSpec(account_id=opening.id, amount=_q(-amount), currency=currency),
            ],
            date=on,
            payee_name=PAYEE,
            description=REMOVED_DESCRIPTION if reason == REMOVED else DESCRIPTION,
            origin=core.Origin.SYSTEM,
            action="account.starting_balance_replace",
            audit_extra={"account_id": str(account.id), "reason": reason},
        )
    row.transaction_id = tx.id if tx is not None else None
    row.amount = amount
    row.updated_at = clock._utc_now()
    session.flush()
    facts = {
        "account_id": str(account.id),
        "reason": reason,
        "amount": str(amount),
        "previous_amount": str(previous),
        "date": on.isoformat(),
        "basis": row.basis,
        "bank_balance": str(_q(Decimal(row.reported))),
        "bank_balance_date": row.balance_date.isoformat() if row.balance_date else None,
        "raw_fetch_id": str(row.raw_fetch_id),
        "transaction_id": str(tx.id) if tx is not None else None,
    }
    audit.record(
        session,
        actor_token_id=actor_token_id,
        action="account.starting_balance",
        entity_type="account",
        entity_id=account.id,
        before={"amount": str(previous)},
        after=facts,
    )
    events.emit(
        session,
        type_="account.starting_balance_set",
        entity_type="account",
        entity_id=account.id,
        payload={"path": account.path, "currency": currency, **facts},
    )
    return tx


def _refuse(code: str, message: str) -> StartingBalanceError:
    return StartingBalanceError(message, code=code)


def set_from_bank(
    session: Session,
    *,
    account_id: uuid.UUID,
    actor_token_id: uuid.UUID | None,
    dry_run: bool = False,
) -> Outcome:
    """Give ``account_id`` a starting balance from the bank's latest reported
    balance. Idempotent: an account that has one is answered with it
    (``status = unchanged``). Raises :class:`StartingBalanceError` when the
    bank's figure cannot be used, naming why."""
    from purser.feed import snapshot as snapshot_service
    from purser.feed.service import latest_sync_fetch

    account = _lock(session, account_id)
    if account is None:
        raise ledger.NotFoundError(f"account {account_id} not found")
    if account.is_virtual:
        raise _refuse("not_an_account", f"{account.path} is a category, not an account")
    existing = get(session, account.id)
    if existing is not None:
        return _describe(session, account, existing)
    if account.type not in drift.ELIGIBLE_TYPES:
        raise _refuse(
            "account_type",
            f"{account.path} is a {account.type} account; a starting balance from the bank "
            f"is for {', '.join(sorted(drift.ELIGIBLE_TYPES))} accounts",
        )
    snap = snapshot_service.account_snapshot(session, account)
    if snap.status != "ok" or snap.comparison is None or snap.balance is None:
        why = snap.comparison_unavailable or snap.status
        raise _refuse(
            str(why),
            f"{account.path} has no bank balance Purser can use ({why}); it needs a mapped, "
            "open account whose bank reported a balance in its currency in the latest sync",
        )
    if snap.freshness == "stale":
        raise _refuse(
            "stale_balance",
            f"the bank's balance for {account.path} is {snap.age_days} days old; sync the "
            "connection first",
        )
    assert account.connection_id is not None  # snapshot status ok
    raw = latest_sync_fetch(session, account.connection_id)
    assert raw is not None  # snapshot status ok
    comp = snap.comparison
    basis = comp.basis if comp.basis_pinned else _fresh_basis(session, account)
    _, earliest = _history(session, account.id)
    horizon = earliest or snap.balance_date or clock.ledger_today()
    row = AccountStartingBalance(
        account_id=account.id,
        transaction_id=None,
        raw_fetch_id=raw.id,
        balance_date=snap.balance_date,
        reported=_q(snap.balance),
        posted_before=_q(comp.posted_balance),
        pending_before=_q(comp.pending_inclusive_balance),
        basis=basis,
        horizon=horizon,
        amount=_q(Decimal(0)),
        created_by=actor_token_id,
    )
    amount = _amount(row, basis, Decimal(0))
    on = _entry_date(row, earliest)
    if account.reconciled_through is not None and amount != 0:
        raise _refuse(
            "reconciled",
            f"{account.path} is reconciled through {account.reconciled_through} and differs "
            f"from the bank by {amount}: a starting balance would move the balance that "
            "reconciliation recorded. Reconcile a later statement instead (reconcile_account)",
        )
    outcome = Outcome(
        account=account,
        status="set",
        amount=amount,
        entry_date=on if amount != 0 else None,
        basis=basis,
        reported=row.reported,
        balance_date=row.balance_date,
        transaction_id=None,
        dry_run=dry_run,
    )
    if dry_run:
        return outcome
    session.add(row)
    session.flush()
    tx = _write(
        session, account, row, amount=amount, on=on, actor_token_id=actor_token_id, reason=SET
    )
    return Outcome(
        account=account,
        status="set",
        amount=amount,
        entry_date=tx.date if tx is not None else None,
        basis=basis,
        reported=row.reported,
        balance_date=row.balance_date,
        transaction_id=tx.id if tx is not None else None,
    )


def _fresh_basis(session: Session, account: Account) -> str:
    """The basis to derive a new starting balance on: the one the account's
    sync history shows, else pending-inclusive. Without evidence the per-sync
    rule picks whichever delta is smaller, which between two figures that
    both lack the whole history says nothing; pending-inclusive is a bank's
    "current balance" and the comparison's own tie-break."""
    return _evident_basis(session, account) or drift.PENDING_INCLUSIVE


def remove(
    session: Session,
    *,
    account_id: uuid.UUID,
    actor_token_id: uuid.UUID | None,
    dry_run: bool = False,
) -> Outcome:
    """Take the starting balance out again: the entry is reversed to zero
    (its chain stays, as for every correction) and Purser stops keeping it.
    ``status = none`` when the account has none."""
    account = _lock(session, account_id)
    if account is None:
        raise ledger.NotFoundError(f"account {account_id} not found")
    row = get(session, account.id)
    if row is None:
        return Outcome(account, "none", _q(Decimal(0)), None, None, None, None, None, dry_run)
    if account.closed_at is not None:
        raise ledger.LedgerError(f"{account.path} is closed (read-only); reopen it first")
    was = _describe(session, account, row)
    outcome = Outcome(
        account=account,
        status="removed",
        amount=_q(Decimal(0)),
        entry_date=None,
        basis=row.basis,
        reported=was.reported,
        balance_date=row.balance_date,
        transaction_id=None,
        dry_run=dry_run,
    )
    if dry_run:
        return outcome
    if row.transaction_id is not None and Decimal(row.amount) != 0:
        _write(
            session,
            account,
            row,
            amount=_q(Decimal(0)),
            on=was.entry_date or _entry_date(row, None),
            actor_token_id=actor_token_id,
            reason=REMOVED,
        )
    else:
        audit.record(
            session,
            actor_token_id=actor_token_id,
            action="account.starting_balance",
            entity_type="account",
            entity_id=account.id,
            before={"amount": str(_q(Decimal(row.amount)))},
            after={"account_id": str(account.id), "reason": REMOVED, "amount": "0.0000"},
        )
    session.delete(row)
    session.flush()
    return outcome


def reseat(session: Session, account_id: uuid.UUID, *, skip_locked: bool = True) -> Outcome | None:
    """Recompute one account's starting balance and replace the entry when it
    differs: older history arrived (rows dated before the horizon), or the
    bank's basis is now known to be the other one. None when the account has
    none, is closed, or is held by another writer (the next pass has it)."""
    if get(session, account_id) is None:
        return None
    account = _lock(session, account_id, skip_locked=skip_locked)
    if account is None or account.closed_at is not None:
        return None
    row = session.execute(
        select(AccountStartingBalance)
        .where(AccountStartingBalance.account_id == account.id)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if row is None:
        return None
    basis = row.basis
    reason = OLDER_HISTORY
    if account.reconciled_through is None and row.posted_before != row.pending_before:
        # Only while no person has reconciled the account: a reconciliation
        # is a statement about its balance that a basis switch would move.
        known = _known_basis(session, account)
        if known is not None and known != basis:
            basis, reason = known, BASIS
    older, _ = _history(session, account.id, before=row.horizon)
    _, earliest = _history(session, account.id)
    amount = _amount(row, basis, older)
    on = _entry_date(row, earliest)
    tx = session.get(Transaction, row.transaction_id) if row.transaction_id else None
    current_date = tx.date if tx is not None else None
    if (
        amount == _q(Decimal(row.amount))
        and basis == row.basis
        and (tx is None or current_date == on)
    ):
        return _describe(session, account, row)
    row.basis = basis
    written = _write(
        session, account, row, amount=amount, on=on, actor_token_id=None, reason=reason
    )
    return Outcome(
        account=account,
        status="set",
        amount=amount,
        entry_date=written.date if written is not None else None,
        basis=basis,
        reported=_q(Decimal(row.reported)),
        balance_date=row.balance_date,
        transaction_id=written.id if written is not None else None,
    )


def _known_basis(session: Session, account: Account) -> str | None:
    """The account's basis where something settles it: its pin, else what its
    sync history shows. None when neither does."""
    return drift.pinned_basis(account) or _evident_basis(session, account)


def _evident_basis(session: Session, account: Account) -> str | None:
    """The basis the account's stored sync observations give evidence for
    (decision 64's rule without its tie-breaks): the one whose delta changed
    on fewer consecutive-sync steps. None while the two never moved apart, so
    a tie-break is never mistaken for knowledge of the bank."""
    if account.connection_id is None:
        return None
    key = str(account.id)
    history = [
        deltas
        for block in reversed(drift.recent_blocks(session, account.connection_id))
        if (deltas := drift._stored_deltas(block.get(key))) is not None
    ]
    posted = sum(1 for before, after in pairwise(history) if before[0] != after[0])
    pending = sum(1 for before, after in pairwise(history) if before[1] != after[1])
    if posted == pending:
        return None
    return drift.POSTED_ONLY if posted < pending else drift.PENDING_INCLUSIVE


def reseat_all(session: Session, *, connection_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
    """Reseat every account that has a starting balance (or one connection's),
    each in its own savepoint. Returns the accounts whose entry changed, for
    the caller's audit row. Never raises."""
    changed: list[dict[str, Any]] = []
    try:
        query = select(AccountStartingBalance.account_id)
        if connection_id is not None:
            query = query.join(Account, Account.id == AccountStartingBalance.account_id).where(
                Account.connection_id == connection_id
            )
        for account_id in session.execute(
            query.order_by(AccountStartingBalance.account_id)
        ).scalars():
            try:
                with session.begin_nested():
                    outcome = reseat(session, account_id)
            except Exception:
                log.exception("starting balance reseat failed", account_id=str(account_id))
                continue
            if outcome is not None and outcome.status == "set":
                changed.append(outcome.as_dict())
    except Exception:
        log.exception("starting balance reseat pass failed")
    return changed
