"""Spending baseline mode (issue #76): like-days pace adjustment, delta-sum
identity, payee movers, and the recursive drill — service level with an
injected 'today' so mid-month behavior is deterministic."""

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from purser import ledger as ledger_service
from purser.core import ledger as core

TODAY = date(2026, 7, 15)  # frozen mid-month
PERIOD = "2026-07"


def _seed(session: Session) -> None:
    ledger_service.create_account(
        session, name="Checking", type_="checking", path="assets.checking", actor_token_id=None
    )
    for name, path in [
        ("Dining", "expenses.dining"),
        ("Delivery", "expenses.dining.delivery"),
        ("Groceries", "expenses.groceries"),
    ]:
        ledger_service.create_account(
            session, name=name, type_="category_expense", path=path, actor_token_id=None
        )

    def post(category: str, amount: str, on: date, payee: str) -> None:
        ledger_service.create_flat_transaction(
            session,
            account_ref="assets.checking",
            category_ref=category,
            amount=Decimal(amount),
            date=on,
            payee_name=payee,
            requested_status=core.Status("posted"),
            token=None,
        )

    # Baseline months Apr/May/Jun — early-month dining (within like-days=15)...
    for month, day, amount in [(4, 10, "-100.00"), (5, 10, "-110.00"), (6, 10, "-120.00")]:
        post("expenses.dining", amount, date(2026, month, day), "Bistro")
    # ...plus one LATE baseline transaction (day 28 > like-days) that a naive
    # full-month average would count.
    post("expenses.dining", "-90.00", date(2026, 4, 28), "Bistro")
    # DoorDash history: 1 early order per baseline month
    for month in (4, 5, 6):
        post("expenses.dining.delivery", "-40.00", date(2026, month, 10), "DoorDash")
    # Current month (through the frozen today=Jul 15)
    post("expenses.dining", "-60.00", date(2026, 7, 14), "Bistro")
    for day in (2, 4, 6, 8, 10, 12):
        post("expenses.dining.delivery", "-30.00", date(2026, 7, day), "DoorDash")
    # Groceries: flat-ish
    for month, amount in [(4, "-200.00"), (5, "-200.00"), (6, "-200.00"), (7, "-180.00")]:
        post("expenses.groceries", amount, date(2026, month, 10), "Market")
    session.flush()


class TestSpendingBaseline:
    def test_like_days_pace_beats_full_month_average(
        self, clean_db: None, db_session: Session
    ) -> None:
        _seed(db_session)
        result = ledger_service.spending_baseline(
            db_session, period=PERIOD, baseline="avg_3mo", today=TODAY
        )
        dining = next(r for r in result["rows"] if r["key"] == "expenses.dining")
        # like-days baseline: (100+110+120 + 3*40)/3 = 150.00 — the Apr 28
        # transaction is excluded because it lands after day 15.
        assert dining["baseline"] == "150.00"
        # current: 60 + 6*30 = 240
        assert dining["amount"] == "240.00"
        assert dining["delta"] == "90.00"
        # a naive full-month average would have been (330+90+120)/3 = 180
        assert Decimal(dining["baseline"]) != Decimal("180.00")

    def test_deltas_sum_to_totals_and_movers(self, clean_db: None, db_session: Session) -> None:
        _seed(db_session)
        result = ledger_service.spending_baseline(
            db_session, period=PERIOD, baseline="avg_3mo", today=TODAY
        )
        assert result["truncated"] is False
        delta_sum = sum(Decimal(r["delta"]) for r in result["rows"])
        assert delta_sum == Decimal(result["grand_total"]) - Decimal(result["baseline_total"])

        dining = next(r for r in result["rows"] if r["key"] == "expenses.dining")
        mover = dining["top_payee_mover"]
        assert mover is not None
        assert mover["payee"] == "DoorDash"
        # current 180 vs baseline avg 40 -> +140; 6 orders vs usual 1
        assert mover["delta"] == "140.00"
        assert mover["count"] == 6
        assert mover["baseline_count"] == 1

    def test_full_past_month_uses_whole_months(self, clean_db: None, db_session: Session) -> None:
        _seed(db_session)
        result = ledger_service.spending_baseline(
            db_session, period="2026-06", baseline="avg_3mo", today=TODAY
        )
        # June is a completed month: no pace adjustment, so April's late
        # transaction counts. Dining baseline = (Mar+Apr+May incl. day-28)/3.
        dining = next(r for r in result["rows"] if r["key"] == "expenses.dining")
        # Mar: 0; Apr: 100+90+40=230; May: 110+40=150 -> 380/3 = 126.67
        assert dining["baseline"] == "126.67"

    def test_payee_drill_same_signature(self, clean_db: None, db_session: Session) -> None:
        _seed(db_session)
        result = ledger_service.spending_baseline(
            db_session,
            period=PERIOD,
            baseline="avg_3mo",
            category_subtree="expenses.dining",
            group_by="payee",
            today=TODAY,
        )
        keys = {r["key"] for r in result["rows"]}
        assert {"DoorDash", "Bistro"} <= keys
        doordash = next(r for r in result["rows"] if r["key"] == "DoorDash")
        assert doordash["amount"] == "180.00"
        assert doordash["baseline"] == "40.00"

    def test_same_month_last_year(self, clean_db: None, db_session: Session) -> None:
        _seed(db_session)
        ledger_service.create_flat_transaction(
            db_session,
            account_ref="assets.checking",
            category_ref="expenses.groceries",
            amount=Decimal("-150.00"),
            date=date(2025, 7, 5),
            payee_name="Market",
            requested_status=core.Status("posted"),
            token=None,
        )
        db_session.flush()
        result = ledger_service.spending_baseline(
            db_session, period=PERIOD, baseline="same_month_last_year", today=TODAY
        )
        groceries = next(r for r in result["rows"] if r["key"] == "expenses.groceries")
        assert groceries["baseline"] == "150.00"
        assert groceries["amount"] == "180.00"
        assert groceries["delta"] == "30.00"

    def test_category_rollup_is_top_level(self, clean_db: None, db_session: Session) -> None:
        _seed(db_session)
        result = ledger_service.spending_baseline(
            db_session, period=PERIOD, baseline="avg_3mo", today=TODAY
        )
        keys = {r["key"] for r in result["rows"]}
        # delivery rolls up into expenses.dining; no deep paths at top level
        assert "expenses.dining" in keys
        assert not any(k.count(".") > 1 for k in keys)
