"""The admin holdings table (#595): GET /accounts/{id}/holdings/summary.

Synthetic fixtures only (made-up symbols and prices). Every figure is
computed server-side: per-line gain and weight, the cash line, and totals
whose cash plus market value is the account's reported balance.
"""

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.conftest import today


@pytest.fixture
def accounts(admin_client: TestClient) -> dict[str, str]:
    ids: dict[str, str] = {}
    for name, type_, path in (
        ("Broker", "brokerage", "assets.broker"),
        ("Feed Broker", "brokerage", "assets.feed_broker"),
        ("Checking", "checking", "assets.checking"),
    ):
        resp = admin_client.post("/accounts", json={"name": name, "type": type_, "path": path})
        assert resp.status_code == 201, resp.text
        ids[path] = resp.json()["id"]
    resp = admin_client.patch(
        f"/accounts/{ids['assets.broker']}", json={"holdings_source": "manual"}
    )
    assert resp.status_code == 200, resp.text
    resp = admin_client.post(
        "/categories",
        json={"name": "Deposits", "type": "category_income", "path": "income.deposits"},
    )
    assert resp.status_code == 201, resp.text
    return ids


def _summary(client: TestClient, account_id: str) -> dict:
    resp = client.get(f"/accounts/{account_id}/holdings/summary")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _price(client: TestClient, symbol: str, close: str, days_ago: int = 0) -> None:
    resp = client.post(
        "/prices",
        json={
            "symbol": symbol,
            "date": (today() - timedelta(days=days_ago)).isoformat(),
            "close": close,
        },
    )
    assert resp.status_code == 200, resp.text


def _cash(client: TestClient, account_id: str, amount: str) -> None:
    resp = client.post(
        "/transactions",
        json={
            "account": account_id,
            "category": "income.deposits",
            "amount": amount,
            "date": (today() - timedelta(days=3)).isoformat(),
            "payee": "Example Deposit",
            "status": "posted",
        },
    )
    assert resp.status_code == 201, resp.text


def test_manual_account_lines_gains_weights_and_totals(
    accounts: dict[str, str], client: TestClient
) -> None:
    broker = accounts["assets.broker"]
    opened = (today() - timedelta(days=30)).isoformat()
    for symbol, name, quantity, basis in (
        ("AAAA", "Example Growth Fund", "10", "1000.00"),
        ("BBBB", None, "4", "500.00"),
    ):
        resp = client.post(
            f"/accounts/{broker}/positions",
            json={
                "symbol": symbol,
                "name": name,
                "quantity": quantity,
                "cost_basis": basis,
                "as_of": opened,
            },
        )
        assert resp.status_code == 201, resp.text
    _price(client, "AAAA", "150.00")
    _price(client, "BBBB", "100.00")
    _cash(client, broker, "500.00")

    body = _summary(client, broker)
    assert body["source"] == "manual"
    assert body["as_of"] == today().isoformat()
    assert body["closed"] is False
    assert [line["symbol"] for line in body["lines"]] == ["AAAA", "BBBB"]  # largest first
    aaaa, bbbb = body["lines"]
    assert aaaa["name"] == "Example Growth Fund"
    assert Decimal(aaaa["market_value"]) == Decimal("1500.00")
    assert Decimal(aaaa["unrealized_gain"]) == Decimal("500.00")
    assert Decimal(aaaa["gain_pct"]) == Decimal("50.00")
    assert Decimal(bbbb["unrealized_gain"]) == Decimal("-100.00")
    assert Decimal(bbbb["gain_pct"]) == Decimal("-20.00")
    # Weights are shares of cash + securities = 500 + 1500 + 400 = 2400.
    assert Decimal(body["total"]) == Decimal("2400.00")
    assert Decimal(aaaa["weight_pct"]) == Decimal("62.50")
    assert Decimal(bbbb["weight_pct"]) == Decimal("16.67")
    assert Decimal(body["cash"]) == Decimal("500.00")
    assert Decimal(body["cash_weight_pct"]) == Decimal("20.83")
    assert Decimal(body["market_value"]) == Decimal("1900.00")
    assert Decimal(body["cost_basis"]) == Decimal("1500.00")
    assert Decimal(body["unrealized_gain"]) == Decimal("400.00")
    assert Decimal(body["gain_pct"]) == Decimal("26.67")
    assert aaaa["stale_price"] is False and aaaa["no_price"] is False

    # The table's total is the account's reported balance.
    account = client.get(f"/accounts/{broker}").json()
    assert Decimal(account["balance"]) == Decimal(body["total"])


def test_unpriced_and_stale_lines_are_flagged_not_zeroed(
    accounts: dict[str, str], client: TestClient
) -> None:
    broker = accounts["assets.broker"]
    opened = (today() - timedelta(days=60)).isoformat()
    for symbol in ("OLDP", "NOPX"):
        resp = client.post(
            f"/accounts/{broker}/positions",
            json={"symbol": symbol, "quantity": "2", "cost_basis": "50.00", "as_of": opened},
        )
        assert resp.status_code == 201, resp.text
    _price(client, "OLDP", "30.00", days_ago=40)

    body = _summary(client, broker)
    lines = {line["symbol"]: line for line in body["lines"]}
    assert lines["OLDP"]["stale_price"] is True
    assert Decimal(lines["OLDP"]["market_value"]) == Decimal("60.00")
    assert lines["NOPX"]["no_price"] is True
    assert lines["NOPX"]["market_value"] is None
    assert lines["NOPX"]["unrealized_gain"] is None and lines["NOPX"]["weight_pct"] is None
    # Only OLDP's gain is known, so the totals cover only its basis.
    assert Decimal(body["cost_basis"]) == Decimal("50.00")
    assert Decimal(body["unrealized_gain"]) == Decimal("10.00")


def test_feed_account_reads_its_snapshot(
    accounts: dict[str, str], client: TestClient, db_session: Session
) -> None:
    from purser.models import HoldingsSnapshot

    feed = accounts["assets.feed_broker"]
    on = today() - timedelta(days=1)
    for symbol, quantity, basis, value in (
        ("CCCC", "3", None, "300.00"),
        ("DDDD", "1", "80.00", "100.00"),
    ):
        db_session.add(
            HoldingsSnapshot(
                account_id=uuid.UUID(feed),
                as_of=on,
                symbol=symbol,
                quantity=Decimal(quantity),
                cost_basis=Decimal(basis) if basis else None,
                market_value=Decimal(value),
            )
        )
    db_session.commit()

    body = _summary(client, feed)
    assert body["source"] == "feed"
    assert body["as_of"] == on.isoformat()
    cccc, dddd = body["lines"]
    assert cccc["symbol"] == "CCCC" and cccc["name"] is None
    assert cccc["unrealized_gain"] is None  # no basis: never all gain
    assert Decimal(dddd["unrealized_gain"]) == Decimal("20.00")
    assert Decimal(body["unrealized_gain"]) == Decimal("20.00")
    assert Decimal(body["total"]) == Decimal("400.00")
    assert Decimal(body["cash"]) == Decimal("0")
    assert Decimal(cccc["weight_pct"]) == Decimal("75.00")


def test_empty_account_and_wrong_type(accounts: dict[str, str], client: TestClient) -> None:
    body = _summary(client, accounts["assets.feed_broker"])
    assert body["lines"] == [] and body["as_of"] is None
    assert body["cost_basis"] is None and body["gain_pct"] is None
    assert body["cash_weight_pct"] is None  # a share of nothing
    resp = client.get(f"/accounts/{accounts['assets.checking']}/holdings/summary")
    assert resp.status_code == 422
    assert "holdings belong to brokerage accounts" in resp.json()["detail"]
