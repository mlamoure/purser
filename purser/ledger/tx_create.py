"""Transaction creation: the flat, transfer and split-loan forms, all through
one persist funnel (closed-account guard, balances, audit, events)."""

import uuid
from collections.abc import Sequence
from datetime import date as date_type
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from purser import audit, balances, events
from purser.core import ledger as core
from purser.ledger.accounts import _assert_accounts_open, get_account, resolve_account
from purser.ledger.errors import LedgerError
from purser.ledger.payees import resolve_payee
from purser.models import Account, ApiToken, Posting, Transaction

# ---------------------------------------------------------------------------
# Transactions — create
# ---------------------------------------------------------------------------


def _actor_origin(token: ApiToken | None) -> core.Origin:
    if token is None:
        return core.Origin.SYSTEM
    # kind is server-controlled (login paths only) — the token NAME is
    # client-chosen and must never drive origin attribution.
    return core.Origin.HUMAN if token.kind == "session" else core.Origin.AGENT


def check_category_currency(currency: str, category: Account) -> None:
    """v1 is single-currency per transaction: a category leg carries its real
    leg's currency, so a category of another currency is refused — at create
    and on every recategorize path (#361). Zero-sum per (transaction,
    currency) would still hold, so no trigger catches it; the category would
    just hold postings that every rollup sums as its own currency."""
    if currency.strip() != category.currency.strip():
        raise LedgerError("currency mismatch (v1 is single-currency per transaction)")


def create_flat_transaction(
    session: Session,
    *,
    account_ref: str,
    category_ref: str,
    amount: Decimal,
    date: date_type,
    payee_name: str | None = None,
    description: str | None = None,
    requested_status: core.Status = core.Status.PENDING,
    origin: core.Origin | None = None,
    token: ApiToken | None,
    schedule_id: uuid.UUID | None = None,
    occurrence_date: date_type | None = None,
    source_fetch_id: uuid.UUID | None = None,
    content_hash: str | None = None,
) -> Transaction:
    account = resolve_account(session, account_ref)
    category = resolve_account(session, category_ref)
    if not category.is_virtual:
        raise LedgerError(f"{category.path} is not a category (virtual account)")
    if account.is_virtual:
        raise LedgerError(f"{account.path} is a category; the flat form needs a real account")
    check_category_currency(account.currency, category)
    if amount == 0:
        raise LedgerError("amount must be nonzero")

    specs = core.expand_flat(
        account_id=account.id,
        category_id=category.id,
        amount=amount,
        currency=account.currency.strip(),
    )
    return _persist_transaction(
        session,
        specs=specs,
        date=date,
        payee_name=payee_name,
        description=description,
        requested_status=requested_status,
        origin=origin,
        token=token,
        schedule_id=schedule_id,
        occurrence_date=occurrence_date,
        source_fetch_id=source_fetch_id,
        content_hash=content_hash,
    )


def create_transfer(
    session: Session,
    *,
    from_account_ref: str,
    to_account_ref: str,
    amount: Decimal,
    date: date_type,
    description: str | None = None,
    requested_status: core.Status = core.Status.PENDING,
    token: ApiToken | None,
    origin: core.Origin | None = None,
    content_hash: str | None = None,
    schedule_id: uuid.UUID | None = None,
    occurrence_date: date_type | None = None,
) -> Transaction:
    from_account = resolve_account(session, from_account_ref)
    to_account = resolve_account(session, to_account_ref)
    if from_account.is_virtual or to_account.is_virtual:
        raise LedgerError("transfers connect two real accounts")
    if from_account.id == to_account.id:
        raise LedgerError("transfer accounts must differ")
    if from_account.currency.strip() != to_account.currency.strip():
        raise LedgerError("currency mismatch (v1 has no FX)")
    if amount <= 0:
        raise LedgerError("transfer amount must be positive (direction is from -> to)")

    specs = core.expand_transfer(
        from_account_id=from_account.id,
        to_account_id=to_account.id,
        amount=amount,
        currency=from_account.currency.strip(),
    )
    return _persist_transaction(
        session,
        specs=specs,
        date=date,
        payee_name=None,
        description=description or f"Transfer {from_account.name} -> {to_account.name}",
        requested_status=requested_status,
        origin=origin,
        token=token,
        content_hash=content_hash,
        schedule_id=schedule_id,
        occurrence_date=occurrence_date,
    )


def create_split_transaction(
    session: Session,
    *,
    account_ref: str,
    amount: Decimal,
    lines: Sequence[core.SplitLine],
    date: date_type,
    payee_name: str | None = None,
    description: str | None = None,
    requested_status: core.Status = core.Status.PENDING,
    origin: core.Origin | None = None,
    token: ApiToken | None,
    content_hash: str | None = None,
) -> Transaction:
    """A split transaction (DESIGN §3.3, decision 87): ONE row with one real
    leg carrying ``amount`` and one category leg per line, zero-sum by
    construction (``core.expand_split``). Not a transfer, and read-only once
    written in v1 (``_flat_legs`` refuses it). Every line names a category;
    money that reaches another real account is a transfer of its own (the QIF
    importer carves those lines out, decision 87)."""
    account = resolve_account(session, account_ref)
    if account.is_virtual:
        raise LedgerError(f"{account.path} is a category; a split needs a real account")
    currency = account.currency.strip()
    for line in lines:
        category = get_account(session, line.category_id)
        if not category.is_virtual:
            raise LedgerError(f"{category.path} is not a category (virtual account)")
        if category.currency.strip() != currency:
            raise LedgerError("currency mismatch (v1 is single-currency per transaction)")
    try:
        specs = core.expand_split(
            account_id=account.id, amount=amount, lines=lines, currency=currency
        )
    except ValueError as exc:
        raise LedgerError(f"not a split transaction: {exc}") from exc
    return _persist_transaction(
        session,
        specs=specs,
        date=date,
        payee_name=payee_name,
        description=description,
        requested_status=requested_status,
        origin=origin,
        token=token,
        content_hash=content_hash,
    )


SPLIT_REMAINDER_MEMO = "split loan payment remainder"


def create_split_transfer(
    session: Session,
    *,
    outgoing_account_ref: str,
    outgoing_amount: Decimal,
    incoming_account_ref: str,
    incoming_amount: Decimal,
    remainder_category_ref: str,
    date: date_type,
    description: str | None,
    requested_status: core.Status,
    token: ApiToken | None,
    origin: core.Origin | None = None,
) -> Transaction:
    """A split loan payment (DESIGN §3.3, decision 62): ONE transaction with
    the exact cash leg, the exact principal leg, and one balancing posting to
    ``remainder_category`` — the interest/fee expense. No synthetic
    full-payment liability leg is ever written, so both bank legs reconcile
    to what each bank reports.

    Only the explicit transfer confirm reaches this (write scope, a token
    always present); the auto-linker, absorb and bracket conversion never do.
    """
    outgoing = resolve_account(session, outgoing_account_ref)
    incoming = resolve_account(session, incoming_account_ref)
    if outgoing.is_virtual or incoming.is_virtual:
        raise LedgerError("a split loan payment connects two real accounts")
    if outgoing.id == incoming.id:
        raise LedgerError("transfer accounts must differ")
    try:
        remainder = core.split_transfer_remainder(outgoing_amount, incoming_amount)
    except ValueError as exc:
        raise LedgerError(f"not a split loan payment: {exc}") from exc
    category = resolve_account(session, remainder_category_ref)
    if not category.is_virtual:
        raise LedgerError(
            f"remainder_category {category.path} is a real account; name the "
            "interest/fee expense category the remainder books to"
        )
    if category.type != "category_expense":
        raise LedgerError(
            f"remainder_category {category.path} is not an expense category: the "
            f"remainder ({remainder}) is interest/fees paid out of the cash leg"
        )
    from purser.categorize import UNCATEGORIZED_PATHS

    if category.path in UNCATEGORIZED_PATHS:
        raise LedgerError(
            "remainder_category must be a specific interest/fee category, not the "
            f"{category.path} placeholder"
        )
    currencies = {a.currency.strip() for a in (outgoing, incoming, category)}
    if len(currencies) != 1:
        raise LedgerError("currency mismatch (v1 has no FX)")
    specs = core.expand_split_transfer(
        outgoing_account_id=outgoing.id,
        outgoing_amount=outgoing_amount,
        incoming_account_id=incoming.id,
        incoming_amount=incoming_amount,
        remainder_category_id=category.id,
        currency=outgoing.currency.strip(),
        memo=SPLIT_REMAINDER_MEMO,
    )
    return _persist_transaction(
        session,
        specs=specs,
        date=date,
        payee_name=None,
        description=description or f"Loan payment {outgoing.name} -> {incoming.name}",
        requested_status=requested_status,
        origin=origin,
        token=token,
    )


def _persist_transaction(
    session: Session,
    *,
    specs: list[core.PostingSpec],
    date: date_type,
    payee_name: str | None,
    description: str | None,
    requested_status: core.Status,
    origin: core.Origin | None,
    token: ApiToken | None,
    schedule_id: uuid.UUID | None = None,
    occurrence_date: date_type | None = None,
    source_fetch_id: uuid.UUID | None = None,
    content_hash: str | None = None,
    reverses_id: uuid.UUID | None = None,
) -> Transaction:
    if not core.is_balanced(specs):
        raise LedgerError("postings do not balance")  # unreachable via public forms
    # Read-only chokepoint (DESIGN §3.4): no new posting lands on a closed
    # account. The INSERT trigger is the structural backstop; this is the
    # friendly 422. Categories never close, so virtual legs are skipped.
    _assert_accounts_open(session, {spec.account_id for spec in specs})
    # A write principal's requested status is honoured (#436): the agent acts
    # with the user's authority, and the safety net is attribution plus
    # reversal, not a forced pending.
    status = requested_status
    tx = Transaction(
        date=date,
        payee_id=resolve_payee(session, payee_name).id if payee_name else None,
        description=description,
        status=status.value,
        origin=(origin or _actor_origin(token)).value,
        schedule_id=schedule_id,
        occurrence_date=occurrence_date,
        source_fetch_id=source_fetch_id,
        content_hash=content_hash,
        reverses_id=reverses_id,
        created_by=token.id if token else None,
    )
    session.add(tx)
    session.flush()
    for spec in specs:
        session.add(
            Posting(
                transaction_id=tx.id,
                account_id=spec.account_id,
                amount=spec.amount,
                currency=spec.currency,
                memo=spec.memo,
            )
        )
    session.flush()
    # THE create funnel is a balance chokepoint (decision 38): every new
    # countable row dirties balances from its date. Expected instances are
    # excluded from balances/net worth, so schedule materialization stays free.
    if status in (core.Status.PENDING, core.Status.POSTED):
        balances.mark_dirty(session, date)
    audit.record(
        session,
        actor_token_id=token.id if token else None,
        action="transaction.create",
        entity_type="transaction",
        entity_id=tx.id,
        after=_tx_audit_shape(tx, specs),
    )
    if status == core.Status.POSTED:
        events.emit(
            session,
            type_="transaction.posted",
            entity_type="transaction",
            entity_id=tx.id,
            payload={"date": date.isoformat(), "description": description},
        )
    return tx


def _tx_audit_shape(tx: Transaction, specs: list[core.PostingSpec]) -> dict[str, Any]:
    return {
        "date": tx.date.isoformat(),
        "status": tx.status,
        "origin": tx.origin,
        "description": tx.description,
        "postings": [
            {"account_id": str(s.account_id), "amount": str(s.amount), "currency": s.currency}
            for s in specs
        ],
    }
