"""Hand-valued assets: changes in value (#566, DESIGN §8, decision 113).

A ``property`` account is a ledger. A change in its value is an entry against
the system account ``equity.revaluation``; its value on a date is set through
``POST /accounts/{id}/reconcile``, which posts the difference the same way.
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from purser import assets, ledger, networth
from purser.admin.auth import WriteToken
from purser.db import get_session

DbSession = Annotated[Session, Depends(get_session)]

router = APIRouter(tags=["assets"])


class ValueChangeCreate(BaseModel):
    as_of: date = Field(
        description="The day the value changed: today (household time zone) or earlier. "
        "A backdated change moves every balance after it, like any transaction."
    )
    amount: Decimal = Field(
        decimal_places=4,
        max_digits=20,
        description="The change, signed: positive when the asset gained value, negative "
        "when it lost value. Nonzero.",
    )
    note: str | None = Field(
        default=None,
        max_length=500,
        description="Where the figure came from (an appraisal, an estimate). Stored "
        "encrypted as the entry's description.",
    )
    kind: Literal["revaluation", "capital"] = Field(
        default="revaluation",
        description="`revaluation` (default): a change in the asset's market value, against "
        "the system account `equity.revaluation`. `capital`: money put into the asset that no "
        "tracked account paid — its purchase before your history, an improvement paid in "
        "cash — against `equity.capital`, so it never counts as a gain. (Money paid from a "
        "tracked account is a transfer to the asset instead.)",
    )
    reference: str | None = Field(
        default=None,
        max_length=100,
        description="Optional identifier that makes a retry safe: a repeated reference on "
        "the account posts nothing and answers with the first change (200, `created: "
        "false`). Letters, digits, '.', '_', ':' and '-' only (stored in plaintext).",
    )


class ValueChangeOut(BaseModel):
    transaction_id: uuid.UUID  # the entry (or, for a reversal, its zero replacement)
    account_id: uuid.UUID
    as_of: date
    amount: Decimal  # signed change on the asset
    kind: Literal["revaluation", "capital"]
    created: bool  # false: a repeated reference answered with the first change
    reversed: bool  # the change has been reversed
    balance: Decimal  # the asset's value now (pending + posted, every date)


def _handle(exc: ledger.LedgerError) -> HTTPException:
    code = status.HTTP_404_NOT_FOUND if isinstance(exc, ledger.NotFoundError) else 422
    return HTTPException(code, str(exc))


def _out(session: Session, change: assets.ValueChange) -> ValueChangeOut:
    balance = ledger.account_balances(session, [change.account.id]).get(
        change.account.id, Decimal(0)
    )
    return ValueChangeOut(
        transaction_id=change.transaction.id,
        account_id=change.account.id,
        as_of=change.transaction.date,
        amount=change.amount,
        kind="capital" if ledger.is_capital(change.transaction.postings) else "revaluation",
        created=change.created,
        reversed=change.reversed,
        balance=balance,
    )


@router.post(
    "/accounts/{account_id}/value-changes",
    status_code=status.HTTP_201_CREATED,
    responses={200: {"description": "A repeated reference: the first change, nothing posted."}},
)
def record_value_change(
    account_id: uuid.UUID,
    body: ValueChangeCreate,
    token: WriteToken,
    session: DbSession,
    response: Response,
    background_tasks: BackgroundTasks,
) -> ValueChangeOut:
    """Record a change in a hand-valued asset's value (type `property`): one
    posted entry dated `as_of` between the asset and the system account
    `equity.revaluation`, so net worth moves and spending reports do not. To
    set the value on a date instead ("worth X"), use `POST
    /accounts/{id}/reconcile`. A wrong change is undone with `POST
    /value-changes/{transaction_id}/reverse`."""
    try:
        change = assets.record_value_change(
            session,
            account_ref=str(account_id),
            as_of=body.as_of,
            amount=body.amount,
            note=body.note,
            reference=body.reference,
            token=token,
            kind=body.kind,
        )
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    if change.created:
        background_tasks.add_task(networth.consume_watermark)
    else:
        response.status_code = status.HTTP_200_OK
    return _out(session, change)


class DisposeRequest(BaseModel):
    as_of: date = Field(
        description="The sale (or disposal) date: today or earlier, and on or after the "
        "asset's latest entry."
    )
    note: str | None = Field(
        default=None,
        max_length=500,
        description="What happened (sold, traded in, written off); kept on the closing entry.",
    )
    dry_run: bool = Field(default=False, description="Answer the same, writing nothing.")


class DisposalOut(BaseModel):
    account_id: uuid.UUID
    as_of: date
    value_before: Decimal  # the asset's value on as_of before the closing change
    closing_change: Decimal  # posted against equity.revaluation; 0 = nothing posted
    gain: Decimal  # all its changes in value, the closing one included
    money_put_in: Decimal  # purchase and transfers before the closing change (proceeds out)
    dry_run: bool


@router.post("/accounts/{account_id}/dispose")
def dispose_asset(
    account_id: uuid.UUID,
    body: DisposeRequest,
    token: WriteToken,
    session: DbSession,
    background_tasks: BackgroundTasks,
) -> DisposalOut:
    """Sell, trade in or write off a hand-valued asset. Record the sale's money
    first, as transfers out of the asset dated on or before `as_of` (the
    proceeds deposit, a loan payoff). What is then left on the asset is the
    rest of its change in value — a gain or a loss at sale, or its whole value
    for a write-off — and is posted against `equity.revaluation` on `as_of`;
    the account closes at zero. `dry_run` shows the figures first."""
    try:
        result = assets.dispose(
            session,
            account_ref=str(account_id),
            as_of=body.as_of,
            note=body.note,
            dry_run=body.dry_run,
            token=token,
        )
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    if body.dry_run:
        session.rollback()
    else:
        session.commit()
        background_tasks.add_task(networth.consume_watermark)
    return DisposalOut(
        account_id=result.account.id,
        as_of=result.as_of,
        value_before=result.value_before,
        closing_change=result.closing_change,
        gain=result.gain,
        money_put_in=result.money_put_in,
        dry_run=result.dry_run,
    )


@router.post("/value-changes/{transaction_id}/reverse")
def reverse_value_change(
    transaction_id: uuid.UUID,
    token: WriteToken,
    session: DbSession,
    background_tasks: BackgroundTasks,
) -> ValueChangeOut:
    """Undo a change in an asset's value — one recorded here or posted by a
    reconcile — through the ledger's reversal pair. The answer's
    `transaction_id` is the zero replacement that records the undo; `amount`
    is the change it took back."""
    try:
        change = assets.reverse_value_change(session, transaction_id=transaction_id, token=token)
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    background_tasks.add_task(networth.consume_watermark)
    return _out(session, change)
