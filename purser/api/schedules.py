"""Schedules & forecast API."""

import uuid
from datetime import date as date_type
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from purser import schedules as service
from purser import suggestions as suggestions_service
from purser.admin.auth import ReadToken, WriteToken
from purser.core import forecasting as core_forecasting
from purser.db import get_session
from purser.ledger import LedgerError, NotFoundError
from purser.models import Schedule, Suggestion

DbSession = Annotated[Session, Depends(get_session)]

router = APIRouter(prefix="/schedules", tags=["schedules"])
forecast_router = APIRouter(prefix="/forecast", tags=["forecast"])


EstimateBasisLiteral = Literal["last", "rolling_avg_3", "rolling_avg_12", "counterparty_balance"]
# Where an expected amount came from (decision 80): core_forecasting.AMOUNT_BASES.
AmountBasisLiteral = Literal[
    "fixed",
    "last",
    "rolling_avg_3",
    "rolling_avg_12",
    "counterparty_balance",
    "override",
    "statement",
]
_BASIS_DESCRIPTION = (
    "where the amount comes from: fixed, an estimate basis (last, rolling_avg_3, "
    "rolling_avg_12, counterparty_balance = the card's current balance), override (a "
    "pinned instance: the amount is yours) or statement (a recorded card statement "
    "balance, decision 80)"
)
AutopostLiteral = Literal["auto_post", "wait_for_match", "reminder_only"]


class ScheduleOut(BaseModel):
    id: uuid.UUID
    name: str
    account_id: uuid.UUID
    counterparty_account_id: uuid.UUID | None = Field(
        default=None, description="set = transfer schedule (two-leg instances); null = flat"
    )
    category_path: str | None = Field(default=None, description="posting template (flat schedules)")
    rrule: str
    dtstart: date_type
    amount_type: str
    amount: Decimal | None
    estimate_basis: str | None
    currency: str
    next_due: date_type | None
    autopost_policy: str
    source: str
    status: str
    ended_at: datetime | None

    @classmethod
    def from_model(cls, s: Schedule) -> "ScheduleOut":
        return cls(
            id=s.id,
            name=s.name,
            account_id=s.account_id,
            counterparty_account_id=s.counterparty_account_id,
            category_path=str(s.category_path) if s.category_path else None,
            rrule=s.rrule,
            dtstart=s.dtstart,
            amount_type=s.amount_type,
            amount=s.amount,
            estimate_basis=s.estimate_basis,
            currency=s.currency.strip(),
            next_due=s.next_due,
            autopost_policy=s.autopost_policy,
            source=s.source,
            status=s.status,
            ended_at=s.ended_at,
        )


class SuggestionRef(BaseModel):
    """What a `propose=true` create returns (#233, #436): the suggestion the
    draft landed in, not a schedule. `status='proposed'` keeps the old
    contract readable; `suggestion_id` is the row to decide on."""

    id: uuid.UUID
    suggestion_id: uuid.UUID
    status: Literal["proposed"] = "proposed"
    source: str
    kind: Literal["schedule"] = "schedule"
    headline: str


def _suggestion_ref(row: Suggestion) -> SuggestionRef:
    return SuggestionRef(
        id=row.id,
        suggestion_id=row.id,
        source=row.source,
        headline=suggestions_service.headline(row),
    )


class InstanceOut(BaseModel):
    """A live (unmatched) expected instance of a schedule."""

    transaction_id: uuid.UUID
    date: date_type
    occurrence_date: date_type | None = Field(
        description="the rule slot the instance fills; differs from date after an edit"
    )
    amount: Decimal | None
    pinned: bool = Field(description="edited by a human/agent; exempt from rule refresh/resync")
    overdue: bool
    basis: AmountBasisLiteral = Field(description=_BASIS_DESCRIPTION)
    statement_balance: Decimal | None = Field(
        default=None,
        description="the card statement balance recorded for this payment, as printed (owed)",
    )
    statement_date: date_type | None = Field(
        default=None, description="the statement's closing date, when recorded"
    )
    statement_source: Literal["human", "agent"] | None = Field(
        default=None, description="who recorded the statement"
    )


class ScheduleDetail(ScheduleOut):
    instances: list[InstanceOut]


class ScheduleCreate(BaseModel):
    name: str
    account: str = Field(description="account id or path")
    rrule: str = Field(description="RFC 5545 RRULE, e.g. FREQ=MONTHLY;BYMONTHDAY=19")
    dtstart: date_type | None = Field(
        default=None,
        description="RRULE anchor (first occurrence for anchor-sensitive rules); default today",
    )
    amount_type: Literal["fixed", "estimated"] = "fixed"
    amount: Decimal | None = Field(default=None, decimal_places=4, max_digits=20)
    payee: str | None = None
    category: str | None = Field(default=None, description="category path for instances")
    counterparty_account: str | None = Field(
        default=None,
        description="the other real account of a recurring transfer (exclusive with category)",
    )
    estimate_basis: EstimateBasisLiteral | None = None
    autopost_policy: AutopostLiteral = "wait_for_match"
    note: str | None = Field(
        default=None,
        max_length=200,
        description="with propose=true: evidence for the person deciding the proposal, such "
        "as the statement line it came from; kept encrypted with the suggestion and shown on "
        "its card. A direct create has no suggestion and keeps no note.",
    )
    propose: bool = Field(
        default=False,
        description="land a suggestion in the inbox for the user to decide instead of "
        "creating the schedule; for an agent acting on its own initiative (#436)",
    )


class ScheduleUpdate(BaseModel):
    """Every field optional; only the fields sent change. A rule change
    resyncs live un-pinned instances; a new name or payee relabels live
    instances whose label still equals the old one (see DESIGN §6)."""

    name: str | None = None
    rrule: str | None = None
    dtstart: date_type | None = None
    amount_type: Literal["fixed", "estimated"] | None = None
    amount: Decimal | None = Field(default=None, decimal_places=4, max_digits=20)
    payee: str | None = None
    category: str | None = None
    counterparty_account: str | None = None
    estimate_basis: EstimateBasisLiteral | None = None
    autopost_policy: AutopostLiteral | None = None


class ForecastRangeOut(BaseModel):
    """An estimated amount's band (#247, decision 85), in this account's
    sign: min/max of the schedule's newest settled amounts and this one."""

    low: Decimal
    high: Decimal


class ForecastPointOut(BaseModel):
    date: date_type
    balance: Decimal


class ForecastEventOut(BaseModel):
    label: str
    amount: Decimal
    transaction_id: uuid.UUID | None
    projected: bool = Field(
        default=False, description="expanded from the rule; no instance row exists yet"
    )
    overdue: bool = Field(default=False, description="dated before today and still due")
    basis: AmountBasisLiteral | None = Field(default=None, description=_BASIS_DESCRIPTION)
    range: ForecastRangeOut | None = Field(
        default=None,
        description="an estimated amount's band; null when fixed, overridden or stated",
    )


class ForecastDayOut(BaseModel):
    date: date_type
    balance: Decimal
    events: list[ForecastEventOut]
    low: Decimal | None = Field(
        default=None,
        description="pessimistic balance: every ranged event at its worst; null without a band",
    )
    high: Decimal | None = Field(
        default=None,
        description="optimistic balance: every ranged event at its best; null without a band",
    )


class ForecastCoverage(BaseModel):
    """What the projection is made of (#235) — zero active schedules means a
    line that is flat by construction, not a calm month."""

    schedules_active: int
    instances: int
    projected: int
    suggestions_open: int
    matches_pending: int = Field(
        default=0,
        description=(
            "instances left out of the line as paid: an open match proposal names a live "
            "bank row already in the opening balance (decision 76)"
        ),
    )
    matches_pending_net: Decimal | None = Field(
        default=None, description="net of the left-out instances' legs; null when zero"
    )


class ForecastOut(BaseModel):
    account_id: uuid.UUID
    account_path: str
    start_balance: Decimal
    min_balance: Decimal | None
    first_shortfall: date_type | None
    coverage: ForecastCoverage
    days: list[ForecastDayOut]
    pessimistic_low: ForecastPointOut | None = Field(
        default=None,
        description=(
            "the minimum of the pessimistic series (decision 85); null without a band. "
            "first_shortfall stays on the point series"
        ),
    )
    optimistic_low: ForecastPointOut | None = Field(
        default=None, description="the minimum of the optimistic series; null without a band"
    )


def _handle(exc: LedgerError) -> HTTPException:
    if isinstance(exc, NotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if "requires write scope" in str(exc):
        return HTTPException(status.HTTP_403_FORBIDDEN, str(exc))
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))


@router.post("", status_code=status.HTTP_201_CREATED)
def create_schedule(
    body: ScheduleCreate, token: WriteToken, session: DbSession
) -> ScheduleOut | SuggestionRef:
    """Create a bill/income schedule, active. With `propose=true` it lands
    a SUGGESTION instead (`status='proposed'`, `suggestion_id`) that the
    user's decision turns into the schedule. A proposal for a payee or
    counterparty the account already has a schedule for is refused (422,
    naming that schedule)."""
    try:
        schedule = service.create_schedule(
            session,
            name=body.name,
            account_ref=body.account,
            rrule=body.rrule,
            amount_type=body.amount_type,
            amount=body.amount,
            payee_name=body.payee,
            category_path=body.category,
            estimate_basis=body.estimate_basis,
            autopost_policy=body.autopost_policy,
            source="manual" if token.kind == "session" else "agent",
            token=token,
            dtstart=body.dtstart,
            counterparty_account_ref=body.counterparty_account,
            note=body.note,
            propose=body.propose,
        )
    except LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    if isinstance(schedule, Suggestion):
        return _suggestion_ref(schedule)
    return ScheduleOut.from_model(schedule)


@router.get("")
def list_schedules(
    token: ReadToken,
    session: DbSession,
    status_in: Annotated[list[str] | None, Query(alias="status")] = None,
) -> list[ScheduleOut]:
    stmt = select(Schedule).order_by(Schedule.created_at)
    if status_in:
        stmt = stmt.where(Schedule.status.in_(status_in))
    return [ScheduleOut.from_model(s) for s in session.execute(stmt).scalars()]


def _detail(session: Session, schedule: Schedule) -> ScheduleDetail:
    base = ScheduleOut.from_model(schedule)
    return ScheduleDetail(
        **base.model_dump(),
        instances=[InstanceOut(**row) for row in service.list_instances(session, schedule)],
    )


@router.get("/{schedule_id}")
def get_schedule(schedule_id: uuid.UUID, token: ReadToken, session: DbSession) -> ScheduleDetail:
    """The rule plus its live instances (oldest first; overdue flagged)."""
    try:
        schedule = service.get_schedule(session, schedule_id)
    except LedgerError as exc:
        raise _handle(exc) from exc
    return _detail(session, schedule)


@router.patch("/{schedule_id}")
def update_schedule(
    schedule_id: uuid.UUID, body: ScheduleUpdate, token: WriteToken, session: DbSession
) -> ScheduleDetail:
    """Edit the rule. Live un-pinned instances follow (slots the new rule no
    longer produces are voided and released, amounts re-estimated, missing
    slots materialized); pinned instances keep their amounts and dates. A new
    `name` or `payee` relabels every live instance — pinned ones too — whose
    description or payee still equals the old one; labels you changed stay."""
    try:
        updated = service.update_schedule(
            session,
            schedule_id=schedule_id,
            token=token,
            name=body.name,
            rrule=body.rrule,
            dtstart=body.dtstart,
            amount_type=body.amount_type,
            amount=body.amount,
            estimate_basis=body.estimate_basis,
            category_path=body.category,
            payee_name=body.payee,
            autopost_policy=body.autopost_policy,
            counterparty_account_ref=body.counterparty_account,
        )
    except LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    return _detail(session, updated.schedule)


@router.post("/{schedule_id}/resume")
def resume_schedule(schedule_id: uuid.UUID, token: WriteToken, session: DbSession) -> ScheduleOut:
    """paused -> active; materializes immediately."""
    try:
        schedule = service.resume_schedule(session, schedule_id=schedule_id, token=token)
    except LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    return ScheduleOut.from_model(schedule)


@router.post("/{schedule_id}/pause")
def pause_schedule(schedule_id: uuid.UUID, token: WriteToken, session: DbSession) -> ScheduleOut:
    """Stop expecting: live un-pinned instances are voided and their slots
    released; resume brings them back."""
    try:
        schedule = service.pause_schedule(session, schedule_id=schedule_id, token=token)
    except LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    return ScheduleOut.from_model(schedule)


@router.post("/{schedule_id}/end")
def end_schedule(schedule_id: uuid.UUID, token: WriteToken, session: DbSession) -> ScheduleOut:
    """Terminal: no more instances, history kept."""
    try:
        schedule = service.end_schedule(session, schedule_id=schedule_id, token=token)
    except LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    return ScheduleOut.from_model(schedule)


@router.post("/materialize")
def materialize(token: WriteToken, session: DbSession) -> dict[str, int]:
    """Materialize expected instances for all active schedules (idempotent)."""
    stats = service.materialize_all(session)
    session.commit()
    return stats


class DetectResult(BaseModel):
    suggestions: list[SuggestionRef]
    created: int
    refreshed: int


@router.post("/detect")
def detect(token: WriteToken, session: DbSession) -> DetectResult:
    """Scan import history for recurring payees and land the patterns in the
    suggestions inbox (#233); re-running refreshes open rows and never
    re-proposes what was rejected. Nothing activates here."""
    before = {
        r.id
        for r in suggestions_service.list_suggestions(
            session, status="all", kind=suggestions_service.KIND_SCHEDULE, limit=25
        )
    }
    touched = service.detect_schedules(session, actor_token_id=token.id)
    session.commit()
    created = sum(1 for r in touched if r.id not in before)
    return DetectResult(
        suggestions=[_suggestion_ref(r) for r in touched],
        created=created,
        refreshed=len(touched) - created,
    )


class StatementBalanceIn(BaseModel):
    """A card statement balance for the payment it is due on (decision 80).
    Address the payment instance by `transaction_id`, or by the card
    (`account`) plus the statement's `due_date`."""

    statement_balance: Decimal = Field(
        decimal_places=4,
        max_digits=20,
        description="as the statement prints it: the amount owed, positive",
    )
    statement_date: date_type | None = Field(
        default=None, description="the statement's closing date (today or earlier)"
    )
    transaction_id: uuid.UUID | None = Field(
        default=None, description="the payment instance (exclusive with account + due_date)"
    )
    account: str | None = Field(default=None, description="the credit card, id or path")
    due_date: date_type | None = Field(
        default=None,
        description=(
            "the statement's payment due date: the card's live payment instance nearest it, "
            f"within {service.STATEMENT_DUE_WINDOW_DAYS} days, takes the balance"
        ),
    )


class StatementBalanceOut(InstanceOut):
    schedule_id: uuid.UUID
    previous_amount: Decimal = Field(
        description="the schedule account's leg before the statement was recorded"
    )


@router.post("/statement-balance")
def record_statement_balance(
    body: StatementBalanceIn, token: WriteToken, session: DbSession
) -> StatementBalanceOut:
    """Record a credit card's statement balance against the payment it is due
    on. The payment instance takes the statement balance (both legs move, the
    card's leg = the balance), is pinned so the current-balance estimate no
    longer moves it, and reports `basis: statement`; the statement, its
    closing date and who recorded it stay on the instance. Recording again
    replaces it; resetting the instance removes it. Credit cards only — a
    loan payment is an ordinary override (`PATCH /transactions/{id}`)."""
    try:
        recorded = service.record_statement_balance(
            session,
            token=token,
            statement_balance=body.statement_balance,
            statement_date=body.statement_date,
            transaction_id=body.transaction_id,
            account_ref=body.account,
            due_date=body.due_date,
        )
    except LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    (row,) = [
        r
        for r in service.list_instances(session, recorded.schedule)
        if r["transaction_id"] == recorded.tx.id
    ]
    return StatementBalanceOut(
        **row, schedule_id=recorded.schedule.id, previous_amount=recorded.previous_amount
    )


@forecast_router.get("")
def forecast(
    token: ReadToken,
    session: DbSession,
    account: Annotated[str, Query(description="account id or path")],
    days: Annotated[int, Query(ge=1, le=service.FORECAST_MAX_DAYS)] = 30,
) -> ForecastOut:
    """Deterministic daily balance projection with annotated events —
    'checking goes negative on the 19th' is answerable in this one call.
    Instances beyond the materialized horizon are expanded from the rules
    (`projected`), so `days` is honoured up to 365; `coverage` says what the
    line is made of. Every event names its `basis`; an estimated one carries
    a `range` band (min/max of its schedule's newest settled amounts and its
    own), and then each day carries `low`/`high` — the balance with every
    ranged event at its worst / best — and `pessimistic_low`/`optimistic_low`
    their minimums. `first_shortfall` stays on the point series."""
    try:
        result = service.forecast_account(session, account_ref=account, horizon_days=days)
    except LedgerError as exc:
        raise _handle(exc) from exc
    fc = result["forecast"]

    def point(p: core_forecasting.ForecastPoint | None) -> ForecastPointOut | None:
        return ForecastPointOut(date=p.date, balance=p.balance) if p is not None else None

    return ForecastOut(
        account_id=result["account_id"],
        account_path=result["account_path"],
        start_balance=fc.start_balance,
        min_balance=fc.min_balance,
        first_shortfall=fc.first_shortfall,
        coverage=ForecastCoverage(**result["coverage"]),
        days=[
            ForecastDayOut(
                date=d.date,
                balance=d.balance,
                low=d.low,
                high=d.high,
                events=[
                    ForecastEventOut(
                        label=e.label,
                        amount=e.amount,
                        transaction_id=e.transaction_id,
                        projected=e.projected,
                        overdue=e.overdue,
                        basis=e.basis,
                        range=(
                            ForecastRangeOut(low=e.range[0], high=e.range[1])
                            if e.range is not None
                            else None
                        ),
                    )
                    for e in d.events
                ],
            )
            for d in fc.days
        ],
        pessimistic_low=point(fc.pessimistic_low),
        optimistic_low=point(fc.optimistic_low),
    )
