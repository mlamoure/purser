"""The admin chart's balance history (#594): GET /accounts/{id}/balance/history.

Every point is the account's REPORTED end-of-day balance (decisions 42/50):
postings for cash accounts, postings plus holdings market value for
brokerage, valuations for loans — where `/balance/series` is the postings
history (decision 129). Change, percent and extremes are computed
server-side.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.conftest import today


@pytest.fixture
def seeded(admin_client: TestClient) -> dict[str, str]:
    ids: dict[str, str] = {}
    for name, type_, path in [
        ("Checking", "checking", "assets.checking"),
        ("Mortgage", "loan", "liabilities.mortgage"),
        ("Brokerage", "brokerage", "assets.brokerage"),
    ]:
        resp = admin_client.post("/accounts", json={"name": name, "type": type_, "path": path})
        assert resp.status_code == 201, resp.text
        ids[path] = resp.json()["id"]
    resp = admin_client.post(
        "/categories",
        json={"name": "Dining", "type": "category_expense", "path": "expenses.dining"},
    )
    assert resp.status_code == 201, resp.text
    ids["expenses.dining"] = resp.json()["id"]
    return ids


def _post_row(client: TestClient, account: str, amount: str, on: date) -> None:
    resp = client.post(
        "/transactions",
        json={
            "account": account,
            "category": "expenses.dining",
            "amount": amount,
            "date": on.isoformat(),
            "payee": "Example Payee",
        },
    )
    assert resp.status_code == 201, resp.text
    assert client.post(f"/transactions/{resp.json()['id']}/post").status_code == 200


def _holdings(session: Session, account_id: str, on: date, market_value: str) -> None:
    from purser.models import HoldingsSnapshot

    session.add(
        HoldingsSnapshot(
            account_id=uuid.UUID(account_id),
            as_of=on,
            symbol="VTI",
            quantity=Decimal("1"),
            cost_basis=Decimal(market_value),
            market_value=Decimal(market_value),
        )
    )
    session.commit()


def _history(client: TestClient, account_id: str, **params: object) -> dict:
    resp = client.get(f"/accounts/{account_id}/balance/history", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_postings_account_is_a_daily_series_with_change_and_extremes(
    seeded: dict[str, str], client: TestClient
) -> None:
    checking = seeded["assets.checking"]
    _post_row(client, checking, "100.00", today() - timedelta(days=20))
    _post_row(client, checking, "-130.00", today() - timedelta(days=10))
    _post_row(client, checking, "80.00", today() - timedelta(days=5))

    body = _history(client, checking, range="1m")
    assert body["method"] == "postings"
    assert body["range"] == "1m"
    # The start clamps forward to the first activity, so no flat run of zeros.
    assert body["first_activity"] == (today() - timedelta(days=20)).isoformat()
    assert body["start"] == body["first_activity"]
    assert body["end"] == today().isoformat()
    assert body["interval_days"] == 1
    dates = [p["date"] for p in body["points"]]
    assert dates == sorted(dates) and dates[0] == body["start"] and dates[-1] == body["end"]
    assert len(dates) == 21  # a true daily series
    by_date = {p["date"]: Decimal(p["balance"]) for p in body["points"]}
    assert by_date[(today() - timedelta(days=15)).isoformat()] == Decimal("100.00")
    assert Decimal(body["current"]) == Decimal("50.00")
    assert Decimal(body["change"]) == Decimal("-50.00")
    assert Decimal(body["change_pct"]) == Decimal("-50.00")
    assert body["low"] == {
        "date": (today() - timedelta(days=10)).isoformat(),
        "balance": "-30.0000",
    }
    assert Decimal(body["high"]["balance"]) == Decimal("100.00")


def test_brokerage_points_include_holdings_market_value(
    seeded: dict[str, str], client: TestClient, db_session: Session
) -> None:
    """The bug this endpoint exists for: /balance/series points are the cash
    alone, while the account reads cash plus securities."""
    brokerage = seeded["assets.brokerage"]
    _post_row(client, brokerage, "500.00", today() - timedelta(days=30))
    _holdings(db_session, brokerage, today() - timedelta(days=20), "2000.00")
    _holdings(db_session, brokerage, today() - timedelta(days=5), "2500.00")

    body = _history(client, brokerage, range="3m")
    assert body["method"] == "postings+holdings"
    by_date = {p["date"]: Decimal(p["balance"]) for p in body["points"]}
    assert by_date[(today() - timedelta(days=25)).isoformat()] == Decimal("500.00")
    assert by_date[(today() - timedelta(days=10)).isoformat()] == Decimal("2500.00")
    assert Decimal(body["current"]) == Decimal("3000.00")
    single = client.get(f"/accounts/{brokerage}").json()
    assert Decimal(single["balance"]) == Decimal(body["current"])
    # Extremes come from the reported points: a price move has no posting.
    assert Decimal(body["high"]["balance"]) == Decimal("3000.00")
    assert Decimal(body["low"]["balance"]) == Decimal("500.00")
    assert body["low"]["date"] == body["start"]


def test_loan_points_are_valuations(seeded: dict[str, str], client: TestClient) -> None:
    mortgage = seeded["liabilities.mortgage"]
    for days, value in ((60, "-300000.00"), (30, "-299000.00")):
        resp = client.post(
            f"/accounts/{mortgage}/valuations",
            json={"as_of": (today() - timedelta(days=days)).isoformat(), "value": value},
        )
        assert resp.status_code == 201, resp.text

    body = _history(client, mortgage, range="1y")
    assert body["method"] == "valuation"
    assert body["start"] == (today() - timedelta(days=60)).isoformat()
    assert Decimal(body["current"]) == Decimal("-299000.00")
    assert Decimal(body["change"]) == Decimal("1000.00")
    assert Decimal(body["change_pct"]) == Decimal("0.33")


def test_ranges_and_explicit_windows(seeded: dict[str, str], client: TestClient) -> None:
    checking = seeded["assets.checking"]
    _post_row(client, checking, "10.00", today() - timedelta(days=800))
    _post_row(client, checking, "5.00", today() - timedelta(days=1))

    ytd = _history(client, checking, range="ytd")
    assert ytd["start"] == today().replace(month=1, day=1).isoformat()
    every = _history(client, checking, range="all")
    assert every["start"] == (today() - timedelta(days=800)).isoformat()
    assert every["range"] == "all"
    assert len(every["points"]) <= 400
    assert every["interval_days"] > 1
    # A year at the default cap is daily.
    assert _history(client, checking)["interval_days"] == 1

    explicit_start = today() - timedelta(days=10)
    window = _history(
        client, checking, start=explicit_start.isoformat(), end=today().isoformat(), range="5y"
    )
    assert window["range"] is None  # start/end win over the named range
    assert window["start"] == explicit_start.isoformat()
    assert Decimal(window["points"][0]["balance"]) == Decimal("10.00")

    capped = _history(client, checking, range="all", max_points=10)
    assert len(capped["points"]) <= 11  # start is always forced in


def test_an_account_with_no_history_is_one_flat_point(
    seeded: dict[str, str], client: TestClient
) -> None:
    body = _history(client, seeded["assets.checking"], range="all")
    assert body["first_activity"] is None
    assert body["points"] == [{"date": today().isoformat(), "balance": "0"}]
    assert body["change_pct"] is None
    assert body["low"] is None and body["high"] is None


def test_rejects_a_category_a_backwards_window_and_a_bad_range(
    seeded: dict[str, str], client: TestClient
) -> None:
    checking = seeded["assets.checking"]
    url = f"/accounts/{checking}/balance/history"
    assert client.get(f"/accounts/{seeded['expenses.dining']}/balance/history").status_code == 422
    backwards = {"start": today().isoformat(), "end": (today() - timedelta(days=3)).isoformat()}
    assert client.get(url, params=backwards).status_code == 422
    assert client.get(url, params={"range": "2w"}).status_code == 422
    assert client.get(url, params={"max_points": 1}).status_code == 422
