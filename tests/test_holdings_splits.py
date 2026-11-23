"""Stock splits of hand-tracked positions (issue #624, decision 127):
forward, reverse and cash in lieu, for simple and lot-tracked positions.

Properties (hypothesis):
- a split multiplies the share count by its ratio (six places, half to
  even) and keeps the total basis;
- the lots split together: each keeps its remaining basis, its basis per
  share is divided by the ratio, and their counts sum to the split total, so
  a fully allocated position stays fully allocated;
- value is continuous across the effective date: a close from before the
  split is divided by the ratio, in the full and the bounded projection;
- on real Postgres, reversing a split restores the shares, the lots, the
  cash and every snapshot row exactly, and the rows its writes leave are the
  rows a full regeneration writes.

Fixtures are synthetic: generated symbols, made-up prices, no float.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session

from purser import db, ledger
from purser.core import holdings as core
from purser.core import lots as core_lots
from purser.holdings import lots as lot_service
from purser.holdings import projection, service
from purser.ledger import LedgerError
from purser.ledger.reporting import account_balances
from purser.models import Account, HoldingsSnapshot, PositionEvent, SecurityPrice
from tests.conftest import today
from tests.test_mcp import LiveServer, _mcp_call

BASE = date(2026, 3, 2)

_ratios = st.tuples(st.integers(min_value=1, max_value=40), st.integers(min_value=1, max_value=40))
_quantities = st.decimals(min_value=Decimal("0.000001"), max_value=Decimal("100000"), places=6)
_money = st.decimals(min_value=Decimal("0"), max_value=Decimal("100000"), places=4)


def _symbol() -> str:
    return f"S{uuid.uuid4().hex[:8].upper()}"


def _exact(quantity: Decimal, numerator: int, denominator: int) -> Fraction:
    return Fraction(quantity) * numerator / denominator


# ---------------------------------------------------------------------------
# Pure
# ---------------------------------------------------------------------------


@settings(max_examples=300, deadline=None)
@given(_quantities, _ratios, st.none() | _money)
def test_a_split_multiplies_the_count_and_keeps_the_basis(
    quantity: Decimal, ratio: tuple[int, int], basis: Decimal | None
) -> None:
    numerator, denominator = ratio
    after = core.split_quantity(quantity, numerator, denominator)
    assert after == after.quantize(core.QUANTITY_QUANT)
    assert abs(Fraction(after) - _exact(quantity, numerator, denominator)) <= Fraction(1, 2_000_000)
    created = datetime(2026, 1, 1, tzinfo=UTC)
    events = [
        core.Event(uuid.UUID(int=1), BASE, created, core.SET_QUANTITY, quantity, cost_basis=basis),
        core.Event(
            uuid.UUID(int=2),
            BASE + timedelta(days=5),
            created + timedelta(seconds=1),
            core.SPLIT,
            Decimal(0),
            split=ratio,
        ),
    ]
    timeline = core.Timeline(core.quantity_timeline(events))
    state = timeline.state_on(BASE + timedelta(days=5))
    assert state is not None and state.quantity == after
    assert state.basis == (basis if after > 0 else Decimal(0))
    assert timeline.quantity_on(BASE + timedelta(days=4)) == quantity


def test_a_split_takes_effect_before_the_days_other_events() -> None:
    """A sale recorded before the split on its effective date is still in
    post-split shares: the split takes effect at the start of the day."""
    created = datetime(2026, 1, 1, tzinfo=UTC)
    events = [
        core.Event(uuid.UUID(int=1), BASE, created, core.SET_QUANTITY, Decimal("10")),
        core.Event(
            uuid.UUID(int=2),
            BASE + timedelta(days=1),
            created + timedelta(seconds=1),
            core.SELL,
            Decimal("15"),
        ),
        core.Event(
            uuid.UUID(int=3),
            BASE + timedelta(days=1),
            created + timedelta(seconds=2),
            core.SPLIT,
            Decimal(0),
            split=(2, 1),
        ),
    ]
    assert core.Timeline(core.quantity_timeline(events)).quantity_on(
        BASE + timedelta(days=1)
    ) == Decimal("5.000000")


@st.composite
def _lot_books(draw: st.DrawFn) -> tuple[list[core_lots.LotFact], list[core_lots.Disposal]]:
    lots = []
    disposals = []
    for index in range(draw(st.integers(min_value=1, max_value=6))):
        quantity = draw(st.decimals(min_value=Decimal("1"), max_value=Decimal("500"), places=6))
        known = draw(st.booleans())
        per_share = draw(st.decimals(min_value=Decimal("0.5"), max_value=Decimal("900"), places=6))
        lot = core_lots.LotFact(
            id=uuid.UUID(int=index + 1),
            acquired_on=BASE - timedelta(days=400 - index),
            effective_on=BASE + timedelta(days=draw(st.integers(min_value=0, max_value=5))),
            quantity=quantity,
            cost_basis=core.money(quantity * per_share) if known else None,
            basis_per_share=per_share if known else None,
            ordinal=1,
            adopted=False,
        )
        lots.append(lot)
        if draw(st.booleans()):
            taken = min(
                quantity,
                draw(st.decimals(min_value=Decimal("0.5"), max_value=Decimal("400"), places=6)),
            )
            disposals.append(
                core_lots.Disposal(
                    event_id=uuid.UUID(int=1000 + index),
                    lot_id=lot.id,
                    on=BASE + timedelta(days=7),
                    quantity=taken,
                    cost_basis=core.money(lot.cost_basis * taken / quantity)
                    if lot.cost_basis is not None
                    else None,
                )
            )
    return lots, disposals


@settings(max_examples=300, deadline=None)
@given(_lot_books(), _ratios)
def test_lots_split_together_keeping_basis_and_term(
    book_facts: tuple[list[core_lots.LotFact], list[core_lots.Disposal]], ratio: tuple[int, int]
) -> None:
    lots, disposals = book_facts
    numerator, denominator = ratio
    split_day = BASE + timedelta(days=10)
    before = core_lots.Book(lots=tuple(lots), disposals=tuple(disposals))
    after = core_lots.Book(
        lots=tuple(lots),
        disposals=tuple(disposals),
        splits=(core_lots.SplitFact(split_day, numerator, denominator),),
    )
    day_before = split_day - timedelta(days=1)
    held = sum((before.remaining(lot, day_before) for lot in lots), Decimal(0))
    # Their counts sum to the split total: a fully allocated position (it
    # holds exactly what its lots hold) stays fully allocated, no dust.
    total = sum((after.remaining(lot, split_day) for lot in lots), Decimal(0))
    assert total == core.split_quantity(held, numerator, denominator)
    position = core.split_quantity(held, numerator, denominator)
    assert core_lots.position_basis(after, split_day, position).unallocated == 0
    for lot in lots:
        left = before.remaining(lot, day_before)
        split_left = after.remaining(lot, split_day)
        # Each lot within a millionth of its exact share.
        assert abs(Fraction(split_left) - _exact(left, numerator, denominator)) < Fraction(
            1, 1_000_000
        )
        # Its remaining basis is unchanged: a split moves no money.
        assert after.remaining_basis(lot, split_day) == before.remaining_basis(lot, day_before)
        # Its basis per share is divided by the ratio (six places).
        if lot.basis_per_share is not None:
            assert after.per_share(lot, split_day) == core.round_fraction(
                Fraction(lot.basis_per_share) * denominator / numerator, core.PRICE_QUANT
            )
        # Acquisition date and term are the lot's own.
        assert core_lots.term(lot.acquired_on, split_day) == core_lots.term(
            lot.acquired_on, day_before
        ) or split_day > core_lots.anniversary(lot.acquired_on)
    # And a lot that took effect on or after the split is not split.
    late = core_lots.LotFact(
        id=uuid.UUID(int=99),
        acquired_on=split_day,
        effective_on=split_day,
        quantity=Decimal("3"),
        cost_basis=Decimal("30"),
        basis_per_share=Decimal("10"),
        ordinal=1,
        adopted=False,
    )
    with_late = core_lots.with_lots(after, [late])
    assert with_late.remaining(late, split_day) == Decimal("3")
    assert with_late.per_share(late, split_day) == Decimal("10")


@settings(max_examples=300, deadline=None)
@given(
    _quantities,
    _ratios,
    st.decimals(min_value=Decimal("0.01"), max_value=Decimal("5000"), places=6),
    st.integers(min_value=1, max_value=6),
)
def test_value_is_continuous_across_the_effective_date(
    quantity: Decimal, ratio: tuple[int, int], close: Decimal, gap: int
) -> None:
    """With only closes from before the split (the effective date's close
    not published yet, a weekend, a price recorded earlier), the shares
    after the split times the close divided by the ratio are worth what the
    shares before were, in the full and the bounded projection."""
    numerator, denominator = ratio
    split_day = BASE + timedelta(days=10)
    created = datetime(2026, 1, 1, tzinfo=UTC)
    position = core.PositionFacts(
        id=uuid.UUID(int=7),
        symbol="AAA",
        events=[
            core.Event(uuid.UUID(int=1), BASE, created, core.SET_QUANTITY, quantity),
            core.Event(uuid.UUID(int=2), split_day, created, core.SPLIT, Decimal(0), split=ratio),
        ],
    )
    books = {"AAA": core.PriceBook([core.PricePoint(split_day - timedelta(days=gap), close, "x")])}
    upper = split_day + timedelta(days=3)
    full = core.projection_rows([position], books, lower=BASE, upper=upper)
    bounded = core.projection_rows([position], books, lower=BASE, upper=upper, start=split_day)
    assert bounded == [r for r in full if r.as_of >= split_day]
    before = max((r for r in full if r.as_of < split_day), key=lambda r: r.as_of)
    on = next(r for r in full if r.as_of == split_day)
    after_quantity = core.split_quantity(quantity, numerator, denominator)
    assert on.quantity == after_quantity
    if after_quantity == 0 or before.market_value is None or on.market_value is None:
        return  # dust split to nothing, or out of range at that close: no value to compare
    assert on.price_as_of == before.price_as_of  # the same close, adjusted
    # Rounding only: the count and the adjusted close to six places, the
    # values to four.
    tolerance = (after_quantity + Decimal(on.price or 0) + 1) * Decimal("0.000001") + Decimal(
        "0.0002"
    )
    assert abs(on.market_value - before.market_value) <= tolerance


# ---------------------------------------------------------------------------
# Real Postgres
# ---------------------------------------------------------------------------


def _brokerage(session: Session) -> Account:
    account = ledger.create_account(
        session,
        name="Broker",
        type_="brokerage",
        path=f"assets.split_{uuid.uuid4().hex[:10]}",
        actor_token_id=None,
    )
    ledger.update_account(
        session, account_id=account.id, holdings_source="manual", actor_token_id=None
    )
    session.flush()
    return account


def _price(session: Session, symbol: str, on: date, close: str) -> None:
    session.merge(
        SecurityPrice(symbol=symbol, price_date=on, source="manual", close=Decimal(close))
    )
    session.flush()


def _rows(session: Session, account_id: uuid.UUID) -> list[tuple[Any, ...]]:
    return [
        (r.as_of, r.position_id, r.quantity, r.cost_basis, r.market_value, r.price, r.price_as_of)
        for r in session.execute(
            select(HoldingsSnapshot)
            .where(HoldingsSnapshot.account_id == account_id, HoldingsSnapshot.source == "manual")
            .order_by(HoldingsSnapshot.as_of, HoldingsSnapshot.position_id)
        ).scalars()
    ]


def _lot_state(session: Session, position: Any) -> list[tuple[Any, ...]]:
    return [
        (v.lot.id, v.status, v.remaining, v.remaining_basis, v.dispositions)
        for v in lot_service._views(session, position)
    ]


def _state(session: Session, account: Account, position: Any) -> dict[str, Any]:
    state = service.position_state(session, position, stale_days=7, today=today())
    return {
        "rows": _rows(session, account.id),
        "lots": _lot_state(session, position),
        "quantity": state.quantity,
        "basis": state.cost_basis,
        "value": state.market_value,
        "cash": account_balances(session, [account.id]).get(account.id, Decimal(0)),
    }


def _hold(session: Session, account: Account, symbol: str, quantity: str, days_ago: int) -> Any:
    return service.record_position(
        session,
        account_ref=str(account.id),
        symbol=symbol,
        quantity=Decimal(quantity),
        as_of=today() - timedelta(days=days_ago),
        token=None,
    ).position


def _buy_lot(session: Session, position: Any, quantity: str, price: str, days_ago: int) -> None:
    service.record_event(
        session,
        position_id=position.id,
        kind="buy",
        occurred_on=today() - timedelta(days=days_ago),
        quantity=Decimal(quantity),
        price=Decimal(price),
        lot=service.LotSpec(),
        token=None,
    )


class TestExamples:
    def test_a_forward_split_keeps_the_basis_and_the_value(self, db_session: Session) -> None:
        account = _brokerage(db_session)
        symbol = _symbol()
        _price(db_session, symbol, today() - timedelta(days=20), "100")
        position = service.record_position(
            db_session,
            account_ref=str(account.id),
            symbol=symbol,
            quantity=Decimal("10"),
            as_of=today() - timedelta(days=20),
            cost_basis=Decimal("800"),
            token=None,
        ).position
        recorded = service.record_split(
            db_session,
            position_id=position.id,
            occurred_on=today() - timedelta(days=10),
            numerator=2,
            denominator=1,
            token=None,
        )
        assert (recorded.before, recorded.after) == (Decimal("10"), Decimal("20.000000"))
        assert recorded.in_lieu is None
        state = service.position_state(db_session, position, stale_days=400, today=today())
        assert state.quantity == Decimal("20")
        assert state.cost_basis == Decimal("800")
        assert state.price is not None and state.price.close == Decimal("50.000000")
        assert state.market_value == Decimal("1000.0000")

        def value_on(day: date) -> Decimal:
            rows = [r for r in _rows(db_session, account.id) if r[0] <= day]
            return max(rows, key=lambda r: r[0])[4]

        assert value_on(today() - timedelta(days=11)) == Decimal("1000.0000")  # 10 x 100
        assert value_on(today() - timedelta(days=10)) == Decimal("1000.0000")  # 20 x 50
        # A post-split close on the effective date is already in new shares.
        from purser.holdings import prices

        prices.record_price(
            db_session,
            symbol=symbol,
            price_date=today() - timedelta(days=10),
            close=Decimal("51"),
            token=None,
        )
        assert value_on(today() - timedelta(days=10)) == Decimal("1020.0000")
        assert value_on(today() - timedelta(days=11)) == Decimal("1000.0000")

    def test_a_reverse_split_divides_each_lots_shares(self, db_session: Session) -> None:
        account = _brokerage(db_session)
        symbol = _symbol()
        position = service.create_position(
            db_session,
            account_ref=str(account.id),
            symbol=symbol,
            opened_on=today() - timedelta(days=60),
            token=None,
        )
        _buy_lot(db_session, position, "30", "10", 60)
        _buy_lot(db_session, position, "20", "12", 50)
        service.record_event(
            db_session,
            position_id=position.id,
            kind="sell",
            occurred_on=today() - timedelta(days=40),
            quantity=Decimal("5"),
            price=Decimal("11"),
            proceeds_to="none",
            allocation=service.AllocationRequest(method="fifo"),
            token=None,
        )
        service.record_split(
            db_session,
            position_id=position.id,
            occurred_on=today() - timedelta(days=30),
            numerator=1,
            denominator=10,
            token=None,
        )
        views = lot_service._views(db_session, position)
        assert [(v.remaining, v.remaining_basis) for v in views] == [
            (Decimal("2.500000"), Decimal("250.0000")),
            (Decimal("2.000000"), Decimal("240.0000")),
        ]
        summary = lot_service.summary(db_session, position, today=today())
        assert summary.quantity == Decimal("4.5") and summary.unallocated == 0
        assert summary.cost_basis == Decimal("490.0000")
        # A later sale draws in post-split shares at the split basis per share.
        service.record_event(
            db_session,
            position_id=position.id,
            kind="sell",
            occurred_on=today() - timedelta(days=5),
            quantity=Decimal("1"),
            price=Decimal("150"),
            proceeds_to="none",
            allocation=service.AllocationRequest(method="hifo"),
            token=None,
        )
        views = lot_service._views(db_session, position)
        # hifo: 120 per share (the second lot's 12 x 10) beats 100.
        assert [v.remaining for v in views] == [Decimal("2.500000"), Decimal("1.000000")]
        assert views[1].remaining_basis == Decimal("120.0000")

    def test_cash_in_lieu_disposes_of_the_fraction_and_reverses_with_the_split(
        self, db_session: Session
    ) -> None:
        account = _brokerage(db_session)
        symbol = _symbol()
        position = service.create_position(
            db_session,
            account_ref=str(account.id),
            symbol=symbol,
            opened_on=today() - timedelta(days=60),
            token=None,
        )
        _buy_lot(db_session, position, "10", "9", 60)
        before = _state(db_session, account, position)
        recorded = service.record_split(
            db_session,
            position_id=position.id,
            occurred_on=today() - timedelta(days=30),
            numerator=1,
            denominator=3,
            cash_in_lieu=Decimal("12.34"),
            allocation=service.AllocationRequest(method="fifo"),
            token=None,
        )
        assert recorded.after == Decimal("3.333333")
        assert recorded.fraction == Decimal("0.333333")
        sale = recorded.in_lieu
        assert sale is not None and sale.kind == "sell" and sale.in_lieu_of == recorded.split.id
        state = service.position_state(db_session, position, stale_days=7, today=today())
        assert state.quantity == Decimal("3")
        cash = account_balances(db_session, [account.id])[account.id]
        assert cash - before["cash"] == Decimal("12.34")
        (view,) = lot_service._views(db_session, position)
        assert view.remaining == Decimal("3.000000")
        # The fraction's basis: its share of the lot's 90.00 (pro rata to the
        # 10/3 shares the lot became), and the gain is the cash less it.
        from purser.models import LotDisposition

        (disposition,) = db_session.execute(
            select(LotDisposition).where(LotDisposition.event_id == sale.id)
        ).scalars()
        assert disposition.cost_basis == Decimal("9.0000")
        assert disposition.proceeds == Decimal("12.3400")
        assert disposition.realized_gain == Decimal("3.3400")
        assert view.remaining_basis == Decimal("81.0000")
        # The sale alone is never reversed.
        with pytest.raises(LedgerError, match="cash in lieu"):
            service.reverse_event(db_session, event_id=sale.id, token=None)
        service.reverse_event(db_session, event_id=recorded.split.id, token=None)
        assert _state(db_session, account, position) == before

    def test_refusals(self, db_session: Session) -> None:
        account = _brokerage(db_session)
        symbol = _symbol()
        position = _hold(db_session, account, symbol, "10", 20)

        def split(**kw: Any) -> service.SplitRecorded:
            args: dict[str, Any] = {
                "position_id": position.id,
                "occurred_on": today() - timedelta(days=5),
                "numerator": 2,
                "denominator": 1,
                "token": None,
            }
            args.update(kw)
            return service.record_split(db_session, **args)

        with pytest.raises(LedgerError, match="holds no shares"):
            split(occurred_on=today() - timedelta(days=20))  # held from that day, not before
        with pytest.raises(LedgerError, match="1:1"):
            split(numerator=3, denominator=3)
        with pytest.raises(LedgerError, match="whole numbers"):
            split(numerator=0)
        with pytest.raises(LedgerError, match="future"):
            split(occurred_on=today() + timedelta(days=1))
        with pytest.raises(LedgerError, match="no fractional share"):
            split(cash_in_lieu=Decimal("5.00"))  # 10 x 2 = 20, a whole number
        with pytest.raises(LedgerError, match="in cents"):
            split(numerator=1, denominator=3, cash_in_lieu=Decimal("5.001"))
        with pytest.raises(LedgerError, match="apply to a split's cash_in_lieu"):
            split(force_cash=True)
        with pytest.raises(LedgerError, match="ratio"):
            service.record_event(
                db_session,
                position_id=position.id,
                kind="split",
                occurred_on=today() - timedelta(days=5),
                quantity=Decimal(0),
                token=None,
            )
        split()
        with pytest.raises(LedgerError, match="already has a split"):
            split(numerator=3)

    def test_a_complete_capture_after_a_split_matches_the_split_lots(
        self, db_session: Session
    ) -> None:
        """The broker lists lots in post-split shares and basis per share:
        a complete capture matches them with no conflict, and filling a basis
        keeps the broker's total."""
        from purser.holdings import captures

        account = _brokerage(db_session)
        symbol = _symbol()
        acquired = today() - timedelta(days=90)

        def capture(as_of: date, quantity: str, per_share: str | None) -> Any:
            body = captures.LotCaptureIn(
                account=str(account.id),
                as_of=as_of,
                captured_at=datetime(as_of.year, as_of.month, as_of.day, 20, tzinfo=UTC),
                completeness="complete",
                positions=(
                    captures.SymbolIn(
                        symbol=symbol,
                        lots=(
                            captures.LotIn(
                                acquired_on=acquired,
                                quantity=Decimal(quantity),
                                external_ref="SYN-1",
                                basis_per_share=Decimal(per_share) if per_share else None,
                            ),
                        ),
                    ),
                ),
            )
            return captures.preview(db_session, body), body

        plan, body = capture(today() - timedelta(days=60), "8", "45")
        captures.commit(db_session, body, expect_hash=plan.plan_hash, token=None)
        position = service.open_position(db_session, account.id, symbol)
        assert position is not None
        service.record_split(
            db_session,
            position_id=position.id,
            occurred_on=today() - timedelta(days=20),
            numerator=3,
            denominator=2,
            token=None,
        )
        plan, body = capture(today() - timedelta(days=5), "12", "30")
        (symbol_plan,) = plan.symbols
        assert symbol_plan.plan.conflicts == ()
        assert symbol_plan.plan.changes == () and symbol_plan.plan.unchanged == 1
        # A lot captured with no basis takes the broker's post-split figure
        # as its total: per share as acquired is that total over 8 shares.
        lot = lot_service._views(db_session, position)[0].lot
        assert lot.cost_basis == Decimal("360.0000")
        assert lot.basis_per_share == Decimal("45.000000")


@st.composite
def _histories(draw: st.DrawFn) -> dict[str, Any]:
    events = []
    for _ in range(draw(st.integers(min_value=1, max_value=5))):
        events.append(
            (
                draw(st.sampled_from(["buy", "sell"])),
                draw(st.integers(min_value=31, max_value=60)),  # days ago: before the split
                draw(st.decimals(min_value=Decimal("1"), max_value=Decimal("40"), places=3)),
            )
        )
    return {
        "lots": draw(st.booleans()),
        "opening": draw(st.decimals(min_value=Decimal("1"), max_value=Decimal("80"), places=3)),
        "events": events,
        "ratio": draw(st.sampled_from([(2, 1), (3, 2), (1, 10), (1, 3), (7, 3), (5, 1)])),
        "in_lieu": draw(st.booleans()),
        "closes": draw(
            st.lists(
                st.tuples(
                    st.integers(min_value=21, max_value=70),
                    st.decimals(min_value=Decimal("1"), max_value=Decimal("300"), places=2),
                ),
                min_size=1,
                max_size=4,
            )
        ),
        "after": draw(st.booleans()),  # a sale after the split
    }


def _build(session: Session, spec: dict[str, Any]) -> tuple[Account, Any]:
    account = _brokerage(session)
    symbol = _symbol()
    for days_ago, close in spec["closes"]:
        _price(session, symbol, today() - timedelta(days=days_ago), str(close))
    position = service.create_position(
        session,
        account_ref=str(account.id),
        symbol=symbol,
        opened_on=today() - timedelta(days=70),
        token=None,
    )
    if spec["lots"]:
        _buy_lot(session, position, str(spec["opening"]), "20", 70)
    else:
        service.record_event(
            session,
            position_id=position.id,
            kind="set_quantity",
            occurred_on=today() - timedelta(days=70),
            quantity=spec["opening"],
            cost_basis=core.money(spec["opening"] * 20),
            token=None,
        )
    for kind, days_ago, quantity in spec["events"]:
        extra: dict[str, Any] = {"price": Decimal("21")}
        if kind == "buy" and spec["lots"]:
            extra["lot"] = service.LotSpec()
        if kind == "sell":
            extra["proceeds_to"] = "none"
            if spec["lots"]:
                extra["allocation"] = service.AllocationRequest(method="fifo")
        try:
            with session.begin_nested():
                service.record_event(
                    session,
                    position_id=position.id,
                    kind=kind,
                    occurred_on=today() - timedelta(days=days_ago),
                    quantity=quantity,
                    token=None,
                    **extra,
                )
        except LedgerError:
            continue
    return account, position


@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(_histories())
def test_reversing_a_split_restores_everything(migrated_engine: Engine, spec: dict) -> None:
    with db.get_sessionmaker()() as session:
        account, position = _build(session, spec)
        before = _state(session, account, position)
        effective = today() - timedelta(days=30)
        numerator, denominator = spec["ratio"]
        held = service.quantity_on(session, position, effective - timedelta(days=1))
        if held == 0:  # sold out before: nothing to split (refused, tested above)
            session.rollback()
            return
        after = core.split_quantity(held, numerator, denominator)
        fraction = after - int(after)
        lieu = Decimal("3.21") if spec["in_lieu"] and fraction > 0 else None
        recorded = service.record_split(
            session,
            position_id=position.id,
            occurred_on=effective,
            numerator=numerator,
            denominator=denominator,
            cash_in_lieu=lieu,
            allocation=(
                service.AllocationRequest(method="fifo")
                if lieu is not None and spec["lots"]
                else None
            ),
            token=None,
        )
        # Quantity conservation: the shares held after are the ratio times
        # those before, less any fraction paid in cash.
        assert service.quantity_on(session, position, effective) == after - (
            fraction if lieu is not None else 0
        )
        if spec["lots"]:
            summary = lot_service.summary(session, position, today=today())
            assert summary.unallocated == 0  # still fully allocated
        # Value continuity: the day before and the effective date, when the
        # close in effect on both is the same pre-split close.
        rows = _rows(session, account.id)
        on = [r for r in rows if r[0] == effective]
        prior = [r for r in rows if r[0] < effective]
        if on and prior and lieu is None and on[0][6] is not None and on[0][6] < effective:
            last = max(prior, key=lambda r: r[0])
            if last[6] == on[0][6] and last[4] is not None and on[0][4] is not None:
                tolerance = (on[0][2] + on[0][5] + 1) * Decimal("0.000001") + Decimal("0.0002")
                assert abs(on[0][4] - last[4]) <= tolerance
        if spec["after"]:
            try:
                with session.begin_nested():
                    service.record_event(
                        session,
                        position_id=position.id,
                        kind="sell",
                        occurred_on=today() - timedelta(days=10),
                        quantity=Decimal("0.5"),
                        price=Decimal("30"),
                        proceeds_to="none",
                        allocation=service.AllocationRequest(method="fifo")
                        if spec["lots"]
                        else None,
                        token=None,
                    )
            except LedgerError:
                pass
        # The rows the bounded writes left are a full regeneration's.
        written = _rows(session, account.id)
        session.execute(
            delete(HoldingsSnapshot).where(
                HoldingsSnapshot.account_id == account.id, HoldingsSnapshot.source == "manual"
            )
        )
        projection.project_account(session, projection.lock_account(session, account.id))
        assert _rows(session, account.id) == written
        if spec["after"]:
            # Undo the later sale first (its shares are post-split).
            later = session.execute(
                select(PositionEvent).where(
                    PositionEvent.position_id == position.id,
                    PositionEvent.occurred_on == today() - timedelta(days=10),
                    PositionEvent.reverses_id.is_(None),
                )
            ).scalar_one_or_none()
            if later is not None:
                service.reverse_event(session, event_id=later.id, token=None)
        service.reverse_event(session, event_id=recorded.split.id, token=None)
        assert _state(session, account, position) == before
        session.rollback()


# ---------------------------------------------------------------------------
# REST and MCP
# ---------------------------------------------------------------------------


@pytest.fixture
def broker(admin_client: TestClient) -> str:
    resp = admin_client.post(
        "/accounts", json={"name": "Broker", "type": "brokerage", "path": "assets.broker"}
    )
    account_id = resp.json()["id"]
    assert (
        admin_client.patch(
            f"/accounts/{account_id}", json={"holdings_source": "manual"}
        ).status_code
        == 200
    )
    return account_id


def test_rest_records_and_reverses_a_split_with_cash_in_lieu(
    broker: str, admin_client: TestClient
) -> None:
    symbol = _symbol()
    created = admin_client.post(
        f"/accounts/{broker}/positions",
        json={
            "symbol": symbol,
            "quantity": "7",
            "as_of": (today() - timedelta(days=30)).isoformat(),
            "cost_basis": "140.00",
        },
    )
    assert created.status_code == 201, created.text
    position_id = created.json()["id"]
    url = f"/positions/{position_id}/events"
    effective = (today() - timedelta(days=10)).isoformat()
    bad = admin_client.post(
        url,
        json={"kind": "split", "occurred_on": effective, "split_numerator": 1, "price": "2"},
    )
    assert bad.status_code == 422 and "price" in bad.text
    missing = admin_client.post(url, json={"kind": "buy", "occurred_on": effective})
    assert missing.status_code == 422 and "quantity" in missing.text
    misplaced = admin_client.post(
        url,
        json={"kind": "buy", "occurred_on": effective, "quantity": "1", "split_numerator": 2},
    )
    assert misplaced.status_code == 422 and "apply to a split" in misplaced.text
    resp = admin_client.post(
        url,
        json={
            "kind": "split",
            "occurred_on": effective,
            "split_numerator": 1,
            "split_denominator": 2,
            "cash_in_lieu": "6.50",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["event"]["kind"] == "split"
    assert (body["event"]["split_numerator"], body["event"]["split_denominator"]) == (1, 2)
    lieu = body["cash_in_lieu"]
    assert lieu["kind"] == "sell" and lieu["in_lieu_of"] == body["event"]["id"]
    assert Decimal(lieu["quantity"]) == Decimal("0.5")
    assert Decimal(body["position"]["quantity"]) == Decimal("3")
    # 140.00 basis for 3.5 shares; the half share took 20.00 of it.
    assert Decimal(body["position"]["cost_basis"]) == Decimal("120")
    refused = admin_client.post(f"/position-events/{lieu['id']}/reverse")
    assert refused.status_code == 422 and "reverse the split" in refused.text
    undone = admin_client.post(f"/position-events/{body['event']['id']}/reverse")
    assert undone.status_code == 200, undone.text
    assert undone.json()["cash_in_lieu"]["reverses_id"] == lieu["id"]
    assert Decimal(undone.json()["position"]["quantity"]) == Decimal("7")
    events = admin_client.get(url).json()
    assert {e["kind"] for e in events} == {"set_quantity", "split", "sell"}


@pytest.fixture(scope="module")
def live_server(migrated_engine: Engine) -> Iterator[LiveServer]:
    server = LiveServer()
    server.start()
    yield server
    server.stop()


def test_mcp_record_split_and_reverse(
    live_server: LiveServer, broker: str, admin_client: TestClient
) -> None:
    tokens = {
        name: admin_client.post("/tokens", json={"name": name, "scopes": scopes}).json()["token"]
        for name, scopes in (("reader", ["read"]), ("agent", ["read", "write"]))
    }
    symbol = _symbol()
    hold = {
        "account": "assets.broker",
        "symbol": symbol,
        "quantity": "10",
        "as_of": (today() - timedelta(days=30)).isoformat(),
    }
    split = {
        "account": "assets.broker",
        "symbol": symbol,
        "ratio": "1:4",
        "effective_date": (today() - timedelta(days=10)).isoformat(),
        "cash_in_lieu": "15.00",
    }
    [denied] = _mcp_call(live_server.url, tokens["reader"], [("record_split", split)])
    assert "error" in denied and "write" in denied["error"]
    [_held, bad, done] = _mcp_call(
        live_server.url,
        tokens["agent"],
        [
            ("record_position", hold),
            ("record_split", {**split, "ratio": "4"}),
            ("record_split", split),
        ],
    )
    assert "new:old" in bad["error"]
    assert done["position"]["quantity_before"] == "10"
    assert done["position"]["quantity_after"] == "2.5"
    assert done["cash_in_lieu"]["fraction"] == "0.5"
    assert done["cash_in_lieu"]["amount"] == "15.00"
    [withheld] = _mcp_call(
        live_server.url,
        tokens["agent"],
        [("reverse_position_event", {"event_id": done["event_id"]})],
    )
    assert withheld["kind"] == "split" and withheld["position"]["quantity_after"] == "10"
    assert withheld["cash_in_lieu"]["transaction_id"] == done["cash_in_lieu"]["transaction_id"]
