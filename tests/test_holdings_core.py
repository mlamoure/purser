"""Pure holdings logic (issue #390, decisions 88/89): timelines, average-cost
basis, price resolution (newest date first, source only on a tie) and the
projection rows. No database; property-based where an invariant is involved.

Fixtures are synthetic: TEST / ACME, made-up prices.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from purser.core import holdings as core

D0 = date(2026, 1, 1)
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def ev(
    on: date,
    kind: str,
    quantity: str,
    *,
    price: str | None = None,
    fees: str | None = None,
    cost_basis: str | None = None,
    seq: int = 0,
    reverses: uuid.UUID | None = None,
    replaces: uuid.UUID | None = None,
    event_id: uuid.UUID | None = None,
) -> core.Event:
    return core.Event(
        id=event_id or uuid.uuid4(),
        occurred_on=on,
        created_at=T0 + timedelta(seconds=seq),
        kind=kind,
        quantity=Decimal(quantity),
        price=Decimal(price) if price is not None else None,
        fees=Decimal(fees) if fees is not None else None,
        cost_basis=Decimal(cost_basis) if cost_basis is not None else None,
        reverses_id=reverses,
        replaces_id=replaces,
    )


def reversal_of(event: core.Event, seq: int) -> core.Event:
    return core.Event(
        id=uuid.uuid4(),
        occurred_on=event.occurred_on,
        created_at=T0 + timedelta(seconds=seq),
        kind=event.kind,
        quantity=event.quantity,
        price=event.price,
        fees=event.fees,
        cost_basis=event.cost_basis,
        reverses_id=event.id,
    )


# ---------------------------------------------------------------------------
# Examples
# ---------------------------------------------------------------------------


class TestTimeline:
    def test_set_quantity_sets_count_and_basis(self) -> None:
        points = core.quantity_timeline([ev(D0, "set_quantity", "10", cost_basis="1000.00")])
        assert points == [core.Point(D0, Decimal("10"), Decimal("1000.00"))]

    def test_set_quantity_without_basis_leaves_it_unknown(self) -> None:
        points = core.quantity_timeline([ev(D0, "set_quantity", "10")])
        assert points[-1].basis is None

    def test_a_recount_of_the_same_count_keeps_the_basis(self) -> None:
        """A set_quantity with no cost_basis that re-confirms the count held
        just before keeps the running basis: re-counting is not a purchase."""
        points = core.quantity_timeline(
            [
                ev(D0, "set_quantity", "10", cost_basis="1000.00", seq=1),
                ev(D0 + timedelta(days=1), "buy", "5", price="100", seq=2),
                ev(D0 + timedelta(days=2), "set_quantity", "15", seq=3),
            ]
        )
        assert points[-1] == core.Point(D0 + timedelta(days=2), Decimal("15"), Decimal("1500.00"))

    def test_a_changed_count_without_basis_makes_it_unknown(self) -> None:
        points = core.quantity_timeline(
            [
                ev(D0, "set_quantity", "10", cost_basis="1000.00", seq=1),
                ev(D0 + timedelta(days=1), "set_quantity", "12", seq=2),
            ]
        )
        assert points[-1].quantity == 12 and points[-1].basis is None

    def test_a_count_of_zero_has_a_zero_basis(self) -> None:
        points = core.quantity_timeline(
            [
                ev(D0, "set_quantity", "10", cost_basis="1000.00", seq=1),
                ev(D0 + timedelta(days=1), "set_quantity", "0", seq=2),
                # Re-bought: a new count from zero, no basis given: unknown,
                # never the zero of the sold-out position.
                ev(D0 + timedelta(days=2), "set_quantity", "12", seq=3),
            ]
        )
        assert points[1].basis == 0
        assert points[-1].quantity == 12 and points[-1].basis is None

    def test_a_backdated_recount_keeps_that_days_basis(self) -> None:
        """A recount of an older date keeps the basis held then, so a later
        buy is counted once (a recount carrying TODAY's basis would count it
        twice)."""
        points = core.quantity_timeline(
            [
                ev(D0, "set_quantity", "10", cost_basis="1000.00", seq=1),
                ev(D0 + timedelta(days=10), "buy", "5", price="100", seq=2),
                ev(D0 + timedelta(days=5), "set_quantity", "10", seq=3),
            ]
        )
        assert points[-1] == core.Point(D0 + timedelta(days=10), Decimal("15"), Decimal("1500.00"))

    def test_a_recount_of_an_unknown_basis_stays_unknown(self) -> None:
        points = core.quantity_timeline(
            [
                ev(D0, "set_quantity", "10", seq=1),
                ev(D0 + timedelta(days=1), "set_quantity", "10", seq=2),
            ]
        )
        assert points[-1].basis is None

    def test_buy_adds_quantity_times_price_plus_fees(self) -> None:
        points = core.quantity_timeline(
            [
                ev(D0, "set_quantity", "10", cost_basis="1000.0000", seq=1),
                ev(D0 + timedelta(days=1), "buy", "2.5", price="40.123456", fees="1.25", seq=2),
            ]
        )
        # 1000 + 2.5 * 40.123456 + 1.25 = 1101.55864 -> 1101.5586
        assert points[-1] == core.Point(
            D0 + timedelta(days=1), Decimal("12.5"), Decimal("1101.5586")
        )

    def test_buy_without_price_makes_basis_unknown(self) -> None:
        points = core.quantity_timeline(
            [ev(D0, "set_quantity", "1", cost_basis="5", seq=1), ev(D0, "buy", "1", seq=2)]
        )
        assert points[-1].quantity == 2 and points[-1].basis is None

    def test_sell_reduces_basis_at_average_cost(self) -> None:
        points = core.quantity_timeline(
            [
                ev(D0, "buy", "3", price="10", seq=1),
                ev(D0, "sell", "1", seq=2),
            ]
        )
        # 30.0000 * 1/3 = 10.0000 removed
        assert points[-1] == core.Point(D0, Decimal("2"), Decimal("20.0000"))

    def test_selling_everything_leaves_zero_basis(self) -> None:
        points = core.quantity_timeline(
            [ev(D0, "buy", "3", price="10.333333", seq=1), ev(D0, "sell", "3", seq=2)]
        )
        assert points[-1] == core.Point(D0, Decimal("0"), Decimal("0"))

    def test_a_position_holding_nothing_has_zero_basis_even_when_unknown(self) -> None:
        points = core.quantity_timeline(
            [ev(D0, "set_quantity", "4", seq=1), ev(D0, "sell", "4", seq=2)]
        )
        assert points[-1].basis == 0

    def test_sell_below_zero_names_the_date(self) -> None:
        with pytest.raises(core.NegativeQuantity) as raised:
            core.quantity_timeline(
                [ev(D0, "set_quantity", "1", seq=1), ev(D0 + timedelta(days=3), "sell", "2", seq=2)]
            )
        assert raised.value.on == D0 + timedelta(days=3)
        assert raised.value.quantity == Decimal("-1")

    def test_a_backdated_set_quantity_can_push_a_later_point_negative(self) -> None:
        """Validation is over the whole timeline, not the new event alone."""
        later_sell = ev(D0 + timedelta(days=10), "sell", "5", seq=2)
        history = [ev(D0, "set_quantity", "10", seq=1), later_sell]
        assert core.quantity_timeline(history)[-1].quantity == 5
        backdated = ev(D0 + timedelta(days=5), "set_quantity", "2", seq=3)
        with pytest.raises(core.NegativeQuantity) as raised:
            core.quantity_timeline([*history, backdated])
        assert raised.value.on == later_sell.occurred_on

    def test_order_is_date_then_recorded_time_then_id(self) -> None:
        a = ev(D0, "set_quantity", "1", seq=2)
        b = ev(D0, "set_quantity", "7", seq=1)
        # b was recorded first, so a (recorded later the same day) wins.
        assert core.quantity_timeline([a, b])[-1].quantity == 1

    def test_reversed_events_and_reversals_do_not_count(self) -> None:
        first = ev(D0, "set_quantity", "10", seq=1)
        mistake = ev(D0 + timedelta(days=1), "buy", "5", price="1", seq=2)
        assert core.quantity_timeline([first, mistake, reversal_of(mistake, 3)]) == (
            core.quantity_timeline([first])
        )


class TestPriceResolution:
    def test_newest_date_wins_over_source(self) -> None:
        """Decision 89 (C4): an old manual close never pins a symbol — a
        newer provider close wins, and so does a feed-implied one once the
        manual close is more than FEED_DEFER_DAYS older (decision 94)."""
        newer = D0 + timedelta(days=2)
        points = [
            core.PricePoint(D0, Decimal("100"), "manual"),
            core.PricePoint(newer, Decimal("95"), "tiingo"),
        ]
        assert core.price_on(points, newer) == core.PricePoint(newer, Decimal("95"), "tiingo")
        far = D0 + timedelta(days=core.FEED_DEFER_DAYS + 1)
        points = [
            core.PricePoint(D0, Decimal("100"), "manual"),
            core.PricePoint(far, Decimal("90"), "feed"),
        ]
        assert core.price_on(points, far) == core.PricePoint(far, Decimal("90"), "feed")

    def test_source_breaks_a_same_date_tie(self) -> None:
        points = [
            core.PricePoint(D0, Decimal("3"), "feed"),
            core.PricePoint(D0, Decimal("2"), "tiingo"),
            core.PricePoint(D0, Decimal("1"), "manual"),
        ]
        assert core.price_on(points, D0) == core.PricePoint(D0, Decimal("1"), "manual")
        assert core.price_on(points[:2], D0) == core.PricePoint(D0, Decimal("2"), "tiingo")

    def test_nothing_before_the_first_close(self) -> None:
        assert (
            core.price_on([core.PricePoint(D0, Decimal("1"), "manual")], D0 - timedelta(1)) is None
        )

    def test_staleness_threshold_is_strict(self) -> None:
        assert not core.stale(D0, D0 + timedelta(days=7), 7)
        assert core.stale(D0, D0 + timedelta(days=8), 7)
        assert not core.stale(None, D0, 7)


class TestValidators:
    def test_symbol_normalizes_and_refuses_junk(self) -> None:
        assert core.normalize_symbol(" brk.b ") == "BRK.B"
        for bad in ("", "-X", "TOO_LONG_SYMBOL_X", "A B", "ACME!"):
            with pytest.raises(ValueError):
                core.normalize_symbol(bad)

    def test_quantity_places_and_sign(self) -> None:
        assert core.check_quantity(Decimal("1.123456")) == Decimal("1.123456")
        with pytest.raises(ValueError, match="6 decimal places"):
            core.check_quantity(Decimal("1.1234567"))
        with pytest.raises(ValueError, match="negative"):
            core.check_quantity(Decimal("-1"))
        with pytest.raises(ValueError, match="out of range"):
            core.check_quantity(Decimal("1e14"))

    def test_price_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="greater than zero"):
            core.check_price(Decimal("0"))

    def test_money_places(self) -> None:
        with pytest.raises(ValueError, match="4 decimal places"):
            core.check_money(Decimal("1.00001"), "fees")


class TestProjectionRows:
    def test_every_change_date_carries_every_held_position(self) -> None:
        acme = core.PositionFacts(uuid.uuid4(), "ACME", [ev(D0, "set_quantity", "1", seq=1)])
        test = core.PositionFacts(
            uuid.uuid4(), "TEST", [ev(D0 + timedelta(days=2), "set_quantity", "3", seq=2)]
        )
        books = {"ACME": core.PriceBook([core.PricePoint(D0, Decimal("10"), "manual")])}
        rows = core.projection_rows([acme, test], books, lower=D0, upper=D0 + timedelta(days=4))
        by_date: dict[date, set[str]] = {}
        for row in rows:
            by_date.setdefault(row.as_of, set()).add(row.symbol)
        assert by_date == {D0: {"ACME"}, D0 + timedelta(days=2): {"ACME", "TEST"}}
        unpriced = next(r for r in rows if r.symbol == "TEST")
        assert unpriced.market_value is None and unpriced.price is None

    def test_a_sell_out_writes_a_tombstone_then_nothing(self) -> None:
        position = core.PositionFacts(
            uuid.uuid4(),
            "ACME",
            [ev(D0, "buy", "2", price="5", seq=1), ev(D0 + timedelta(days=1), "sell", "2", seq=2)],
        )
        books = {
            "ACME": core.PriceBook(
                [core.PricePoint(D0 + timedelta(days=n), Decimal("5"), "tiingo") for n in range(5)]
            )
        }
        rows = core.projection_rows([position], books, lower=D0, upper=D0 + timedelta(days=4))
        assert [(r.as_of, r.quantity, r.market_value) for r in rows] == [
            (D0, Decimal("2"), Decimal("10.0000")),
            (D0 + timedelta(days=1), Decimal("0"), Decimal("0")),
        ]

    def test_price_dates_only_count_while_held(self) -> None:
        position = core.PositionFacts(
            uuid.uuid4(), "ACME", [ev(D0 + timedelta(days=3), "set_quantity", "1", seq=1)]
        )
        books = {
            "ACME": core.PriceBook(
                [
                    core.PricePoint(D0 + timedelta(days=n), Decimal(n + 1), "tiingo")
                    for n in range(6)
                ]
            )
        }
        rows = core.projection_rows(
            [position], books, lower=D0 + timedelta(days=3), upper=D0 + timedelta(days=5)
        )
        assert [(r.as_of, r.price, r.price_as_of) for r in rows] == [
            (D0 + timedelta(days=n), Decimal(n + 1), D0 + timedelta(days=n)) for n in (3, 4, 5)
        ]

    def test_the_first_set_names_known_positions_even_at_zero(self) -> None:
        """At a lower bound past the history's start (the day after the last
        feed snapshot), a position that holds nothing still gets a zero row,
        so the manual set replaces the feed set before it."""
        position = core.PositionFacts(
            uuid.uuid4(),
            "ACME",
            [ev(D0, "set_quantity", "1", seq=1), ev(D0 + timedelta(days=1), "sell", "1", seq=2)],
        )
        rows = core.projection_rows(
            [position], {}, lower=D0 + timedelta(days=5), upper=D0 + timedelta(days=6)
        )
        assert [(r.as_of, r.quantity) for r in rows] == [(D0 + timedelta(days=5), Decimal("0"))]

    def test_a_date_carries_one_row_per_symbol(self) -> None:
        """A closed position and a later one in the same symbol: the set at
        lower (past both starts) writes the one holding shares, not a zero row
        beside it; with none holding, one zero row."""
        closed = core.PositionFacts(
            uuid.UUID(int=1),
            "ACME",
            [
                ev(D0, "set_quantity", "5", seq=1),
                ev(D0 + timedelta(days=2), "set_quantity", "0", seq=2),
            ],
        )
        later = core.PositionFacts(
            uuid.UUID(int=2), "ACME", [ev(D0 + timedelta(days=5), "set_quantity", "3", seq=3)]
        )
        lower = D0 + timedelta(days=10)
        rows = core.projection_rows([closed, later], {}, lower=lower, upper=lower)
        assert [(r.symbol, r.position_id, r.quantity) for r in rows] == [
            ("ACME", uuid.UUID(int=2), Decimal("3"))
        ]
        sold = core.PositionFacts(
            uuid.UUID(int=3),
            "ACME",
            [
                ev(D0 + timedelta(days=5), "set_quantity", "3", seq=3),
                ev(D0 + timedelta(days=7), "set_quantity", "0", seq=4),
            ],
        )
        rows = core.projection_rows([closed, sold], {}, lower=lower, upper=lower)
        assert [(r.symbol, r.quantity) for r in rows] == [("ACME", Decimal("0"))]

    def test_a_value_that_does_not_fit_keeps_its_close_and_has_no_value(self) -> None:
        """Never a failure of the whole account (decision 89): the row keeps
        the close (so readers tell it from a missing price: the share count
        is what to check) and has no value; the other keeps its value."""
        big = core.PositionFacts(
            uuid.UUID(int=1), "ACME", [ev(D0, "set_quantity", "99999999999999", seq=1)]
        )
        small = core.PositionFacts(uuid.UUID(int=2), "TEST", [ev(D0, "set_quantity", "2", seq=2)])
        books = {
            "ACME": core.PriceBook([core.PricePoint(D0, Decimal("99999999"), "manual")]),
            "TEST": core.PriceBook([core.PricePoint(D0, Decimal("5"), "manual")]),
        }
        rows = core.projection_rows([big, small], books, lower=D0, upper=D0)
        assert [(r.symbol, r.market_value, r.price) for r in rows] == [
            ("ACME", None, Decimal("99999999")),
            ("TEST", Decimal("10.0000"), Decimal("5")),
        ]
        worth = core.valued(Decimal("99999999999999"), books["ACME"].on(D0))
        assert worth.out_of_range and worth.value is None
        assert not core.valued(Decimal("1"), None).out_of_range

    def test_a_close_out_of_range_never_enters_valuation(self) -> None:
        """A feed-implied close (dust shares, a large value) can be absurd:
        the book leaves it out, so the date before it stays in effect."""
        book = core.PriceBook(
            [
                core.PricePoint(D0, Decimal("12.5"), "tiingo"),
                core.PricePoint(D0 + timedelta(days=1), Decimal("1E+15"), "feed"),
                core.PricePoint(D0 + timedelta(days=2), Decimal("0"), "feed"),
            ]
        )
        point = book.on(D0 + timedelta(days=2))
        assert point is not None and point.close == Decimal("12.5")


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

_qty = st.decimals(min_value=Decimal("0"), max_value=Decimal("1000"), places=6, allow_nan=False)
_pos_qty = _qty.filter(lambda q: q > 0)
_price = st.decimals(
    min_value=Decimal("0.000001"), max_value=Decimal("5000"), places=6, allow_nan=False
)
_money = st.decimals(min_value=Decimal("0"), max_value=Decimal("100000"), places=4, allow_nan=False)


@st.composite
def event_lists(draw: st.DrawFn, max_size: int = 12) -> list[core.Event]:
    n = draw(st.integers(min_value=1, max_value=max_size))
    out: list[core.Event] = []
    for seq in range(n):
        kind = draw(st.sampled_from(core.KINDS))
        on = D0 + timedelta(days=draw(st.integers(min_value=0, max_value=30)))
        if kind == core.SET_QUANTITY:
            basis = draw(st.none() | _money)
            out.append(
                ev(
                    on,
                    kind,
                    str(draw(_qty)),
                    cost_basis=None if basis is None else str(basis),
                    seq=seq,
                )
            )
        else:
            price = draw(st.none() | _price)
            fees = draw(st.none() | _money)
            out.append(
                ev(
                    on,
                    kind,
                    str(draw(_pos_qty)),
                    price=None if price is None else str(price),
                    fees=None if fees is None else str(fees),
                    seq=seq,
                )
            )
    return out


def _accepted(candidates: list[core.Event]) -> list[core.Event]:
    """What the service would keep: each event in turn, refused when the
    whole timeline with it goes negative anywhere."""
    kept: list[core.Event] = []
    for event in candidates:
        try:
            core.quantity_timeline([*kept, event])
        except core.NegativeQuantity:
            continue
        kept.append(event)
    return kept


@settings(max_examples=200, deadline=None)
@given(event_lists())
def test_accepted_histories_never_go_negative(candidates: list[core.Event]) -> None:
    for point in core.quantity_timeline(_accepted(candidates)):
        assert point.quantity >= 0
        assert point.basis is None or point.basis >= 0


@settings(max_examples=200, deadline=None)
@given(event_lists())
def test_a_refused_history_names_its_first_negative_date(events: list[core.Event]) -> None:
    """Either every point is >= 0, or NegativeQuantity names exactly the
    first event whose running count drops below zero."""
    running = Decimal(0)
    first_negative: date | None = None
    for event in core.effective_events(events):
        if event.kind == core.SET_QUANTITY:
            running = event.quantity
        elif event.kind == core.BUY:
            running += event.quantity
        else:
            running -= event.quantity
        if running < 0:
            first_negative = event.occurred_on
            break
    if first_negative is None:
        assert all(p.quantity >= 0 for p in core.quantity_timeline(events))
    else:
        with pytest.raises(core.NegativeQuantity) as raised:
            core.quantity_timeline(events)
        assert raised.value.on == first_negative


@settings(max_examples=200, deadline=None)
@given(event_lists(), st.data())
def test_reversing_an_event_restores_the_timeline_without_it(
    candidates: list[core.Event], data: st.DataObject
) -> None:
    kept = _accepted(candidates)
    assume(kept)
    target = data.draw(st.sampled_from(kept))
    without = [e for e in kept if e.id != target.id]
    try:
        expected = core.quantity_timeline(without)
    except core.NegativeQuantity:
        # Removing it would itself go negative: the service refuses that
        # reversal, so there is nothing to restore.
        return
    assert core.quantity_timeline([*kept, reversal_of(target, 10_000)]) == expected


@settings(max_examples=200, deadline=None)
@given(
    st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=20),
            st.sampled_from(["manual", "tiingo", "feed"]),
            _price,
        ),
        max_size=15,
    ),
    st.integers(min_value=-2, max_value=25),
)
def test_price_on_is_newest_date_then_source(
    raw: list[tuple[int, str, Decimal]], offset: int
) -> None:
    """Against a reference model of decisions 89 and 94: an unchanged
    feed-implied close keeps its first day; the newest date wins, the source
    breaks a same-date tie; a feed-implied winner yields to a manual or
    provider close at most FEED_DEFER_DAYS older."""
    points = [core.PricePoint(D0 + timedelta(days=d), close, source) for d, source, close in raw]
    day = D0 + timedelta(days=offset)

    def key(p: core.PricePoint) -> tuple[int, str, Decimal]:
        return (core.source_rank(p.source), p.source, p.close)

    def newest_best(pool: list[core.PricePoint]) -> core.PricePoint | None:
        if not pool:
            return None
        newest = max(p.price_date for p in pool)
        return min((p for p in pool if p.price_date == newest), key=key)

    valid = [p for p in points if 0 < p.close < core.MAX_PRICE]
    kept, last = [], None
    for p in sorted(valid, key=lambda p: p.price_date):
        if p.source == "feed":
            if p.close == last:
                continue
            last = p.close
        kept.append(p)
    eligible = [p for p in kept if p.price_date <= day]
    expected = newest_best(eligible)
    if expected is not None and expected.source == "feed":
        real = newest_best([p for p in eligible if p.source != "feed"])
        if (
            real is not None
            and (expected.price_date - real.price_date).days <= core.FEED_DEFER_DAYS
        ):
            expected = real
    assert core.price_on(points, day) == expected


@settings(max_examples=200, deadline=None)
@given(
    _pos_qty,
    _price,
    st.lists(st.decimals(min_value=Decimal("0.01"), max_value=Decimal("1"), places=2), max_size=8),
)
def test_average_cost_basis_is_conserved(
    bought: Decimal, price: Decimal, fractions: list[Decimal]
) -> None:
    """Selling at average cost never creates or destroys basis: the removed
    amounts plus what remains always equal the basis bought, each removal is
    the proportional share to within rounding, and selling out leaves zero."""
    events = [ev(D0, "buy", str(bought), price=str(price), seq=0)]
    held = bought
    for i, fraction in enumerate(fractions, start=1):
        sold = (held * fraction).quantize(core.QUANTITY_QUANT)
        if sold <= 0 or sold > held:
            continue
        events.append(ev(D0 + timedelta(days=i), "sell", str(sold), seq=i))
        held -= sold
    points = core.quantity_timeline(events)
    initial = points[0].basis
    assert initial is not None
    removed = Decimal(0)
    for before, after, event in zip(points[:-1], points[1:], events[1:], strict=True):
        assert before.basis is not None and after.basis is not None
        step = before.basis - after.basis
        exact = before.basis * event.quantity / before.quantity
        assert abs(step - exact) <= Decimal("0.00005")
        assert after.basis >= 0
        removed += step
    final = points[-1]
    assert final.basis is not None
    assert removed + final.basis == initial
    if final.quantity == 0:
        assert final.basis == 0


@settings(max_examples=100, deadline=None)
@given(st.lists(event_lists(max_size=6), min_size=1, max_size=3), st.data())
def test_projection_rows_are_deterministic_and_complete(
    histories: list[list[core.Event]], data: st.DataObject
) -> None:
    positions = [
        core.PositionFacts(uuid.uuid4(), symbol, _accepted(events))
        for symbol, events in zip(("ACME", "TEST", "SYNT"), histories, strict=False)
    ]
    positions = [p for p in positions if p.events]
    if not positions:
        return
    closes = data.draw(st.lists(st.tuples(st.integers(0, 35), _price), max_size=6))
    books = {
        p.symbol: core.PriceBook(
            core.PricePoint(D0 + timedelta(days=d), c, "tiingo") for d, c in closes
        )
        for p in positions
    }
    lower = core.first_event_date(positions)
    assert lower is not None
    upper = D0 + timedelta(days=40)
    rows = core.projection_rows(positions, books, lower=lower, upper=upper)
    assert rows == core.projection_rows(list(reversed(positions)), books, lower=lower, upper=upper)
    timelines = {p.id: core.Timeline(core.quantity_timeline(p.events)) for p in positions}
    dates = sorted({r.as_of for r in rows})
    for day in dates:
        on_day = {r.position_id: r for r in rows if r.as_of == day}
        for p in positions:
            held = timelines[p.id].quantity_on(day)
            if held > 0:
                assert on_day[p.id].quantity == held
    # Between two written dates nothing a reader sees changes: the newest set
    # at or before any day equals the timeline on that day.
    for offset in range((upper - lower).days + 1):
        day = lower + timedelta(days=offset)
        written = [d for d in dates if d <= day]
        if not written:
            continue
        latest = {r.position_id: r.quantity for r in rows if r.as_of == written[-1]}
        for p in positions:
            assert latest.get(p.id, Decimal(0)) == timelines[p.id].quantity_on(day)


# --------------------------------------------------------------------------
# fetch_windows: what the price job asks a provider for (decision 89)
# --------------------------------------------------------------------------

_T = date(2026, 10, 15)


def _ago(days: int) -> date:
    return _T - timedelta(days=days)


_WEEK = timedelta(days=7)


def _windows(spans: list, answered: list | None = None) -> list[tuple[date, date]]:
    return core.fetch_windows(core.needed_ranges(spans), answered or [])


@pytest.mark.parametrize(
    ("case", "spans", "answered", "windows"),
    [
        (
            "nothing answered: the whole span, from a week before the first day",
            [(_ago(10), _T)],
            [],
            [(_ago(10) - _WEEK, _T)],
        ),
        ("caught up: nothing", [(_ago(10), _T)], [(_ago(17), _T)], []),
        (
            "the tail after what was answered",
            [(_ago(10), _T)],
            [(_ago(17), _ago(3))],
            [(_ago(2), _T)],
        ),
        (
            "history grew earlier: the head gap, from a week before the new first day",
            [(_ago(30), _T)],
            [(_ago(17), _T)],
            [(_ago(30) - _WEEK, _ago(18))],
        ),
        (
            "head and tail: the newest first, so a failing head never starves it",
            [(_ago(30), _T)],
            [(_ago(17), _ago(1))],
            [(_T, _T), (_ago(30) - _WEEK, _ago(18))],
        ),
        (
            "an island of old closes covers its own dates only",
            [(_ago(10), _T), (_ago(100), _T)],
            [(_ago(307), _ago(291)), (_ago(17), _T)],
            [(_ago(100) - _WEEK, _ago(18))],
        ),
        (
            "a weekend start after an island still gets its week before",
            [(_ago(20), _T)],
            [(_ago(60), _ago(40))],
            [(_ago(27), _T)],
        ),
        (
            "two positions apart: the dates between them are not needed",
            [(_ago(100), _ago(90)), (_ago(10), _T)],
            [],
            [(_ago(17), _T), (_ago(107), _ago(90))],
        ),
        (
            "a gap between two answered ranges is asked",
            [(_ago(60), _T)],
            [(_ago(67), _ago(40)), (_ago(20), _T)],
            [(_ago(39), _ago(21))],
        ),
        (
            "an unknown ticker answered once: nothing",
            [(_ago(30), _ago(10))],
            [(_ago(37), _ago(10))],
            [],
        ),
        (
            "history that grows later than what was answered: the new tail",
            [(_ago(30), _ago(2))],
            [(_ago(37), _ago(10))],
            [(_ago(9), _ago(2))],
        ),
    ],
)
def test_fetch_windows(case: str, spans: list, answered: list, windows: list) -> None:
    assert _windows(spans, answered) == windows, case


def test_the_week_before_a_first_day_stops_at_the_first_date() -> None:
    """A position dated in the first days of year 1 cannot overflow."""
    first = date.min + timedelta(days=2)
    assert core.needed_ranges([(first, first)]) == [(date.min, first)]


@pytest.mark.parametrize(
    ("case", "window", "newest", "through"),
    [
        ("settled: the whole window", (_ago(20), _ago(3)), None, _ago(3)),
        ("recent and empty: up to the settled boundary", (_ago(20), _T), None, _ago(3)),
        ("recent with closes: up to the newest close", (_ago(20), _T), _ago(1), _ago(1)),
        ("all too recent and empty: nothing", (_ago(2), _T), None, None),
        ("never past the window", (_ago(20), _ago(1)), _T, _ago(1)),
    ],
)
def test_answered_through(
    case: str, window: tuple, newest: date | None, through: date | None
) -> None:
    assert core.answered_through(window[0], window[1], newest, _T) == through, case


def test_an_answer_is_final_three_days_back() -> None:
    assert core.settled(_ago(3), _T)
    assert not core.settled(_ago(2), _T) and not core.settled(_T, _T)


@pytest.mark.parametrize(
    ("case", "events", "upper", "span"),
    [
        ("held to the bound", [(30, "10")], _T, (_ago(30), _T)),
        ("sold out: the day before", [(30, "10"), (10, "0")], _T, (_ago(30), _ago(11))),
        ("a closed account: its close date", [(30, "10")], _ago(5), (_ago(30), _ago(5))),
        ("bought and sold the same day: nothing", [(10, "5"), (10, "0")], _T, None),
        ("re-bought: one range to today", [(30, "1"), (20, "0"), (10, "2")], _T, (_ago(30), _T)),
        ("opened empty: from the first day it holds", [(30, "0"), (20, "3")], _T, (_ago(20), _T)),
        ("after the bound: nothing", [(3, "1")], _ago(5), None),
    ],
)
def test_held_span(case: str, events: list, upper: date, span: tuple | None) -> None:
    points = core.quantity_timeline(
        [ev(_ago(days), core.SET_QUANTITY, qty, seq=n) for n, (days, qty) in enumerate(events)]
    )
    assert core.held_span(points, upper) == span, case


# --------------------------------------------------------------------------
# Ranges: what an event, a sum of events and a value may reach
# --------------------------------------------------------------------------


@pytest.mark.parametrize("check", [core.check_quantity, core.check_price])
def test_a_huge_exponent_is_out_of_range_not_a_decimal_overflow(check) -> None:
    """Decimal("1e1000000") overflows the default context when compared: the
    validators turn that into their own out-of-range error."""
    with pytest.raises(ValueError, match="out of range"):
        check(Decimal("1e1000000"))


def test_a_huge_exponent_amount_is_out_of_range_too() -> None:
    with pytest.raises(ValueError, match="out of range"):
        core.check_money(Decimal("1e1000000"), "cost_basis")


def test_a_share_count_that_sums_past_the_column_is_out_of_range() -> None:
    big = "99999999999999"  # one event's largest whole count
    events = [ev(D0, core.SET_QUANTITY, big, seq=1), ev(D0, core.BUY, big, seq=2)]
    with pytest.raises(core.OutOfRange, match="share count"):
        core.quantity_timeline(events)


def test_a_cost_basis_that_sums_past_the_column_is_out_of_range() -> None:
    events = [
        ev(D0, core.SET_QUANTITY, "0", seq=1),
        ev(D0, core.BUY, "10000000", price="1500000000", seq=2),
    ]
    with pytest.raises(core.OutOfRange, match="cost basis"):
        core.quantity_timeline(events)


@pytest.mark.parametrize(
    ("case", "events", "upper", "span"),
    [
        ("held to the bound", [(30, "10")], _T, (_ago(30), _T)),
        ("sold out: through the sell-out day", [(30, "10"), (10, "0")], _T, (_ago(30), _ago(10))),
        (
            "a later re-confirmed zero adds nothing",
            [(40, "10"), (30, "0"), (10, "0")],
            _T,
            (_ago(40), _ago(30)),
        ),
        ("never held: nothing", [(30, "0")], _T, None),
    ],
)
def test_occupied_span(case: str, events: list, upper: date, span: tuple | None) -> None:
    points = core.quantity_timeline(
        [ev(_ago(days), core.SET_QUANTITY, qty, seq=n) for n, (days, qty) in enumerate(events)]
    )
    assert core.occupied_span(points, upper) == span, case


# --------------------------------------------------------------------------
# feed_value: a feed row the feed values at 0, valued from a stored close
# (#392, decision 92)
# --------------------------------------------------------------------------

_any_quantity = st.none() | st.decimals(
    min_value=Decimal("-1000000"), max_value=Decimal("99999999999999"), places=6, allow_nan=False
)
_stored_value = st.decimals(
    min_value=Decimal("-9999999999999999.9999"),
    max_value=Decimal("9999999999999999.9999"),
    places=4,
    allow_nan=False,
)
_close = st.decimals(
    min_value=Decimal("0.000001"), max_value=Decimal("99999999999999.999999"), places=6
)
_feed_quantity = st.decimals(
    min_value=Decimal("0.000001"), max_value=Decimal("99999999999999.999999"), places=6
)


@settings(max_examples=300, deadline=None)
@given(_any_quantity, _stored_value, _close)
def test_feed_value_never_changes_a_row_the_feed_valued(
    quantity: Decimal | None, stored: Decimal, close: Decimal
) -> None:
    """A non-zero feed value is the feed's own, never overridden, whatever
    the stored closes say."""
    assume(stored != 0)
    book = core.PriceBook([core.PricePoint(D0, close, core.SOURCE_MANUAL)])
    assert core.feed_value(quantity, stored, book, D0 + timedelta(days=3)) == (stored, None)


@settings(max_examples=300, deadline=None)
@given(_feed_quantity, _close, st.integers(min_value=0, max_value=10))
def test_feed_value_of_a_zero_row_is_quantity_times_the_close(
    quantity: Decimal, close: Decimal, days_after: int
) -> None:
    """quantity x the close in effect, quantized like every market value
    (core.market_value); when it does not fit an amount, the close stays
    and there is no value (value_out_of_range, as core.valued)."""
    point = core.PricePoint(D0, close, "tiingo")
    value, used = core.feed_value(
        quantity, Decimal("0.0000"), core.PriceBook([point]), D0 + timedelta(days=days_after)
    )
    with localcontext() as ctx:
        ctx.prec = 60  # exact: NUMERIC(20,6) x NUMERIC(20,6) needs up to 40 digits
        expected = (quantity * close).quantize(core.MONEY_QUANT)  # half to even
    if expected < core.MAX_MONEY:
        assert used == point
        assert value == core.market_value(quantity, close) == expected
    else:
        assert (value, used) == (None, point)


def test_feed_value_needs_a_close_on_or_before_the_rows_date() -> None:
    book = core.PriceBook([core.PricePoint(D0 + timedelta(days=1), Decimal("5"), "manual")])
    assert core.feed_value(Decimal("3"), Decimal("0"), book, D0) == (None, None)
    assert core.feed_value(Decimal("3"), Decimal("0"), None, D0) == (None, None)
    value, used = core.feed_value(Decimal("3"), Decimal("0"), book, D0 + timedelta(days=1))
    assert value == Decimal("15.0000") and used is not None and used.source == "manual"
    # Holding nothing, or no stored value at all: not a fund the feed values at 0.
    assert core.feed_value(Decimal("0"), Decimal("0"), book, D0) == (Decimal("0"), None)
    assert core.feed_value(Decimal("3"), None, book, D0) == (None, None)


class TestReplacement:
    """#401: a correction recorded with ``replaces`` takes the reversed
    event's place in its day, so fixing one event never reorders the day."""

    def test_a_replacement_keeps_the_original_slot(self) -> None:
        opening = ev(D0, "set_quantity", "100", cost_basis="1000", seq=1)
        buy = ev(D0, "buy", "10", price="12", seq=2)
        undone = reversal_of(opening, seq=3)
        fixed = ev(D0, "set_quantity", "100", cost_basis="900", seq=4, replaces=opening.id)
        final = core.quantity_timeline([opening, buy, undone, fixed])[-1]
        assert (final.quantity, final.basis) == (Decimal("110"), Decimal("1020.0000"))

    def test_without_replaces_the_new_event_goes_last(self) -> None:
        """Unchanged behaviour for a plain re-record: it orders after the
        day's live events (what #401 reports, now a choice)."""
        opening = ev(D0, "set_quantity", "100", cost_basis="1000", seq=1)
        buy = ev(D0, "buy", "10", price="12", seq=2)
        undone = reversal_of(opening, seq=3)
        again = ev(D0, "set_quantity", "100", cost_basis="900", seq=4)
        final = core.quantity_timeline([opening, buy, undone, again])[-1]
        assert (final.quantity, final.basis) == (Decimal("100"), Decimal("900"))

    def test_a_chain_of_replacements_keeps_the_first_slot(self) -> None:
        opening = ev(D0, "set_quantity", "100", cost_basis="1000", seq=1)
        buy = ev(D0, "buy", "10", price="12", seq=2)
        first_fix = ev(D0, "set_quantity", "100", cost_basis="950", seq=4, replaces=opening.id)
        second_fix = ev(D0, "set_quantity", "100", cost_basis="900", seq=6, replaces=first_fix.id)
        history = [
            opening,
            buy,
            reversal_of(opening, seq=3),
            first_fix,
            reversal_of(first_fix, seq=5),
            second_fix,
        ]
        final = core.quantity_timeline(history)[-1]
        assert (final.quantity, final.basis) == (Decimal("110"), Decimal("1020.0000"))

    @settings(max_examples=60, deadline=None)
    @given(
        quantities=st.lists(st.integers(min_value=1, max_value=50), min_size=2, max_size=6),
        target=st.integers(min_value=0, max_value=5),
    )
    def test_reversing_and_replacing_with_an_equal_event_changes_nothing(
        self, quantities: list[int], target: int
    ) -> None:
        """Property: replacing any event of a day with an identical copy
        leaves every point of the timeline as it was."""
        assume(target < len(quantities))
        day = [
            ev(D0, "set_quantity" if i == 0 else "buy", str(q), price="10", seq=i)
            for i, q in enumerate(quantities)
        ]
        before = core.quantity_timeline(day)
        original = day[target]
        copy = core.Event(
            id=uuid.uuid4(),
            occurred_on=original.occurred_on,
            created_at=T0 + timedelta(seconds=100),
            kind=original.kind,
            quantity=original.quantity,
            price=original.price,
            fees=original.fees,
            cost_basis=original.cost_basis,
            replaces_id=original.id,
        )
        after = core.quantity_timeline([*day, reversal_of(original, seq=99), copy])
        assert after == before


class TestFeedImpliedCloses:
    """#406: a feed-implied close (market value / quantity of a feed set) is
    dated by the fetch day, so it used to outrank a provider's or the user's
    close of the latest session, and a frozen bank value never turned
    stale."""

    @staticmethod
    def p(day: date, close: str, source: str) -> core.PricePoint:
        return core.PricePoint(day, Decimal(close), source)

    def test_a_current_provider_or_manual_close_outranks_a_newer_feed_one(self) -> None:
        for source in ("tiingo", core.SOURCE_MANUAL):
            book = core.PriceBook(
                [self.p(D0, "10", source), self.p(D0 + timedelta(days=1), "11", core.SOURCE_FEED)]
            )
            point = book.on(D0 + timedelta(days=1))
            assert point is not None and (point.source, point.close) == (source, Decimal("10"))

    def test_a_feed_close_takes_over_once_the_real_one_is_out_of_date(self) -> None:
        later = D0 + timedelta(days=core.FEED_DEFER_DAYS + 1)
        book = core.PriceBook([self.p(D0, "10", "tiingo"), self.p(later, "12", core.SOURCE_FEED)])
        point = book.on(later)
        assert point is not None and point.source == core.SOURCE_FEED

    def test_a_feed_only_symbol_keeps_its_feed_close(self) -> None:
        book = core.PriceBook([self.p(D0, "10", core.SOURCE_FEED)])
        point = book.on(D0)
        assert point is not None and point.close == Decimal("10")

    def test_an_unchanged_feed_close_keeps_the_day_it_first_appeared(self) -> None:
        """A weekend repeats Friday's value; a frozen bank repeats it for
        weeks. Either way the close is dated when it first appeared, so a
        frozen one ages and turns stale like any other."""
        days = [D0 + timedelta(days=n) for n in range(12)]
        book = core.PriceBook(
            [self.p(d, "10", core.SOURCE_FEED) for d in days]
            + [self.p(D0 + timedelta(days=12), "10.5", core.SOURCE_FEED)]
        )
        frozen = book.on(days[-1])
        assert frozen is not None and frozen.price_date == D0
        moved = book.on(D0 + timedelta(days=12))
        assert moved is not None and moved.price_date == D0 + timedelta(days=12)
        assert book.dates_between(D0, D0 + timedelta(days=12)) == [D0, D0 + timedelta(days=12)]
