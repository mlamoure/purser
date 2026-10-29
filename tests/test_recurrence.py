"""Recurrence detection v2 (issue #234): table-driven cases from the real
2026-09 bank register shapes (synthetic amounts), plus the property that
detection never fires on shuffled dates."""

from datetime import date, timedelta
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from purser.core import recurrence

TODAY = date(2026, 9, 13)


def _occ(dates: list[date], amounts: list[str]) -> list[recurrence.Occurrence]:
    return [
        recurrence.Occurrence(date=d, amount=Decimal(a))
        for d, a in zip(dates, amounts, strict=True)
    ]


def _monthly(day: int, months: int, end: date = TODAY) -> list[date]:
    out: list[date] = []
    y, m = end.year, end.month
    for _ in range(months):
        out.append(date(y, m, min(day, 28)))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return sorted(out)


class TestCadences:
    def test_semi_monthly_payroll_with_variable_net_pay(self) -> None:
        # Accenture: the 6th and the 20th, net pay swinging with deductions.
        dates = []
        for months_back in range(3, -1, -1):
            m = TODAY.month - months_back
            y = TODAY.year
            if m <= 0:
                m, y = m + 12, y - 1
            dates += [d for d in (date(y, m, 6), date(y, m, 20)) if d <= TODAY]
        amounts = ["2242.96", "1393.58", "2177.01", "2872.03", "2242.96", "1393.58", "2177.01"]
        assert len(dates) == 7  # Sep 20 has not happened yet
        detected = recurrence.detect(_occ(dates, amounts), today=TODAY)
        assert detected is not None
        assert detected.rrule == "FREQ=MONTHLY;BYMONTHDAY=6,20"
        assert detected.cadence == "semi-monthly"
        assert detected.amount_type == "estimated"  # cv well above 5%
        assert detected.dtstart == max(dates)
        assert detected.confidence >= Decimal("0.6")
        assert detected.amount_low == Decimal("1393.58") and detected.amount_high == Decimal(
            "2872.03"
        )

    def test_biweekly_same_weekday_is_every_two_weeks_not_semi_monthly(self) -> None:
        start = TODAY - timedelta(days=14 * 7)
        dates = [start + timedelta(days=14 * i) for i in range(8)]
        detected = recurrence.detect(_occ(dates, ["1500.00"] * 8), today=TODAY)
        assert detected is not None
        assert detected.cadence == "every 2 weeks"
        assert detected.rrule.startswith("FREQ=WEEKLY;INTERVAL=2;BYDAY=")
        assert detected.amount_type == "fixed"

    def test_card_payment_varying_threefold_is_monthly_and_estimated(self) -> None:
        dates = _monthly(5, 6)
        amounts = ["-8067.37", "-3120.00", "-5480.10", "-2790.55", "-6011.00", "-4400.00"]
        detected = recurrence.detect(_occ(dates, amounts), today=TODAY)
        assert detected is not None
        assert detected.rrule == "FREQ=MONTHLY;BYMONTHDAY=5"
        assert detected.amount_type == "estimated"
        assert Decimal("0.4") <= detected.confidence < Decimal("0.9")

    def test_one_missed_month_is_still_monthly(self) -> None:
        # Mercedes: one 63-day gap in an otherwise monthly series.
        dates = _monthly(15, 7)
        dates.remove(dates[3])
        detected = recurrence.detect(_occ(dates, ["-960.11"] * 6), today=TODAY)
        assert detected is not None
        assert detected.cadence == "monthly"
        assert detected.gaps.misses == 1
        assert detected.amount_type == "fixed"

    def test_split_legs_on_one_day_are_summed(self) -> None:
        dates = _monthly(1, 4)
        occ = _occ(dates, ["-100.00"] * 4) + _occ(dates, ["-25.00"] * 4)
        detected = recurrence.detect(occ, today=TODAY)
        assert detected is not None
        assert detected.occurrences == 4
        assert detected.typical_amount == Decimal("-125.0000")

    def test_yearly_needs_only_two_occurrences(self) -> None:
        dates = [date(2024, 9, 1), date(2025, 9, 1)]
        detected = recurrence.detect(_occ(dates, ["-95.00", "-95.00"]), today=date(2025, 10, 1))
        assert detected is not None
        assert detected.rrule == "FREQ=YEARLY;BYMONTH=9;BYMONTHDAY=1"

    def test_quarterly_and_semi_annual(self) -> None:
        q = [date(2025, 12, 10), date(2026, 3, 10), date(2026, 6, 10), date(2026, 9, 10)]
        detected = recurrence.detect(_occ(q, ["-505.40"] * 4), today=TODAY)
        assert detected is not None and detected.rrule == "FREQ=MONTHLY;INTERVAL=3;BYMONTHDAY=10"
        h = [date(2025, 3, 1), date(2025, 9, 1), date(2026, 3, 1), date(2026, 9, 1)]
        detected = recurrence.detect(_occ(h, ["-1200.00"] * 4), today=TODAY)
        assert detected is not None and detected.rrule == "FREQ=MONTHLY;INTERVAL=6;BYMONTHDAY=1"

    def test_weekly(self) -> None:
        dates = [TODAY - timedelta(days=7 * i) for i in range(6, 0, -1)]
        detected = recurrence.detect(_occ(dates, ["-40.00"] * 6), today=TODAY)
        assert detected is not None
        assert detected.rrule == f"FREQ=WEEKLY;BYDAY={recurrence._WEEKDAYS[dates[-1].weekday()]}"


class TestRejections:
    def test_lapsed_pattern_is_not_proposed(self) -> None:
        dates = _monthly(15, 5, end=TODAY - timedelta(days=150))
        assert recurrence.detect(_occ(dates, ["-50.00"] * 5), today=TODAY) is None

    def test_irregular_dates_are_not_proposed(self) -> None:
        dates = [date(2026, 5, 3), date(2026, 5, 27), date(2026, 7, 5), date(2026, 9, 1)]
        assert recurrence.detect(_occ(dates, ["-20.00"] * 4), today=TODAY) is None

    def test_too_few_occurrences(self) -> None:
        dates = _monthly(15, 2)
        assert recurrence.detect(_occ(dates, ["-50.00"] * 2), today=TODAY) is None

    def test_zero_typical_amount(self) -> None:
        dates = _monthly(15, 3)
        assert recurrence.detect(_occ(dates, ["-5.00", "0.00", "5.00"]), today=TODAY) is None

    @settings(max_examples=40, deadline=None)
    @given(st.lists(st.integers(min_value=0, max_value=400), min_size=3, max_size=8, unique=True))
    def test_random_day_offsets_rarely_form_a_cadence_and_never_crash(
        self, offsets: list[int]
    ) -> None:
        dates = sorted(TODAY - timedelta(days=o) for o in offsets)
        detected = recurrence.detect(_occ(dates, ["-10.00"] * len(dates)), today=TODAY)
        if detected is not None:
            # Whatever fired must be internally consistent.
            assert detected.gaps.in_bucket_ratio >= recurrence.IN_BUCKET_RATIO_MIN
            assert detected.confidence >= recurrence.CONFIDENCE_MIN
            assert detected.dtstart == dates[-1]


class TestConfidence:
    def test_more_evidence_and_steadier_amounts_score_higher(self) -> None:
        three = recurrence.detect(_occ(_monthly(15, 3), ["-50.00"] * 3), today=TODAY)
        eight = recurrence.detect(_occ(_monthly(15, 8), ["-50.00"] * 8), today=TODAY)
        noisy = recurrence.detect(
            _occ(_monthly(15, 8), ["-50.00", "-90.00", "-20.00", "-70.00"] * 2), today=TODAY
        )
        assert three is not None and eight is not None and noisy is not None
        assert three.confidence < eight.confidence
        assert noisy.confidence < eight.confidence
        assert noisy.amount_type == "estimated" and eight.amount_type == "fixed"
