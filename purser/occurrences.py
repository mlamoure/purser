"""Evidence and independent certainty on existing schedule occurrences (#512).

The ledger remains authoritative for current values and actual cash movements.
Original snapshots are immutable; encrypted history keeps superseded evidence.
Read paths never manufacture evidence or write metadata.
"""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from purser import audit, clock
from purser.admin import security
from purser.core import certainty
from purser.ledger import LedgerError
from purser.models import (
    ApiToken,
    InstanceCertainty,
    InstanceStatement,
    Match,
    Schedule,
    SlotCertainty,
    Transaction,
)

DIMENSIONS = ("amount", "date")
EVIDENCE_KEYS = ("state", "value", "source", "as_of", "note", "set_by", "set_at")
# pg_advisory_xact_lock(SLOT_LOCK, hashtext(schedule_id)): serializes a slot
# evidence write with the slot materializing (#518), so evidence never strands.
SLOT_LOCK = 1347773013


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: Literal["estimated", "confirmed"]
    source: Literal[
        "history", "statement", "payroll", "invoice", "approval", "user", "agent", "other"
    ]
    as_of: date
    note: str = Field(min_length=1, max_length=500)


class AmountEvidence(Evidence):
    value: Decimal | None = Field(default=None, max_digits=20, decimal_places=4)


class DateEvidence(Evidence):
    value: date | None = None


class CertaintyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: AmountEvidence | None = None
    date: DateEvidence | None = None
    clear: list[Literal["amount", "date"]] = Field(
        default_factory=list,
        description="dimensions whose evidence returns to the rule's; values are untouched",
    )


def amount_of(tx: Transaction, schedule: Schedule) -> Decimal:
    leg = next((p for p in tx.postings if p.account_id == schedule.account_id), None)
    if leg is None:
        raise LedgerError("occurrence no longer has the schedule account's leg")
    return Decimal(leg.amount)


def rule_states(schedule: Schedule) -> tuple[str, str]:
    # Accepting a detected pattern is not evidence of a particular obligation.
    inferred = schedule.source == "detected"
    # A card payment sized from its statement cycle is an estimate (#674).
    cycle = schedule.payment_amount is not None
    return (
        "estimated" if inferred or cycle or schedule.amount_type == "estimated" else "confirmed",
        "estimated" if inferred or schedule.date_type == "estimated" else "confirmed",
    )


def _basis(schedule: Schedule) -> str:
    if schedule.payment_amount is not None:
        return "statement_cycle"  # a card payment sized from its statement cycle (#674)
    return schedule.estimate_basis or ("history" if schedule.source == "detected" else "fixed")


def ensure(
    session: Session, tx: Transaction, *, inherit: InstanceCertainty | None = None
) -> InstanceCertainty:
    """Capture before the first mutation; rematerialized slots inherit their original."""
    row = session.get(InstanceCertainty, tx.id)
    if row is not None:
        return row
    schedule = session.get(Schedule, tx.schedule_id)
    if schedule is None:
        raise LedgerError("not a schedule occurrence")
    slot = tx.occurrence_date or tx.date
    if inherit is None:
        inherit = session.scalar(
            select(InstanceCertainty)
            .where(
                InstanceCertainty.schedule_id == schedule.id,
                InstanceCertainty.occurrence_date == slot,
            )
            .order_by(InstanceCertainty.original_at)
            .limit(1)
        )
    row = InstanceCertainty(
        transaction_id=tx.id, schedule_id=schedule.id, occurrence_date=slot, amount_overridden=False
    )
    if inherit is not None:
        for key in (
            "original_amount",
            "original_date",
            "original_amount_basis",
            "original_date_basis",
            "original_window",
            "original_backfilled",
            "original_at",
            "history",
        ):
            setattr(row, key, getattr(inherit, key))
    else:
        from purser import schedules

        actuals = schedules.matched_actuals(session, schedule)
        n = (
            1
            if schedule.estimate_basis == "last"
            else 12
            if schedule.estimate_basis == "rolling_avg_12"
            else 3
        )
        window = actuals[:n]
        row.original_amount = amount_of(tx, schedule)
        row.original_date = tx.date
        row.original_amount_basis = _basis(schedule)
        row.original_date_basis = (
            "estimated" if rule_states(schedule)[1] == "estimated" else "fixed"
        )
        row.original_window = {
            "count": len(window),
            "from": min(d for _, d in window).isoformat() if window else None,
            "to": max(d for _, d in window).isoformat() if window else None,
            "as_of": clock.ledger_today().isoformat(),
            "source": schedule.source,
        }
        row.original_backfilled = False
        row.history = []
    _adopt_slot(session, row, tx, retain_original=inherit is not None)
    session.add(row)
    session.flush()
    return row


def lock_slots(session: Session, schedule_id: uuid.UUID) -> None:
    session.execute(
        text("SELECT pg_advisory_xact_lock(:ns, hashtext(:schedule))"),
        {"ns": SLOT_LOCK, "schedule": str(schedule_id)},
    )


def _copy(source: Any, target: Any, dimension: str) -> None:
    for key in EVIDENCE_KEYS:
        setattr(target, f"{dimension}_{key}", getattr(source, f"{dimension}_{key}"))


def _adopt_slot(
    session: Session, row: InstanceCertainty, tx: Transaction, *, retain_original: bool
) -> None:
    """A slot's projected evidence moves onto the instance that fills it (#518)."""
    slot = session.scalar(
        select(SlotCertainty).where(
            SlotCertainty.schedule_id == row.schedule_id,
            SlotCertainty.occurrence_date == row.occurrence_date,
            SlotCertainty.materialized_into.is_(None),
        )
    )
    if slot is None:
        return
    if not retain_original:
        original = slot.original
        row.original_amount = Decimal(original["amount"])
        row.original_date = date.fromisoformat(original["date"])
        row.original_amount_basis = original["amount_basis"]
        row.original_date_basis = original["date_basis"]
        row.original_window = original["window"]
        row.original_backfilled = original["backfilled"]
        row.original_at = datetime.fromisoformat(original["as_of"])
    for dimension in DIMENSIONS:
        _copy(slot, row, dimension)
    now = datetime.now(UTC)
    row.history = [
        *(slot.history or row.history or []),
        {"reason": "materialized", "at": now.isoformat(), "actor": None},
    ]
    slot.materialized_into = tx.id
    slot.materialized_at = now


def release(session: Session, tx: Transaction, *, reason: str, actor: uuid.UUID | None) -> None:
    """Before a slot is released, its instance's evidence returns to the slot,
    so the rematerialized instance carries it again (#518). Reset clears the
    evidence first, so nothing returns."""
    if tx.schedule_id is None or tx.occurrence_date is None:
        return
    row = session.get(InstanceCertainty, tx.id)
    if row is None:
        return
    slot = slot_row(session, tx.schedule_id, tx.occurrence_date)
    if slot is None:
        slot = SlotCertainty(
            schedule_id=tx.schedule_id,
            occurrence_date=tx.occurrence_date,
            original=describe(session, tx)["original"],
        )
        session.add(slot)
    _copy(row, slot, "amount")
    # A moved date is not the slot's: it stays in history, not as live evidence.
    if row.date_value == tx.occurrence_date:
        _copy(row, slot, "date")
    else:
        clear(slot, "date")
    slot.history = [
        *(row.history or []),
        {
            "reason": "released",
            "release_reason": reason,
            "transaction_id": str(tx.id),
            "at": datetime.now(UTC).isoformat(),
            "actor": str(actor) if actor else None,
            "amount": _explicit(row, "amount", row.amount_value),
            "date": _explicit(row, "date", row.date_value),
        },
    ]
    slot.materialized_into = None
    slot.materialized_at = None
    session.flush()


def held(
    row: InstanceCertainty | SlotCertainty | None, dimension: str, value: Decimal | date
) -> bool:
    return (
        row is not None
        and getattr(row, f"{dimension}_state") == "confirmed"
        and getattr(row, f"{dimension}_value") == value
    )


def protected(session: Session, tx: Transaction) -> bool:
    if tx.schedule_id is None:
        return False
    row = session.get(InstanceCertainty, tx.id)
    schedule = session.get(Schedule, tx.schedule_id)
    assert schedule is not None
    leg = next((p for p in tx.postings if p.account_id == schedule.account_id), None)
    return (leg is not None and held(row, "amount", Decimal(leg.amount))) or held(
        row, "date", tx.date
    )


def _explicit(
    row: InstanceCertainty | SlotCertainty | None, dimension: str, value: Decimal | date | None
) -> dict[str, Any] | None:
    if row is None or getattr(row, f"{dimension}_state") is None:
        return None
    # Changed values invalidate confirmations, but never turn explicit uncertainty
    # into a confirmation. Retain the evidence's bound value for inspection.
    stale = getattr(row, f"{dimension}_value") != value
    detail = {
        key: (
            v.isoformat()
            if isinstance(v, (date, datetime))
            else str(v)
            if isinstance(v, (Decimal, uuid.UUID))
            else v
        )
        for key in ("state", "value", "source", "as_of", "note", "set_by", "set_at")
        for v in [getattr(row, f"{dimension}_{key}")]
    }
    detail["value"] = str(value) if value is not None else None
    if stale:
        detail["state"] = "estimated"
        detail["evidence_value"] = str(getattr(row, f"{dimension}_value"))
    if dimension == "amount" and isinstance(row, InstanceCertainty):
        detail["value_overridden"] = row.amount_overridden if not stale else False
    return detail


def describe(session: Session, tx: Transaction, *, history: bool = False) -> dict[str, Any]:
    schedule = session.get(Schedule, tx.schedule_id)
    if schedule is None:
        raise LedgerError("not a schedule occurrence")
    leg = next((p for p in tx.postings if p.account_id == schedule.account_id), None)
    amount = Decimal(leg.amount) if leg is not None else None
    row = session.get(InstanceCertainty, tx.id)
    amount_state, date_state = rule_states(schedule)
    # A generic override is a supplied value, not a claim backed by evidence.
    if row is not None and row.original_backfilled and tx.schedule_pinned_at is not None:
        amount_state = "estimated"
        if tx.date != tx.occurrence_date:
            date_state = "estimated"
    amount_detail = _explicit(row, "amount", amount)
    date_detail = _explicit(row, "date", tx.date)
    statement = session.get(InstanceStatement, tx.id)
    if (
        amount_detail is None
        and amount is not None
        and statement is not None
        and any(Decimal(p.amount) == statement.statement_balance for p in tx.postings)
    ):
        amount_detail = {
            "state": "confirmed",
            "value": str(amount),
            "source": "statement",
            "as_of": (statement.statement_date or statement.recorded_at.date()).isoformat(),
            "set_by": str(statement.recorded_by),
            "set_at": statement.recorded_at.isoformat(),
        }
    amount_detail = amount_detail or {
        "state": amount_state if amount is not None else "estimated",
        "value": str(amount) if amount is not None else None,
        "source": _basis(schedule) if amount is not None else "account_leg_removed",
        "as_of": clock.ledger_today().isoformat(),
    }
    if row is not None:
        amount_detail.setdefault("value_overridden", row.amount_overridden)
    date_detail = date_detail or {
        "state": date_state,
        "value": tx.date.isoformat(),
        "source": "recurrence_rule",
        "as_of": schedule.created_at.date().isoformat(),
    }
    state = certainty.status(amount_detail["state"], date_detail["state"])
    match = session.scalar(
        select(Match).where(Match.expected_id == tx.id, Match.state.in_(["proposed", "confirmed"]))
    )
    actual: dict[str, Any] | None = None
    if match is not None:
        from purser import ledger

        posted = ledger.get_transaction(session, match.posted_id)
        leg = next((p for p in posted.postings if p.account_id == schedule.account_id), None)
        actual_amount = Decimal(leg.amount) if leg else None
        if actual_amount is None and schedule.counterparty_account_id is not None:
            other = next(
                (p for p in posted.postings if p.account_id == schedule.counterparty_account_id),
                None,
            )
            actual_amount = -Decimal(other.amount) if other else None
        actual = {
            "transaction_id": str(posted.id),
            "match_id": str(match.id),
            "match_state": match.state,
            "status": posted.status,
            "date": posted.date.isoformat(),
            "amount": str(actual_amount) if actual_amount is not None else None,
        }
        state = (
            "pending_review"
            if match.state == "proposed"
            else "matched"
            if posted.status == "posted"
            else "matched_pending"
        )
    original = None
    if row is not None:
        original = {
            "amount": str(row.original_amount),
            "date": row.original_date.isoformat(),
            "amount_basis": row.original_amount_basis,
            "date_basis": row.original_date_basis,
            "window": row.original_window,
            "as_of": row.original_at.isoformat(),
            "backfilled": row.original_backfilled,
        }
        if actual is not None and actual["amount"] is not None:
            actual["variance"] = certainty.variance(
                row.original_amount,
                row.original_date,
                Decimal(actual["amount"]),
                date.fromisoformat(actual["date"]),
            )
    return {
        "status": state,
        "amount_estimated": amount_detail["state"] == "estimated",
        "date_estimated": date_detail["state"] == "estimated",
        "amount": amount_detail,
        "date": date_detail,
        "original": original,
        "actual": actual,
        **({"history": row.history or []} if history and row else {}),
    }


def remember(
    session: Session, tx: Transaction, *, reason: str, actor: uuid.UUID | None
) -> InstanceCertainty:
    row = ensure(session, tx)
    # Keep the then-current evidence and any actual link before an undo.
    current = describe(session, tx)
    row.history = [
        *(row.history or []),
        {
            "reason": reason,
            "at": datetime.now(UTC).isoformat(),
            "actor": str(actor) if actor else None,
            "amount": current["amount"],
            "date": current["date"],
            **({"actual": current["actual"]} if current["actual"] is not None else {}),
        },
    ]
    return row


def clear(row: InstanceCertainty | SlotCertainty, dimension: str) -> None:
    for key in EVIDENCE_KEYS:
        setattr(row, f"{dimension}_{key}", None)


def set_evidence(
    row: InstanceCertainty | SlotCertainty,
    dimension: str,
    evidence: Evidence,
    value: Decimal | date,
    token: ApiToken,
) -> None:
    for key, val in {
        "state": evidence.state,
        "value": value,
        "source": evidence.source,
        "as_of": evidence.as_of,
        "note": evidence.note.strip(),
        "set_by": token.id,
        "set_at": datetime.now(UTC),
    }.items():
        setattr(row, f"{dimension}_{key}", val)


def _validate(change: CertaintyUpdate, token: ApiToken) -> None:
    if not security.can_write(token.scopes):
        raise LedgerError("occurrence certainty requires write scope")
    if change.amount is None and change.date is None and not change.clear:
        raise LedgerError("supply amount and/or date evidence, or dimensions to clear")
    for dimension in change.clear:
        if getattr(change, dimension) is not None:
            raise LedgerError(f"{dimension} evidence cannot be both supplied and cleared")
    for evidence in (change.amount, change.date):
        if evidence is None:
            continue
        if not evidence.note.strip():
            raise LedgerError("evidence note must not be blank")
        if evidence.as_of > clock.ledger_today():
            raise LedgerError("evidence as_of cannot be in the future")
        if evidence.state == "confirmed" and evidence.source == "history":
            raise LedgerError("historical inference cannot confirm an occurrence")


def update(
    session: Session, *, tx_id: uuid.UUID, change: CertaintyUpdate, token: ApiToken
) -> dict[str, Any]:
    from purser import ledger, schedules

    _validate(change, token)
    tx = ledger.lock_transaction(session, tx_id)
    if tx.schedule_id is None or tx.status != "expected" or tx.superseded_by is not None:
        raise LedgerError("not a live schedule occurrence")
    schedule = schedules.get_schedule(session, tx.schedule_id)
    before = describe(session, tx)
    row = remember(session, tx, reason="certainty_update", actor=token.id)
    for dimension in change.clear:
        clear(row, dimension)
    if change.amount and row.amount_state is None and row.amount_overridden is None:
        row.amount_overridden = False
    old_amount = amount_of(tx, schedule)
    old_pin = tx.schedule_pinned_at
    if (change.amount and change.amount.value is not None) or (
        change.date and change.date.value is not None
    ):
        ledger.update_transaction(
            session,
            tx_id=tx.id,
            token=token,
            amount=change.amount.value if change.amount else None,
            date=change.date.value if change.date else None,
            account_ref=str(schedule.account_id),
        )
        tx.schedule_pinned_at = old_pin  # dimension protection replaces a new blanket pin
    if change.amount and change.amount.value is not None and change.amount.value != old_amount:
        row.amount_overridden = True
    for dimension, evidence, value in (
        ("amount", change.amount, amount_of(tx, schedule)),
        ("date", change.date, tx.date),
    ):
        if evidence is not None:
            set_evidence(row, dimension, evidence, value, token)
    session.flush()
    if (change.amount and change.amount.value is not None) or (
        change.date and change.date.value is not None
    ):
        schedules.touch_instance(session, tx, pin=False)
    after = describe(session, tx)
    audit.record(
        session,
        actor_token_id=token.id,
        action="schedule.certainty_update",
        entity_type="transaction",
        entity_id=tx.id,
        before=before,
        after=after,
    )
    return after


def clone(session: Session, old: Transaction, new: Transaction) -> None:
    source = ensure(session, old)
    target = ensure(session, new, inherit=source)
    target.amount_overridden = source.amount_overridden
    for dimension in DIMENSIONS:
        _copy(source, target, dimension)
    session.flush()


# ---------------------------------------------------------------------------
# Projected slots (#518): evidence on a rule slot with no instance yet
# ---------------------------------------------------------------------------


def slot_holder(session: Session, schedule_id: uuid.UUID, slot: date) -> Transaction | None:
    """The transaction filling this slot, a live instance first; None = projected."""
    return session.scalar(
        select(Transaction)
        .where(Transaction.schedule_id == schedule_id, Transaction.occurrence_date == slot)
        .order_by(
            (Transaction.status == "expected").desc(),
            Transaction.superseded_by.is_(None).desc(),
            Transaction.created_at.desc(),
        )
        .limit(1)
    )


def slot_row(session: Session, schedule_id: uuid.UUID, slot: date) -> SlotCertainty | None:
    return session.scalar(
        select(SlotCertainty).where(
            SlotCertainty.schedule_id == schedule_id, SlotCertainty.occurrence_date == slot
        )
    )


def live_slots(session: Session, schedule_id: uuid.UUID) -> dict[date, SlotCertainty]:
    """Evidence still on unmaterialized slots of one schedule, by slot."""
    return {
        row.occurrence_date: row
        for row in session.scalars(
            select(SlotCertainty).where(
                SlotCertainty.schedule_id == schedule_id,
                SlotCertainty.materialized_into.is_(None),
            )
        )
    }


def _projected_amount(session: Session, schedule: Schedule, slot: date) -> Decimal:
    """The amount the forecast projects for this slot (schedule account's leg),
    refusing a slot the forecast cannot project."""
    from purser import schedules
    from purser.core import forecasting

    today = clock.ledger_today()
    if schedule.status != "active":
        raise LedgerError(f"only an active schedule projects slots (status is {schedule.status})")
    if slot < today:
        raise LedgerError(f"{slot} is in the past; a past slot is never projected")
    if (slot - today).days > schedules.FORECAST_MAX_DAYS:
        raise LedgerError(f"{slot} is beyond the {schedules.FORECAST_MAX_DAYS}-day forecast")
    if not forecasting.expand_rrule(schedule.rrule, dtstart=schedule.dtstart, start=slot, end=slot):
        raise LedgerError(f"{slot} is not a slot of this schedule's rule")
    amount = schedules.slot_amount(session, schedule, slot)
    if amount == 0:
        raise LedgerError("the rule estimates no amount yet, so the slot is not projected")
    return amount


def snapshot(session: Session, schedule: Schedule, slot: date, amount: Decimal) -> dict[str, Any]:
    from purser import schedules

    actuals = schedules.matched_actuals(session, schedule)
    n = (
        1
        if schedule.estimate_basis == "last"
        else 12
        if schedule.estimate_basis == "rolling_avg_12"
        else 3
    )
    window = actuals[:n] if schedule.amount_type == "estimated" else []
    return {
        "amount": str(amount),
        "date": slot.isoformat(),
        "amount_basis": schedules.instance_basis(schedule, None, None),
        "date_basis": "estimated" if rule_states(schedule)[1] == "estimated" else "fixed",
        "window": {
            "count": len(window),
            "from": min(d for _, d in window).isoformat() if window else None,
            "to": max(d for _, d in window).isoformat() if window else None,
            "source": schedule.source,
            "as_of": clock.ledger_today().isoformat(),
        },
        "as_of": datetime.now(UTC).isoformat(),
        "backfilled": True,
    }


def slot_ref(schedule: Schedule, slot: date, tx: Transaction | None) -> dict[str, Any]:
    return {
        "schedule_id": str(schedule.id),
        "occurrence_date": slot.isoformat(),
        "transaction_id": str(tx.id) if tx is not None else None,
        "projected": tx is None,
    }


def describe_slot(
    session: Session, *, schedule_id: uuid.UUID, slot: date, history: bool = False
) -> dict[str, Any]:
    """One slot's certainty, materialized or not: a filled slot reads its
    transaction; a projected one its slot evidence over the rule's."""
    from purser import schedules

    schedule = schedules.get_schedule(session, schedule_id)
    tx = slot_holder(session, schedule.id, slot)
    if tx is not None:
        detail = describe(session, tx, history=history)
    else:
        amount = _projected_amount(session, schedule, slot)
        row = live_slots(session, schedule.id).get(slot)
        detail = projected(schedule, slot, amount, row, history=history)
    return {**detail, "slot": slot_ref(schedule, slot, tx)}


def update_slot(
    session: Session,
    *,
    schedule_id: uuid.UUID,
    slot: date,
    change: CertaintyUpdate,
    token: ApiToken,
) -> dict[str, Any]:
    """Set or clear evidence on one slot. A materialized slot is its
    instance's ``update``; a projected one keeps the rule's values (evidence
    only), never posts, and leaves every other slot alone."""
    from purser import schedules

    _validate(change, token)
    schedule = schedules.get_schedule(session, schedule_id)
    tx = slot_holder(session, schedule.id, slot)
    if tx is None:
        lock_slots(session, schedule_id)
        # Materialization may have won while this write waited for the slot lock.
        session.refresh(schedule)
        tx = slot_holder(session, schedule.id, slot)
    if tx is not None:
        if tx.status != "expected" or tx.superseded_by is not None:
            raise LedgerError(
                f"slot {slot} is settled or skipped (transaction {tx.id}); "
                "read it with its transaction_id"
            )
        for dimension, evidence, current in (
            ("amount", change.amount, amount_of(tx, schedule)),
            ("date", change.date, tx.date),
        ):
            if evidence is not None and evidence.value is not None and evidence.value != current:
                raise LedgerError(f"slot evidence cannot change {dimension}; omit value")
        # Even an identical supplied value must not enter the ledger edit path.
        change = change.model_copy(deep=True)
        for evidence in (change.amount, change.date):
            if evidence is not None:
                evidence.value = None
        result = update(session, tx_id=tx.id, change=change, token=token)
        return {**result, "slot": slot_ref(schedule, slot, tx)}
    amount = _projected_amount(session, schedule, slot)
    for name, evidence, current in (("amount", change.amount, amount), ("date", change.date, slot)):
        if evidence is not None and evidence.value is not None and evidence.value != current:
            raise LedgerError(
                f"a projected slot's {name} comes from the rule ({current}); omit value to "
                "keep it, change the rule with update_schedule, or set a different value "
                "once the slot materializes"
            )
    row = slot_row(session, schedule.id, slot)
    if row is not None:
        # An instance can disappear during account cleanup. Reactivate its slot
        # metadata without discarding the retained original or prior evidence.
        row.materialized_into = None
        row.materialized_at = None
    before = projected(schedule, slot, amount, row)
    if row is None:
        row = SlotCertainty(
            schedule_id=schedule.id,
            occurrence_date=slot,
            history=[],
            original=snapshot(session, schedule, slot, amount),
        )
        session.add(row)
    row.history = [
        *(row.history or []),
        {
            "reason": "certainty_update",
            "at": datetime.now(UTC).isoformat(),
            "actor": str(token.id),
            "amount": before["amount"],
            "date": before["date"],
        },
    ]
    for dimension in change.clear:
        clear(row, dimension)
    if change.amount is not None:
        set_evidence(row, "amount", change.amount, amount, token)
    if change.date is not None:
        set_evidence(row, "date", change.date, slot, token)
    session.flush()
    after = projected(schedule, slot, amount, row)
    audit.record(
        session,
        actor_token_id=token.id,
        action="schedule.slot_certainty_update",
        entity_type="schedule",
        entity_id=schedule.id,
        before={"occurrence_date": slot.isoformat(), **before},
        after={"occurrence_date": slot.isoformat(), **after},
    )
    return {**after, "slot": slot_ref(schedule, slot, None)}


class DimensionOut(BaseModel):
    state: Literal["estimated", "confirmed"]
    value: str | None
    source: str
    as_of: str | None = None
    note: str | None = None
    set_by: str | None = None
    set_at: str | None = None
    evidence_value: str | None = None
    value_overridden: bool | None = None


class OriginalOut(BaseModel):
    amount: str
    date: str
    amount_basis: str
    date_basis: str
    window: dict[str, Any] | None = None
    as_of: str
    backfilled: bool


class VarianceOut(BaseModel):
    amount: str
    days: int


class ActualOut(BaseModel):
    transaction_id: str
    match_id: str
    match_state: str
    status: str
    date: str
    amount: str | None
    variance: VarianceOut | None = None


class SlotOut(BaseModel):
    schedule_id: str
    occurrence_date: str
    transaction_id: str | None = Field(
        description="the instance filling the slot; null = projected"
    )
    projected: bool


class CertaintyOut(BaseModel):
    status: Literal["estimated", "confirmed", "pending_review", "matched", "matched_pending"]
    amount_estimated: bool
    date_estimated: bool
    amount: DimensionOut
    date: DimensionOut
    original: OriginalOut | None = None
    actual: ActualOut | None = None
    history: list[dict[str, Any]] | None = None
    slot: SlotOut | None = Field(
        default=None,
        description="the rule slot: projected slots take evidence by schedule_id + occurrence_date",
    )


def projected(
    schedule: Schedule,
    slot: date,
    amount: Decimal,
    row: SlotCertainty | None = None,
    *,
    history: bool = False,
) -> dict[str, Any]:
    """A slot with no instance: the rule's states unless slot evidence still
    matches the projected leg (``amount``, the schedule account's). There is
    an original only after the first metadata write retains a snapshot."""
    amount_state, date_state = rule_states(schedule)
    amount_detail = _explicit(row, "amount", amount) or {
        "state": amount_state,
        "value": str(amount),
        "source": _basis(schedule),
        "as_of": clock.ledger_today().isoformat(),
    }
    date_detail = _explicit(row, "date", slot) or {
        "state": date_state,
        "value": slot.isoformat(),
        "source": "recurrence_rule",
        "as_of": schedule.created_at.date().isoformat(),
    }
    return {
        "status": certainty.status(amount_detail["state"], date_detail["state"]),
        "amount_estimated": amount_detail["state"] == "estimated",
        "date_estimated": date_detail["state"] == "estimated",
        "amount": amount_detail,
        "date": date_detail,
        "original": row.original if row is not None else None,
        "actual": None,
        **({"history": row.history or []} if history and row is not None else {}),
    }


def perspective(detail: dict[str, Any], *, reverse: bool) -> dict[str, Any]:
    """Forecast provenance uses the same leg as its event, including variances."""
    if not reverse:
        return detail
    from copy import deepcopy

    out = deepcopy(detail)
    for obj, key in (
        (out["amount"], "value"),
        (out["amount"], "evidence_value"),
        (out.get("original"), "amount"),
        (out.get("actual"), "amount"),
        ((out.get("actual") or {}).get("variance"), "amount"),
    ):
        if obj is not None and obj.get(key) is not None:
            obj[key] = str(-Decimal(obj[key]))
    return out
