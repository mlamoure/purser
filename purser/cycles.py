"""Card payments sized from the statement cycle (#674, DESIGN §6, decision 136).

The I/O side of :mod:`purser.core.cycles`: it reads a credit card's posted
history and closing day, and sizes each payment slot of a card-payment
schedule. A schedule is *cycle-sized* when it is a transfer to a credit card
that has a closing day and the schedule sets ``payment_amount``; every other
schedule keeps its estimate basis, as before.

The statement balance is the balance on the closing day counted over POSTED
rows (a statement leaves pendings out), using the register's chain heads
(decision 37): a reversed row and its reversal both drop out.
"""

import uuid
from dataclasses import dataclass
from datetime import date as date_type
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from purser import clock
from purser.core import cycles as core_cycles
from purser.core import forecasting as core_forecasting
from purser.ledger import LedgerError
from purser.models import Account, Schedule

# How far back a plan reads: a year for same-cycle-last-year, the widest
# later window, and a cycle of slack.
HISTORY_DAYS = 31 * (core_cycles.MAX_LATER_WINDOW + 2)
# A planned slot's predecessor is found within this many days before it.
_PREVIOUS_LOOKBACK_DAYS = 400
# A cycle-sized instance whose close has not passed is moved only when the
# plan differs by at least this share (or the slot's close has just passed):
# the projection drifts a little with every charge, and an audit row per card
# per night would say nothing.
REFRESH_SHARE = Decimal("0.05")
REFRESH_FLOOR = Decimal("1.00")


_HISTORY_SQL = text(
    """
WITH heads AS (
    SELECT t.id, t.date
    FROM transactions t
    WHERE t.status = 'posted'
      AND NOT EXISTS (SELECT 1 FROM transactions r WHERE r.reverses_id = t.id)
)
SELECT h.date,
       SUM(p.amount) AS amount,
       bool_or(EXISTS (
           SELECT 1 FROM postings o JOIN accounts oa ON oa.id = o.account_id
           WHERE o.transaction_id = h.id AND o.account_id <> p.account_id
             AND NOT oa.is_virtual
       )) AS is_payment,
       h.date < CAST(:start AS DATE) AS before_start
FROM postings p
JOIN heads h ON h.id = p.transaction_id
WHERE p.account_id = CAST(:card AS UUID)
GROUP BY h.id, h.date
ORDER BY h.date
"""
)


def card_history(session: Session, card: Account, *, start: date_type) -> core_cycles.CardHistory:
    """The card's posted history from ``start`` on, with its balance before
    ``start`` and the day its history begins."""
    opening = Decimal(0)
    rows: list[tuple[date_type, Decimal, bool]] = []
    first: date_type | None = None
    for day, amount, is_payment, before_start in session.execute(
        _HISTORY_SQL, {"card": str(card.id), "start": start}
    ):
        first = day if first is None else min(first, day)
        if before_start:
            opening += Decimal(amount)
        else:
            rows.append((day, Decimal(amount), bool(is_payment)))
    return core_cycles.CardHistory(
        start=start, opening=opening, rows=tuple(rows), first_activity=first
    )


def cycle_card(session: Session, schedule: Schedule) -> Account | None:
    """The credit card a cycle-sized schedule pays, or None when the schedule
    is not cycle-sized (no payment setting, not a transfer to a card, or the
    card has no closing day)."""
    if schedule.payment_amount is None or schedule.counterparty_account_id is None:
        return None
    card = session.get(Account, schedule.counterparty_account_id)
    if card is None or card.type != "credit_card" or card.statement_close_day is None:
        return None
    return card


def is_cycle_sized(session: Session, schedule: Schedule) -> bool:
    return cycle_card(session, schedule) is not None


def _owed(leg: Decimal | None) -> Decimal | None:
    """A schedule amount (the schedule account's leg, negative when it pays
    out) as the amount owed."""
    return None if leg is None else abs(Decimal(leg))


def _plan_settings(session: Session, schedule: Schedule, card: Account) -> core_cycles.CyclePlan:
    from purser import schedules as schedules_service

    fallback = schedules_service.legacy_estimate(session, schedule).amount
    assert card.statement_close_day is not None
    return core_cycles.CyclePlan(
        close_day=card.statement_close_day,
        payment_amount=schedule.payment_amount or core_cycles.PAYMENT_STATEMENT_BALANCE,
        later_estimate=schedule.later_estimate or core_cycles.LATER_STATEMENT_AVERAGE,
        later_window=schedule.later_window or core_cycles.DEFAULT_LATER_WINDOW,
        fixed_amount=_owed(schedule.amount),
        fallback=abs(fallback),
    )


@dataclass(frozen=True)
class SchedulePlan:
    """Every planned payment of one cycle-sized schedule, as the schedule
    account's leg (negative when that account pays the card), with its basis."""

    schedule_id: uuid.UUID
    card: Account
    sign: int
    figures: dict[date_type, core_cycles.SlotFigure]
    statements: tuple[tuple[date_type, Decimal], ...]

    def amount(self, slot: date_type) -> Decimal | None:
        figure = self.figures.get(slot)
        return None if figure is None else self.sign * figure.amount

    def basis(self, slot: date_type) -> str | None:
        figure = self.figures.get(slot)
        return None if figure is None else figure.basis

    def band_actuals(self) -> tuple[Decimal, ...]:
        """Past statement balances in the schedule account's sign, newest
        first: the range band of the later estimates (decision 85)."""
        return tuple(self.sign * owed for _, owed in self.statements)


def _rule_slots(schedule: Schedule, start: date_type, end: date_type) -> list[date_type]:
    return core_forecasting.expand_rrule(
        schedule.rrule, dtstart=schedule.dtstart, start=start, end=end
    )


def plan_schedule(
    session: Session,
    schedule: Schedule,
    *,
    through: date_type | None = None,
    extra_slots: tuple[date_type, ...] = (),
) -> SchedulePlan | None:
    """Plan a cycle-sized schedule's payments: every rule slot from its
    earliest live (unpaid) instance — or today — through ``through``
    (default: the full forecast horizon), plus ``extra_slots``. None when the
    schedule is not cycle-sized."""
    from purser import schedules as schedules_service

    card = cycle_card(session, schedule)
    if card is None:
        return None
    today = clock.ledger_today()
    end = through or today + timedelta(days=schedules_service.FORECAST_MAX_DAYS)
    live = [
        d
        for (d,) in session.execute(
            text(
                "SELECT occurrence_date FROM transactions WHERE schedule_id = CAST(:s AS UUID)"
                " AND status = 'expected' AND superseded_by IS NULL"
                " AND occurrence_date IS NOT NULL"
            ),
            {"s": str(schedule.id)},
        )
    ]
    first = min([today, *live, *extra_slots])
    end = max([end, *extra_slots])
    window = _rule_slots(schedule, first - timedelta(days=_PREVIOUS_LOOKBACK_DAYS), end)
    planned: list[tuple[date_type, date_type | None]] = []
    previous: date_type | None = None
    for slot in window:
        if slot >= first:
            planned.append((slot, previous))
        previous = slot
    settings = _plan_settings(session, schedule, card)
    history = card_history(session, card, start=today - timedelta(days=HISTORY_DAYS))
    from purser import networth as networth_service

    balance = networth_service.reported_balances(session, [card.id])[card.id].balance
    figures = core_cycles.plan_payments(
        planned,
        plan=settings,
        history=history,
        current_owed=max(Decimal(0), -balance),
        today=today,
    )
    statements = core_cycles.statement_balances(
        history, settings.close_day, today, settings.later_window
    )
    sign = 1 if schedule.account_id == card.id else -1
    return SchedulePlan(
        schedule_id=schedule.id,
        card=card,
        sign=sign,
        figures=figures,
        statements=tuple(statements),
    )


def should_refresh(old: Decimal, new: Decimal, *, closed: bool) -> bool:
    """Whether a live instance moves to its slot's new plan: always once the
    slot's statement has closed (that figure is the statement's), otherwise
    only past ``REFRESH_SHARE`` of the old amount (and ``REFRESH_FLOOR``)."""
    if old == new:
        return False
    if closed:
        return True
    return abs(new - old) >= max(REFRESH_FLOOR, abs(old) * REFRESH_SHARE)


# ---------------------------------------------------------------------------
# Closing-day suggestion
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CloseDaySuggestion:
    close_day: int | None
    explained: int
    tried: int
    source: str  # history | statement | none
    detail: str


def _recorded_statement_day(session: Session, card: Account) -> int | None:
    """The day of the most recent recorded statement date on a payment to
    ``card`` (decision 80): the statement itself says when it closed."""
    row = session.execute(
        text(
            """
SELECT s.statement_date FROM instance_statements s
JOIN postings p ON p.transaction_id = s.transaction_id
WHERE p.account_id = CAST(:card AS UUID) AND s.statement_date IS NOT NULL
ORDER BY s.recorded_at DESC LIMIT 1
"""
        ),
        {"card": str(card.id)},
    ).scalar_one_or_none()
    return row.day if row is not None else None


def suggest_close_day(session: Session, card: Account) -> CloseDaySuggestion:
    """Suggest a card's closing day. A recorded statement date wins: the
    statement prints it. Otherwise the day whose closing balance equals most
    of the card's full payments over the last year (``fit_close_day``);
    offered only when it explains a majority of at least three payments.
    Never written: the user (or an agent on their behalf) sets it."""
    if card.type != "credit_card":
        raise LedgerError(f"{card.path} is not a credit card; only cards have a closing day")
    stated = _recorded_statement_day(session, card)
    if stated is not None:
        return CloseDaySuggestion(
            close_day=stated,
            explained=0,
            tried=0,
            source="statement",
            detail=f"the latest recorded statement closed on day {stated}",
        )
    today = clock.ledger_today()
    history = card_history(session, card, start=today - timedelta(days=HISTORY_DAYS))
    payments = [(d, amt) for d, amt, pay in history.rows if pay and amt > 0]
    fit = core_cycles.fit_close_day(history, payments)
    if fit is None:
        closest = core_cycles.best_close_day(history, payments)
        if closest is None:
            detail = (
                "too few payments to suggest a closing day: it needs at least three "
                "payments (transfers in from another account) on record"
            )
        else:
            day, explained, tried = closest
            detail = (
                f"no closing day explains most of this card's {tried} payments; the closest, "
                f"day {day}, explains {explained}. Refunds or credits posted after the close, "
                "or payments that are not the full statement, do this: take the closing day "
                "from a statement"
            )
        return CloseDaySuggestion(
            close_day=None,
            explained=closest[1] if closest else 0,
            tried=closest[2] if closest else len(payments),
            source="none",
            detail=detail,
        )
    day, explained, tried = fit
    return CloseDaySuggestion(
        close_day=day,
        explained=explained,
        tried=tried,
        source="history",
        detail=f"the balance on day {day} equals {explained} of {tried} payments",
    )


def describe_card(session: Session, card: Account) -> dict[str, Any]:
    """A card's statement cycle for REST and MCP: its closing day, the
    suggestion, and its recent statements (owed at each close)."""
    suggestion = suggest_close_day(session, card)
    statements: list[dict[str, str]] = []
    if card.statement_close_day is not None:
        today = clock.ledger_today()
        history = card_history(session, card, start=today - timedelta(days=HISTORY_DAYS))
        statements = [
            {"close": close.isoformat(), "owed": str(owed)}
            for close, owed in core_cycles.statement_balances(
                history, card.statement_close_day, today, core_cycles.DEFAULT_LATER_WINDOW
            )
        ]
    return {
        "account": card.path,
        "statement_close_day": card.statement_close_day,
        "suggested_close_day": suggestion.close_day,
        "suggestion_source": suggestion.source,
        "suggestion_detail": suggestion.detail,
        "statements": statements,
    }
