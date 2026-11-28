"""Hand-valued asset tools (#566, decision 113): a ``property`` account is a
ledger, and a change in its value is an entry against ``equity.revaluation``.
Its value on a date is set with ``reconcile_account``. Registered on import.
"""

import uuid
from datetime import date as date_type
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from purser.mcp._registry import _amount, _principal, _run, _token_row, audited_tool
from purser.privacy import Emits

_CHANGE_EMITS = Emits(amount=("amount",), balance=("balance",))


def _change_out(session: Session, change: Any) -> dict[str, Any]:
    from purser import ledger as ledger_service

    balance = ledger_service.account_balances(session, [change.account.id]).get(
        change.account.id, Decimal(0)
    )
    return {
        "transaction_id": str(change.transaction.id),
        "account": change.account.path,
        "as_of": change.transaction.date.isoformat(),
        "amount": _amount(change.amount),
        "kind": "capital"
        if ledger_service.is_capital(change.transaction.postings)
        else "revaluation",
        "created": change.created,
        "reversed": change.reversed,
        "balance": _amount(balance),
    }


@audited_tool(kind="write", entity="account", scope="write", emits=_CHANGE_EMITS)
async def record_value_change(
    account: str,
    as_of: str,
    amount: str,
    note: str | None = None,
    reference: str | None = None,
    kind: str = "revaluation",
) -> dict[str, Any]:
    """Record a change in a hand-valued asset's value (a house, a car: an
    account of type property; write scope). amount is a signed decimal
    string: "25000.00" when it gained value, "-4000.00" when it lost value.
    as_of is YYYY-MM-DD, today or earlier; a backdated change moves every
    balance after it, like any transaction. Purser posts one entry against
    the system account equity.revaluation, so net worth moves and spending
    does not. note says where the figure came from. reference (letters,
    digits, . _ : - only) makes a retry safe: a reference already used on the
    account posts nothing and answers with the first change (created=false).
    To set the value on a date ("worth X"), use reconcile_account with the
    value as statement_balance; it posts the difference the same way. A wrong
    change is undone with reverse_value_change. The answer's balance is the
    asset's value now. kind="capital" records money put in instead of a
    change in market value: the purchase price before the user's records, or
    an improvement paid in cash (never a gain). An improvement paid from a
    tracked account is a transfer to the asset (update_transaction with
    category="[asset path]"), not a value change."""

    def op(session: Session) -> dict[str, Any]:
        from purser import assets

        principal = _principal()
        try:
            figure = Decimal(amount)
        except InvalidOperation as exc:
            raise ValueError('amount must be a decimal string such as "25000.00"') from exc
        change = assets.record_value_change(
            session,
            account_ref=account,
            as_of=date_type.fromisoformat(as_of),
            amount=figure,
            note=note,
            reference=reference,
            token=_token_row(session, principal),
            kind=kind,
        )
        return _change_out(session, change)

    return await _run(op)


@audited_tool(kind="write", entity="transaction", scope="write", emits=_CHANGE_EMITS)
async def reverse_value_change(transaction_id: str) -> dict[str, Any]:
    """Undo a change in a hand-valued asset's value (write scope): one made
    with record_value_change or posted by reconcile_account on a property
    account. transaction_id is the change's id (search_transactions on the
    account shows it).
    Purser corrects it through a reversal pair, so the history keeps both;
    amount in the answer is what was taken back and balance the asset's value
    now. Record the right figure afterwards if there is one."""

    def op(session: Session) -> dict[str, Any]:
        from purser import assets

        principal = _principal()
        change = assets.reverse_value_change(
            session,
            transaction_id=uuid.UUID(transaction_id),
            token=_token_row(session, principal),
        )
        return _change_out(session, change)

    return await _run(op)


@audited_tool(
    kind="write",
    entity="account",
    scope="write",
    emits=Emits(
        amount=("closing_change",),
        balance=("value_before", "gain", "money_put_in"),
    ),
)
async def dispose_asset(
    account: str, as_of: str, note: str | None = None, dry_run: bool = False
) -> dict[str, Any]:
    """Sell, trade in or write off a hand-valued asset (write scope). First
    record the sale's money as transfers out of the asset dated on or before
    as_of: the bank's deposit of the proceeds (update_transaction on that
    row with category="[asset path]") and any loan payoff. Then call this:
    what is left on the asset is the rest of its change in value — a gain or
    loss at sale, or its whole value for a write-off — posted against
    equity.revaluation on as_of, and the account closes at zero. Refused when
    an entry on the asset is dated after as_of. dry_run=true answers the
    same without writing; read value_before back to the user first: a value
    equal to the whole asset means no proceeds were recorded (a write-off)."""

    def op(session: Session) -> dict[str, Any]:
        from purser import assets

        principal = _principal()
        result = assets.dispose(
            session,
            account_ref=account,
            as_of=date_type.fromisoformat(as_of),
            note=note,
            dry_run=dry_run,
            token=_token_row(session, principal),
        )
        if dry_run:
            session.rollback()
        return {
            "account": result.account.path,
            "as_of": result.as_of.isoformat(),
            "value_before": _amount(result.value_before),
            "closing_change": _amount(result.closing_change),
            "gain": _amount(result.gain),
            "money_put_in": _amount(result.money_put_in),
            "dry_run": result.dry_run,
        }

    return await _run(op)
