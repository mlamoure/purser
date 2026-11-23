"""The bounded holdings paths of issue #404 give exactly what the full
computations give.

- ``core.projection_rows(..., start=s)`` is the full projection's rows dated
  ``s`` or later (pure, many examples).
- A book loaded from ``s`` (``projection.price_books(start=s)``) answers
  every day from ``s - 1`` on like the full book, feed-implied closes and
  their unchanged runs included: as a pure rule, and against real Postgres.
- On real Postgres, every write projects only from its date, and the rows it
  leaves are the rows a full regeneration writes; ``computed_rows`` from any
  start equals the full computation from there.
- One position's state (``service.position_state``) is the all-positions
  computation's, and the today-only books (``projection.latest_books``)
  answer today like the full history.

Fixtures are synthetic: generated symbols, made-up prices.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session

from purser import db, ledger
from purser.core import holdings as core
from purser.holdings import projection, service
from purser.ledger import LedgerError
from purser.models import Account, HoldingsSnapshot, Position, SecurityPrice
from tests.conftest import today

BASE = date(2026, 1, 5)


def _symbol(prefix: str = "B") -> str:
    return f"{prefix}{uuid.uuid4().hex[:8].upper()}"


# ---------------------------------------------------------------------------
# Pure: the projection window
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeLots:
    """A lot sub-ledger as the projection reads it (``core.LotBasis``): its
    basis changes on given days, and is a known function of the day."""

    days: tuple[date, ...]

    def change_dates(self) -> Iterable[date]:
        return sorted(set(self.days))

    def basis_on(self, day: date, quantity: Decimal, average: Decimal | None) -> Decimal | None:
        counted = [d for d in self.days if d <= day]
        if not counted:
            return average
        return core.money(quantity * Decimal(len(counted)))


_amounts = st.decimals(min_value=Decimal("0.5"), max_value=Decimal("40"), places=2)
_closes = st.decimals(min_value=Decimal("1"), max_value=Decimal("300"), places=2)


@st.composite
def _positions(draw: st.DrawFn) -> list[core.PositionFacts]:
    """Up to three positions in two symbols. Each history is drawn in date
    order and never goes below zero, and sells often sell out, so the window
    often opens on the day a position empties (its tombstone)."""
    symbols = ["AAA", "BBB"]
    out: list[core.PositionFacts] = []
    for index in range(draw(st.integers(min_value=1, max_value=3))):
        events = []
        transits = []
        held = Decimal(0)
        days = sorted(
            # Distinct days: a split takes effect first in its day, ahead of
            # events drawn before it.
            draw(
                st.lists(
                    st.integers(min_value=0, max_value=60), min_size=1, max_size=6, unique=True
                )
            )
        )
        for n, offset in enumerate(days):
            day = BASE + timedelta(days=offset)
            kind = draw(
                st.sampled_from([core.SET_QUANTITY, core.BUY, core.SELL, "sell_out", core.SPLIT])
            )
            quantity = draw(_amounts)
            ratio = draw(st.sampled_from([(2, 1), (1, 3), (3, 2)])) if kind == core.SPLIT else None
            if ratio is not None:
                quantity = Decimal(0)
            if kind in (core.SELL, "sell_out"):
                if held == 0:
                    kind = core.BUY
                else:
                    quantity = held if kind == "sell_out" else min(quantity, held)
                    kind = core.SELL
            if kind == core.SET_QUANTITY and draw(st.booleans()):
                quantity = Decimal(0)
            event = core.Event(
                id=uuid.UUID(int=index * 100 + n + 1),
                occurred_on=day,
                created_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=n),
                kind=kind,
                quantity=quantity,
                price=draw(st.none() | _closes) if ratio is None else None,
                split=ratio,
            )
            if ratio is not None:
                held = core.split_quantity(held, *ratio)
            else:
                held = (
                    quantity
                    if kind == core.SET_QUANTITY
                    else held + quantity
                    if kind == core.BUY
                    else held - quantity
                )
            events.append(event)
            if kind == core.SELL and draw(st.booleans()):
                transits.append(
                    core.InTransit(
                        event_id=event.id,
                        start=day,
                        end=day + timedelta(days=draw(st.integers(min_value=0, max_value=6))),
                        amount=draw(st.decimals(min_value=-50, max_value=50, places=2)),
                    )
                )
        lots = None
        if draw(st.booleans()):
            lots = FakeLots(
                tuple(
                    BASE + timedelta(days=d)
                    for d in draw(st.lists(st.integers(min_value=0, max_value=60), max_size=3))
                )
            )
        out.append(
            core.PositionFacts(
                id=uuid.UUID(int=(index + 1) << 64),
                symbol=draw(st.sampled_from(symbols)),
                events=events,
                in_transit=transits,
                lots=lots,
            )
        )
    return out


@st.composite
def _points(draw: st.DrawFn) -> list[core.PricePoint]:
    """Closes of every source, feed-implied ones often repeating a value so
    unchanged runs occur."""
    feed_values = [Decimal("10"), Decimal("10.5"), Decimal("11")]
    out = []
    taken: set[tuple[date, str]] = set()
    for _ in range(draw(st.integers(min_value=0, max_value=14))):
        source = draw(st.sampled_from([core.SOURCE_MANUAL, "tiingo", core.SOURCE_FEED]))
        day = BASE + timedelta(days=draw(st.integers(min_value=-10, max_value=70)))
        if (day, source) in taken:
            continue  # one close per (date, source), as stored and implied
        taken.add((day, source))
        close = draw(st.sampled_from(feed_values)) if source == core.SOURCE_FEED else draw(_closes)
        out.append(core.PricePoint(day, close, source))
    return out


def _bounded(points: list[core.PricePoint], start: date) -> list[core.PricePoint]:
    """The closes ``projection.price_points(start=...)`` loads, as a pure
    rule: everything dated ``start`` or later, every stored close of the
    newest stored date before it, and the feed-implied close in effect the
    day before, dated the first day of its unchanged run."""
    stored = [p for p in points if p.source != core.SOURCE_FEED]
    feed = sorted((p for p in points if p.source == core.SOURCE_FEED), key=lambda p: p.price_date)
    before = [p.price_date for p in stored if p.price_date < start]
    floor = max(before) if before else start
    out = [p for p in stored if p.price_date >= floor]
    out += [p for p in feed if p.price_date >= start]
    earlier = [p for p in feed if p.price_date < start and 0 < p.close < core.MAX_PRICE]
    if earlier:
        last = earlier[-1]
        first_day = last.price_date
        for point in reversed(earlier):
            if point.close != last.close:
                break
            first_day = point.price_date
        out.append(core.PricePoint(first_day, last.close, core.SOURCE_FEED))
    return out


@settings(max_examples=400, deadline=None)
@given(
    _positions(),
    st.dictionaries(st.sampled_from(["AAA", "BBB"]), _points(), max_size=2),
    st.integers(min_value=-5, max_value=40),
    st.integers(min_value=0, max_value=40),
    st.integers(min_value=-8, max_value=50),
    st.booleans(),
)
def test_a_window_is_the_full_projection_from_its_start(
    positions: list[core.PositionFacts],
    points: dict[str, list[core.PricePoint]],
    lower_offset: int,
    length: int,
    start_offset: int,
    settle_after: bool,
) -> None:
    lower = BASE + timedelta(days=lower_offset)
    upper = lower + timedelta(days=length)
    start = lower + timedelta(days=start_offset)
    full_books = {s: core.PriceBook(p) for s, p in points.items()}
    window_books = {s: core.PriceBook(_bounded(p, start)) for s, p in points.items()}
    full = core.projection_rows(
        positions, full_books, lower=lower, upper=upper, settle_after=settle_after
    )
    for books in (full_books, window_books):
        bounded = core.projection_rows(
            positions, books, lower=lower, upper=upper, settle_after=settle_after, start=start
        )
        assert bounded == [r for r in full if r.as_of >= start]


def test_a_window_opening_on_a_sell_out_day_ends_the_position() -> None:
    """The set before the window held the position, and the window's first
    day sells it out: that day carries its zero row, as in the full
    projection (else the set before would stay newest and bring the shares
    back)."""
    position = core.PositionFacts(
        id=uuid.UUID(int=1),
        symbol="AAA",
        events=[
            core.Event(
                uuid.UUID(int=2),
                BASE,
                datetime(2026, 1, 1, tzinfo=UTC),
                core.BUY,
                Decimal("3"),
                price=Decimal("10"),
            ),
            core.Event(
                uuid.UUID(int=3),
                BASE + timedelta(days=9),
                datetime(2026, 1, 2, tzinfo=UTC),
                core.SELL,
                Decimal("3"),
            ),
        ],
    )
    end = BASE + timedelta(days=20)
    start = BASE + timedelta(days=9)
    full = core.projection_rows([position], {}, lower=BASE, upper=end)
    bounded = core.projection_rows([position], {}, lower=BASE, upper=end, start=start)
    assert [(r.as_of, r.quantity) for r in bounded] == [(start, Decimal("0.000000"))]
    assert bounded == [r for r in full if r.as_of >= start]


@settings(max_examples=400, deadline=None)
@given(_points(), st.integers(min_value=-12, max_value=72))
def test_a_bounded_book_answers_like_the_full_one(
    points: list[core.PricePoint], start_offset: int
) -> None:
    start = BASE + timedelta(days=start_offset)
    full = core.PriceBook(points)
    bounded = core.PriceBook(_bounded(points, start))
    for offset in range(-1, 40):
        day = start + timedelta(days=offset)
        assert bounded.on(day) == full.on(day), day
    end = start + timedelta(days=40)
    assert bounded.dates_between(start, end) == full.dates_between(start, end)


# ---------------------------------------------------------------------------
# Real Postgres
# ---------------------------------------------------------------------------


def _brokerage(session: Session, *, manual: bool) -> Account:
    account = ledger.create_account(
        session,
        name="Broker",
        type_="brokerage",
        path=f"assets.bounded_{uuid.uuid4().hex[:10]}",
        actor_token_id=None,
    )
    if manual:
        ledger.update_account(
            session, account_id=account.id, holdings_source="manual", actor_token_id=None
        )
    session.flush()
    return account


def _feed_row(
    session: Session, account_id: uuid.UUID, on: date, symbol: str, quantity: str, market: str
) -> None:
    session.add(
        HoldingsSnapshot(
            account_id=account_id,
            as_of=on,
            symbol=symbol,
            quantity=Decimal(quantity),
            market_value=Decimal(market),
        )
    )


def _manual_rows(session: Session, account_id: uuid.UUID) -> list[tuple]:
    return [
        (
            r.as_of,
            r.position_id,
            r.symbol,
            r.quantity,
            r.cost_basis,
            r.market_value,
            r.price,
            r.price_as_of,
            r.in_transit,
        )
        for r in session.execute(
            select(HoldingsSnapshot)
            .where(HoldingsSnapshot.account_id == account_id, HoldingsSnapshot.source == "manual")
            .order_by(HoldingsSnapshot.as_of, HoldingsSnapshot.position_id)
        ).scalars()
    ]


@st.composite
def _histories(draw: st.DrawFn) -> dict:
    positions = draw(st.integers(min_value=1, max_value=2))
    events = [
        (
            draw(st.integers(min_value=0, max_value=positions - 1)),
            draw(st.sampled_from(["set_quantity", "buy", "sell", "sell_settles_later"])),
            draw(st.integers(min_value=0, max_value=40)),  # days ago
            draw(st.decimals(min_value=Decimal("0.5"), max_value=Decimal("30"), places=2)),
        )
        for _ in range(draw(st.integers(min_value=1, max_value=7)))
    ]
    closes = draw(
        st.lists(
            st.tuples(
                st.integers(min_value=0, max_value=positions - 1),
                st.integers(min_value=0, max_value=50),
                _closes,
                st.sampled_from(["manual", "tiingo"]),
            ),
            max_size=8,
        )
    )
    # Another account's feed rows of the same symbols: feed-implied closes,
    # often unchanged from one day to the next (a frozen bank value).
    implied = draw(
        st.lists(
            st.tuples(
                st.integers(min_value=0, max_value=positions - 1),
                st.integers(min_value=0, max_value=50),
                st.sampled_from(["100.00", "105.00", "110.00"]),
            ),
            max_size=10,
        )
    )
    return {
        "positions": positions,
        "lots": draw(st.booleans()),
        "events": events,
        "closes": closes,
        "implied": implied,
        "feed": draw(st.none() | st.integers(min_value=10, max_value=45)),
        "starts": draw(st.lists(st.integers(min_value=-2, max_value=52), min_size=1, max_size=4)),
    }


def _build(session: Session, spec: dict) -> tuple[Account, list[str]]:
    symbols = [_symbol() for _ in range(spec["positions"])]
    account = _brokerage(session, manual=False)
    if spec["feed"] is not None:
        # The account's own feed history: its manual period starts after it.
        _feed_row(
            session, account.id, today() - timedelta(days=spec["feed"]), symbols[0], "1", "10.00"
        )
    ledger.update_account(
        session, account_id=account.id, holdings_source="manual", actor_token_id=None
    )
    other = _brokerage(session, manual=False)
    seen: set[tuple[int, int]] = set()
    for index, days_ago, market in spec["implied"]:
        if (index, days_ago) in seen:
            continue
        seen.add((index, days_ago))
        _feed_row(
            session, other.id, today() - timedelta(days=days_ago), symbols[index], "10", market
        )
    for index, days_ago, close, source in spec["closes"]:
        session.merge(
            SecurityPrice(
                symbol=symbols[index],
                price_date=today() - timedelta(days=days_ago),
                source=source,
                close=close,
            )
        )
    session.flush()
    positions = [
        service.create_position(
            session,
            account_ref=str(account.id),
            symbol=s,
            opened_on=today() - timedelta(days=45),
            token=None,
        )
        for s in symbols
    ]
    for index, kind, days_ago, quantity in spec["events"]:
        day = today() - timedelta(days=days_ago)
        extra: dict = {}
        if kind == "buy":
            extra = {"price": Decimal("20")}
            if spec["lots"]:
                extra["lot"] = service.LotSpec()
        elif kind in ("sell", "sell_settles_later"):
            extra = {"price": Decimal("21")}
            if kind == "sell":
                extra["proceeds_to"] = "none"
            else:  # cash that lands later: in transit until it does
                extra["settle_on"] = min(day + timedelta(days=3), today() + timedelta(days=2))
            if spec["lots"]:
                extra["allocation"] = service.AllocationRequest(method="fifo")
        try:
            with session.begin_nested():
                service.record_event(
                    session,
                    position_id=positions[index].id,
                    kind="sell" if kind == "sell_settles_later" else kind,
                    occurred_on=day,
                    quantity=quantity,
                    token=None,
                    **extra,
                )
        except LedgerError:
            continue  # refused: the service keeps only valid histories
    return account, symbols


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(_histories())
def test_bounded_writes_leave_the_full_projection(migrated_engine: Engine, spec: dict) -> None:
    with db.get_sessionmaker()() as session:
        account, symbols = _build(session, spec)
        # Every write above projected from its own date only.
        written = _manual_rows(session, account.id)
        locked = projection.lock_account(session, account.id)
        full, _spans = projection.computed_rows(session, locked, today=today())
        full_books = projection.price_books(session, symbols, upper=today())
        for offset in spec["starts"]:
            start = today() - timedelta(days=offset)
            bounded, _ = projection.computed_rows(session, locked, today=today(), start=start)
            assert bounded == [r for r in full if r.as_of >= start]
            books = projection.price_books(session, symbols, upper=today(), start=start)
            for symbol in symbols:
                for day_offset in range(-1, (today() - start).days + 1):
                    day = start + timedelta(days=day_offset)
                    got = books[symbol].on(day) if symbol in books else None
                    want = full_books[symbol].on(day) if symbol in full_books else None
                    assert got == want, (symbol, day)
            # A projection from the start rewrites nothing.
            assert projection.project_account(
                session, locked, from_date=start, today=today()
            ) == projection.ProjectionResult(None, 0, 0)
        # Regenerated from nothing, the rows are the ones the writes left.
        session.execute(
            delete(HoldingsSnapshot).where(
                HoldingsSnapshot.account_id == account.id, HoldingsSnapshot.source == "manual"
            )
        )
        projection.project_account(session, locked, today=today())
        assert _manual_rows(session, account.id) == written
        session.rollback()


@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(_histories(), st.booleans())
def test_one_position_state_is_the_all_positions_state(
    migrated_engine: Engine, spec: dict, close_one: bool
) -> None:
    with db.get_sessionmaker()() as session:
        account, symbols = _build(session, spec)
        positions = list(
            session.execute(select(Position).where(Position.account_id == account.id)).scalars()
        )
        if close_one:
            for position in positions:
                if service.quantity_on(session, position, today()) == 0:
                    service.update_position(
                        session, position_id=position.id, close=True, token=None
                    )
                    break
        states = {
            s.position.id: s
            for s in service.position_states(
                session, account, include_closed=True, stale_days=7, today=today()
            )
        }
        assert set(states) == {p.id for p in positions}
        for position in positions:
            assert (
                service.position_state(session, position, stale_days=7, today=today())
                == (states[position.id])
            )
        # Today's close from a few rows per symbol is the full history's.
        latest = projection.latest_books(session, symbols, today=today())
        full = projection.price_books(session, symbols, upper=today())
        for symbol in symbols:
            got = latest[symbol].on(today()) if symbol in latest else None
            want = full[symbol].on(today()) if symbol in full else None
            assert got == want
        session.rollback()


def test_a_close_projects_every_holder_with_one_load_and_one_mark(
    migrated_engine: Engine, monkeypatch
) -> None:
    """#404: ``record_price`` re-projects each manual account holding the
    symbol from the close's date with the closes loaded once, and lowers the
    watermark once, after the last projection, to the earliest changed date.
    The rows it leaves are a full regeneration's."""
    from purser import balances
    from purser.holdings import prices

    with db.get_sessionmaker()() as session:
        symbol = _symbol("K")
        accounts = [_brokerage(session, manual=True) for _ in range(3)]
        for days_ago, account in zip((30, 20, 10), accounts, strict=True):
            service.record_position(
                session,
                account_ref=str(account.id),
                symbol=symbol,
                quantity=Decimal("4"),
                as_of=today() - timedelta(days=days_ago),
                token=None,
            )
        loads: list[date | None] = []
        marks: list[date] = []
        real_books = projection.price_books
        real_mark = balances.mark_dirty

        def books(session, symbols, *, upper, start=None):  # type: ignore[no-untyped-def]
            loads.append(start)
            return real_books(session, symbols, upper=upper, start=start)

        def mark(session, day, **kwargs):  # type: ignore[no-untyped-def]
            marks.append(day)
            return real_mark(session, day, **kwargs)

        monkeypatch.setattr(projection, "price_books", books)
        monkeypatch.setattr(balances, "mark_dirty", mark)
        day = today() - timedelta(days=25)
        recorded = prices.record_price(
            session, symbol=symbol, price_date=day, close=Decimal("12.5"), token=None
        )
        assert recorded.accounts == 3
        assert loads == [day] and marks == [day]
        monkeypatch.undo()
        for account in accounts:
            written = _manual_rows(session, account.id)
            session.execute(
                delete(HoldingsSnapshot).where(
                    HoldingsSnapshot.account_id == account.id, HoldingsSnapshot.source == "manual"
                )
            )
            projection.project_account(session, projection.lock_account(session, account.id))
            assert _manual_rows(session, account.id) == written
        session.rollback()
