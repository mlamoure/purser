"""Per-occurrence evidence, separate from ledger posting (#512, #518)."""

import uuid
from datetime import date as date_type
from typing import Any, Literal

from sqlalchemy.orm import Session

from purser import ledger, occurrences
from purser.ledger import LedgerError
from purser.mcp._registry import _principal, _run, _token_row, audited_tool
from purser.mcp.privacy import Emits

CERTAINTY_EMITS = Emits(
    description=("amount.note", "date.note"),
    amount=(
        "amount.value",
        "amount.evidence_value",
        "original.amount",
        "actual.amount",
        "actual.variance.amount",
        "amount.note",
        "date.note",
    ),
)


def _slot(
    transaction_id: str | None, schedule_id: str | None, occurrence_date: str | None
) -> tuple[uuid.UUID, date_type] | None:
    """The occurrence's address: a transaction id, or a schedule slot (#518)."""
    if transaction_id is not None:
        if schedule_id is not None or occurrence_date is not None:
            raise LedgerError("pass transaction_id, or schedule_id with occurrence_date, not both")
        return None
    if schedule_id is None or occurrence_date is None:
        raise LedgerError("pass transaction_id, or schedule_id with occurrence_date")
    return uuid.UUID(schedule_id), date_type.fromisoformat(occurrence_date)


@audited_tool(kind="read", entity="transaction", scope="read", emits=CERTAINTY_EMITS)
async def get_schedule_occurrence(
    transaction_id: str | None = None,
    schedule_id: str | None = None,
    occurrence_date: str | None = None,
) -> dict[str, Any]:
    """Read one occurrence's independent amount/date certainty, evidence,
    immutable original estimate and actual link with amount/timing variance.
    Address it by transaction_id (search_transactions), or by schedule_id
    (list_schedules) plus occurrence_date for a projected slot with no
    instance yet (slot.projected). The first evidence write retains its original
    projection; untouched projections have original null. confirmed means an upcoming
    obligation, never posted cash. pending_review means a proposed match,
    not a confirmation. A backfilled original is the first retained snapshot,
    not a reconstructed historical estimate. No bank fetch or writes."""

    def op(session: Session) -> dict[str, Any]:
        slot = _slot(transaction_id, schedule_id, occurrence_date)
        if slot is not None:
            return occurrences.describe_slot(session, schedule_id=slot[0], slot=slot[1])
        assert transaction_id is not None
        return occurrences.describe(
            session, ledger.get_transaction(session, uuid.UUID(transaction_id))
        )

    return await _run(op)


@audited_tool(kind="write", entity="transaction", scope="write", emits=CERTAINTY_EMITS)
async def update_occurrence_certainty(
    transaction_id: str | None = None,
    amount: occurrences.AmountEvidence | None = None,
    date: occurrences.DateEvidence | None = None,
    schedule_id: str | None = None,
    occurrence_date: str | None = None,
    clear: list[Literal["amount", "date"]] | None = None,
) -> dict[str, Any]:
    """Set evidence on just this occurrence's amount and/or date. Address it
    by transaction_id, or by schedule_id plus occurrence_date (works for a
    projected slot beyond the materialized window: the rule's amount and
    date stay, the evidence moves onto the instance when it materializes).
    Each supplied dimension needs state (estimated/confirmed), source, as_of
    and a note; value is optional (omit to keep the current value; a
    schedule-slot address accepts only its current value). A fixed amount is a
    model, not proof: mark it estimated without changing it. History is an
    estimate, never confirmation evidence. Confirm only what the supplied
    statement, paystub, invoice or user instruction establishes; omit the
    other dimension. clear returns a dimension to the rule's certainty. This
    never posts cash or confirms later occurrences. Link a real posted
    payment with match instead; ambiguous candidates require review. Original
    estimates and superseded confirmations remain available in Purser."""

    def op(session: Session) -> dict[str, Any]:
        change = occurrences.CertaintyUpdate(amount=amount, date=date, clear=clear or [])
        token = _token_row(session, _principal())
        slot = _slot(transaction_id, schedule_id, occurrence_date)
        if slot is not None:
            return occurrences.update_slot(
                session, schedule_id=slot[0], slot=slot[1], change=change, token=token
            )
        assert transaction_id is not None
        return occurrences.update(
            session, tx_id=uuid.UUID(transaction_id), change=change, token=token
        )

    return await _run(op)
