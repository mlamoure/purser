"""Pure holdings logic: share-count timelines, price resolution, and the
snapshot projection of manually tracked positions (DESIGN §8, decisions
88/89). No I/O in this module — it is part of the mypy --strict core; the
service layer (``purser.holdings``) loads the facts and persists the rows.

Simple mode only (issue #390 phase 1a): a position's history is a sequence
of ``set_quantity`` / ``buy`` / ``sell`` events. Its cost basis is a total,
carried at average cost. Lots arrive in a later phase.

Arithmetic runs in a 60-digit decimal context: a NUMERIC(20,6) quantity
times a NUMERIC(20,6) price has 40 significant digits, more than the default
28, and the default context would round the product before it is quantized.
"""

import re
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext
from typing import NamedTuple
from uuid import UUID

SET_QUANTITY = "set_quantity"
BUY = "buy"
SELL = "sell"
KINDS = (SET_QUANTITY, BUY, SELL)

QUANTITY_QUANT = Decimal("0.000001")  # position_events.quantity, NUMERIC(20,6)
PRICE_QUANT = Decimal("0.000001")  # prices, NUMERIC(20,6)
MONEY_QUANT = Decimal("0.0001")  # basis and market value, NUMERIC(20,4)
MAX_MONEY = Decimal("1e16")  # NUMERIC(20,4) holds |x| < 10^16

SOURCE_MANUAL = "manual"
SOURCE_FEED = "feed"  # feed-implied: computed on read, never stored

_PRECISION = 60

# A ticker as Purser stores it: upper case, 1-15 characters, letters and
# digits plus '.' and '-' (share classes such as BRK.B or BF-B). The same
# pattern is a CHECK on positions.symbol and security_prices.symbol.
SYMBOL_PATTERN = r"^[A-Z0-9][A-Z0-9.-]{0,14}$"
_SYMBOL_RE = re.compile(SYMBOL_PATTERN)

# What NUMERIC(20,6) and NUMERIC(20,4) hold: |x| < 10^14 and |x| < 10^16.
_MAX_QUANTITY = Decimal("1e14")
MAX_PRICE = _MAX_QUANTITY  # a close is NUMERIC(20,6) too


def normalize_symbol(raw: str) -> str:
    """``raw`` stripped and upper-cased, or ValueError when it is not a
    ticker Purser can store."""
    symbol = raw.strip().upper()
    if not _SYMBOL_RE.match(symbol):
        raise ValueError(
            f"symbol {raw!r} is not a ticker: 1-15 letters, digits, '.' or '-', "
            "starting with a letter or digit"
        )
    return symbol


def _fits(value: Decimal, quant: Decimal, limit: Decimal, field: str) -> Decimal:
    try:
        if not value.is_finite() or abs(value) >= limit:
            raise ValueError(f"{field} is out of range")
        if value != value.quantize(quant):
            places = -quant.as_tuple().exponent  # type: ignore[operator]
            raise ValueError(f"{field} has more than {places} decimal places")
    except ArithmeticError as exc:  # InvalidOperation, Overflow (a huge exponent)
        raise ValueError(f"{field} is out of range") from exc
    return value


def check_quantity(value: Decimal, field: str = "quantity") -> Decimal:
    """A share count NUMERIC(20,6) holds exactly, and not negative."""
    _fits(value, QUANTITY_QUANT, _MAX_QUANTITY, field)
    if value < 0:
        raise ValueError(f"{field} must not be negative")
    return value


def check_price(value: Decimal, field: str = "price") -> Decimal:
    """A per-share price NUMERIC(20,6) holds exactly, above zero."""
    _fits(value, PRICE_QUANT, _MAX_QUANTITY, field)
    if value <= 0:
        raise ValueError(f"{field} must be greater than zero")
    return value


def check_money(value: Decimal, field: str) -> Decimal:
    """An amount NUMERIC(20,4) holds exactly, and not negative."""
    _fits(value, MONEY_QUANT, MAX_MONEY, field)
    if value < 0:
        raise ValueError(f"{field} must not be negative")
    return value


def source_rank(source: str) -> int:
    """Decision 89's tie-break between closes of ONE date: a manual price
    beats a provider's close, which beats a feed-implied price. It only
    breaks ties — resolution is newest date first (``PriceBook.on``)."""
    if source == SOURCE_MANUAL:
        return 0
    if source == SOURCE_FEED:
        return 2
    return 1


def money(value: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        return value.quantize(MONEY_QUANT)


@dataclass(frozen=True)
class Event:
    """One ``position_events`` row as the timeline reads it."""

    id: UUID
    occurred_on: date
    created_at: datetime
    kind: str
    quantity: Decimal
    price: Decimal | None = None
    fees: Decimal | None = None
    cost_basis: Decimal | None = None
    reverses_id: UUID | None = None
    # A correction of a reversed event (#401): it takes that event's place
    # in its day instead of going after the day's other events.
    replaces_id: UUID | None = None


@dataclass(frozen=True)
class Point:
    """The position after one effective event: its share count and total
    cost basis (None when unknown)."""

    on: date
    quantity: Decimal
    basis: Decimal | None


class NegativeQuantity(ValueError):
    """An event history that would hold fewer than zero shares at some
    point. ``on`` names the first such date."""

    def __init__(self, on: date, quantity: Decimal) -> None:
        super().__init__(f"the position would hold {quantity} shares on {on.isoformat()}")
        self.on = on
        self.quantity = quantity


class OutOfRange(ValueError):
    """A share count, cost basis or market value the ledger's columns cannot
    hold (NUMERIC(20,6) / NUMERIC(20,4)): each event's own fields are
    checked on entry, and so is what they add up to."""


class MarketValueOutOfRange(OutOfRange):
    """A quantity times its price does not fit NUMERIC(20,4)."""


def effective_events(events: Iterable[Event]) -> list[Event]:
    """The events that count: neither a reversal nor reversed, in the total
    order ``(occurred_on, slot, created_at, id)`` — the same idea as the
    ledger's register order, with the id as the last tiebreak. ``slot`` is
    ``created_at``, except that a replacement (``replaces_id``) takes the
    slot of the event it replaces, through a chain of replacements (#401)."""
    every = list(events)
    by_id = {e.id: e for e in every}
    reversed_ids = {e.reverses_id for e in every if e.reverses_id is not None}
    live = [e for e in every if e.reverses_id is None and e.id not in reversed_ids]

    def slot(event: Event) -> datetime:
        seen = {event.id}
        while event.replaces_id is not None and event.replaces_id in by_id:
            event = by_id[event.replaces_id]
            if event.id in seen:  # a cycle cannot be written; never loop on one
                break
            seen.add(event.id)
        return event.created_at

    live.sort(key=lambda e: (e.occurred_on, slot(e), e.created_at, e.id))
    return live


def quantity_timeline(events: Iterable[Event]) -> list[Point]:
    """The position after each effective event, in order.

    - ``set_quantity`` sets the count. Its ``cost_basis``, when given, becomes
      the basis. Without one, a re-count of the count held just before keeps
      the running basis (counting again is not a purchase), a changed count
      makes the basis unknown, and a count of zero has a basis of zero.
    - ``buy`` adds to the count, and ``quantity * price + fees`` to the basis
      when the price is known; otherwise the basis becomes unknown.
    - ``sell`` removes shares at average cost: the basis drops by
      ``basis * sold / held_before``, rounded to 4 places, so the removed
      amounts and the remaining basis always sum to the basis before.

    A position that holds nothing has a basis of zero. Raises
    ``NegativeQuantity`` at the first point below zero: a backdated sell or
    ``set_quantity`` can push a LATER point negative, so every point counts.
    """
    quantity = Decimal(0)
    basis: Decimal | None = Decimal(0)
    points: list[Point] = []
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        for event in effective_events(events):
            if event.kind == SET_QUANTITY:
                if event.cost_basis is not None:
                    basis = event.cost_basis
                elif event.quantity != quantity:
                    basis = None  # a new count of unknown cost
                quantity = event.quantity
            elif event.kind == BUY:
                quantity += event.quantity
                if basis is not None and event.price is not None:
                    increment = event.quantity * event.price + (event.fees or Decimal(0))
                    basis = money(basis + increment)
                else:
                    basis = None
            elif event.kind == SELL:
                held = quantity
                quantity -= event.quantity
                if quantity < 0:
                    raise NegativeQuantity(event.occurred_on, quantity)
                if basis is not None and held > 0:
                    basis -= money(basis * event.quantity / held)
            else:
                raise ValueError(f"unknown position event kind {event.kind!r}")
            if quantity < 0:
                raise NegativeQuantity(event.occurred_on, quantity)
            if quantity >= _MAX_QUANTITY:
                raise OutOfRange(
                    f"the position would hold {quantity} shares on "
                    f"{event.occurred_on.isoformat()}: out of range for a share count"
                )
            if quantity == 0:
                basis = Decimal(0)
            if basis is not None and abs(basis) >= MAX_MONEY:
                raise OutOfRange(
                    f"its cost basis would reach {basis} on {event.occurred_on.isoformat()}: "
                    "out of range for an amount"
                )
            points.append(Point(event.occurred_on, quantity, basis))
    return points


class Timeline:
    """A position's points, searchable by date."""

    def __init__(self, points: Sequence[Point]) -> None:
        self.points = list(points)
        self._dates = [p.on for p in self.points]

    def state_on(self, on: date) -> Point | None:
        """The position at the end of ``on``: its last point dated on or
        before it, or None before its first event."""
        i = bisect_right(self._dates, on)
        return self.points[i - 1] if i else None

    def quantity_on(self, on: date) -> Decimal:
        state = self.state_on(on)
        return state.quantity if state is not None else Decimal(0)


@dataclass(frozen=True)
class PricePoint:
    price_date: date
    close: Decimal
    source: str


# A manual or provider close at most this many days older than a newer
# feed-implied close still wins over it (#406, decision 94).
FEED_DEFER_DAYS = 7


class PriceBook:
    """One symbol's closes, resolved by decision 89: the NEWEST date at or
    before the day asked wins, and the source order (manual, then a provider,
    then feed-implied) only breaks a tie between closes of the same date. So
    one old manual price can never pin a symbol once newer closes arrive.

    Feed-implied closes (decision 94, #406) are dated by the fetch day, not
    the session they price, so they rank lower: one wins over a manual or
    provider close only when that close is more than ``FEED_DEFER_DAYS``
    older, and an unchanged feed close keeps the day it first appeared (a
    weekend's repeat of Friday's value is Friday's; a frozen bank value ages
    and turns stale)."""

    def __init__(self, points: Iterable[PricePoint]) -> None:
        best: dict[date, PricePoint] = {}
        last_feed: Decimal | None = None
        ordered = sorted(points, key=lambda p: p.price_date)
        for point in ordered:
            if not 0 < point.close < MAX_PRICE:
                # A close enters valuation only if it fits a price: a
                # feed-implied one (dust shares, a large value) can be absurd.
                continue
            if point.source == SOURCE_FEED:
                if point.close == last_feed:
                    continue  # unchanged since the day it first appeared
                last_feed = point.close
            current = best.get(point.price_date)
            if current is None or _price_key(point) < _price_key(current):
                best[point.price_date] = point
        self._points = sorted(best.values(), key=lambda p: p.price_date)
        self._dates = [p.price_date for p in self._points]
        self._real = [p for p in self._points if p.source != SOURCE_FEED]
        self._real_dates = [p.price_date for p in self._real]

    def on(self, day: date) -> PricePoint | None:
        i = bisect_right(self._dates, day)
        if not i:
            return None
        point = self._points[i - 1]
        if point.source == SOURCE_FEED:
            j = bisect_right(self._real_dates, day)
            if j:
                real = self._real[j - 1]
                if (point.price_date - real.price_date).days <= FEED_DEFER_DAYS:
                    return real
        return point

    def dates_between(self, lower: date, upper: date) -> list[date]:
        start = bisect_left(self._dates, lower)
        end = bisect_right(self._dates, upper)
        return self._dates[start:end]


def _price_key(point: PricePoint) -> tuple[int, str, Decimal]:
    # The close is the last key only so a same-date, same-rank tie is
    # deterministic; the caller feeds at most one close per (date, source).
    return (source_rank(point.source), point.source, point.close)


def price_on(points: Iterable[PricePoint], day: date) -> PricePoint | None:
    """The close in effect on ``day`` (decision 89). See ``PriceBook``."""
    return PriceBook(points).on(day)


@dataclass(frozen=True)
class PositionFacts:
    id: UUID
    symbol: str
    events: Sequence[Event]


@dataclass(frozen=True)
class Row:
    """One manual ``holdings_snapshots`` row."""

    as_of: date
    position_id: UUID
    symbol: str
    quantity: Decimal
    cost_basis: Decimal | None
    market_value: Decimal | None
    price: Decimal | None
    price_as_of: date | None


def first_event_date(positions: Iterable[PositionFacts]) -> date | None:
    dates = [e.occurred_on for p in positions for e in effective_events(p.events)]
    return min(dates) if dates else None


def market_value(quantity: Decimal, close: Decimal) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        value = money(quantity * close)
    if abs(value) >= MAX_MONEY:
        raise MarketValueOutOfRange(f"{quantity} shares at {close} is out of range")
    return value


class Valued(NamedTuple):
    """The close a holding uses and its market value (``valued``)."""

    point: PricePoint | None
    value: Decimal | None

    @property
    def out_of_range(self) -> bool:
        """A close, but a value that does not fit an amount: the share count
        is the likely error, and no close can fix it (decision 89)."""
        return self.point is not None and self.value is None


def valued(quantity: Decimal, close: PricePoint | None) -> Valued:
    """The close and the market value a holding of ``quantity`` gets: none
    without a close (no price). When the value at that close does not fit an
    amount (``MarketValueOutOfRange``) the close stays and the value is
    None: ``out_of_range``, reported as ``value_out_of_range`` rather than
    "no price", never a failure of the whole account (decision 89)."""
    if close is None:
        return Valued(None, None)
    try:
        return Valued(close, market_value(quantity, close.close))
    except MarketValueOutOfRange:
        return Valued(close, None)


def feed_value(
    quantity: Decimal | None,
    market_value: Decimal | None,
    book: PriceBook | None,
    as_of: date,
) -> tuple[Decimal | None, PricePoint | None]:
    """What a FEED holdings row is worth on read (decision 92): its stored
    market value, unless it holds shares (``quantity > 0``) with no stored
    value — 0, as a feed sends for a plan-only fund no provider prices, or
    NULL, as the importer stores a numeric 0 or a missing value. That row
    is worth ``quantity * close``, the close in
    effect on its ``as_of`` in ``book`` (stored closes only: a zero row
    implies no price), quantized like every market value
    (``market_value``); with no close it has no price (None, None), never
    a silent 0, and a value that does not fit an amount keeps the close with
    no value (None, the close: ``value_out_of_range``, as ``valued``).
    Returns (the value, the close used: None unless a close applied)."""
    if quantity is None or quantity <= 0 or (market_value is not None and market_value != 0):
        return market_value, None
    close, value = valued(quantity, book.on(as_of) if book is not None else None)
    return value, close


def projection_rows(
    positions: Sequence[PositionFacts],
    prices: Mapping[str, PriceBook],
    *,
    lower: date,
    upper: date,
) -> list[Row]:
    """The manual snapshot rows of one account, for every date in
    ``[lower, upper]`` on which something changes (decision 88).

    A row set is written on each event date and on each price date of a
    symbol some position holds that day, plus ``lower`` itself. Readers take
    "the newest set at or before d" per account, so:

    - every set carries a row for EVERY position held that day (a set that
      listed only the position that changed would drop the others);
    - the day a position is sold out gets a zero row (a tombstone): without
      it the previous set would stay newest and bring the shares back, both
      in net worth and in Grafana's per-symbol latest view;
    - the set at ``lower`` also carries a zero row for every symbol whose
      history began before it and holds nothing then, so it always replaces
      whatever set came before;
    - a date carries at most one row per symbol: the position holding shares,
      or, when none does, one zero row (the position that just sold out, else
      the first in id order), even when a closed position and a later one
      share the symbol.

    A held position with no price gets ``market_value`` None: it adds
    nothing, and the read surfaces report it as unpriced rather than as 0.
    One whose value at that close does not fit an amount gets ``market_value``
    None too, but keeps its ``price`` and ``price_as_of`` (reported as
    ``value_out_of_range``, decision 89); the account's other rows are
    untouched. Pure in
    (positions, prices, lower, upper): the same facts always give the same
    rows, ordered by (as_of, position_id). Raises ``NegativeQuantity`` and
    ``OutOfRange`` (a share count or basis beyond its column)."""
    if upper < lower:
        return []
    ordered = sorted(positions, key=lambda p: p.id)
    timelines = {p.id: Timeline(quantity_timeline(p.events)) for p in ordered}

    change: set[date] = {lower}
    for position in ordered:
        timeline = timelines[position.id]
        change.update(point.on for point in timeline.points if lower <= point.on <= upper)
        book = prices.get(position.symbol)
        if book is not None:
            change.update(
                day for day in book.dates_between(lower, upper) if timeline.quantity_on(day) > 0
            )

    rows: list[Row] = []
    held_before = dict.fromkeys(timelines, False)
    symbols = sorted({p.symbol for p in ordered})
    for day in sorted(change):
        for symbol in symbols:
            # At most one row per (date, symbol): positions in one symbol never
            # hold shares on the same day (the overlap rule), so the one that
            # holds is written; with none holding, a single zero row ends it.
            held: list[Row] = []
            zero: list[tuple[bool, PositionFacts]] = []
            for position in ordered:
                if position.symbol != symbol:
                    continue
                state = timelines[position.id].state_on(day)
                quantity = state.quantity if state is not None else Decimal(0)
                if quantity > 0:
                    assert state is not None
                    book = prices.get(position.symbol)
                    close, value = valued(quantity, book.on(day) if book is not None else None)
                    held.append(
                        Row(
                            as_of=day,
                            position_id=position.id,
                            symbol=position.symbol,
                            quantity=quantity.quantize(QUANTITY_QUANT),
                            cost_basis=state.basis,
                            market_value=value,
                            price=close.close if close is not None else None,
                            price_as_of=close.price_date if close is not None else None,
                        )
                    )
                elif held_before[position.id] or (day == lower and state is not None):
                    zero.append((held_before[position.id], position))
                held_before[position.id] = quantity > 0
            if held:
                rows.extend(held)
            elif zero:
                # The position that just sold out, else the first in id order.
                _, ended = next(((h, p) for h, p in zero if h), zero[0])
                rows.append(
                    Row(
                        as_of=day,
                        position_id=ended.id,
                        symbol=symbol,
                        quantity=Decimal(0).quantize(QUANTITY_QUANT),
                        cost_basis=Decimal(0).quantize(MONEY_QUANT),
                        market_value=Decimal(0).quantize(MONEY_QUANT),
                        price=None,
                        price_as_of=None,
                    )
                )
    rows.sort(key=lambda r: (r.as_of, r.position_id))
    return rows


def unrealized_gain(market: Decimal | None, basis: Decimal | None) -> Decimal | None:
    """Market value less cost basis, when both are known."""
    if market is None or basis is None:
        return None
    return money(market - basis)


def stale(price_as_of: date | None, today: date, stale_days: int) -> bool:
    """A price is stale when its date is more than ``stale_days`` before
    ``today`` (decision 89). No price at all is not "stale": it is unpriced,
    which the read surfaces report on their own."""
    return price_as_of is not None and (today - price_as_of).days > stale_days


# The close in effect on a date may be dated up to a few days before it (a
# weekend, a holiday): with no stored close on or before the first date a
# symbol needs, the first ask starts this much earlier.
CLOSE_LOOKBACK = timedelta(days=7)
# An empty answer, or a not-found, is final only for dates this old: a
# provider publishes the latest closes a day or two late.
ANSWER_SETTLES = timedelta(days=3)


def settled(end: date, today: date) -> bool:
    """Whether a provider's "nothing" for a range ending on ``end`` is final:
    the range ends at least ``ANSWER_SETTLES`` before ``today``."""
    return end <= today - ANSWER_SETTLES


def held_span(points: Sequence[Point], upper: date) -> tuple[date, date] | None:
    """The first and the last day a position holds shares at the end of the
    day, at or before ``upper``: the last is ``upper`` while it still holds
    them there, else the day before the date that left it at zero. None when
    it holds nothing on any day by then. A sold-out stretch between holdings
    counts as held: the span is one range."""
    end_of_day: dict[date, Decimal] = {}
    for point in points:
        end_of_day[point.on] = point.quantity  # the day's last point wins
    first: date | None = None
    last: date | None = None
    holding = False
    for day in sorted(end_of_day):
        if day > upper:
            break
        if end_of_day[day] > 0:
            first = first if first is not None else day
            holding = True
        elif holding:
            last = day - timedelta(days=1)
            holding = False
    if first is None:
        return None
    if holding:
        last = upper
    assert last is not None
    return first, last


Range = tuple[date, date]  # inclusive at both ends


def _merged(ranges: Iterable[Range]) -> list[Range]:
    """Sorted, disjoint ranges: overlapping or adjacent ones merged."""
    out: list[Range] = []
    for first, last in sorted(r for r in ranges if r[0] <= r[1]):
        if out and first <= out[-1][1] + timedelta(days=1):
            out[-1] = (out[-1][0], max(out[-1][1], last))
        else:
            out.append((first, last))
    return out


def needed_ranges(spans: Iterable[Range]) -> list[Range]:
    """The dates one symbol needs closes for (decision 89): each position's
    held span (``held_span``), from ``CLOSE_LOOKBACK`` before its first day
    held (the close in effect on a weekend or holiday first day is dated
    before it; clamped at ``date.min``), merged."""
    out: list[Range] = []
    for first, last in spans:
        try:
            start = first - CLOSE_LOOKBACK
        except OverflowError:
            start = date.min
        out.append((start, last))
    return _merged(out)


def occupied_span(points: Sequence[Point], upper: date) -> tuple[date, date] | None:
    """The days a position occupies in its symbol on its account: its
    ``held_span``, plus the day it sold out when it did (that day's set
    still carries its zero row). Two positions of one symbol on one account
    never occupy the same day (decision 88): the same-day re-buy is refused
    on purpose. None when it holds nothing on any day."""
    span = held_span(points, upper)
    if span is None:
        return None
    first, last = span
    return (first, last + timedelta(days=1)) if last < upper else span


def fetch_windows(needed: Iterable[Range], answered: Iterable[Range]) -> list[Range]:
    """The date ranges to ask a price provider for, for one symbol
    (decision 89): the needed ranges (``needed_ranges``) less the ranges the
    provider has answered for good, newest first.

    Coverage is the set of answered ranges itself, never the span between
    the oldest and the newest stored close: an island of old closes (a
    reversed history, an account switched back to the feed) covers its own
    dates and nothing around it, so a gap next to it is still asked for.

    Newest first: the newest window holds the closes the newest sets need,
    and an older one the provider refuses must never starve it."""
    done = _merged(answered)
    windows: list[Range] = []
    for first, last in _merged(needed):
        cursor: date | None = first  # the first date not yet accounted for
        for a_first, a_last in done:
            if cursor is None or a_first > last:
                break
            if a_last < cursor:
                continue
            if a_first > cursor:
                windows.append((cursor, a_first - timedelta(days=1)))
            cursor = a_last + timedelta(days=1) if a_last < last else None
        if cursor is not None:
            windows.append((cursor, last))
    return sorted(windows, reverse=True)


def answered_through(start: date, end: date, newest_close: date | None, today: date) -> date | None:
    """How far an answer to the window [start, end] is final (decision 89):
    the whole window once it is settled (``settled``); else up to the newest
    close it returned or the settled boundary, whichever is later, never past
    ``end``. None when no part of it is final yet."""
    if settled(end, today):
        return end
    bounds = [today - ANSWER_SETTLES] + ([newest_close] if newest_close is not None else [])
    through = min(max(bounds), end)
    return through if through >= start else None


__all__ = [
    "ANSWER_SETTLES",
    "BUY",
    "CLOSE_LOOKBACK",
    "FEED_DEFER_DAYS",
    "KINDS",
    "MAX_MONEY",
    "MAX_PRICE",
    "MONEY_QUANT",
    "PRICE_QUANT",
    "QUANTITY_QUANT",
    "SELL",
    "SET_QUANTITY",
    "SOURCE_FEED",
    "SOURCE_MANUAL",
    "SYMBOL_PATTERN",
    "Event",
    "MarketValueOutOfRange",
    "NegativeQuantity",
    "OutOfRange",
    "Point",
    "PositionFacts",
    "PriceBook",
    "PricePoint",
    "Row",
    "Timeline",
    "Valued",
    "answered_through",
    "check_money",
    "check_price",
    "check_quantity",
    "effective_events",
    "feed_value",
    "fetch_windows",
    "first_event_date",
    "held_span",
    "market_value",
    "money",
    "needed_ranges",
    "normalize_symbol",
    "occupied_span",
    "price_on",
    "projection_rows",
    "quantity_timeline",
    "settled",
    "source_rank",
    "stale",
    "unrealized_gain",
    "valued",
]
