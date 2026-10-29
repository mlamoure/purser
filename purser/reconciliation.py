"""Audited account reconciliation (issue #268, DESIGN §3.7, decision 61).

An owner states "the statement balance of account A on date D was X". Purser
compares X with its own balance of A on D, posts ONE adjustment for the
difference (none when they already agree), records the claim, and advances
``accounts.reconciled_through``.

What makes the marker trustworthy:

- **Recorded either way.** A clean reconcile (difference 0) is the common case
  and is recorded like any other: adjustment 0, no adjustment transaction.
- **Two bases.** Banks report a posted-only or a pending-inclusive balance
  (§7.3), so every claim names the one it was compared on.
- **Statement convention in, ledger sign inside.** Callers pass the figure the
  statement shows (positive = money held, or for a liability the amount owed);
  Purser converts it. Every amount it returns, except the echoed
  ``statement_balance``, is in ledger sign.
- **Staleness is computed, never hidden.** A claim keeps its target, and every
  read that shows the marker recomputes the ledger balance on that date in that
  basis, so a backdated import, edit or replay that moved it is reported with
  the difference. A retry of the same claim is idempotent only while it still
  holds; once the ledger has moved it is a conflict carrying the difference.
- **Dry run is the commit path.** :func:`reconcile` computes the outcome first
  and writes only when ``dry_run`` is false.
- **Serialized per account.** The account row is locked before anything is
  read; UNIQUE (account_id, as_of) is the backstop.
"""

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from purser import audit, events, ledger, locks
from purser.admin import security
from purser.core import ledger as core
from purser.feed.drift import ELIGIBLE_TYPES as _DRIFT_TYPES
from purser.models import Account, AccountReconciliation, ApiToken

BASIS_POSTED = "posted"
BASIS_PENDING_INCLUSIVE = "pending_inclusive"
BASES = (BASIS_POSTED, BASIS_PENDING_INCLUSIVE)

# Postings-balance accounts: the set the drift check compares (§7.3). A
# loan/property balance is its latest valuation and a brokerage balance adds
# market value (§8), so their postings sum is not the figure a statement
# shows — record a valuation for those instead.
ELIGIBLE_TYPES = _DRIFT_TYPES

# A liability's statement shows the amount OWED as a positive number, while
# its ledger balance is negative when owed (§7.3 — SimpleFIN uses the ledger's
# convention). Statement figures for these types are negated on the way in.
LIABILITY_TYPES = frozenset({"credit_card", "loan"})

# The one place adjustments land, so "how much did reconciling plug this
# year" is a category rollup. Auto-created on first use, like close's
# expenses.adjustments.
CATEGORY_PATH = "expenses.reconciliation_adjustments"
CATEGORY_NAME = "Reconciliation Adjustments"
CATEGORY_DESCRIPTION = (
    "System category: the balancing entry Purser posts when an account is "
    "reconciled to a statement balance. Never use it for an ordinary transaction."
)
PAYEE = "Balance reconciliation"

_MAX_ABS = Decimal("1e16")  # NUMERIC(20,4)

# The ledger balance of an account at the end of a date, in both bases, for a
# batch of (account, date) points in ONE statement — the single computation
# behind the reconcile itself, its dry run, the retry check and every
# staleness read. Live sums over chain heads (no row reverses them), which is
# the stored running balance's own definition (§3.5) restricted by status; live
# rather than stored because the store is recomputed only at commit, and the
# reconcile reads its own uncommitted adjustment back.
_BALANCES = text(
    """
WITH req AS (
    SELECT DISTINCT r.account_id, r.as_of
    FROM unnest(CAST(:accounts AS uuid[]), CAST(:dates AS date[])) AS r(account_id, as_of)
), daily AS (
    SELECT p.account_id, t.date,
           SUM(p.amount) FILTER (WHERE t.status = 'posted') AS posted,
           SUM(p.amount) AS pending_inclusive
    FROM postings p
    JOIN transactions t ON t.id = p.transaction_id
    WHERE p.account_id IN (SELECT account_id FROM req)
      AND t.status IN ('pending', 'posted')
      AND NOT EXISTS (SELECT 1 FROM transactions x WHERE x.reverses_id = t.id)
    GROUP BY p.account_id, t.date
)
SELECT q.account_id, q.as_of,
       COALESCE(SUM(d.posted), 0) AS posted,
       COALESCE(SUM(d.pending_inclusive), 0) AS pending_inclusive
FROM req q
LEFT JOIN daily d ON d.account_id = q.account_id AND d.date <= q.as_of
GROUP BY q.account_id, q.as_of
"""
)


class ReconciliationConflict(ledger.LedgerError):
    """The request contradicts a recorded reconciliation (409 at the API).

    ``detail`` is the structured body REST returns as-is; ``str()`` states the
    same facts for MCP, whose errors are text."""

    def __init__(self, message: str, *, code: str, **facts: Any) -> None:
        super().__init__(message)
        self.code = code
        self.detail: dict[str, Any] = {"code": code, "message": message, **facts}


@dataclass(frozen=True)
class Check:
    """A recorded reconciliation, re-checked against the ledger now."""

    reconciliation: AccountReconciliation
    statement_balance: Decimal
    ledger_balance: Decimal  # on as_of, in the recorded basis, now
    difference: Decimal  # ledger_balance - target; 0 = it still holds

    @property
    def in_balance(self) -> bool:
        return self.difference == 0


@dataclass(frozen=True)
class Outcome:
    """What a reconcile did — or, on a dry run, would do."""

    account_id: uuid.UUID
    as_of: date
    basis: str
    statement_balance: Decimal  # as the statement shows it
    target_balance: Decimal  # ledger sign
    ledger_balance: Decimal  # ledger balance on as_of in basis, before the adjustment
    adjustment: Decimal  # target - ledger_balance; 0 = already in balance
    category_path: str | None  # where the adjustment posts; None when 0
    reconciled_through: date | None  # the account's marker once this commits
    reconciliation_id: uuid.UUID | None  # None until recorded
    adjustment_tx_id: uuid.UUID | None
    created: bool  # this call recorded a new reconciliation
    dry_run: bool


def _q(value: Decimal) -> Decimal:
    """NUMERIC(20,4) form, with negative zero folded to zero."""
    quantized = core.quantize(value)
    return quantized if quantized != 0 else core.quantize(Decimal(0))


def to_ledger_sign(account_type: str, statement_balance: Decimal) -> Decimal:
    """A statement figure in the ledger's sign convention."""
    return _q(-statement_balance if account_type in LIABILITY_TYPES else statement_balance)


def to_statement_sign(account_type: str, ledger_balance: Decimal) -> Decimal:
    """A ledger balance as a statement would print it (the inverse)."""
    return to_ledger_sign(account_type, ledger_balance)


def ledger_balances(
    session: Session, points: Iterable[tuple[uuid.UUID, date]]
) -> dict[tuple[uuid.UUID, date], dict[str, Decimal]]:
    """``{(account, date): {"posted": x, "pending_inclusive": y}}`` at the end
    of each date. Heads-only, so it agrees with the register's stored running
    balance on the pending-inclusive basis."""
    wanted = list(dict.fromkeys(points))
    if not wanted:
        return {}
    rows = session.execute(
        _BALANCES,
        {
            "accounts": [str(account_id) for account_id, _ in wanted],
            "dates": [as_of for _, as_of in wanted],
        },
    )
    return {
        (account_id, as_of): {
            BASIS_POSTED: _q(Decimal(posted)),
            BASIS_PENDING_INCLUSIVE: _q(Decimal(pending)),
        }
        for account_id, as_of, posted, pending in rows
    }


def ledger_balance(session: Session, account_id: uuid.UUID, as_of: date, basis: str) -> Decimal:
    return ledger_balances(session, [(account_id, as_of)])[(account_id, as_of)][basis]


def check(session: Session, rows: Sequence[AccountReconciliation]) -> dict[uuid.UUID, Check]:
    """Re-check recorded reconciliations against the ledger, keyed by row id."""
    if not rows:
        return {}
    account_types = {
        account_id: type_
        for account_id, type_ in session.execute(
            select(Account.id, Account.type).where(Account.id.in_({r.account_id for r in rows}))
        )
    }
    sums = ledger_balances(session, [(r.account_id, r.as_of) for r in rows])
    out: dict[uuid.UUID, Check] = {}
    for row in rows:
        target = _q(Decimal(row.target_balance))
        now = sums[(row.account_id, row.as_of)][row.basis]
        out[row.id] = Check(
            reconciliation=row,
            statement_balance=to_statement_sign(account_types[row.account_id], target),
            ledger_balance=now,
            difference=_q(now - target),
        )
    return out


def latest_checks(session: Session, accounts: Iterable[Account]) -> dict[uuid.UUID, Check]:
    """Each reconciled account's latest reconciliation, re-checked — keyed by
    account id. The marker is consulted only to skip never-reconciled
    accounts; the row itself comes from the table."""
    ids = [a.id for a in accounts if not a.is_virtual and a.reconciled_through is not None]
    if not ids:
        return {}
    latest = list(
        session.execute(
            select(AccountReconciliation)
            .where(AccountReconciliation.account_id.in_(ids))
            .order_by(AccountReconciliation.account_id, AccountReconciliation.as_of.desc())
            .distinct(AccountReconciliation.account_id)
        ).scalars()
    )
    checks = check(session, latest)
    return {row.account_id: checks[row.id] for row in latest}


def history(session: Session, account_id: uuid.UUID, *, limit: int = 20) -> list[Check]:
    """An account's reconciliations, newest first, each re-checked."""
    rows = list(
        session.execute(
            select(AccountReconciliation)
            .where(AccountReconciliation.account_id == account_id)
            .order_by(AccountReconciliation.as_of.desc())
            .limit(limit)
        ).scalars()
    )
    checks = check(session, rows)
    return [checks[row.id] for row in rows]


def _validated_amount(value: Decimal) -> Decimal:
    try:
        if not value.is_finite() or abs(value) >= _MAX_ABS:
            raise ledger.LedgerError("statement_balance is out of range")
        if value != value.quantize(core.QUANT):
            raise ledger.LedgerError("statement_balance has more than 4 decimal places")
    except InvalidOperation as exc:
        raise ledger.LedgerError("statement_balance is out of range") from exc
    return value


def _lock_account(session: Session, account_id: uuid.UUID) -> Account:
    """Serialize every reconcile of one account on its row lock. Read fresh:
    a waiter must see the marker the previous holder committed.

    FOR NO KEY UPDATE, not FOR UPDATE: two reconciles still exclude each
    other, but an import inserting postings on the account (whose FK check
    takes KEY SHARE on this row) is not blocked — with FOR UPDATE, a sync
    holding the balance watermark and waiting on this row, while this
    reconcile waits on the watermark to post its adjustment, deadlocks."""
    account = locks.lock_account(session, account_id)
    if account is None:
        raise ledger.NotFoundError(f"account {account_id} not found")
    if account.is_virtual:
        raise ledger.LedgerError(f"{account.path} is a category; only real accounts reconcile")
    if account.type not in ELIGIBLE_TYPES:
        raise ledger.LedgerError(
            f"{account.path} is a {account.type} account; reconciliation compares a "
            f"postings balance ({', '.join(sorted(ELIGIBLE_TYPES))}). Record a "
            "valuation for loan and property accounts instead (MCP record_valuation, "
            "or POST /accounts/{id}/valuations)."
        )
    return account


def reconcile(
    session: Session,
    *,
    account_id: uuid.UUID,
    as_of: date,
    statement_balance: Decimal,
    token: ApiToken,
    basis: str = BASIS_POSTED,
    dry_run: bool = False,
    today: date | None = None,
) -> Outcome:
    """Reconcile ``account_id`` to ``statement_balance`` (statement convention)
    at the end of ``as_of`` in ``basis``.

    Raises :class:`ReconciliationConflict` when the request contradicts a
    recorded reconciliation — a different claim for the same date, a date
    before the latest reconciled one, or a repeat of a claim the ledger no
    longer agrees with — and :class:`ledger.LedgerError` for invalid input.
    A dry run raises exactly what the commit would.
    """
    if not security.can_write(token.scopes):
        raise ledger.LedgerError("reconciling an account requires write scope")
    if basis not in BASES:
        raise ledger.LedgerError(f"basis must be one of: {', '.join(BASES)}")
    statement_balance = _validated_amount(statement_balance)
    today = today or datetime.now(UTC).date()
    if as_of > today:
        raise ledger.LedgerError(
            f"as_of {as_of} is in the future; reconcile a statement date on or before {today}"
        )

    account = _lock_account(session, account_id)
    target = to_ledger_sign(account.type, statement_balance)
    statement = to_statement_sign(account.type, target)

    existing = session.execute(
        select(AccountReconciliation).where(
            AccountReconciliation.account_id == account.id,
            AccountReconciliation.as_of == as_of,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return _repeat(session, account, existing, basis=basis, target=target, dry_run=dry_run)

    if account.closed_at is not None:
        raise ledger.LedgerError(f"{account.path} is closed (read-only); reopen it to reconcile")
    marker = account.reconciled_through
    if marker is not None and as_of < marker:
        raise ReconciliationConflict(
            f"{account.path} is reconciled through {marker}; a reconciliation for "
            f"{as_of} would move the marker backward. Reconcile {marker} or later.",
            code="reconciliation_out_of_order",
            account_id=str(account.id),
            as_of=as_of.isoformat(),
            reconciled_through=marker.isoformat(),
        )

    observed = ledger_balance(session, account.id, as_of, basis)
    adjustment = _q(target - observed)
    outcome = Outcome(
        account_id=account.id,
        as_of=as_of,
        basis=basis,
        statement_balance=statement,
        target_balance=target,
        ledger_balance=observed,
        adjustment=adjustment,
        category_path=CATEGORY_PATH if adjustment != 0 else None,
        reconciled_through=as_of,
        reconciliation_id=None,
        adjustment_tx_id=None,
        created=False,
        dry_run=dry_run,
    )
    if dry_run:
        return outcome
    return _record(session, account, outcome, token=token)


def _repeat(
    session: Session,
    account: Account,
    existing: AccountReconciliation,
    *,
    basis: str,
    target: Decimal,
    dry_run: bool,
) -> Outcome:
    """A request for a date that is already reconciled: the same claim is
    idempotent while it still holds; anything else is a conflict that says
    exactly what is recorded and how far the ledger is from it."""
    recorded_target = _q(Decimal(existing.target_balance))
    recorded_statement = to_statement_sign(account.type, recorded_target)
    claim = (
        f"{account.path} is reconciled on {existing.as_of} to a statement balance of "
        f"{recorded_statement} ({existing.basis} basis)"
    )
    if existing.basis != basis or recorded_target != target:
        raise ReconciliationConflict(
            f"{claim}; one reconciliation per date, so reconcile a later date instead",
            code="reconciliation_conflict",
            reconciliation_id=str(existing.id),
            account_id=str(account.id),
            as_of=existing.as_of.isoformat(),
            basis=existing.basis,
            statement_balance=str(recorded_statement),
            target_balance=str(recorded_target),
            requested_basis=basis,
            requested_target_balance=str(target),
        )
    now = ledger_balance(session, account.id, existing.as_of, existing.basis)
    difference = _q(now - recorded_target)
    if difference != 0:
        raise ReconciliationConflict(
            f"{claim}, but the ledger has moved since: its balance on that date is now "
            f"{now} against the target {recorded_target} (ledger sign), a difference of "
            f"{difference}. Find the backdated change, or reconcile a later date.",
            code="reconciliation_moved",
            reconciliation_id=str(existing.id),
            account_id=str(account.id),
            as_of=existing.as_of.isoformat(),
            basis=existing.basis,
            statement_balance=str(recorded_statement),
            target_balance=str(recorded_target),
            ledger_balance=str(now),
            difference=str(difference),
        )
    adjustment = _q(Decimal(existing.adjustment))
    return Outcome(
        account_id=account.id,
        as_of=existing.as_of,
        basis=existing.basis,
        statement_balance=to_statement_sign(account.type, recorded_target),
        target_balance=recorded_target,
        ledger_balance=_q(recorded_target - adjustment),
        adjustment=adjustment,
        category_path=CATEGORY_PATH if adjustment != 0 else None,
        reconciled_through=account.reconciled_through,
        reconciliation_id=existing.id,
        adjustment_tx_id=existing.adjustment_tx_id,
        created=False,
        dry_run=dry_run,
    )


def _category(session: Session, *, actor_token_id: uuid.UUID) -> Account:
    try:
        category = ledger.account_by_path(session, CATEGORY_PATH)
    except ledger.NotFoundError:
        parent = ledger.ensure_category_path(session, "expenses", actor_token_id=actor_token_id)
        category = ledger.create_account(
            session,
            name=CATEGORY_NAME,
            type_="category_expense",
            parent_id=parent.id,
            path=CATEGORY_PATH,
            description=CATEGORY_DESCRIPTION,
            actor_token_id=actor_token_id,
        )
    if not category.is_virtual:
        raise ledger.LedgerError(f"{CATEGORY_PATH} exists but is not a category")
    return category


def _record(session: Session, account: Account, outcome: Outcome, *, token: ApiToken) -> Outcome:
    adjustment_tx_id: uuid.UUID | None = None
    if outcome.adjustment != 0:
        category = _category(session, actor_token_id=token.id)
        # origin=system: Purser computed this row, so it joins the review set
        # (§4) and a human sees it, whoever asked for the reconcile. The
        # matcher excludes it explicitly — it is not bank reality (§5).
        tx = ledger.create_flat_transaction(
            session,
            account_ref=str(account.id),
            category_ref=str(category.id),
            amount=outcome.adjustment,
            date=outcome.as_of,
            payee_name=PAYEE,
            description=(
                f"Reconciliation adjustment: {outcome.basis} balance on {outcome.as_of} "
                f"was {outcome.ledger_balance}, statement target {outcome.target_balance}"
            ),
            requested_status=core.Status.POSTED,
            origin=core.Origin.SYSTEM,
            token=token,
        )
        # A posted row on as_of counts in BOTH bases — that is what makes the
        # target exact whichever basis was asked for. The requested status is
        # honoured (#436); the check stays as a backstop.
        if tx.status != core.Status.POSTED.value:
            raise ledger.LedgerError("the reconciliation adjustment did not post")
        adjustment_tx_id = tx.id

    row = AccountReconciliation(
        account_id=account.id,
        as_of=outcome.as_of,
        basis=outcome.basis,
        target_balance=outcome.target_balance,
        adjustment=outcome.adjustment,
        adjustment_tx_id=adjustment_tx_id,
        created_by=token.id,
    )
    session.add(row)
    previous = account.reconciled_through
    account.reconciled_through = outcome.as_of
    session.flush()

    facts = {
        "reconciliation_id": str(row.id),
        "as_of": outcome.as_of.isoformat(),
        "basis": outcome.basis,
        "statement_balance": str(outcome.statement_balance),
        "target_balance": str(outcome.target_balance),
        "ledger_balance": str(outcome.ledger_balance),
        "adjustment": str(outcome.adjustment),
        "adjustment_tx_id": str(adjustment_tx_id) if adjustment_tx_id else None,
    }
    audit.record(
        session,
        actor_token_id=token.id,
        action="account.reconcile",
        entity_type="account",
        entity_id=account.id,
        before={"reconciled_through": previous.isoformat() if previous else None},
        after={"reconciled_through": outcome.as_of.isoformat(), **facts},
    )
    events.emit(
        session,
        type_="account.reconciled",
        entity_type="account",
        entity_id=account.id,
        payload={"path": account.path, "currency": account.currency.strip(), **facts},
    )
    return replace(
        outcome, created=True, reconciliation_id=row.id, adjustment_tx_id=adjustment_tx_id
    )
