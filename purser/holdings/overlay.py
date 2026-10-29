"""Feed holdings valued from stored prices (issue #392, DESIGN §8, decision 92).

Some plans' SimpleFIN feeds report a held fund with no value: a plan-only
fund (a collective trust, say) that no market-data provider prices, so the
feed sends 0, or nothing (the importer stores a numeric 0 or a missing value
as NULL). Left as it is, the position adds nothing to net worth while its
share count keeps growing.

The readers value such a FEED row (``quantity > 0``, ``market_value`` 0 or
NULL) on read as ``quantity * close``, the stored close (``security_prices``:
manual, or a provider) in effect on the row's own date, by decision 89's
rule: newest date first, manual over a provider on the same date
(``core.feed_value``). The row's symbol is matched the way ``record_price``
stores one: stripped and upper-cased (``normalized``); a feed symbol that can
never be a ticker (a description the feed sent instead) is unpriceable, and
no recorded close can value it. Feed-implied closes do not apply (such a row
implies none), every close is range-checked where it is valued
(``core.PriceBook``), and a value that does not fit an amount keeps the
close with no value (``value_out_of_range``, decision 89), never unpriced.
The stored row is never rewritten, and a non-zero feed value is never
overridden.

Where the rule lives — change them together:

- Python: ``core.feed_value`` for one row, applied by ``Valuer`` — the one
  helper behind ``as_held`` (``latest_holdings``, ``unpriced_holdings``) and
  ``overlay_totals`` (the account balance series);
- SQL: ``FEED_VALUE_SQL`` here, folded into ``networth.holdings_addends``'
  aggregate, and ``readmodel.v1_holdings_latest`` (migration 0039) for
  Grafana. Parity tests pin both to the Python rule (``test_feed_overlay``).

A row the overlay applies to with no stored close is unpriced: the readers
report it with no value (``market_value`` None, counted in ``no_price``),
never a silent 0, and the daily price job emits ``holding.unpriced`` once per
account and symbol when it turns unpriced.
"""

import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Date, Text, and_, cast, func, or_, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session

from purser.core import holdings as core
from purser.models import Account, HoldingsSnapshot, SecurityPrice


def normalized(symbol: str) -> str | None:
    """The ticker a feed symbol stands for, spelled as ``record_price``
    stores it (stripped, upper case), or None when it can never be one: a
    description the feed sent in place of a ticker. Such a row is
    unpriceable."""
    try:
        return core.normalize_symbol(symbol)
    except ValueError:
        return None


def zero_valued_clause() -> Any:
    """SQL: a FEED row that holds shares with no stored value (0 or NULL)."""
    return and_(
        HoldingsSnapshot.source == "feed",
        HoldingsSnapshot.quantity > 0,
        or_(HoldingsSnapshot.market_value.is_(None), HoldingsSnapshot.market_value == 0),
    )


def zero_valued(row: HoldingsSnapshot) -> bool:
    """A FEED row that holds shares with no stored value (0 or NULL)."""
    return (
        row.source == "feed"
        and row.quantity is not None
        and row.quantity > 0
        and (row.market_value is None or row.market_value == 0)
    )


# The overlay's value of ``holdings_snapshots`` row ``h`` in SQL, for a row
# ``zero_valued_clause`` holds for: quantity x the stored close in effect on
# its as_of (normalized symbol; newest date first, manual first on a tie),
# rounded to 4 places half to even (s is that product times 10^4, exact
# numeric arithmetic), NULL without a close or when the value does not fit
# NUMERIC(20,4). The same rule as core.feed_value and migration 0039's view.
FEED_VALUE_SQL = """(
  SELECT CASE WHEN v.value < 1e16 THEN v.value END
  FROM (
    SELECT (trunc(b.s) + CASE
              WHEN b.s - trunc(b.s) > 0.5 THEN 1
              WHEN b.s - trunc(b.s) < 0.5 THEN 0
              WHEN mod(trunc(b.s), 2) = 0 THEN 0
              ELSE 1
            END) / 10000 AS value
    FROM (
      SELECT h.quantity * p.close * 10000 AS s
      FROM security_prices p
      WHERE p.symbol = upper(btrim(h.symbol))
        AND p.price_date <= h.as_of
        AND p.close > 0 AND p.close < 1e14
      ORDER BY p.price_date DESC, (p.source <> 'manual'), p.source, p.close
      LIMIT 1
    ) b
  ) v
)"""


def stored_books(
    session: Session, symbols: Iterable[str], *, upper: date
) -> dict[str, core.PriceBook]:
    """Each symbol's STORED closes dated on or before ``upper`` (manual and
    provider): no feed-implied ones, which a zero row cannot supply."""
    wanted = sorted(set(symbols))
    if not wanted:
        return {}
    points: dict[str, list[core.PricePoint]] = defaultdict(list)
    for symbol, price_date, close, source in session.execute(
        select(
            SecurityPrice.symbol,
            SecurityPrice.price_date,
            SecurityPrice.close,
            SecurityPrice.source,
        ).where(SecurityPrice.symbol.in_(wanted), SecurityPrice.price_date <= upper)
    ):
        points[symbol].append(core.PricePoint(price_date, Decimal(close), source))
    return {symbol: core.PriceBook(rows) for symbol, rows in points.items()}


@dataclass(frozen=True)
class Overlaid:
    """What the overlay makes of one row (``Valuer.value``)."""

    value: Decimal | None  # None: no value (unpriced, unpriceable or out of range)
    close: core.PricePoint | None  # the close used, when one applied
    unpriceable: bool  # the symbol can never be a ticker


class Valuer:
    """The overlay for a batch of feed rows: the stored closes of every
    symbol they name, loaded once, and ``core.feed_value`` per row. The one
    Python implementation every reader goes through (``as_held``,
    ``overlay_totals``)."""

    def __init__(self, session: Session, rows: Iterable[tuple[str, date]]) -> None:
        """``rows``: (symbol, as_of) of the rows the overlay applies to."""
        wanted = [(normalized(symbol), as_of) for symbol, as_of in rows]
        priced = [(s, d) for s, d in wanted if s is not None]
        self.books = (
            stored_books(session, {s for s, _d in priced}, upper=max(d for _s, d in priced))
            if priced
            else {}
        )

    def book(self, symbol: str) -> core.PriceBook | None:
        ticker = normalized(symbol)
        return self.books.get(ticker) if ticker is not None else None

    def value(
        self, quantity: Decimal, market_value: Decimal | None, symbol: str, as_of: date
    ) -> Overlaid:
        """A row the overlay applies to (``zero_valued``), valued."""
        ticker = normalized(symbol)
        if ticker is None:
            return Overlaid(None, None, unpriceable=True)
        value, close = core.feed_value(quantity, market_value, self.books.get(ticker), as_of)
        return Overlaid(value, close, unpriceable=False)


@dataclass(frozen=True)
class Holding:
    """One holdings row as the readers see it (decision 92): the stored row,
    or, for a feed row with no stored value, the overlay's value and the
    close it used. Never written back."""

    account_id: uuid.UUID
    as_of: date
    symbol: str
    quantity: Decimal | None
    cost_basis: Decimal | None
    market_value: Decimal | None  # None: no value (adds nothing), never a silent 0
    source: str  # feed | manual: where the row comes from
    position_id: uuid.UUID | None = None
    price: Decimal | None = None  # the close the value uses (manual rows; overlaid feed rows)
    price_as_of: date | None = None
    # A feed row valued from a stored close: that close's source, manual or
    # the provider. None otherwise.
    price_source: str | None = None
    unpriced: bool = False  # a feed row with no stored value and no stored close
    # Its symbol can never be a ticker (a description): no close values it.
    unpriceable: bool = False


def _held(rows: Sequence[HoldingsSnapshot], valuer: Valuer) -> list[Holding]:
    out: list[Holding] = []
    for row in rows:
        if not zero_valued(row):
            out.append(
                Holding(
                    account_id=row.account_id,
                    as_of=row.as_of,
                    symbol=row.symbol,
                    quantity=row.quantity,
                    cost_basis=row.cost_basis,
                    market_value=row.market_value,
                    source=row.source,
                    position_id=row.position_id,
                    price=row.price,
                    price_as_of=row.price_as_of,
                )
            )
            continue
        assert row.quantity is not None
        result = valuer.value(
            Decimal(row.quantity),
            Decimal(row.market_value) if row.market_value is not None else None,
            row.symbol,
            row.as_of,
        )
        close = result.close
        out.append(
            Holding(
                account_id=row.account_id,
                as_of=row.as_of,
                symbol=row.symbol,
                quantity=row.quantity,
                cost_basis=row.cost_basis,
                market_value=result.value,
                source=row.source,
                position_id=row.position_id,
                price=close.close if close is not None else None,
                price_as_of=close.price_date if close is not None else None,
                price_source=close.source if close is not None else None,
                unpriced=close is None,
                unpriceable=result.unpriceable,
            )
        )
    return out


def as_held(session: Session, rows: Sequence[HoldingsSnapshot]) -> list[Holding]:
    """``rows`` as the readers see them: the overlay applied to each feed
    row with no stored value, the others as stored."""
    return _held(rows, Valuer(session, [(r.symbol, r.as_of) for r in rows if zero_valued(r)]))


def overlay_totals(session: Session, rows: Iterable[Any]) -> dict[tuple[uuid.UUID, date], Decimal]:
    """What the overlay adds to each (account, set date): the summed values
    of its feed rows with no stored value ``(account_id, as_of, symbol,
    quantity)``, through the same ``Valuer`` as ``as_held``. Their stored
    value adds nothing (0 or NULL), so a reader adds this to its stored sum."""
    zero_rows = [(a, d, s, q) for a, d, s, q in rows if q is not None]
    if not zero_rows:
        return {}
    valuer = Valuer(session, [(s, d) for _a, d, s, _q in zero_rows])
    totals: dict[tuple[uuid.UUID, date], Decimal] = defaultdict(Decimal)
    for account_id, as_of, symbol, quantity in zero_rows:
        value = valuer.value(Decimal(quantity), None, symbol, as_of).value
        if value is not None:
            totals[(account_id, as_of)] += value
    return dict(totals)


def accounts_valued_from(session: Session, symbol: str, on: date) -> list[uuid.UUID]:
    """The accounts holding ``symbol`` (normalized) in a feed row with no
    stored value dated ``on`` or later: a close of ``symbol`` dated ``on``
    re-values them from that date (``record_price`` marks net worth dirty for
    them)."""
    return list(
        session.execute(
            select(HoldingsSnapshot.account_id)
            .where(
                func.upper(func.btrim(HoldingsSnapshot.symbol)) == symbol,
                HoldingsSnapshot.as_of >= on,
                zero_valued_clause(),
            )
            .distinct()
            .order_by(HoldingsSnapshot.account_id)
        ).scalars()
    )


def first_revalued(session: Session, earliest: dict[str, date]) -> date | None:
    """Of the symbols given with the earliest date a close of theirs was
    stored, the earliest such date some feed row with no stored value of the
    symbol is dated on or after: net worth heals from there. One statement
    for every symbol (one pass over the zero-valued feed rows), not one per
    symbol inside the fetch."""
    if not earliest:
        return None
    pairs = sorted(earliest.items())
    wanted = (
        select(
            func.unnest(cast([s for s, _d in pairs], ARRAY(Text))).label("symbol"),
            func.unnest(cast([d for _s, d in pairs], ARRAY(Date))).label("since"),
        )
    ).subquery("stored")
    return session.execute(
        select(func.min(wanted.c.since))
        .select_from(HoldingsSnapshot)
        .join(
            wanted,
            and_(
                func.upper(func.btrim(HoldingsSnapshot.symbol)) == wanted.c.symbol,
                HoldingsSnapshot.as_of >= wanted.c.since,
            ),
        )
        .where(zero_valued_clause())
    ).scalar_one_or_none()


def _newest_zero_rows(session: Session, today: date) -> tuple[list[tuple[Holding, str]], Valuer]:
    """Every feed row with no stored value in an open brokerage account's
    newest set (at or before ``today``), as held, with the account path; and
    the ``Valuer`` that valued them (its books hold the stored closes)."""
    newest = (
        select(
            HoldingsSnapshot.account_id.label("account_id"),
            func.max(HoldingsSnapshot.as_of).label("as_of"),
        )
        .where(HoldingsSnapshot.as_of <= today)
        .group_by(HoldingsSnapshot.account_id)
        .cte("newest_sets")
    )
    rows = list(
        session.execute(
            select(HoldingsSnapshot, Account.path)
            .join(
                newest,
                (newest.c.account_id == HoldingsSnapshot.account_id)
                & (newest.c.as_of == HoldingsSnapshot.as_of),
            )
            .join(Account, Account.id == HoldingsSnapshot.account_id)
            .where(
                Account.type == "brokerage",
                Account.closed_at.is_(None),
                zero_valued_clause(),
            )
            .order_by(Account.path, HoldingsSnapshot.symbol)
        ).all()
    )
    valuer = Valuer(session, [(row.symbol, row.as_of) for row, _path in rows])
    held = _held([row for row, _path in rows], valuer)
    return [(h, str(path)) for h, (_row, path) in zip(held, rows, strict=True)], valuer


@dataclass(frozen=True)
class Unpriced:
    """A feed position with no stored value that no stored close prices."""

    account_id: uuid.UUID
    account: str  # the account's path
    symbol: str
    quantity: Decimal
    as_of: date  # the date of the set it sits in
    # The newest stored close of the symbol on or before as_of, if any (one
    # that cannot value it): it keys the transition.
    last_close: date | None
    # The symbol can never be a ticker (a description the feed sent): no
    # recorded close values it, so never advise record_price.
    unpriceable: bool = False


def unpriced_holdings(session: Session, *, today: date) -> list[Unpriced]:
    """Every feed row with no stored value in an open brokerage account's
    newest set (at or before ``today``) that no stored close prices. The
    last close comes from the books the overlay already loaded."""
    held, valuer = _newest_zero_rows(session, today)
    out: list[Unpriced] = []
    for h, path in held:
        if not h.unpriced:
            continue
        book = valuer.book(h.symbol)
        point = book.on(h.as_of) if book is not None else None
        out.append(
            Unpriced(
                account_id=h.account_id,
                account=path,
                symbol=h.symbol,
                quantity=Decimal(h.quantity or 0),
                as_of=h.as_of,
                last_close=point.price_date if point is not None else None,
                unpriceable=h.unpriceable,
            )
        )
    return out


def overlaid_symbols(session: Session, *, today: date) -> set[str]:
    """The tickers the overlay values from a stored close in an open
    brokerage account's newest set: their close can turn stale like a manual
    position's (``price.stale``, ``/prices/status``, ``whats_new``)."""
    held, _valuer = _newest_zero_rows(session, today)
    return {
        ticker
        for h, _path in held
        if h.price is not None and (ticker := normalized(h.symbol)) is not None
    }
