"""Every brokerage account's holdings in one view (#684): the #595 summary of
each open account, with totals per currency.

A line's price is not labelled with its origin (#689): an account from
SimpleFIN is valued by the bank, any other by Market data. Only a price
entered by hand (a ``manual`` close, decision 89, including one that values a
feed row with no value, decision 92) is flagged, as ``manual_price``. A line
the bank values carries its implied price (market value / shares), computed
here in Decimal so the admin UI only formats strings (guardrail 1).
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from purser import clock
from purser.core import holdings as core
from purser.holdings import projection
from purser.holdings.summary import HoldingsSummary, SummaryLine, summarize
from purser.models import Account

MANUAL = "manual"  # security_prices.source of a close entered by hand


@dataclass(frozen=True)
class PortfolioLine:
    line: SummaryLine
    # The price is a close entered by hand (#689).
    manual_price: bool
    # The price shown: the line's own, or a feed value's implied price.
    price: Decimal | None


@dataclass(frozen=True)
class PortfolioAccount:
    account_id: uuid.UUID
    name: str
    path: str
    currency: str
    summary: HoldingsSummary
    lines: list[PortfolioLine]


@dataclass
class CurrencyTotal:
    currency: str
    market_value: Decimal = Decimal(0)
    cash: Decimal = Decimal(0)
    total: Decimal = Decimal(0)


@dataclass
class Portfolio:
    as_of: date
    accounts: list[PortfolioAccount] = field(default_factory=list)
    totals: list[CurrencyTotal] = field(default_factory=list)
    lines: int = 0
    stale_price: int = 0
    no_price: int = 0
    unpriceable: int = 0


def implied_price(line: SummaryLine) -> Decimal | None:
    """A feed line's value per share, at a price's precision; None without
    a value or shares."""
    if line.market_value is None or not line.quantity or line.quantity <= 0:
        return None
    try:
        price = (line.market_value / line.quantity).quantize(core.PRICE_QUANT)
    except ArithmeticError:
        return None
    return price if abs(price) < core.MAX_PRICE else None


def _priced(
    line: SummaryLine, books: dict[str, core.PriceBook], today: date
) -> tuple[bool, Decimal | None]:
    """Whether the line's price is a close entered by hand, and the price
    shown."""
    if line.market_value is None:
        return False, line.price
    if line.source == "feed":
        if line.price_source is not None:  # decision 92: valued from a stored close
            return line.price_source == MANUAL, line.price
        return False, line.price if line.price is not None else implied_price(line)
    if not line.quantity:  # a carrier row: only a trade's cash in transit
        return False, line.price
    book = books.get(line.symbol)
    point = book.on(today) if book is not None else None
    current = point is not None and point.price_date == line.price_as_of
    source = point.source if point is not None and current else line.price_source
    return source == MANUAL, line.price


def portfolio(session: Session) -> Portfolio:
    """Every open brokerage account's newest holdings (``summarize``), the
    largest first, with ``manual_price`` per line, totals per currency, and
    counts of stale, unpriced and unpriceable lines."""
    today = clock.ledger_today()
    accounts = list(
        session.execute(
            select(Account)
            .where(Account.type == "brokerage", Account.closed_at.is_(None))
            .order_by(Account.path)
        ).scalars()
    )
    result = Portfolio(as_of=today)
    summaries = [(account, summarize(session, account)) for account in accounts]
    symbols = {
        line.symbol
        for _account, summary in summaries
        for line in summary.lines
        if line.source == "manual"
    }
    books = projection.latest_books(session, symbols, today=today) if symbols else {}
    totals: dict[str, CurrencyTotal] = defaultdict(lambda: CurrencyTotal(""))
    for account, summary in summaries:
        lines = []
        for line in summary.lines:
            manual_price, price = _priced(line, books, today)
            lines.append(PortfolioLine(line=line, manual_price=manual_price, price=price))
            result.lines += 1
            result.stale_price += line.stale_price
            result.no_price += line.no_price
            result.unpriceable += line.unpriceable
        result.accounts.append(
            PortfolioAccount(
                account_id=account.id,
                name=account.name,
                path=str(account.path),
                currency=account.currency,
                summary=summary,
                lines=lines,
            )
        )
        total = totals[account.currency]
        total.currency = account.currency
        total.market_value += summary.market_value
        total.cash += summary.cash
        total.total += summary.total
    result.accounts.sort(key=lambda a: (-a.summary.total, a.path))
    result.totals = sorted(totals.values(), key=lambda t: t.currency)
    return result
