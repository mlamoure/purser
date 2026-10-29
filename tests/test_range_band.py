# ruff: noqa: F401, F811  (pytest fixtures re-exported from sibling modules)
"""The estimate range band (issue #247, decision 85).

An estimated amount is a point from history, so the forecast also carries
its band: min/max of the schedule's newest six settled amounts together with
the point (a card payment: its past payments together with the card's
current balance). A pessimistic pass takes every ranged event at its worst,
an optimistic pass at its best; shortfall alerts stay on the point line.
"""

import json
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import Engine, text

from purser import crypto
from purser.core import forecasting as core_forecasting
from tests.test_mcp import (
    LiveServer,
    live_server,
    seeded_tokens,
)
from tests.test_mcp_apps import _mcp_call_raw, _read_resource
from tests.test_schedules import (
    _card_purchase,
    _near_term_bymonthday,
    seeded,
    with_card,
)

D = Decimal


# --------------------------------------------------------------------------
# Core (mypy strict, no I/O)
# --------------------------------------------------------------------------


class TestEstimateRange:
    def test_window_and_the_point(self) -> None:
        newest_first = [D("-100"), D("-130"), D("-90"), D("-110"), D("-120"), D("-95"), D("-999")]
        # The seventh actual is outside the window.
        assert core_forecasting.estimate_range(newest_first, D("-107")) == (D("-130"), D("-90"))
        # The event's own amount widens the band: the point is always inside.
        assert core_forecasting.estimate_range(newest_first, D("-150")) == (D("-150"), D("-90"))
        assert core_forecasting.estimate_range(newest_first, D("-50")) == (D("-130"), D("-50"))

    def test_too_little_history_or_no_spread_is_no_band(self) -> None:
        assert core_forecasting.estimate_range([], D("-10")) is None
        assert core_forecasting.estimate_range([D("-10")], D("-12")) is None
        assert core_forecasting.estimate_range([D("-10"), D("-10")], D("-10")) is None
        assert core_forecasting.estimate_range([D("-10"), D("-10")], D("-12")) == (
            D("-12"),
            D("-10"),
        )

    def test_passes_and_lows(self) -> None:
        start = date(2026, 10, 1)
        forecast = core_forecasting.forecast_balance(
            start_balance=D("1000"),
            start=start,
            horizon_days=10,
            items=[
                core_forecasting.ExpectedItem(
                    date=date(2026, 10, 3),
                    amount=D("-700"),
                    label="Card payment",
                    range=(D("-900"), D("-500")),
                ),
                core_forecasting.ExpectedItem(
                    date=date(2026, 10, 5), amount=D("400"), label="Paycheck"
                ),
                core_forecasting.ExpectedItem(
                    date=date(2026, 10, 7),
                    amount=D("-600"),
                    label="Utilities",
                    range=(D("-650"), D("-550")),
                ),
            ],
        )
        by_day = {d.date: d for d in forecast.days}
        assert (by_day[date(2026, 10, 3)].low, by_day[date(2026, 10, 3)].high) == (
            D("100"),
            D("500"),
        )
        assert by_day[date(2026, 10, 7)].balance == D("100")
        assert (by_day[date(2026, 10, 7)].low, by_day[date(2026, 10, 7)].high) == (
            D("-150"),
            D("350"),
        )
        assert forecast.pessimistic_low == core_forecasting.ForecastPoint(
            date(2026, 10, 7), D("-150")
        )
        assert forecast.optimistic_low == core_forecasting.ForecastPoint(
            date(2026, 10, 7), D("350")
        )
        # Shortfall stays on the point line, which never crosses zero.
        assert forecast.first_shortfall is None

    def test_no_band_without_a_ranged_item(self) -> None:
        forecast = core_forecasting.forecast_balance(
            start_balance=D("10"),
            start=date(2026, 10, 1),
            horizon_days=3,
            items=[
                core_forecasting.ExpectedItem(date=date(2026, 10, 2), amount=D("-5"), label="Bill")
            ],
        )
        assert all(d.low is None and d.high is None for d in forecast.days)
        assert forecast.pessimistic_low is None and forecast.optimistic_low is None


_AMOUNTS = st.decimals(min_value=D("-5000"), max_value=D("5000"), places=2, allow_nan=False)
_SPREADS = st.decimals(min_value=D("0"), max_value=D("800"), places=2, allow_nan=False)
_ITEMS = st.lists(
    st.tuples(
        st.integers(-3, 40),  # day offset; negative = overdue, past the end = ignored
        _AMOUNTS,
        st.one_of(st.none(), st.tuples(_SPREADS, _SPREADS)),
    ),
    max_size=12,
)


class TestBandInvariants:
    @given(items=_ITEMS, start_balance=_AMOUNTS)
    def test_pessimistic_point_optimistic(
        self,
        items: list[tuple[int, Decimal, tuple[Decimal, Decimal] | None]],
        start_balance: Decimal,
    ) -> None:
        start = date(2026, 10, 1)
        expected = [
            core_forecasting.ExpectedItem(
                date=start + timedelta(days=offset),
                amount=amount,
                label="x",
                range=(amount - spread[0], amount + spread[1]) if spread else None,
            )
            for offset, amount, spread in items
        ]
        forecast = core_forecasting.forecast_balance(
            start_balance=start_balance, start=start, horizon_days=30, items=expected
        )
        banded = any(i.range is not None and i.date <= start + timedelta(days=30) for i in expected)
        low = min(d.balance for d in forecast.days)
        if not banded:
            assert all(d.low is None and d.high is None for d in forecast.days)
            assert forecast.pessimistic_low is None
            return
        for day in forecast.days:
            assert day.low is not None and day.high is not None
            assert day.low <= day.balance <= day.high
        assert forecast.pessimistic_low is not None and forecast.optimistic_low is not None
        assert forecast.pessimistic_low.balance <= low <= forecast.optimistic_low.balance
        # Zero spreads are a band of width zero: the three series coincide.
        if all(s is None or s == (0, 0) for _, _, s in items):
            assert all(d.low == d.balance == d.high for d in forecast.days)


# --------------------------------------------------------------------------
# Service: which events get a band
# --------------------------------------------------------------------------


def _settle_history(
    engine: Engine,
    schedule_id: str,
    history: list[tuple[str, date]],
    *,
    account_path: str = "assets.checking",
    category_path: str = "expenses.housing",
) -> None:
    """Past occurrences of a schedule, each settled by a posted bank row
    through a confirmed match — the history ``matched_actuals`` reads."""
    with engine.begin() as conn:
        account = conn.execute(
            text("SELECT id FROM accounts WHERE path = :p"), {"p": account_path}
        ).scalar_one()
        category = conn.execute(
            text("SELECT id FROM accounts WHERE path = :p"), {"p": category_path}
        ).scalar_one()
        for amount, on in history:
            posted = conn.execute(
                text(
                    "INSERT INTO transactions (date, description_enc, status, origin)"
                    " VALUES (:d, :desc, 'posted', 'import') RETURNING id"
                ),
                {"d": on, "desc": crypto.encrypt("SYNTHETIC BILL")},
            ).scalar_one()
            expected = conn.execute(
                text(
                    "INSERT INTO transactions (date, status, origin, schedule_id, superseded_by)"
                    " VALUES (:d, 'void', 'rule', :s, :p) RETURNING id"
                ),
                {"d": on, "s": schedule_id, "p": posted},
            ).scalar_one()
            for tx in (posted, expected):
                conn.execute(
                    text(
                        "INSERT INTO postings (transaction_id, account_id, amount)"
                        " VALUES (:tx, :a, :amt), (:tx, :c, :neg)"
                    ),
                    {"tx": tx, "a": account, "c": category, "amt": D(amount), "neg": -D(amount)},
                )
            conn.execute(
                text(
                    "INSERT INTO matches (expected_id, posted_id, score, variance, state)"
                    " VALUES (:e, :p, 0.9, 0, 'confirmed')"
                ),
                {"e": expected, "p": posted},
            )


def _past(weeks: int) -> date:
    return date.today() - timedelta(weeks=weeks)


def _events(client: TestClient, account: str, days: int) -> tuple[dict[str, Any], list[Any]]:
    resp = client.get("/forecast", params={"account": account, "days": days})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return body, [e for d in body["days"] for e in d["events"]]


class TestServiceBands:
    def test_estimated_instances_and_projections_carry_the_band(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        sid = client.post(
            "/schedules",
            json={
                "name": "Utilities",
                "account": "assets.checking",
                "category": "expenses.housing",
                "rrule": "FREQ=WEEKLY",
                "amount_type": "estimated",
                "amount": "-100.00",
                "estimate_basis": "rolling_avg_3",
            },
        ).json()["id"]
        _settle_history(
            migrated_engine,
            sid,
            [("-100.00", _past(1)), ("-130.00", _past(2)), ("-90.00", _past(3))],
        )
        client.post("/schedules/materialize")  # instances follow the estimate
        body, events = _events(client, "assets.checking", 90)
        mine = [e for e in events if e["label"] == "Utilities"]
        assert mine and any(e["projected"] for e in mine) and any(e["transaction_id"] for e in mine)
        for event in mine:
            assert event["amount"] == "-106.6667"
            assert event["basis"] == "rolling_avg_3"
            assert event["range"] == {"low": "-130.0000", "high": "-90.0000"}
        for day in body["days"]:
            assert D(day["low"]) <= D(day["balance"]) <= D(day["high"])
        assert D(body["pessimistic_low"]["balance"]) <= min(D(d["balance"]) for d in body["days"])
        assert body["optimistic_low"] is not None

    def test_known_amounts_get_no_band(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        estimated = client.post(
            "/schedules",
            json={
                "name": "Water",
                "account": "assets.checking",
                "category": "expenses.housing",
                "rrule": "FREQ=WEEKLY",
                "amount_type": "estimated",
                "amount": "-50.00",
                "estimate_basis": "last",
            },
        ).json()["id"]
        _settle_history(migrated_engine, estimated, [("-55.00", _past(1)), ("-45.00", _past(2))])
        client.post("/schedules/materialize")
        first = client.get(f"/schedules/{estimated}").json()["instances"][0]
        client.patch(f"/transactions/{first['transaction_id']}", json={"amount": "-60.00"})
        thin = client.post(
            "/schedules",
            json={
                "name": "Gas",
                "account": "assets.checking",
                "category": "expenses.housing",
                "rrule": "FREQ=WEEKLY",
                "amount_type": "estimated",
                "amount": "-30.00",
                "estimate_basis": "last",
            },
        ).json()["id"]
        _settle_history(migrated_engine, thin, [("-31.00", _past(1))])  # one actual: no band
        client.post(
            "/schedules",
            json={
                "name": "Rent",
                "account": "assets.checking",
                "category": "expenses.housing",
                "rrule": "FREQ=WEEKLY",
                "amount": "-900.00",
            },
        )
        one_off = client.post(
            "/transactions",
            json={
                "account": "assets.checking",
                "category": "expenses.housing",
                "amount": "-42.00",
                "date": (date.today() + timedelta(days=4)).isoformat(),
                "payee": "Plumber",
                "status": "expected",
            },
        ).json()["id"]
        _body, events = _events(client, "assets.checking", 30)
        by_id = {e["transaction_id"]: e for e in events if e["transaction_id"]}
        assert (
            by_id[first["transaction_id"]]["basis"],
            by_id[first["transaction_id"]]["range"],
        ) == (
            "override",
            None,
        )
        assert by_id[one_off]["range"] is None
        assert all(e["range"] is None for e in events if e["label"] in ("Rent", "Gas"))
        water = [e for e in events if e["label"] == "Water" and e["basis"] == "last"]
        assert water and all(e["range"] == {"low": "-55.0000", "high": "-45.0000"} for e in water)

    def test_card_payment_band_is_past_payments_and_the_current_balance(
        self, with_card: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        _card_purchase(client, "-500.00")
        resp = client.post(
            "/schedules",
            json={
                "name": "Citi payment",
                "account": "assets.checking",
                "counterparty_account": "liabilities.citicard",
                "rrule": f"FREQ=MONTHLY;BYMONTHDAY={_near_term_bymonthday()}",
                "amount_type": "estimated",
                "estimate_basis": "counterparty_balance",
            },
        )
        assert resp.status_code == 201, resp.text
        sid = resp.json()["id"]
        _settle_history(
            migrated_engine,
            sid,
            [("-300.00", _past(4)), ("-450.00", _past(8)), ("-380.00", _past(12))],
        )
        first, second = client.get(f"/schedules/{sid}").json()["instances"][:2]
        # Record the statement on the first payment: a stated amount, no band.
        assert (
            client.post(
                "/schedules/statement-balance",
                json={"transaction_id": first["transaction_id"], "statement_balance": "350.00"},
            ).status_code
            == 200
        )
        _body, events = _events(client, "assets.checking", 60)
        by_id = {e["transaction_id"]: e for e in events if e["transaction_id"]}
        assert by_id[first["transaction_id"]]["range"] is None
        current = by_id[second["transaction_id"]]
        assert (current["amount"], current["basis"]) == ("-500.0000", "counterparty_balance")
        # The band holds the current balance, not the balance's swing to zero.
        assert current["range"] == {"low": "-500.0000", "high": "-300.0000"}

        # The card's side sees the negation, ends swapped.
        _card, card_events = _events(client, "liabilities.citicard", 60)
        card_side = {e["transaction_id"]: e for e in card_events if e["transaction_id"]}
        assert card_side[second["transaction_id"]]["amount"] == "500.0000"
        assert card_side[second["transaction_id"]]["range"] == {
            "low": "300.0000",
            "high": "500.0000",
        }

    def test_shortfall_stays_on_the_point(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        from purser import schedules as service
        from purser.db import get_sessionmaker

        client.post(
            "/categories",
            json={"name": "Salary", "type": "category_income", "path": "income.salary"},
        )
        sid = client.post(
            "/schedules",
            json={
                "name": "Big bill",
                "account": "assets.checking",
                "category": "expenses.housing",
                # One occurrence in 2 days, the next a quarter later: the
                # nightly scan's 30 days see exactly one.
                "rrule": "FREQ=MONTHLY;INTERVAL=3",
                "dtstart": (date.today() + timedelta(days=2)).isoformat(),
                "amount_type": "estimated",
                "amount": "-1000.00",
                "estimate_basis": "rolling_avg_3",
            },
        ).json()["id"]
        _settle_history(migrated_engine, sid, [("-900.00", _past(4)), ("-1500.00", _past(8))])
        client.post("/schedules/materialize")  # the point: -1200
        # Opening 1300 after the history rows: the point low is 100, the
        # pessimistic low -200.
        client.post(
            "/transactions",
            json={
                "account": "assets.checking",
                "category": "income.salary",
                "amount": "3700.00",
                "date": _past(9).isoformat(),
                "status": "posted",
            },
        )
        with migrated_engine.begin() as conn:
            conn.execute(text("UPDATE accounts SET min_balance = 0 WHERE path = 'assets.checking'"))
        body, _ = _events(client, "assets.checking", 20)
        assert min(D(d["balance"]) for d in body["days"]) == D("100")
        assert D(body["pessimistic_low"]["balance"]) == D("-200")
        assert body["first_shortfall"] is None
        service.shortfall_scan_job()
        with get_sessionmaker()() as session:
            count = session.execute(
                text("SELECT COUNT(*) FROM events WHERE type = 'forecast.shortfall'")
            ).scalar_one()
        assert count == 0


# --------------------------------------------------------------------------
# MCP: range only when set, low.range, the headline, the budget
# --------------------------------------------------------------------------


def _mcp_estimated(
    client: TestClient,
    engine: Engine,
    *,
    name: str,
    amount: str,
    history: list[str],
    rrule: str = "FREQ=WEEKLY",
    dtstart: date | None = None,
) -> str:
    resp = client.post(
        "/schedules",
        json={
            "name": name,
            "account": "assets.checking",
            "category": "expenses.groceries",
            "rrule": rrule,
            "dtstart": (dtstart or date.today()).isoformat(),
            "amount_type": "estimated",
            "amount": amount,
            "estimate_basis": "rolling_avg_3",
            "payee": name,
        },
    )
    assert resp.status_code == 201, resp.text
    sid = str(resp.json()["id"])
    _settle_history(
        engine,
        sid,
        [(a, _past(i + 1)) for i, a in enumerate(history)],
        category_path="expenses.groceries",
    )
    client.post("/schedules/materialize")
    return sid


class TestMcpBand:
    def test_range_keys_low_range_and_the_headline(
        self,
        seeded_tokens: dict[str, str],
        live_server: LiveServer,
        client: TestClient,
        migrated_engine: Engine,
    ) -> None:
        client.post(
            "/categories",
            json={"name": "Salary", "type": "category_income", "path": "income.salary"},
        )
        _mcp_estimated(
            client,
            migrated_engine,
            name="Citi payment",
            amount="-1000.00",
            history=["-900.00", "-1500.00"],
            rrule="FREQ=MONTHLY;INTERVAL=3",
            dtstart=date.today() + timedelta(days=2),
        )
        client.post(
            "/schedules",
            json={
                "name": "Gym",
                "account": "assets.checking",
                "category": "expenses.groceries",
                "rrule": "FREQ=WEEKLY",
                "amount": "-10.00",
            },
        )
        # seeded_tokens leaves checking at -695; the history took 2400 more.
        client.post(
            "/transactions",
            json={
                "account": "assets.checking",
                "category": "income.salary",
                "amount": "4500.00",
                "date": _past(10).isoformat(),
                "status": "posted",
            },
        )
        got = _mcp_call_raw(
            live_server.url,
            seeded_tokens["reader"],
            "forecast_balance",
            {"account": "assets.checking", "days": 20},
        )
        data = got["structured"]
        citi = [e for e in data["events"] if e["name"] == "Citi payment"]
        gym = [e for e in data["events"] if e["name"] == "Gym"]
        assert citi and gym
        assert citi[0]["range"] == {"low": "-1500.00", "high": "-900.00"}
        assert all("range" not in e for e in gym)
        point_low = D(data["low"]["balance"])
        band = data["low"]["range"]
        assert D(band["low"]) < 0 <= point_low <= D(band["high"])
        assert (
            f"It could dip to {band['low']} on {band['low_date']} if Citi payment comes in high."
            in got["text"]
        )

        daily = json.loads(
            _read_resource(live_server.url, seeded_tokens["reader"], data["daily_uri"])["text"]
        )
        for day in daily["days"]:
            assert D(day["low"]) <= D(day["balance"]) <= D(day["high"])
            for event in day["events"]:
                assert "range" in event and "basis" in event
        ranged = [e for d in daily["days"] for e in d["events"] if e["range"]]
        assert {e["name"] for e in ranged} == {"Citi payment"}

    def test_no_band_keeps_the_payload_as_it_was(
        self, seeded_tokens: dict[str, str], live_server: LiveServer, client: TestClient
    ) -> None:
        client.post(
            "/schedules",
            json={
                "name": "Gym",
                "account": "assets.checking",
                "category": "expenses.groceries",
                "rrule": "FREQ=WEEKLY",
                "amount": "-10.00",
            },
        )
        got = _mcp_call_raw(
            live_server.url,
            seeded_tokens["reader"],
            "forecast_balance",
            {"account": "assets.checking", "days": 30},
        )
        data = got["structured"]
        assert "range" not in data["low"]
        assert all("range" not in e for e in data["events"])
        assert "could dip" not in got["text"]
        daily = json.loads(
            _read_resource(live_server.url, seeded_tokens["reader"], data["daily_uri"])["text"]
        )
        assert all("low" not in d and "high" not in d for d in daily["days"])

    def test_budget_holds_with_every_event_ranged(
        self,
        seeded_tokens: dict[str, str],
        live_server: LiveServer,
        client: TestClient,
        migrated_engine: Engine,
    ) -> None:
        names = [
            "Northeastern Regional Electric Utility Monthly Service Charge",
            "Metropolitan Water and Sewer Authority Quarterly Billing Cycle",
            "Platinum Rewards Travel Credit Card Statement Balance Payment",
        ]
        for i, name in enumerate(names):
            _mcp_estimated(
                client,
                migrated_engine,
                name=name,
                amount=f"-{100 + i * 37}.25",
                history=[f"-{90 + i * 37}.10", f"-{125 + i * 37}.75", f"-{101 + i * 37}.33"],
            )
        got = _mcp_call_raw(
            live_server.url,
            seeded_tokens["reader"],
            "forecast_balance",
            {"account": "assets.checking", "days": 120},
        )
        data = got["structured"]
        size = len(json.dumps(data))
        assert size <= 1900  # _FORECAST_BUDGET_CHARS
        assert size / 4 < 500  # guardrail 6
        assert data["truncated"] is True and data["omitted"]["count"] > 0
        assert "range" in data["low"]
        # The band costs events, not the budget: the low plus the largest
        # movements still fit, every one ranged.
        assert len(data["events"]) >= 4
        assert all("range" in e for e in data["events"])
