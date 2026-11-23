"""Current-price scenario for unvested awards; never an owned-balance overlay."""

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from purser import appsettings, networth
from purser.core import holdings as core
from purser.holdings import awards, projection
from purser.models import Account, Award


def overview(session: Session, today: date) -> dict[str, Any]:
    owned = networth.net_worth_at(session, today)
    accounts = session.scalars(
        select(Account).where(
            Account.type == "brokerage",
            Account.closed_at.is_(None),
            Account.id.in_(select(Award.account_id)),
        )
    ).all()
    units: dict[str, Decimal] = {}
    unknown = unsupported = count = 0
    next_dates: list[date] = []
    for account in accounts:
        for view in awards.award_views(session, account, today=today):
            # Unknown remaining units can have zero known scheduled units;
            # they must not disappear into a complete zero-value answer.
            if view.unvested <= 0 and view.unvested_known:
                continue
            count += 1
            unknown += not view.unvested_known
            if view.next_vest is not None:
                next_dates.append(view.next_vest.vest_date)
            if account.currency.strip() != "USD":
                unsupported += 1
                continue
            if view.unvested > 0:
                units[view.award.symbol] = units.get(view.award.symbol, Decimal(0)) + view.unvested
    books = projection.latest_books(session, set(units), today=today)
    known = Decimal(0)
    missing: list[str] = []
    stale: list[str] = []
    price_dates: list[date] = []
    for symbol, quantity in sorted(units.items()):
        book = books.get(symbol)
        close = book.on(today) if book is not None else None
        point, value = core.valued(quantity, close)
        if value is None:
            missing.append(symbol)
            continue
        known += value
        if point is not None:
            price_dates.append(point.price_date)
            if core.stale(point.price_date, today, appsettings.effective().price_stale_days):
                stale.append(symbol)
    complete = not (unknown or unsupported or missing)
    return {
        "as_of": today,
        "owned_assets": owned["assets"],
        "liabilities": owned["liabilities"],
        "owned_net_worth": owned["net_worth"],
        "unvested_value": known if complete else None,
        "known_unvested_value": known,
        "potential_assets": owned["assets"] + known if complete else None,
        "potential_net_worth": owned["net_worth"] + known if complete else None,
        "complete": complete,
        "unvested_awards": count,
        "unknown_quantity_awards": unknown,
        "unsupported_currency_awards": unsupported,
        "unpriced_symbols": missing,
        "stale_price_symbols": stale,
        "oldest_price_as_of": min(price_dates, default=None),
        "next_vest_date": min(next_dates, default=None),
    }
