"""Credit-card statement cycles (#674, DESIGN §6, decision 136). No I/O.

A card's statement closes on a day of the month. A full-pay autopay pays
what the statement showed, less the refunds, credits and payments posted
after it closed. This module turns a card's posted history and its closing
day into the amount each upcoming payment of a card-payment schedule
projects, and says where each amount came from.

Signs: card history is in ledger sign (a charge is negative, a payment or
refund positive, a balance owed negative). Every figure this module returns
is *owed*: zero or positive, the payment's size.
"""

import calendar
import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date as date_type
from datetime import timedelta
from decimal import Decimal

from purser.core import forecasting

QUANT = Decimal("0.0001")
ZERO = Decimal(0)

PAYMENT_STATEMENT_BALANCE = "statement_balance"
PAYMENT_MINIMUM_DUE = "minimum_due"
PAYMENT_FIXED = "fixed"
PAYMENT_CURRENT_BALANCE = "current_balance"
PAYMENT_AMOUNTS: tuple[str, ...] = (
    PAYMENT_STATEMENT_BALANCE,
    PAYMENT_MINIMUM_DUE,
    PAYMENT_FIXED,
    PAYMENT_CURRENT_BALANCE,
)

LATER_STATEMENT_AVERAGE = "statement_average"
LATER_SAME_CYCLE_LAST_YEAR = "same_cycle_last_year"
LATER_FIXED = "fixed"
LATER_ESTIMATES: tuple[str, ...] = (
    LATER_STATEMENT_AVERAGE,
    LATER_SAME_CYCLE_LAST_YEAR,
    LATER_FIXED,
)

DEFAULT_LATER_WINDOW = 6
MAX_LATER_WINDOW = 24

# Where a cycle-sized amount came from: the forecast's ``basis`` vocabulary
# (``core.forecasting.AMOUNT_BASES``).
BASIS_STATEMENT_CLOSE = forecasting.BASIS_STATEMENT_CLOSE  # close passed: what remains
BASIS_CYCLE_PROJECTION = forecasting.BASIS_CYCLE_PROJECTION  # next payment, close ahead
BASIS_STATEMENT_AVERAGE = forecasting.BASIS_STATEMENT_AVERAGE
BASIS_SAME_CYCLE_LAST_YEAR = forecasting.BASIS_SAME_CYCLE_LAST_YEAR
BASIS_CURRENT_BALANCE = forecasting.EstimateBasis.COUNTERPARTY_BALANCE.value
BASIS_FIXED = forecasting.BASIS_FIXED
CYCLE_BASES: tuple[str, ...] = (
    BASIS_STATEMENT_CLOSE,
    BASIS_CYCLE_PROJECTION,
    BASIS_STATEMENT_AVERAGE,
    BASIS_SAME_CYCLE_LAST_YEAR,
)


def close_date(year: int, month: int, close_day: int) -> date_type:
    """The month's closing date: ``close_day``, or the month's last day when
    the month is shorter."""
    if not 1 <= close_day <= 31:
        raise ValueError(f"close_day must be 1..31, got {close_day}")
    return date_type(year, month, min(close_day, calendar.monthrange(year, month)[1]))


def _month_before(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def last_close_on_or_before(day: date_type, close_day: int) -> date_type:
    """The latest closing date on or before ``day``."""
    this_month = close_date(day.year, day.month, close_day)
    if this_month <= day:
        return this_month
    return close_date(*_month_before(day.year, day.month), close_day)


def closes_through(day: date_type, close_day: int, count: int) -> list[date_type]:
    """The ``count`` latest closing dates on or before ``day``, newest first."""
    out: list[date_type] = []
    cursor = last_close_on_or_before(day, close_day)
    while len(out) < count:
        out.append(cursor)
        cursor = close_date(*_month_before(cursor.year, cursor.month), close_day)
    return out


def statement_close_for(
    slot: date_type, previous_slot: date_type | None, close_day: int
) -> date_type | None:
    """The close whose statement the payment on ``slot`` pays: the latest
    close after the previous payment and on or before this one. None when no
    close falls between them (two payments in one cycle: the second has no
    statement of its own). With no previous payment, the latest close on or
    before the slot."""
    close = last_close_on_or_before(slot, close_day)
    if previous_slot is not None and close <= previous_slot:
        return None
    return close


@dataclass(frozen=True)
class CardHistory:
    """A card's posted history from ``start`` on, plus its balance before
    ``start``. ``rows`` are (posting date, amount in ledger sign, is_payment):
    a payment is a transfer in from another real account — it settles the
    card, it is not spending."""

    start: date_type
    opening: Decimal
    rows: tuple[tuple[date_type, Decimal, bool], ...]
    # The first day the card has any history at all (None: no history). A
    # close before it says nothing about the card.
    first_activity: date_type | None

    def balance_at(self, day: date_type) -> Decimal:
        """Ledger balance at the end of ``day`` (negative = owed)."""
        return self.opening + sum((amt for d, amt, _ in self.rows if d <= day), ZERO)

    def owed_at(self, day: date_type) -> Decimal:
        return max(ZERO, -self.balance_at(day))

    def credits_after(self, close: date_type, through: date_type) -> Decimal:
        """Payments, refunds and credits posted after ``close`` up to
        ``through``: every positive row in (close, through]."""
        return sum((amt for d, amt, _ in self.rows if close < d <= through and amt > 0), ZERO)

    def spending(self, after: date_type, through: date_type) -> Decimal:
        """Net spending in (after, through]: charges less refunds, payments
        left out. Never negative."""
        net = sum((amt for d, amt, pay in self.rows if after < d <= through and not pay), ZERO)
        return max(ZERO, -net)

    def covers(self, close: date_type) -> bool:
        """Whether the card has history on or before ``close``."""
        return self.first_activity is not None and self.first_activity <= close


def remaining_statement(history: CardHistory, close: date_type, today: date_type) -> Decimal:
    """What is still owed of the statement that closed on ``close``: the
    balance owed at the close less every credit posted since, never below
    zero."""
    owed = history.owed_at(close)
    return max(ZERO, owed - history.credits_after(close, today)).quantize(QUANT)


def statement_balances(
    history: CardHistory, close_day: int, today: date_type, count: int
) -> list[tuple[date_type, Decimal]]:
    """The last ``count`` statements the card has history for, newest
    first: (closing date, amount owed at the close)."""
    out: list[tuple[date_type, Decimal]] = []
    for close in closes_through(today, close_day, count):
        if not history.covers(close):
            break
        out.append((close, history.owed_at(close).quantize(QUANT)))
    return out


def statement_average(statements: Sequence[tuple[date_type, Decimal]]) -> Decimal | None:
    """The mean of the statement balances, a statement with nothing owed
    counting as zero. None without statements."""
    if not statements:
        return None
    total = sum((owed for _, owed in statements), ZERO)
    return (total / len(statements)).quantize(QUANT)


def daily_spending(history: CardHistory, close_day: int, today: date_type, cycles: int) -> Decimal:
    """Average net spending per day over the last ``cycles`` complete
    statement cycles the card has history for. Zero without one."""
    closes = closes_through(today, close_day, cycles + 1)
    spent, days = ZERO, 0
    for newer, older in itertools.pairwise(closes):
        if not history.covers(older):
            break
        spent += history.spending(older, newer)
        days += (newer - older).days
    if days == 0:
        return ZERO
    return (spent / days).quantize(QUANT)


@dataclass(frozen=True)
class CyclePlan:
    """The settings a card-payment schedule sizes its payments by."""

    close_day: int
    payment_amount: str
    later_estimate: str
    later_window: int
    # The schedule's own amount, owed (positive): the fixed payment, the
    # fixed later estimate, and the minimum-due fallback. None when unset.
    fixed_amount: Decimal | None
    # What the payment falls back to when the card has no usable history:
    # the schedule's ordinary estimate, owed. Zero when there is none.
    fallback: Decimal


@dataclass(frozen=True)
class SlotFigure:
    """One payment's projected size (owed, >= 0) and where it came from."""

    amount: Decimal
    basis: str
    close: date_type | None = None


def plan_payments(
    slots: Sequence[tuple[date_type, date_type | None]],
    *,
    plan: CyclePlan,
    history: CardHistory,
    current_owed: Decimal,
    today: date_type,
) -> dict[date_type, SlotFigure]:
    """Size each unpaid payment slot of a card-payment schedule.

    ``slots`` are (slot date, the rule's previous slot date) in date order:
    every slot still unpaid (overdue ones too) and every future slot to plan.
    ``current_owed`` is what the card owes now, pendings included.

    Precedence (recorded statements and overrides sit above this, on the
    instance): a slot whose close has passed pays what remains of that
    statement; the first slot whose close is still ahead pays a projection
    (what is owed now, less the closed statements still to be paid, plus
    the average daily spending until the close); every later slot takes the
    later estimate. ``current_balance`` pays the current balance on the
    first unpaid slot instead. The payment setting then applies: a fixed
    payment never pays more than a known statement, minimum due falls back
    to the schedule's amount (a recorded statement supplies the real one)."""
    average = statement_average(
        statement_balances(history, plan.close_day, today, plan.later_window)
    )
    rate = daily_spending(history, plan.close_day, today, plan.later_window)
    later_default = average if average is not None else plan.fallback

    def later(close: date_type | None) -> SlotFigure:
        if plan.later_estimate == LATER_FIXED:
            return SlotFigure(plan.fixed_amount or ZERO, BASIS_FIXED, close)
        if plan.later_estimate == LATER_SAME_CYCLE_LAST_YEAR and close is not None:
            year_ago = close_date(close.year - 1, close.month, plan.close_day)
            if history.covers(year_ago) and year_ago <= today:
                return SlotFigure(
                    history.owed_at(year_ago).quantize(QUANT), BASIS_SAME_CYCLE_LAST_YEAR, close
                )
        return SlotFigure(later_default.quantize(QUANT), BASIS_STATEMENT_AVERAGE, close)

    def apply_payment(figure: SlotFigure, *, known: bool) -> SlotFigure:
        if plan.payment_amount == PAYMENT_FIXED and plan.fixed_amount is not None:
            if known and figure.amount < plan.fixed_amount:
                return figure  # a fixed autopay never pays more than is owed
            return SlotFigure(plan.fixed_amount, BASIS_FIXED, figure.close)
        if plan.payment_amount == PAYMENT_MINIMUM_DUE and plan.fixed_amount is not None:
            return SlotFigure(min(plan.fixed_amount, figure.amount), BASIS_FIXED, figure.close)
        return figure

    out: dict[date_type, SlotFigure] = {}
    pending_closed = ZERO  # closed statements still to be paid, in slot order
    projected = False
    current_taken = False
    for slot, previous in slots:
        close = statement_close_for(slot, previous, plan.close_day)
        if plan.payment_amount == PAYMENT_CURRENT_BALANCE:
            if not current_taken:
                current_taken = True
                out[slot] = SlotFigure(current_owed.quantize(QUANT), BASIS_CURRENT_BALANCE, close)
            else:
                out[slot] = later(close)
            continue
        if close is None:
            out[slot] = SlotFigure(ZERO, BASIS_STATEMENT_CLOSE, None)
            continue
        if close <= today and history.covers(close):
            remaining = remaining_statement(history, close, today)
            pending_closed += remaining
            out[slot] = apply_payment(
                SlotFigure(remaining, BASIS_STATEMENT_CLOSE, close), known=True
            )
            continue
        if not projected and close > today:
            projected = True
            ahead = max(ZERO, current_owed - pending_closed)
            figure = (ahead + rate * (close - today).days).quantize(QUANT)
            out[slot] = apply_payment(
                SlotFigure(figure, BASIS_CYCLE_PROJECTION, close), known=False
            )
            continue
        out[slot] = apply_payment(later(close), known=False)
    return out


def fit_close_day(
    history: CardHistory,
    payments: Sequence[tuple[date_type, Decimal]],
    *,
    min_payments: int = 3,
    tolerance: Decimal = Decimal("1.00"),
) -> tuple[int, int, int] | None:
    """Guess a card's closing day from its full payments: the day whose
    closing balance equals the most payments (within ``tolerance``).
    ``payments`` are (posting date, amount paid, positive), oldest first.

    Returns (close_day, payments explained, payments tried), or None with
    fewer than ``min_payments`` payments the history covers or when no day
    explains a majority of them (``best_close_day`` still names the
    closest). Each payment is tried against the closes between its
    predecessor and itself, so the fit needs no grace-period constant."""
    best = best_close_day(history, payments, min_payments=min_payments, tolerance=tolerance)
    if best is None or best[1] * 2 <= best[2]:
        return None
    return best


def best_close_day(
    history: CardHistory,
    payments: Sequence[tuple[date_type, Decimal]],
    *,
    min_payments: int = 3,
    tolerance: Decimal = Decimal("1.00"),
) -> tuple[int, int, int] | None:
    """``fit_close_day`` without the majority bar: the day that explains the
    most payments however few, or None with fewer than ``min_payments``
    payments the history covers."""
    best: tuple[int, int, int] | None = None
    best_error = ZERO
    for day in range(1, 32):
        tried = explained = 0
        error = ZERO
        previous: date_type | None = None
        for paid_on, amount in payments:
            close = statement_close_for(paid_on - timedelta(days=1), previous, day)
            previous = paid_on
            if close is None or not history.covers(close):
                continue
            tried += 1
            miss = abs(history.owed_at(close) - amount)
            error += miss
            if miss <= tolerance:
                explained += 1
        if tried < min_payments:
            continue
        # More payments explained wins; among equals, the smaller total miss.
        if best is None or (explained, -error) > (best[1], -best_error):
            best, best_error = (day, explained, tried), error
    return best
