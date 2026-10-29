"""Pure scheduling & forecasting logic (DESIGN §6). Deterministic only in v1.

No I/O — RRULE expansion, amount estimation, and the daily balance
projection with annotated events. Recurrence detection lives in
``purser.core.recurrence`` (#234).
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from dateutil.rrule import rrulestr


class EstimateBasis(StrEnum):
    LAST = "last"
    ROLLING_AVG_3 = "rolling_avg_3"
    ROLLING_AVG_12 = "rolling_avg_12"
    # The counterparty account's reported balance — a card's CURRENT balance
    # (pendings and charges since the close included, not its statement), a
    # loan's payoff — resolved by the service, which has the I/O; here it
    # estimates like ROLLING_AVG_3 so a counterparty with no balance still
    # yields a number (issue #231/#232). A card's statement balance is
    # recorded per instance instead (#351, decision 80).
    COUNTERPARTY_BALANCE = "counterparty_balance"


# Where an expected amount came from (#351, decision 80): a fixed amount, one
# of the estimate bases, a user's override (a pinned instance), or a recorded
# card statement. The vocabulary every forecast surface reports as ``basis``.
BASIS_FIXED = "fixed"
BASIS_OVERRIDE = "override"
BASIS_STATEMENT = "statement"
AMOUNT_BASES: tuple[str, ...] = (
    BASIS_FIXED,
    *(b.value for b in EstimateBasis),
    BASIS_OVERRIDE,
    BASIS_STATEMENT,
)


# RFC 5545 requires UNTIL in UTC ("...Z") when it carries a time, but our
# dtstart is a naive day-granularity datetime and dateutil refuses to mix the
# two — normalize to naive; at day resolution the semantics are identical (#221).
_UNTIL_UTC = re.compile(r"(UNTIL=\d{8}T\d{6})Z", re.IGNORECASE)


def validate_rrule(rrule: str) -> None:
    """Reject rules the ledger cannot materialize sanely (#221): FREQ is
    required and must be DAILY or coarser — a sub-daily rule expands to
    thousands of occurrences that all collapse onto duplicate dates."""
    freq = _rrule_params(rrule).get("FREQ", "").upper()
    if freq not in _FREQ_PER_YEAR:
        allowed = ", ".join(sorted(_FREQ_PER_YEAR))
        raise ValueError(f"FREQ must be one of {allowed}; got {freq or 'none'}")


def expand_rrule(
    rrule: str, *, dtstart: date_type, start: date_type, end: date_type
) -> list[date_type]:
    """Occurrences of an RFC 5545 RRULE within [start, end], anchored on
    ``dtstart`` — the schedule's persisted anchor (#231): anchor-sensitive
    rules (no BYMONTHDAY/BYDAY) count from it, never from row creation."""
    rule_text = rrule if rrule.upper().startswith("RRULE:") else f"RRULE:{rrule}"
    rule_text = _UNTIL_UTC.sub(r"\1", rule_text)
    rule = rrulestr(rule_text, dtstart=datetime(dtstart.year, dtstart.month, dtstart.day))
    occurrences = rule.between(
        datetime(start.year, start.month, start.day) - timedelta(seconds=1),
        datetime(end.year, end.month, end.day) + timedelta(days=1),
        inc=True,
    )
    return [o.date() for o in occurrences if start <= o.date() <= end]


_FREQ_PER_YEAR: dict[str, int] = {"YEARLY": 1, "MONTHLY": 12, "WEEKLY": 52, "DAILY": 365}


def _rrule_params(rrule: str) -> dict[str, str]:
    body = rrule.split(":", 1)[1] if rrule.upper().startswith("RRULE:") else rrule
    out: dict[str, str] = {}
    for part in body.split(";"):
        if "=" in part:
            key, value = part.split("=", 1)
            out[key.strip().upper()] = value.strip()
    return out


def _occurrences_per_year(rrule: str) -> Decimal:
    """Approximate occurrences/year for the cadences schedules use.

    FREQ/INTERVAL drive it; a BYDAY/BYMONTHDAY *list* multiplies (e.g.
    FREQ=WEEKLY;BYDAY=MO,WE fires twice a week). Fine-grained calendar
    nuances beyond that don't matter for a monthly-cost normalization.
    """
    params = _rrule_params(rrule)
    freq = params.get("FREQ", "").upper()
    if freq not in _FREQ_PER_YEAR:
        raise ValueError(f"unsupported FREQ for monthly normalization: {freq or rrule!r}")
    interval = int(params.get("INTERVAL") or "1")
    if interval < 1:
        raise ValueError(f"INTERVAL must be >= 1, got {interval}")
    multiplier = 1
    for by_key in ("BYDAY", "BYMONTHDAY"):
        value = params.get(by_key)
        if value:
            multiplier = max(multiplier, len(value.split(",")))
    return Decimal(_FREQ_PER_YEAR[freq] * multiplier) / Decimal(interval)


def monthly_equivalent(rrule: str, amount: Decimal) -> Decimal:
    """Normalize a per-occurrence amount to a monthly figure so annual and
    semi-annual bills carry honest weight in one total (issue #74)."""
    per_year = _occurrences_per_year(rrule)
    return (amount * per_year / Decimal(12)).quantize(Decimal("0.01"))


def cadence_label(rrule: str) -> str:
    """Human cadence: 'monthly', 'every 2 weeks', 'every 6 months', 'yearly'."""
    params = _rrule_params(rrule)
    freq = params.get("FREQ", "").upper()
    interval = int(params.get("INTERVAL") or "1")
    unit = {"YEARLY": "year", "MONTHLY": "month", "WEEKLY": "week", "DAILY": "day"}.get(freq)
    if unit is None:
        return rrule
    if interval == 1:
        return {"year": "yearly", "month": "monthly", "week": "weekly", "day": "daily"}[unit]
    return f"every {interval} {unit}s"


def estimate_amount(basis: EstimateBasis, actuals: list[Decimal], fallback: Decimal) -> Decimal:
    """Estimate the next instance amount from matched actuals (newest first).

    Variance feeding back into estimates is the point of recording it
    (DESIGN §5): actuals come from confirmed matches.
    """
    if not actuals:
        return fallback
    if basis is EstimateBasis.LAST:
        return actuals[0]
    n = 12 if basis is EstimateBasis.ROLLING_AVG_12 else 3
    window = actuals[:n]
    return (sum(window, Decimal(0)) / len(window)).quantize(Decimal("0.0001"))


# The estimate range band (#247, decision 85): min/max of history, never a
# model (§13). The newest ESTIMATE_RANGE_WINDOW settled amounts plus the
# event's own amount, from at least ESTIMATE_RANGE_MIN_ACTUALS of them.
ESTIMATE_RANGE_WINDOW = 6
ESTIMATE_RANGE_MIN_ACTUALS = 2


def estimate_range(actuals: Sequence[Decimal], amount: Decimal) -> tuple[Decimal, Decimal] | None:
    """(low, high) of an estimated amount: the min and max of the newest
    ``ESTIMATE_RANGE_WINDOW`` actuals (newest first) together with ``amount``
    itself, so the point always sits inside the band. None with fewer than
    ``ESTIMATE_RANGE_MIN_ACTUALS`` actuals or when every value is equal — no
    spread, no band."""
    window = list(actuals[:ESTIMATE_RANGE_WINDOW])
    if len(window) < ESTIMATE_RANGE_MIN_ACTUALS:
        return None
    values = [*window, amount]
    low, high = min(values), max(values)
    if low == high:
        return None
    return low, high


# ---------------------------------------------------------------------------
# Deterministic balance forecast
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExpectedItem:
    date: date_type
    amount: Decimal
    label: str
    transaction_id: UUID | None = None
    # A rule occurrence beyond the materialized horizon (#235): no row exists
    # yet, the forecaster expanded it on the fly.
    projected: bool = False
    # One of AMOUNT_BASES (#351); None when the caller states no provenance.
    basis: str | None = None
    # (low, high) in this account's sign (#247): an estimate's band, None for
    # a fixed, overridden or stated amount.
    range: tuple[Decimal, Decimal] | None = None


@dataclass(frozen=True)
class ForecastEvent:
    label: str
    amount: Decimal
    transaction_id: UUID | None
    projected: bool = False
    # Dated before the forecast start and still live: it lands on day 0
    # because it is still due, and the flag says so (#235).
    overdue: bool = False
    basis: str | None = None
    range: tuple[Decimal, Decimal] | None = None


@dataclass(frozen=True)
class ForecastDay:
    date: date_type
    balance: Decimal
    events: tuple[ForecastEvent, ...]
    # The pessimistic and optimistic running balances (#247): every ranged
    # event at its low / at its high. None when the forecast has no band.
    low: Decimal | None = None
    high: Decimal | None = None


@dataclass(frozen=True)
class ForecastPoint:
    date: date_type
    balance: Decimal


@dataclass(frozen=True)
class Forecast:
    start_balance: Decimal
    days: tuple[ForecastDay, ...]
    min_balance: Decimal | None
    first_shortfall: date_type | None
    # The minimum of the pessimistic / optimistic series (#247), None when
    # no event in the horizon is ranged. Shortfall stays on the point series.
    pessimistic_low: ForecastPoint | None = None
    optimistic_low: ForecastPoint | None = None


def _band(item: ExpectedItem) -> tuple[Decimal, Decimal]:
    """An item's (pessimistic, optimistic) amount; the point is kept inside."""
    if item.range is None:
        return item.amount, item.amount
    return min(item.range[0], item.amount), max(item.range[1], item.amount)


def forecast_balance(
    *,
    start_balance: Decimal,
    start: date_type,
    horizon_days: int,
    items: list[ExpectedItem],
    min_balance: Decimal | None = None,
) -> Forecast:
    """current balance + expanded expected instances -> daily series where
    every dip is explained by its event annotations.

    When any item in the horizon is ranged (#247), two more passes run beside
    the point series: pessimistic (every ranged item at its low — its signed
    min) and optimistic (at its high); unranged items take their amount in
    both. So low <= balance <= high on every day."""
    floor = min_balance if min_balance is not None else Decimal(0)
    end = start + timedelta(days=horizon_days)
    by_day: dict[date_type, list[ExpectedItem]] = {}
    for item in items:
        if item.date > end:
            continue
        effective = max(item.date, start)  # overdue expecteds land today
        by_day.setdefault(effective, []).append(item)
    banded = any(i.range is not None for todays in by_day.values() for i in todays)

    balance = low = high = start_balance
    days: list[ForecastDay] = []
    first_shortfall: date_type | None = None
    for offset in range(horizon_days + 1):
        day = start + timedelta(days=offset)
        todays = by_day.get(day, [])
        balance += sum((i.amount for i in todays), Decimal(0))
        bands = [_band(i) for i in todays]
        low += sum((b[0] for b in bands), Decimal(0))
        high += sum((b[1] for b in bands), Decimal(0))
        days.append(
            ForecastDay(
                date=day,
                balance=balance,
                events=tuple(
                    ForecastEvent(
                        label=i.label,
                        amount=i.amount,
                        transaction_id=i.transaction_id,
                        projected=i.projected,
                        overdue=i.date < start,
                        basis=i.basis,
                        range=i.range,
                    )
                    for i in todays
                ),
                low=low if banded else None,
                high=high if banded else None,
            )
        )
        if first_shortfall is None and balance < floor:
            first_shortfall = day
    pessimistic_low = optimistic_low = None
    if banded:
        worst = min(days, key=lambda d: d.low if d.low is not None else d.balance)
        best = min(days, key=lambda d: d.high if d.high is not None else d.balance)
        assert worst.low is not None and best.high is not None
        pessimistic_low = ForecastPoint(date=worst.date, balance=worst.low)
        optimistic_low = ForecastPoint(date=best.date, balance=best.high)
    return Forecast(
        start_balance=start_balance,
        days=tuple(days),
        min_balance=min_balance,
        first_shortfall=first_shortfall,
        pessimistic_low=pessimistic_low,
        optimistic_low=optimistic_low,
    )
