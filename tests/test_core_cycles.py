"""Statement-cycle math (#674, decision 136): pure, no database.

Synthetic cards only. Card history is in ledger sign (a charge negative, a
payment or refund positive); every figure the module returns is owed.
"""

from datetime import date, timedelta
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from purser.core import cycles

D = Decimal


def _history(
    rows: list[tuple[date, str, bool]], *, opening: str = "0", start: date | None = None
) -> cycles.CardHistory:
    parsed = tuple(sorted((d, D(a), pay) for d, a, pay in rows))
    first = parsed[0][0] if parsed else None
    return cycles.CardHistory(
        start=start or (first or date(2025, 1, 1)),
        opening=D(opening),
        rows=parsed,
        first_activity=first,
    )


def _plan(**overrides: object) -> cycles.CyclePlan:
    base: dict[str, object] = {
        "close_day": 15,
        "payment_amount": cycles.PAYMENT_STATEMENT_BALANCE,
        "later_estimate": cycles.LATER_STATEMENT_AVERAGE,
        "later_window": 6,
        "fixed_amount": None,
        "fallback": D("0"),
    }
    base.update(overrides)
    return cycles.CyclePlan(**base)  # type: ignore[arg-type]


class TestCloseDates:
    def test_a_short_month_closes_on_its_last_day(self) -> None:
        assert cycles.close_date(2027, 2, 31) == date(2027, 2, 28)
        assert cycles.close_date(2028, 2, 30) == date(2028, 2, 29)
        assert cycles.close_date(2027, 4, 31) == date(2027, 4, 30)

    def test_last_close_on_or_before(self) -> None:
        assert cycles.last_close_on_or_before(date(2027, 3, 15), 15) == date(2027, 3, 15)
        assert cycles.last_close_on_or_before(date(2027, 3, 14), 15) == date(2027, 2, 15)
        assert cycles.last_close_on_or_before(date(2027, 1, 3), 20) == date(2026, 12, 20)

    def test_the_statement_a_payment_pays_falls_between_payments(self) -> None:
        # Close on the 15th, payment due on the 10th of the next month.
        assert cycles.statement_close_for(date(2027, 4, 10), date(2027, 3, 10), 15) == date(
            2027, 3, 15
        )
        # Two payments in one cycle: the second has no statement of its own.
        assert cycles.statement_close_for(date(2027, 4, 14), date(2027, 4, 10), 15) is None


class TestRemainingStatement:
    def test_credits_after_the_close_reduce_the_payment(self) -> None:
        history = _history(
            [
                (date(2027, 3, 2), "-400.00", False),
                (date(2027, 3, 9), "-600.00", False),
                # After the 3/15 close: a refund and new spending.
                (date(2027, 3, 20), "250.00", False),
                (date(2027, 3, 22), "-80.00", False),
            ]
        )
        assert history.owed_at(date(2027, 3, 15)) == D("1000.00")
        assert cycles.remaining_statement(history, date(2027, 3, 15), date(2027, 3, 25)) == D(
            "750.0000"
        )

    def test_a_payment_already_made_leaves_nothing(self) -> None:
        history = _history(
            [(date(2027, 3, 2), "-300.00", False), (date(2027, 4, 1), "300.00", True)]
        )
        assert cycles.remaining_statement(history, date(2027, 3, 15), date(2027, 4, 2)) == 0


class TestPlan:
    def _card(self) -> cycles.CardHistory:
        """Six months of a card closing on the 15th, about 1,000 a month,
        each statement paid in full on the 10th."""
        rows: list[tuple[date, str, bool]] = []
        for month, spend in zip(
            range(1, 7), ["900", "1100", "1000", "1300", "800", "900"], strict=True
        ):
            rows.append((date(2027, month, 5), f"-{spend}.00", False))
            if month > 1:
                previous = ["900", "1100", "1000", "1300", "800", "900"][month - 2]
                rows.append((date(2027, month, 10), f"{previous}.00", True))
        rows.append((date(2027, 6, 20), "-200.00", False))  # spending after the 6/15 close
        return _history(rows)

    def test_closed_projected_and_later_payments(self) -> None:
        history = self._card()
        today = date(2027, 6, 25)
        slots = [
            (date(2027, 7, 10), date(2027, 6, 10)),
            (date(2027, 8, 10), date(2027, 7, 10)),
            (date(2027, 9, 10), date(2027, 8, 10)),
        ]
        figures = cycles.plan_payments(
            slots,
            plan=_plan(),
            history=history,
            current_owed=history.owed_at(today),
            today=today,
        )
        july, august, september = (figures[s] for s, _ in slots)
        # The 6/15 statement closed with 900 owed; nothing was credited since.
        assert (july.amount, july.basis, july.close) == (
            D("900.0000"),
            cycles.BASIS_STATEMENT_CLOSE,
            date(2027, 6, 15),
        )
        # The 7/15 close is ahead: owed now (1,100) less the 900 still to pay
        # on the closed statement, plus spending until the close.
        assert august.basis == cycles.BASIS_CYCLE_PROJECTION
        assert august.amount > D("200")
        # September: the average of the statements on record.
        assert september.basis == cycles.BASIS_STATEMENT_AVERAGE
        assert september.amount == cycles.statement_average(
            cycles.statement_balances(history, 15, today, 6)
        )

    def test_a_fixed_payment_never_pays_more_than_the_statement(self) -> None:
        history = self._card()
        today = date(2027, 6, 25)
        figures = cycles.plan_payments(
            [(date(2027, 7, 10), date(2027, 6, 10)), (date(2027, 8, 10), date(2027, 7, 10))],
            plan=_plan(payment_amount=cycles.PAYMENT_FIXED, fixed_amount=D("1500.00")),
            history=history,
            current_owed=history.owed_at(today),
            today=today,
        )
        assert figures[date(2027, 7, 10)].amount == D("900.0000")
        assert figures[date(2027, 8, 10)] == cycles.SlotFigure(
            D("1500.00"), cycles.BASIS_FIXED, date(2027, 7, 15)
        )

    def test_current_balance_pays_the_balance_once_then_the_later_estimate(self) -> None:
        history = self._card()
        today = date(2027, 6, 25)
        figures = cycles.plan_payments(
            [(date(2027, 7, 10), date(2027, 6, 10)), (date(2027, 8, 10), date(2027, 7, 10))],
            plan=_plan(payment_amount=cycles.PAYMENT_CURRENT_BALANCE),
            history=history,
            current_owed=D("1234.56"),
            today=today,
        )
        assert figures[date(2027, 7, 10)].amount == D("1234.5600")
        assert figures[date(2027, 7, 10)].basis == cycles.BASIS_CURRENT_BALANCE
        assert figures[date(2027, 8, 10)].basis == cycles.BASIS_STATEMENT_AVERAGE

    def test_same_cycle_last_year_falls_back_to_the_average(self) -> None:
        history = self._card()
        today = date(2027, 6, 25)
        figures = cycles.plan_payments(
            [
                (date(2027, 7, 10), date(2027, 6, 10)),
                (date(2027, 8, 10), date(2027, 7, 10)),
                (date(2027, 9, 10), date(2027, 8, 10)),
            ],
            plan=_plan(later_estimate=cycles.LATER_SAME_CYCLE_LAST_YEAR),
            history=history,
            current_owed=history.owed_at(today),
            today=today,
        )
        assert figures[date(2027, 9, 10)].basis == cycles.BASIS_STATEMENT_AVERAGE

    def test_same_cycle_last_year_with_a_years_history(self) -> None:
        rows = [(date(2026, 8, 1), "-4321.00", False), (date(2026, 9, 10), "4321.00", True)]
        rows += [(date(2027, m, 1), "-100.00", False) for m in range(1, 7)]
        history = _history(rows)
        today = date(2027, 6, 25)
        figures = cycles.plan_payments(
            [
                (date(2027, 7, 10), date(2027, 6, 10)),
                (date(2027, 8, 10), date(2027, 7, 10)),
                (date(2027, 9, 10), date(2027, 8, 10)),
            ],
            plan=_plan(later_estimate=cycles.LATER_SAME_CYCLE_LAST_YEAR),
            history=history,
            current_owed=history.owed_at(today),
            today=today,
        )
        assert figures[date(2027, 9, 10)] == cycles.SlotFigure(
            D("4321.0000"), cycles.BASIS_SAME_CYCLE_LAST_YEAR, date(2027, 8, 15)
        )

    def test_no_history_falls_back_to_the_schedules_estimate(self) -> None:
        history = _history([])
        today = date(2027, 6, 25)
        figures = cycles.plan_payments(
            [(date(2027, 8, 10), date(2027, 7, 10)), (date(2027, 9, 10), date(2027, 8, 10))],
            plan=_plan(fallback=D("450.00")),
            history=history,
            current_owed=D("0"),
            today=today,
        )
        assert figures[date(2027, 9, 10)].amount == D("450.0000")


class TestFitCloseDay:
    def test_the_day_whose_balance_equals_the_payments(self) -> None:
        rows: list[tuple[date, str, bool]] = []
        payments: list[tuple[date, Decimal]] = []
        spends = ["310.00", "275.50", "402.25", "199.99", "350.00"]
        for i, spend in enumerate(spends):
            month = i + 1
            rows.append((date(2027, month, 12), f"-{spend}", False))  # before the 19th close
            rows.append((date(2027, month, 25), "-50.00", False))  # after it
            if i:
                rows.append((date(2027, month, 14), f"{payments[-1][1]}", True))
            payments.append((date(2027, month + 1, 14), D(spend) + (D("50.00") if i else 0)))
        history = _history(rows)
        fit = cycles.fit_close_day(history, payments[:-1])
        assert fit is not None and fit[0] in range(14, 25)
        assert fit[1] * 2 > fit[2]

    def test_no_majority_still_names_the_closest_day(self) -> None:
        # Payments that never equal a closing balance (partial payments).
        rows = [(date(2027, m, 5), "-500.00", False) for m in range(1, 6)]
        history = _history(rows)
        payments = [(date(2027, m, 20), D("123.45")) for m in range(1, 5)]
        assert cycles.fit_close_day(history, payments) is None
        closest = cycles.best_close_day(history, payments)
        assert closest is not None and closest[1] == 0 and closest[2] >= 3

    def test_too_few_payments_suggest_nothing(self) -> None:
        history = _history([(date(2027, 1, 5), "-100.00", False)])
        assert cycles.fit_close_day(history, [(date(2027, 2, 10), D("100.00"))]) is None


# --------------------------------------------------------------------------
# Properties
# --------------------------------------------------------------------------

_amounts = st.decimals(min_value="0.01", max_value="5000", places=2)
_rows = st.lists(
    st.tuples(st.integers(min_value=0, max_value=200), _amounts, st.booleans(), st.booleans()),
    max_size=40,
)


def _random_history(rows: list[tuple[int, Decimal, bool, bool]]) -> cycles.CardHistory:
    base = date(2027, 1, 1)
    parsed = [
        (base + timedelta(days=offset), amount if credit else -amount, credit and pay)
        for offset, amount, credit, pay in rows
    ]
    return _history([(d, str(a), p) for d, a, p in parsed], start=base)


@settings(max_examples=150, deadline=None)
@given(rows=_rows, close_day=st.integers(min_value=1, max_value=31), today=st.integers(0, 230))
def test_the_remaining_statement_is_between_zero_and_the_closing_balance(
    rows: list[tuple[int, Decimal, bool, bool]], close_day: int, today: int
) -> None:
    history = _random_history(rows)
    day = date(2027, 1, 1) + timedelta(days=today)
    close = cycles.last_close_on_or_before(day, close_day)
    remaining = cycles.remaining_statement(history, close, day)
    assert 0 <= remaining <= history.owed_at(close)


@settings(max_examples=150, deadline=None)
@given(
    rows=_rows,
    close_day=st.integers(min_value=1, max_value=31),
    today=st.integers(0, 200),
    later=st.integers(1, 30),
)
def test_after_the_close_the_remaining_statement_only_falls(
    rows: list[tuple[int, Decimal, bool, bool]], close_day: int, today: int, later: int
) -> None:
    history = _random_history(rows)
    day = date(2027, 1, 1) + timedelta(days=today)
    close = cycles.last_close_on_or_before(day, close_day)
    assert cycles.remaining_statement(
        history, close, day + timedelta(days=later)
    ) <= cycles.remaining_statement(history, close, day)


@settings(max_examples=150, deadline=None)
@given(
    close_day=st.integers(min_value=1, max_value=31),
    first=st.integers(0, 300),
    gap=st.integers(1, 120),
)
def test_a_payment_with_no_close_in_its_window_has_no_statement(
    close_day: int, first: int, gap: int
) -> None:
    previous = date(2027, 1, 1) + timedelta(days=first)
    slot = previous + timedelta(days=gap)
    close = cycles.statement_close_for(slot, previous, close_day)
    if close is None:
        closes = [
            d
            for d in (previous + timedelta(days=k) for k in range(1, gap + 1))
            if d == cycles.close_date(d.year, d.month, close_day)
        ]
        assert closes == []
    else:
        assert previous < close <= slot


@settings(max_examples=100, deadline=None)
@given(rows=_rows, close_day=st.integers(min_value=1, max_value=31), today=st.integers(30, 200))
def test_every_planned_payment_is_owed_and_never_negative(
    rows: list[tuple[int, Decimal, bool, bool]], close_day: int, today: int
) -> None:
    history = _random_history(rows)
    day = date(2027, 1, 1) + timedelta(days=today)
    slots: list[tuple[date, date | None]] = []
    previous: date | None = None
    for k in range(4):
        slot = cycles.close_date(day.year, day.month, close_day) + timedelta(days=25 + 30 * k)
        slots.append((slot, previous))
        previous = slot
    figures = cycles.plan_payments(
        slots,
        plan=_plan(close_day=close_day),
        history=history,
        current_owed=history.owed_at(day),
        today=day,
    )
    assert all(f.amount >= 0 for f in figures.values())
    assert set(figures) == {s for s, _ in slots}
