"""Schedules & forecast API."""

import uuid
from datetime import date as date_value_type
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from purser import occurrences
from purser import schedules as service
from purser import suggestions as suggestions_service
from purser.admin.auth import ReadToken, WriteToken
from purser.api.disclosure import Emits, disclose
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
    "statement_close",
    "cycle_projection",
    "statement_average",
    "same_cycle_last_year",
]
_BASIS_DESCRIPTION = (
    "where the amount comes from: fixed, an estimate basis (last, rolling_avg_3, "
    "rolling_avg_12, counterparty_balance = the card's current balance), override (a "
    "pinned instance: the amount is yours), statement (a recorded card statement, decision "
    "80), or a card payment sized from its statement cycle (decision 136): statement_close "
    "(the statement has closed; what remains of it), cycle_projection (the next statement, "
    "projected to its close), statement_average or same_cycle_last_year (later payments)"
)
PaymentAmountLiteral = Literal["statement_balance", "minimum_due", "fixed", "current_balance"]
LaterEstimateLiteral = Literal["statement_average", "same_cycle_last_year", "fixed"]
_PAYMENT_AMOUNT_DESCRIPTION = (
    "a payment to a credit card with a statement closing day (decision 136): how much is "
    "paid — statement_balance (what the statement showed, less credits since), minimum_due "
    "(a recorded statement's minimum; else the schedule's amount), fixed (the schedule's "
    "amount, never more than the statement) or current_balance (the card's balance now). "
    "Null: the estimate basis sizes every payment"
)
_LATER_ESTIMATE_DESCRIPTION = (
    "with payment_amount: how payments after the next are estimated — statement_average "
    "(the average of the last later_window statements), same_cycle_last_year (the "
    "statement a year earlier, else the average) or fixed (the schedule's amount)"
)
AutopostLiteral = Literal["auto_post", "wait_for_match", "reminder_only"]


class ScheduleOut(BaseModel):
    id: uuid.UUID
    name: str = Field(
        description="the label, derived: the memo, else the payee, else the transfer "
        "counterparty account's name; written as each instance's description"
    )
    payee: str | None = Field(default=None, description="who is paid or pays (#534)")
    memo: str | None = Field(default=None, description="free-text note (#534)")
    account_id: uuid.UUID
    counterparty_account_id: uuid.UUID | None = Field(
        default=None, description="set = transfer schedule (two-leg instances); null = flat"
    )
    category_path: str | None = Field(default=None, description="posting template (flat schedules)")
    rrule: str
    dtstart: date_value_type
    date_type: Literal["fixed", "estimated"] = "fixed"
    amount_type: str
    amount: Decimal | None
    estimate_basis: str | None
    payment_amount: PaymentAmountLiteral | None = Field(
        default=None, description=_PAYMENT_AMOUNT_DESCRIPTION
    )
    later_estimate: LaterEstimateLiteral | None = Field(
        default=None, description=_LATER_ESTIMATE_DESCRIPTION
    )
    later_window: int | None = Field(
        default=None, description="statements later_estimate averages; null = 6"
    )
    currency: str
    next_due: date_value_type | None
    autopost_policy: str
    source: str
    status: str
    ended_at: datetime | None

    @classmethod
    def from_model(cls, s: Schedule) -> "ScheduleOut":
        return cls(
            id=s.id,
            name=s.name,
            payee=s.payee.name if s.payee is not None else None,
            memo=s.memo,
            account_id=s.account_id,
            counterparty_account_id=s.counterparty_account_id,
            category_path=str(s.category_path) if s.category_path else None,
            rrule=s.rrule,
            dtstart=s.dtstart,
            date_type=s.date_type,
            amount_type=s.amount_type,
            amount=s.amount,
            estimate_basis=s.estimate_basis,
            payment_amount=s.payment_amount,
            later_estimate=s.later_estimate,
            later_window=s.later_window,
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


def _suggestion_ref(row: Suggestion, names: suggestions_service.AccountNames) -> SuggestionRef:
    return SuggestionRef(
        id=row.id,
        suggestion_id=row.id,
        source=row.source,
        headline=suggestions_service.headline(row, names),
    )


class InstanceOut(BaseModel):
    """A live (unmatched) expected instance of a schedule."""

    transaction_id: uuid.UUID
    certainty: occurrences.CertaintyOut | None = None
    date: date_value_type
    occurrence_date: date_value_type | None = Field(
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
    statement_date: date_value_type | None = Field(
        default=None, description="the statement's closing date, when recorded"
    )
    statement_source: Literal["human", "agent"] | None = Field(
        default=None, description="who recorded the statement"
    )
    minimum_due: Decimal | None = Field(
        default=None, description="the recorded statement's minimum payment, when given"
    )


class ScheduleDetail(ScheduleOut):
    instances: list[InstanceOut]
    matched_instances: list[InstanceOut] = Field(
        default_factory=list, description="Most recent 50 matched occurrences"
    )


class ScheduleCreate(BaseModel):
    name: str | None = Field(
        default=None,
        description="deprecated (#534): send payee and memo. Without a memo it is split: "
        "a transfer's memo, a flat schedule's payee when none is sent, else its memo",
    )
    account: str = Field(description="account id or path")
    rrule: str = Field(description="RFC 5545 RRULE, e.g. FREQ=MONTHLY;BYMONTHDAY=19")
    dtstart: date_value_type | None = Field(
        default=None,
        description="RRULE anchor (first occurrence for anchor-sensitive rules); default today",
    )
    date_type: Literal["fixed", "estimated"] = "fixed"
    amount_type: Literal["fixed", "estimated"] = "fixed"
    amount: Decimal | None = Field(default=None, decimal_places=4, max_digits=20)
    payee: str | None = Field(
        default=None, description="who is paid or pays; required for a flat schedule without a memo"
    )
    memo: str | None = Field(default=None, max_length=200, description="free-text note")
    category: str | None = Field(default=None, description="category path for instances")
    counterparty_account: str | None = Field(
        default=None,
        description="the other real account of a recurring transfer (exclusive with category)",
    )
    estimate_basis: EstimateBasisLiteral | None = None
    payment_amount: PaymentAmountLiteral | None = Field(
        default=None,
        description=_PAYMENT_AMOUNT_DESCRIPTION
        + ". Omitted on an estimated payment to a card with a closing day: statement_balance",
    )
    later_estimate: LaterEstimateLiteral | None = Field(
        default=None, description=_LATER_ESTIMATE_DESCRIPTION
    )
    later_window: int | None = Field(default=None, ge=1, le=24, description="default 6")
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

    name: str | None = Field(default=None, description="deprecated (#534): send payee and memo")
    rrule: str | None = None
    dtstart: date_value_type | None = None
    date_type: Literal["fixed", "estimated"] | None = None
    amount_type: Literal["fixed", "estimated"] | None = None
    amount: Decimal | None = Field(default=None, decimal_places=4, max_digits=20)
    payee: str | None = Field(default=None, description='"" clears it (a memo must remain)')
    memo: str | None = Field(default=None, max_length=200, description='"" clears it')
    category: str | None = None
    counterparty_account: str | None = None
    estimate_basis: EstimateBasisLiteral | None = None
    payment_amount: PaymentAmountLiteral | None = Field(
        default=None,
        description=_PAYMENT_AMOUNT_DESCRIPTION
        + ". Sending payment_amount, later_estimate and later_window all null turns "
        "statement-cycle sizing off",
    )
    later_estimate: LaterEstimateLiteral | None = Field(
        default=None, description=_LATER_ESTIMATE_DESCRIPTION
    )
    later_window: int | None = Field(default=None, ge=1, le=24, description="default 6")
    autopost_policy: AutopostLiteral | None = None


class ForecastRangeOut(BaseModel):
    """An estimated amount's band (#247, decision 85), in this account's
    sign: min/max of the schedule's newest settled amounts and this one."""

    low: Decimal
    high: Decimal


class ForecastPointOut(BaseModel):
    date: date_value_type
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
    certainty: occurrences.CertaintyOut | None = None
    range: ForecastRangeOut | None = Field(
        default=None,
        description="an estimated amount's band; null when fixed, overridden or stated",
    )


class ForecastDayOut(BaseModel):
    date: date_value_type
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
    first_shortfall: date_value_type | None
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
            date_mode=body.date_type,
            amount_type=body.amount_type,
            amount=body.amount,
            payee_name=body.payee,
            memo=body.memo,
            category_path=body.category,
            estimate_basis=body.estimate_basis,
            autopost_policy=body.autopost_policy,
            source="manual" if token.kind == "session" else "agent",
            token=token,
            dtstart=body.dtstart,
            counterparty_account_ref=body.counterparty_account,
            note=body.note,
            propose=body.propose,
            payment_amount=body.payment_amount,
            later_estimate=body.later_estimate,
            later_window=body.later_window,
        )
    except LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    if isinstance(schedule, Suggestion):
        return _suggestion_ref(schedule, suggestions_service.AccountNames(session))
    return ScheduleOut.from_model(schedule)


@router.get(
    "",
    dependencies=[
        disclose(
            Emits(
                payee=True,
                narrative=True,
                description=("[].name", "[].memo"),
                amount=("[].amount",),
            )
        )
    ],
)
def list_schedules(
    token: ReadToken,
    session: DbSession,
    status_in: Annotated[list[str] | None, Query(alias="status")] = None,
) -> list[ScheduleOut]:
    stmt = select(Schedule).options(selectinload(Schedule.payee)).order_by(Schedule.created_at)
    if status_in:
        stmt = stmt.where(Schedule.status.in_(status_in))
    return [ScheduleOut.from_model(s) for s in session.execute(stmt).scalars()]


def _detail(session: Session, schedule: Schedule) -> ScheduleDetail:
    base = ScheduleOut.from_model(schedule)
    return ScheduleDetail(
        **base.model_dump(),
        instances=[InstanceOut(**row) for row in service.list_instances(session, schedule)],
        matched_instances=[
            InstanceOut(**row) for row in service.list_instances(session, schedule, matched=True)
        ],
    )


@router.get(
    "/{schedule_id}",
    dependencies=[
        disclose(
            Emits(
                payee=True,
                narrative=True,
                description=(
                    "name",
                    "memo",
                    "instances[].certainty.amount.note",
                    "instances[].certainty.date.note",
                    "matched_instances[].certainty.amount.note",
                    "matched_instances[].certainty.date.note",
                ),
                amount=(
                    "amount",
                    "instances[].amount",
                    "matched_instances[].amount",
                    "instances[].certainty.amount.value",
                    "instances[].certainty.amount.evidence_value",
                    "instances[].certainty.original.amount",
                    "instances[].certainty.actual.amount",
                    "instances[].certainty.actual.variance.amount",
                    "matched_instances[].certainty.amount.value",
                    "matched_instances[].certainty.amount.evidence_value",
                    "matched_instances[].certainty.original.amount",
                    "matched_instances[].certainty.actual.amount",
                    "matched_instances[].certainty.actual.variance.amount",
                ),
                balance=(
                    "instances[].statement_balance",
                    "matched_instances[].statement_balance",
                    "instances[].minimum_due",
                    "matched_instances[].minimum_due",
                ),
            )
        )
    ],
)
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
    `payee` or `memo` relabels every live instance — pinned ones too — whose
    description or payee still equals the old one; labels you changed stay."""
    try:
        updated = service.update_schedule(
            session,
            schedule_id=schedule_id,
            token=token,
            name=body.name,
            rrule=body.rrule,
            dtstart=body.dtstart,
            date_mode=body.date_type,
            amount_type=body.amount_type,
            amount=body.amount,
            estimate_basis=body.estimate_basis,
            category_path=body.category,
            payee_name=body.payee,
            memo=body.memo,
            autopost_policy=body.autopost_policy,
            counterparty_account_ref=body.counterparty_account,
            payment_amount=body.payment_amount,
            later_estimate=body.later_estimate,
            later_window=body.later_window,
            cycle_set=bool(
                {"payment_amount", "later_estimate", "later_window"} & body.model_fields_set
            ),
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
    names = suggestions_service.AccountNames(session)
    return DetectResult(
        suggestions=[_suggestion_ref(r, names) for r in touched],
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
    statement_date: date_value_type | None = Field(
        default=None,
        description="the statement's closing date (today or earlier); checked against the "
        "card's statement_close_day",
    )
    minimum_due: Decimal | None = Field(
        default=None,
        decimal_places=4,
        max_digits=20,
        description="the statement's minimum payment: a schedule that pays the minimum takes it",
    )
    transaction_id: uuid.UUID | None = Field(
        default=None, description="the payment instance (exclusive with account + due_date)"
    )
    account: str | None = Field(default=None, description="the credit card, id or path")
    due_date: date_value_type | None = Field(
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
    close_day_hint: str | None = Field(
        default=None,
        description="set when the statement_date disagrees with the card's statement_close_day: "
        "what to set it to. Nothing is changed for you",
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
            minimum_due=body.minimum_due,
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
        **row,
        schedule_id=recorded.schedule.id,
        previous_amount=recorded.previous_amount,
        close_day_hint=recorded.close_day_hint,
    )


@forecast_router.get(
    "",
    dependencies=[
        disclose(
            Emits(
                payee=True,
                narrative=True,
                description=(
                    "days[].events[].label",
                    "days[].events[].certainty.amount.note",
                    "days[].events[].certainty.date.note",
                ),
                amount=(
                    "days[].events[].amount",
                    "days[].events[].certainty.amount.value",
                    "days[].events[].certainty.amount.evidence_value",
                    "days[].events[].certainty.original.amount",
                    "days[].events[].certainty.actual.amount",
                    "days[].events[].certainty.actual.variance.amount",
                ),
            )
        )
    ],
)
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
                        certainty=e.certainty,
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


@router.get(
    "/instances/{transaction_id}/certainty",
    dependencies=[
        disclose(
            Emits(
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
        )
    ],
)
def get_occurrence_certainty(
    transaction_id: uuid.UUID, token: ReadToken, session: DbSession
) -> occurrences.CertaintyOut:
    """Original estimate, current independent evidence and linked actual; no writes."""
    from purser import ledger

    try:
        return occurrences.CertaintyOut(
            **occurrences.describe(
                session, ledger.get_transaction(session, transaction_id), history=True
            )
        )
    except LedgerError as exc:
        raise _handle(exc) from exc


@router.patch("/instances/{transaction_id}/certainty")
def update_occurrence_certainty(
    transaction_id: uuid.UUID,
    body: occurrences.CertaintyUpdate,
    token: WriteToken,
    session: DbSession,
) -> occurrences.CertaintyOut:
    """Update only the supplied dimensions on one occurrence, never post it.
    `clear` returns a dimension's certainty to the rule's without moving its value."""
    try:
        result = occurrences.update(session, tx_id=transaction_id, change=body, token=token)
    except LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    return occurrences.CertaintyOut(**result)


@router.get(
    "/{schedule_id}/occurrences/{occurrence_date}/certainty",
    dependencies=[
        disclose(
            Emits(
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
        )
    ],
)
def get_slot_certainty(
    schedule_id: uuid.UUID,
    occurrence_date: date_value_type,
    token: ReadToken,
    session: DbSession,
) -> occurrences.CertaintyOut:
    """One rule slot's certainty, with or without an instance (#518). A
    materialized slot reads its instance (`slot.transaction_id`); a projected
    slot (today .. 365 days) reads its slot evidence over the rule's states,
    with its first retained projection when previously annotated. No writes."""
    try:
        return occurrences.CertaintyOut(
            **occurrences.describe_slot(
                session, schedule_id=schedule_id, slot=occurrence_date, history=True
            )
        )
    except LedgerError as exc:
        raise _handle(exc) from exc


@router.patch("/{schedule_id}/occurrences/{occurrence_date}/certainty")
def update_slot_certainty(
    schedule_id: uuid.UUID,
    occurrence_date: date_value_type,
    body: occurrences.CertaintyUpdate,
    token: WriteToken,
    session: DbSession,
) -> occurrences.CertaintyOut:
    """Set or clear amount/date evidence on one rule slot (#518). A projected
    slot keeps the rule's amount and date (a `value` must equal them), moves
    no cash and creates no transaction; its evidence moves onto the instance
    when the slot materializes. This address is metadata-only even after
    materialization; numeric edits use the transaction endpoint. Other slots are
    untouched. Audited `schedule.slot_certainty_update`."""
    try:
        result = occurrences.update_slot(
            session, schedule_id=schedule_id, slot=occurrence_date, change=body, token=token
        )
    except LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    return occurrences.CertaintyOut(**result)
