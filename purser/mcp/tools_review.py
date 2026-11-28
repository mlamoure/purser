"""Review tools (issue #694, DESIGN §4, decision 141).

Registered on import by ``purser.mcp.server``, like the other tool groups.
"""

import uuid
from typing import Any

from sqlalchemy.orm import Session

from purser.mcp._registry import _principal, _run, _token_row, audited_tool
from purser.privacy import NOTHING, Emits


@audited_tool(
    kind="read",
    entity="transaction",
    scope="read",
    emits=Emits(
        payee=True,
        narrative=True,
        description=("rows[].description",),
        amount=("rows[].amount",),
    ),
)
async def review_batch(limit: int = 10, account: str | None = None) -> dict[str, Any]:
    """The transactions that need review, newest first (at most 25), with
    `total` and `remaining`. A row needs review for one `reason`:
    uncategorized (a bank-sync row with no category: categorize it with
    update_transaction, and propose_rule when the payee will recur) or
    reconciliation_adjustment (the difference a reconcile posted: recategorize
    or split it to what it really was, or accept_reconciliation_adjustment
    when it is right as it is). Each row carries its account, payee,
    description, amount, status, category and the financial notes linked to
    its account or payee. A fixed row leaves the list by itself."""

    def op(session: Session) -> dict[str, Any]:
        from purser import review as review_service

        return review_service.batch(session, limit=limit, account_ref=account)

    return await _run(op)


@audited_tool(kind="write", entity="account", scope="write", emits=NOTHING)
async def accept_reconciliation_adjustment(
    transaction_id: str, note: str | None = None
) -> dict[str, Any]:
    """Accept a reconciliation adjustment as it is (write scope), so it no
    longer needs review: use it when the difference a reconcile posted is
    right and nothing explains it better (otherwise recategorize or split the
    row). transaction_id is the adjustment (review_batch reason
    reconciliation_adjustment). note (200 characters or fewer) says why and
    goes on the audit record. Idempotent: created=false when it was already
    accepted. A row that is no reconciliation adjustment is refused."""

    def op(session: Session) -> dict[str, Any]:
        from purser import reconciliation

        accepted = reconciliation.accept_adjustment(
            session,
            tx_id=uuid.UUID(transaction_id),
            token=_token_row(session, _principal()),
            note=note,
        )
        return {
            "reconciliation_id": str(accepted.reconciliation_id),
            "account_id": str(accepted.account_id),
            "adjustment_tx_id": str(accepted.adjustment_tx_id),
            "accepted_at": accepted.accepted_at.isoformat(),
            "created": accepted.created,
        }

    return await _run(op)
