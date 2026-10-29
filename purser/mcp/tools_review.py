"""Review-flag tools (issue #175, DESIGN §4, decision 47).

Registered on import by ``purser.mcp.server``, like the other tool groups.
"""

import uuid
from typing import Any

from sqlalchemy.orm import Session

from purser.mcp._registry import _principal, _run, _token_row, audited_tool
from purser.mcp.privacy import NOTHING, Emits


@audited_tool(
    kind="read",
    entity="transaction",
    scope="read",
    emits=Emits(
        payee=True,
        narrative=True,
        description=("rows[].description", "escalated[].description"),
        amount=("rows[].amount", "escalated[].amount"),
    ),
)
async def review_batch(
    limit: int = 10, account: str | None = None, include_guidance: bool = True
) -> dict[str, Any]:
    """The review loop: the next unreviewed transactions with everything you
    need to judge them INLINE — current category and how it was assigned, any
    transfer already on the row, raw-vs-friendly payee, what automation did —
    plus the checklist to review against. No per-row round-trips.

    Check `context` before clearing rows: it says which surfaces were actually
    available. If one you needed was absent, escalate rather than clear.
    Rows the backend reviewer handed back appear under `escalated` first.
    Fix what is wrong with update_transaction/confirm_transfer, then
    mark_reviewed. Set include_guidance=false on later pages."""

    def op(session: Session) -> dict[str, Any]:
        from purser import review as review_service

        return review_service.batch(
            session, limit=limit, account_ref=account, include_guidance=include_guidance
        )

    return await _run(op)


@audited_tool(kind="write", entity="transaction", scope="write", emits=NOTHING)
async def mark_reviewed(
    transaction_ids: list[str], note: str | None = None, escalate: bool = False
) -> dict[str, int]:
    """Mark reviewed transactions as reviewed (write scope). Bulk and
    idempotent. Set escalate=true to clear the row but flag it for the user —
    use that when the evidence is ambiguous rather than guessing, for example
    when more than one row could be a transfer's pair."""

    def op(session: Session) -> dict[str, int]:
        from purser import review as review_service

        token = _token_row(session, _principal())
        marked = 0
        already = 0
        for raw in transaction_ids:
            created = review_service.mark(
                session,
                tx_id=uuid.UUID(raw),
                token=token,
                via=review_service.VIA_EXPLICIT,
                note=note,
                escalate=escalate,
            )
            if created:
                marked += 1
            else:
                already += 1
        return {"marked": marked, "already_reviewed": already}

    return await _run(op)
