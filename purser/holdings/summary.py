"""One brokerage account's holdings as a table (#595): the newest set,
each line's gain and share of the account, a cash line and totals.

Every figure is computed here in Decimal so the admin UI only formats
strings (guardrail 1). The rules are the ones MCP ``list_holdings`` already
applies per line: cash still in transit is no gain (decision 104), a feed row
with no ticker cannot be priced (decision 92), a close past
``price_stale_days`` is stale (decision 89), and a value too large to store
is flagged rather than shown as zero. Cash plus the lines' market value is
the account's reported balance (decision 50), so the table's total and the
account header always agree.
"""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from purser import appsettings, clock
from purser.core import holdings as core
from purser.models import Account, Position

_HUNDRED = Decimal(100)
_PCT = Decimal("0.01")


def _pct(part: Decimal, whole: Decimal) -> Decimal | None:
    """``part`` as a percent of ``whole`` to two places; None when ``whole``
    is not positive (a share of nothing, or of a negative total, means
    nothing)."""
    if whole <= 0:
        return None
    return (part / whole * _HUNDRED).quantize(_PCT)


@dataclass(frozen=True)
class SummaryLine:
    symbol: str
    name: str | None
    source: str
    position_id: uuid.UUID | None
    quantity: Decimal | None
    price: Decimal | None
    price_as_of: date | None
    price_source: str | None
    market_value: Decimal | None
    cost_basis: Decimal | None
    unrealized_gain: Decimal | None
    gain_pct: Decimal | None
    weight_pct: Decimal | None
    in_transit: Decimal | None
    stale_price: bool
    no_price: bool
    unpriceable: bool
    value_out_of_range: bool


@dataclass(frozen=True)
class HoldingsSummary:
    account_id: uuid.UUID
    as_of: date | None
    source: str
    closed: bool
    cash: Decimal
    cash_weight_pct: Decimal | None
    market_value: Decimal
    cost_basis: Decimal | None
    unrealized_gain: Decimal | None
    gain_pct: Decimal | None
    total: Decimal
    lines: list[SummaryLine]


def summarize(session: Session, account: Account) -> HoldingsSummary:
    """The newest holdings set of ``account`` (a brokerage account, feed or
    hand-tracked) with per-line gains and weights and account totals.

    Totals: ``market_value`` sums the lines that have a value; ``cost_basis``
    and ``unrealized_gain`` sum only the lines whose gain is known (basis and
    value both present), so a line without a basis never reads as a 100%
    gain; both are None when no line has a known gain. Weights are shares of
    ``total`` (cash plus market value), so they and the cash weight add to
    100% up to rounding."""
    from purser import networth

    today = clock.ledger_today()
    stale_days = appsettings.effective().price_stale_days
    rows = networth.latest_holdings(session, account.id)
    names: dict[uuid.UUID, str | None] = {}
    position_ids = [h.position_id for h in rows if h.position_id is not None]
    if position_ids:
        names = {
            position_id: name
            for position_id, name in session.execute(
                select(Position.id, Position.name).where(Position.id.in_(position_ids))
            )
        }

    reported = networth.reported_balances(session, [account.id])[account.id]
    cash = reported.postings
    market = sum((h.market_value for h in rows if h.market_value is not None), Decimal(0))
    total = cash + market

    lines: list[SummaryLine] = []
    basis_total = Decimal(0)
    gain_total = Decimal(0)
    any_gain = False
    for h in rows:
        out_of_range = h.market_value is None and h.price is not None and bool(h.quantity)
        # The shares' value: cash in transit sits inside market_value until
        # it lands (decision 104) and is no gain.
        shares_value = (
            h.market_value - (h.in_transit or Decimal(0)) if h.market_value is not None else None
        )
        carrier = h.source == "manual" and not h.quantity  # only cash in transit
        gain = None if carrier else core.unrealized_gain(shares_value, h.cost_basis)
        if gain is not None and h.cost_basis is not None:
            any_gain = True
            gain_total += gain
            basis_total += h.cost_basis
        lines.append(
            SummaryLine(
                symbol=h.symbol,
                name=names.get(h.position_id) if h.position_id is not None else None,
                source=h.source,
                position_id=h.position_id,
                quantity=h.quantity,
                price=h.price,
                price_as_of=h.price_as_of,
                price_source=h.price_source,
                market_value=h.market_value,
                cost_basis=h.cost_basis,
                unrealized_gain=gain,
                gain_pct=_pct(gain, h.cost_basis) if gain is not None and h.cost_basis else None,
                weight_pct=_pct(h.market_value, total) if h.market_value is not None else None,
                in_transit=h.in_transit,
                stale_price=core.stale(h.price_as_of, today, stale_days),
                no_price=h.market_value is None and not out_of_range and not h.unpriceable,
                unpriceable=h.unpriceable,
                value_out_of_range=out_of_range,
            )
        )
    lines.sort(key=lambda line: (-(line.market_value or Decimal(0)), line.symbol))
    return HoldingsSummary(
        account_id=account.id,
        as_of=rows[0].as_of if rows else None,
        # Just switched, the newest set can still be the other source's.
        source=rows[0].source if rows else account.holdings_source,
        closed=account.closed_at is not None,
        cash=cash,
        cash_weight_pct=_pct(cash, total),
        market_value=market,
        cost_basis=basis_total if any_gain else None,
        unrealized_gain=gain_total if any_gain else None,
        gain_pct=_pct(gain_total, basis_total) if any_gain else None,
        total=total,
        lines=lines,
    )
