"""A trade's cash that lands after the trade counts as in transit (issue
#421, decision 104): the shares leave the holdings on the trade date, the
cash keeps its own date (a linked bank row's, a later settle_on, a broker row
that took Purser's over), and until the cash lands the position's row carries
it, so net worth is unmoved on every day by a sale at the market.

Synthetic fixtures only: made-up symbols, prices and amounts.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from purser import networth
from purser.core import holdings as core
from purser.holdings import prices, service
from purser.models import Account
from tests.conftest import today
from tests.test_holdings_cash import _broker, _epoch, _feed_row, _hold, _symbol, _sync

D0 = date(2026, 3, 2)  # a Monday
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _ev(on: date, kind: str, quantity: str, seq: int, **kw) -> core.Event:
    return core.Event(
        id=uuid.UUID(int=seq),
        occurred_on=on,
        created_at=T0 + timedelta(seconds=seq),
        kind=kind,
        quantity=Decimal(quantity),
        **kw,
    )


def _book(close: str) -> dict[str, core.PriceBook]:
    return {
        "ACME": core.PriceBook([core.PricePoint(D0 - timedelta(days=10), Decimal(close), "manual")])
    }


class TestProjectionRows:
    def test_a_partial_sale_carries_its_cash_in_the_held_row_until_it_lands(self) -> None:
        sell = _ev(D0, "sell", "20", 2, price=Decimal("50"))
        position = core.PositionFacts(
            uuid.uuid4(),
            "ACME",
            [_ev(D0 - timedelta(days=5), "set_quantity", "100", 1), sell],
            in_transit=[core.InTransit(sell.id, D0, D0 + timedelta(days=2), Decimal("1000"))],
        )
        rows = core.projection_rows(
            [position], _book("50"), lower=D0 - timedelta(days=5), upper=D0 + timedelta(days=4)
        )
        by_day = {r.as_of: r for r in rows}
        assert set(by_day) == {D0 - timedelta(days=5), D0, D0 + timedelta(days=2)}
        assert by_day[D0 - timedelta(days=5)].market_value == Decimal("5000.0000")
        assert by_day[D0].quantity == Decimal("80.000000")
        assert by_day[D0].market_value == Decimal("5000.0000")  # 4000 shares + 1000 cash
        assert by_day[D0].in_transit == Decimal("1000.0000")
        landed = by_day[D0 + timedelta(days=2)]
        assert landed.market_value == Decimal("4000.0000") and landed.in_transit is None

    def test_a_landing_after_upper_is_written_only_for_the_current_period(self) -> None:
        sell = _ev(D0, "sell", "20", 2, price=Decimal("50"))
        position = core.PositionFacts(
            uuid.uuid4(),
            "ACME",
            [_ev(D0 - timedelta(days=5), "set_quantity", "100", 1), sell],
            in_transit=[core.InTransit(sell.id, D0, D0 + timedelta(days=2), Decimal("1000"))],
        )
        lower = D0 - timedelta(days=5)
        closed = core.projection_rows([position], _book("50"), lower=lower, upper=D0)
        assert max(r.as_of for r in closed) == D0
        current = core.projection_rows(
            [position], _book("50"), lower=lower, upper=D0, settle_after=True
        )
        landed = {r.as_of: r for r in current}[D0 + timedelta(days=2)]
        assert landed.market_value == Decimal("4000.0000") and landed.in_transit is None

    def test_a_sale_of_every_share_writes_a_cash_row_then_a_tombstone(self) -> None:
        sell = _ev(D0, "sell", "10", 2, price=Decimal("50"))
        position = core.PositionFacts(
            uuid.uuid4(),
            "ACME",
            [_ev(D0 - timedelta(days=5), "set_quantity", "10", 1), sell],
            in_transit=[core.InTransit(sell.id, D0, D0 + timedelta(days=3), Decimal("500"))],
        )
        rows = core.projection_rows(
            [position], _book("50"), lower=D0 - timedelta(days=5), upper=D0 + timedelta(days=5)
        )
        by_day = {r.as_of: r for r in rows}
        carried = by_day[D0]
        assert carried.quantity == 0 and carried.market_value == Decimal("500.0000")
        assert carried.in_transit == Decimal("500.0000") and carried.price is None
        tombstone = by_day[D0 + timedelta(days=3)]
        assert tombstone.quantity == 0 and tombstone.market_value == 0
        assert tombstone.in_transit is None

    def test_a_reversed_trade_carries_nothing_and_a_funded_buy_owes_its_cost(self) -> None:
        sell = _ev(D0, "sell", "10", 2, price=Decimal("50"))
        undone = _ev(D0, "sell", "10", 3, reverses_id=sell.id)
        buy = _ev(D0 + timedelta(days=1), "buy", "4", 4, price=Decimal("50"))
        position = core.PositionFacts(
            uuid.uuid4(),
            "ACME",
            [_ev(D0 - timedelta(days=5), "set_quantity", "10", 1), sell, undone, buy],
            in_transit=[
                core.InTransit(sell.id, D0, D0 + timedelta(days=3), Decimal("500")),
                core.InTransit(buy.id, buy.occurred_on, D0 + timedelta(days=3), Decimal("-200")),
            ],
        )
        rows = core.projection_rows(
            [position], _book("50"), lower=D0 - timedelta(days=5), upper=D0 + timedelta(days=5)
        )
        by_day = {r.as_of: r for r in rows}
        assert D0 not in by_day  # the reversed sale is no history, its cash none
        assert by_day[D0 - timedelta(days=5)].market_value == Decimal("500.0000")
        bought = by_day[D0 + timedelta(days=1)]
        assert bought.quantity == Decimal("14.000000")
        assert bought.market_value == Decimal("500.0000")  # 700 of shares less 200 owed
        assert bought.in_transit == Decimal("-200.0000")
        assert by_day[D0 + timedelta(days=3)].market_value == Decimal("700.0000")


# --------------------------------------------------------------------------
# Net worth, end to end: a sale at the market moves no day.
# --------------------------------------------------------------------------


def _priced_position(session: Session) -> tuple[Account, str]:
    account = _broker(session)
    symbol = _symbol()
    _hold(session, account, symbol, "100", today() - timedelta(days=40))
    prices.record_price(
        session,
        symbol=symbol,
        price_date=today() - timedelta(days=40),
        close=Decimal("50"),
        token=None,
    )
    return account, symbol


def _trade_on(weekday: int) -> date:
    trade = today() - timedelta(days=14)
    while trade.weekday() != weekday:
        trade -= timedelta(days=1)
    return trade


def _values(session: Session, account: Account, days: list[date]) -> dict[date, Decimal]:
    out = {}
    for day in days:
        entry = {b["path"]: b for b in networth.net_worth_at(session, day)["breakdown"]}
        out[day] = entry[account.path]["value"]
    return out


def _sell(session: Session, account: Account, symbol: str, trade: date, **kw):
    position = service.open_position(session, account.id, symbol)
    assert position is not None
    return service.record_event(
        session,
        position_id=position.id,
        kind="sell",
        occurred_on=trade,
        quantity=Decimal("20"),
        price=Decimal("50"),
        token=None,
        **kw,
    )


class TestNetWorthDoesNotDip:
    @pytest.mark.parametrize(
        ("weekday", "bank_days"),
        [(0, 1), (4, 3)],  # a Monday trade, Tuesday's row; a Friday trade, Monday's row
        ids=["monday-trade", "friday-trade-monday-row"],
    )
    def test_a_sale_linked_to_the_brokers_row(
        self, db_session, weekday: int, bank_days: int
    ) -> None:
        account, symbol = _priced_position(db_session)
        trade = _trade_on(weekday)
        row = _feed_row(db_session, account, "1000.00", trade + timedelta(days=bank_days), "SOLD")
        event = _sell(db_session, account, symbol, trade, transaction_id=row.id)
        assert event.settle_on == trade + timedelta(days=bank_days)
        days = [trade + timedelta(days=n) for n in range(-2, (today() - trade).days + 1)]
        assert set(_values(db_session, account, days).values()) == {Decimal("5000.0000")}
        held = networth.latest_holdings(db_session, account.id, trade)  # before the cash
        (line,) = [h for h in held if h.symbol == symbol]
        assert line.in_transit == Decimal("1000.0000")
        assert line.market_value == Decimal("5000.0000")

    def test_purser_cash_posted_at_a_later_settlement(self, db_session) -> None:
        account, symbol = _priced_position(db_session)
        trade = _trade_on(1)
        _sell(db_session, account, symbol, trade, settle_on=trade + timedelta(days=2))
        days = [trade + timedelta(days=n) for n in range(-2, (today() - trade).days + 1)]
        assert set(_values(db_session, account, days).values()) == {Decimal("5000.0000")}

    def test_cash_settling_after_today_does_not_double_count_on_its_day(self, db_session) -> None:
        # #498 review: a sale recorded today that settles in two days. No
        # projection runs on the settlement day, so the set that ends the
        # transit must already be there, written ahead, or the posted cash and
        # the transit both count that day.
        account, symbol = _priced_position(db_session)
        settle = today() + timedelta(days=2)
        _sell(db_session, account, symbol, today(), settle_on=settle)
        days = [today() + timedelta(days=n) for n in range(-1, 5)]
        assert set(_values(db_session, account, days).values()) == {Decimal("5000.0000")}
        # ...and the set written ahead is not "the latest" before its day: a
        # read with no date is the ledger day's, still carrying the cash.
        (line,) = [
            h for h in networth.latest_holdings(db_session, account.id) if h.symbol == symbol
        ]
        assert line.in_transit == Decimal("1000.0000")
        assert line.market_value == Decimal("5000.0000")
        latest = db_session.execute(
            text(
                "SELECT as_of, market_value FROM readmodel.v1_holdings_latest"
                " WHERE account_path = :path AND symbol = :symbol"
            ),
            {"path": account.path, "symbol": symbol},
        ).one()
        assert latest.as_of == today() and latest.market_value == Decimal("5000.0000")

    def test_a_broker_copy_taking_over_moves_the_cash_but_not_net_worth(self, db_session) -> None:
        account, symbol = _priced_position(db_session)
        trade = _trade_on(3)  # a Thursday; the broker books it Monday
        _sell(db_session, account, symbol, trade)
        stats = _sync(
            db_session,
            account,
            [
                {
                    "id": "sold",
                    "posted": _epoch(trade + timedelta(days=4)),
                    "amount": "1000.00",
                    "description": "SOLD ACME",
                }
            ],
        )
        assert stats["linked_trade_cash"] == 1
        days = [trade + timedelta(days=n) for n in range(-2, (today() - trade).days + 1)]
        assert set(_values(db_session, account, days).values()) == {Decimal("5000.0000")}

    def test_reversing_a_linked_sale_ends_its_cash_in_transit(self, db_session) -> None:
        account, symbol = _priced_position(db_session)
        trade = _trade_on(0)
        row = _feed_row(db_session, account, "1000.00", trade + timedelta(days=1), "SOLD")
        event = _sell(db_session, account, symbol, trade, transaction_id=row.id)
        service.reverse_event(db_session, event_id=event.id, token=None)
        # The shares are back and the bank's row is an ordinary deposit again.
        values = _values(
            db_session, account, [trade - timedelta(days=1), trade, trade + timedelta(days=1)]
        )
        assert values == {
            trade - timedelta(days=1): Decimal("5000.0000"),
            trade: Decimal("5000.0000"),
            trade + timedelta(days=1): Decimal("6000.0000"),
        }
