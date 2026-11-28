# ruff: noqa: F811  (pytest fixtures re-exported from sibling modules)
"""Card payments sized from the statement cycle (#674, decision 136).

A synthetic card closes on a day of the month; a card-payment schedule that
sets ``payment_amount`` pays what remains of the closed statement, then a
projection of the next one, then the average of past statements. Dates are
built from the test's pinned clock.
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from purser.core import cycles as core_cycles
from tests.conftest import today
from tests.test_schedules import (  # noqa: F401 (fixture re-export)
    seeded,
    with_card,
)

CARD = "liabilities.citicard"


def _close_day() -> int:
    """A closing day about ten days back, kept to 1..28 so every month has it."""
    return min((today() - timedelta(days=10)).day, 28)


def _last_close() -> date:
    return core_cycles.last_close_on_or_before(today(), _close_day())


def _months_back(close: date, months: int) -> date:
    year, month = close.year, close.month
    for _ in range(months):
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return core_cycles.close_date(year, month, _close_day())


def _charge(client: TestClient, amount: str, on: date) -> None:
    resp = client.post(
        "/transactions",
        json={
            "account": CARD,
            "category": "expenses.housing",
            "amount": amount,
            "date": on.isoformat(),
            "status": "posted",
        },
    )
    assert resp.status_code == 201, resp.text


def _pay(client: TestClient, amount: str, on: date) -> None:
    resp = client.post(
        "/transactions/transfer",
        json={
            "from_account": "assets.checking",
            "to_account": CARD,
            "amount": amount,
            "date": on.isoformat(),
            "status": "posted",
        },
    )
    assert resp.status_code == 201, resp.text


@pytest.fixture
def card_history(with_card: None, client: TestClient) -> dict[str, Any]:
    """Three paid-in-full statements (700, 500, then 300 at the last close),
    and after the last close a 120 charge and a 20 refund."""
    last = _last_close()
    closes = [_months_back(last, 2), _months_back(last, 1), last]
    for close, spend in zip(closes, ["700.00", "500.00", "300.00"], strict=True):
        _charge(client, f"-{spend}", close - timedelta(days=5))
    # Each earlier statement paid in full a week after its close.
    _pay(client, "700.00", closes[0] + timedelta(days=7))
    _pay(client, "500.00", closes[1] + timedelta(days=7))
    _charge(client, "-120.00", today() - timedelta(days=2))
    _charge(client, "20.00", today() - timedelta(days=1))
    return {"closes": closes}


def _payment_schedule(client: TestClient, **extra: object) -> str:
    due = _last_close() + timedelta(days=20)
    body: dict[str, object] = {
        "account": "assets.checking",
        "counterparty_account": CARD,
        "rrule": f"FREQ=MONTHLY;BYMONTHDAY={min(due.day, 28)}",
        "dtstart": (due - timedelta(days=62)).isoformat(),
        "amount_type": "estimated",
        "estimate_basis": "counterparty_balance",
        **extra,
    }
    resp = client.post("/schedules", json=body)
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _account(client: TestClient, path: str) -> dict[str, Any]:
    return dict(next(a for a in client.get("/accounts").json() if a["path"] == path))


def _set_close_day(client: TestClient, day: int | None) -> dict[str, Any]:
    account = _account(client, CARD)
    resp = client.patch(f"/accounts/{account['id']}", json={"statement_close_day": day})
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def _upcoming(client: TestClient, schedule_id: str) -> list[dict[str, Any]]:
    detail = client.get(f"/schedules/{schedule_id}").json()
    return [i for i in detail["instances"] if i["date"] >= today().isoformat()]


class TestStatementCycleSizing:
    def test_closed_statement_then_projection_then_average(
        self, card_history: dict[str, Any], client: TestClient
    ) -> None:
        sid = _payment_schedule(client, payment_amount="statement_balance")
        # No closing day yet: the schedule still pays the card's balance.
        before = _upcoming(client, sid)
        assert {i["basis"] for i in before} == {"counterparty_balance"}

        card = _set_close_day(client, _close_day())
        assert card["statement_close_day"] == _close_day()
        live = _upcoming(client, sid)
        first, second = live[0], live[1]
        # 300 owed at the last close, less the 20 refund since.
        assert (first["basis"], first["amount"]) == ("statement_close", "-280.0000")
        # Owed now (300 + 120 - 20 = 400) less the 280 still due, plus spending
        # until the close: at least 120.
        assert second["basis"] == "cycle_projection"
        assert Decimal(second["amount"]) <= Decimal("-120")

        forecast = client.get(
            "/forecast", params={"account": "assets.checking", "days": 120}
        ).json()
        bases = [
            e["basis"]
            for day in forecast["days"]
            for e in day["events"]
            if e["label"] == "Citi Card"
        ]
        assert bases[:2] == ["statement_close", "cycle_projection"]
        assert "statement_average" in bases[2:]
        averages = [
            e
            for day in forecast["days"]
            for e in day["events"]
            if e["label"] == "Citi Card" and e["basis"] == "statement_average"
        ]
        # (700 + 500 + 300) / 3 statements on record.
        assert averages[0]["amount"] == "-500.0000"
        assert averages[0]["range"] == {"low": "-700.0000", "high": "-300.0000"}

    def test_a_new_estimated_card_payment_defaults_to_the_cycle(
        self, card_history: dict[str, Any], client: TestClient
    ) -> None:
        _set_close_day(client, _close_day())
        sid = _payment_schedule(client)
        detail = client.get(f"/schedules/{sid}").json()
        assert (detail["payment_amount"], detail["later_estimate"]) == (
            "statement_balance",
            "statement_average",
        )
        assert _upcoming(client, sid)[0]["basis"] == "statement_close"

    def test_a_fixed_payment_and_turning_it_off(
        self, card_history: dict[str, Any], client: TestClient
    ) -> None:
        _set_close_day(client, _close_day())
        sid = _payment_schedule(
            client, payment_amount="fixed", later_estimate="fixed", amount="-250.00"
        )
        first, second = _upcoming(client, sid)[:2]
        assert (first["amount"], first["basis"]) == ("-250.0000", "fixed")
        assert (second["amount"], second["basis"]) == ("-250.0000", "fixed")

        resp = client.patch(
            f"/schedules/{sid}",
            json={"payment_amount": None, "later_estimate": None, "later_window": None},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["payment_amount"] is None
        assert {i["basis"] for i in _upcoming(client, sid)} == {"counterparty_balance"}

    def test_settings_are_refused_off_a_card(
        self, card_history: dict[str, Any], client: TestClient
    ) -> None:
        resp = client.post(
            "/schedules",
            json={
                "account": "assets.checking",
                "category": "expenses.housing",
                "payee": "Landlord",
                "rrule": "FREQ=MONTHLY;BYMONTHDAY=1",
                "amount_type": "fixed",
                "amount": "-100.00",
                "payment_amount": "statement_balance",
            },
        )
        assert resp.status_code == 422
        assert "credit card" in resp.text

    def test_close_day_only_on_a_card(
        self, card_history: dict[str, Any], client: TestClient
    ) -> None:
        checking = _account(client, "assets.checking")
        resp = client.patch(f"/accounts/{checking['id']}", json={"statement_close_day": 5})
        assert resp.status_code == 422
        assert "credit cards" in resp.text


class TestStatementRecording:
    def test_minimum_due_and_the_close_day_hint(
        self, card_history: dict[str, Any], client: TestClient
    ) -> None:
        _set_close_day(client, _close_day())
        sid = _payment_schedule(client, payment_amount="minimum_due", amount="-40.00")
        first = _upcoming(client, sid)[0]
        off_by_one = _last_close() - timedelta(days=1)
        resp = client.post(
            "/schedules/statement-balance",
            json={
                "transaction_id": first["transaction_id"],
                "statement_balance": "300.00",
                "minimum_due": "35.00",
                "statement_date": off_by_one.isoformat(),
            },
        )
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert (out["amount"], out["basis"], out["minimum_due"]) == (
            "-35.0000",
            "statement",
            "35.0000",
        )
        assert out["close_day_hint"] is not None
        assert str(off_by_one.day) in out["close_day_hint"]

    def test_minimum_due_above_the_balance_is_refused(
        self, card_history: dict[str, Any], client: TestClient
    ) -> None:
        _set_close_day(client, _close_day())
        sid = _payment_schedule(client)
        first = _upcoming(client, sid)[0]
        resp = client.post(
            "/schedules/statement-balance",
            json={
                "transaction_id": first["transaction_id"],
                "statement_balance": "300.00",
                "minimum_due": "301.00",
            },
        )
        assert resp.status_code == 422


class TestWithhold:
    def test_withheld_amounts_leave_out_minimum_due_and_owed(
        self, card_history: dict[str, Any], client: TestClient
    ) -> None:
        """#698: ``minimum_due`` on schedule instances and a statement's
        ``owed`` on the REST statement cycle are amounts a caller can ask to
        leave out, as on MCP."""
        _set_close_day(client, _close_day())
        sid = _payment_schedule(client, payment_amount="minimum_due", amount="-40.00")
        first = _upcoming(client, sid)[0]
        resp = client.post(
            "/schedules/statement-balance",
            json={
                "transaction_id": first["transaction_id"],
                "statement_balance": "300.00",
                "minimum_due": "35.00",
            },
        )
        assert resp.status_code == 200, resp.text
        full = client.get(f"/schedules/{sid}").json()
        assert any(i.get("minimum_due") == "35.0000" for i in full["instances"])
        private = client.get(f"/schedules/{sid}", params={"withhold": "amount"}).json()
        assert private["withheld"] == ["amount"]
        rows = private["instances"] + private["matched_instances"]
        assert rows and not any("minimum_due" in i or "statement_balance" in i for i in rows)

        card = _account(client, CARD)
        cycle = client.get(f"/accounts/{card['id']}/statement-cycle").json()
        assert cycle["statements"] and all("owed" in s for s in cycle["statements"])
        cycle = client.get(
            f"/accounts/{card['id']}/statement-cycle", params={"withhold": "amount"}
        ).json()
        assert cycle["withheld"] == ["amount"]
        assert cycle["statements"] and not any("owed" in s for s in cycle["statements"])


class TestCloseDaySuggestion:
    def test_the_history_suggests_the_closing_day(
        self, card_history: dict[str, Any], client: TestClient
    ) -> None:
        # A third full payment, so three payments can be tried.
        _pay(client, "300.00", today() - timedelta(days=3))
        card = _account(client, CARD)
        out = client.get(f"/accounts/{card['id']}/statement-cycle").json()
        assert out["suggestion_source"] == "history"
        assert out["suggested_close_day"] is not None
        assert out["statement_close_day"] is None
        assert out["statements"] == []

        _set_close_day(client, _close_day())
        out = client.get(f"/accounts/{card['id']}/statement-cycle").json()
        assert [s["owed"] for s in out["statements"][:3]] == [
            "300.0000",
            "500.0000",
            "700.0000",
        ]


def test_the_migration_checks_hold(card_history: dict[str, Any], migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn, pytest.raises(Exception, match="check"):
        conn.execute(
            text("UPDATE accounts SET statement_close_day = 5 WHERE path = 'assets.checking'")
        )
