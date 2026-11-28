"""Every brokerage account's holdings in one view (#684): GET
/holdings/portfolio, with what priced each line and totals per currency.

Synthetic accounts, tickers and amounts; Decimal (as strings) throughout.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from purser import db
from purser.models import Account
from tests.conftest import today
from tests.test_feed_overlay import _ago, _close, _feed_account, _row
from tests.test_prices import _hold, broker  # noqa: F401


def _sym(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:5].upper()}"


def _lines(body: dict[str, Any], account_id: str) -> dict[str, dict[str, Any]]:
    account = next(a for a in body["accounts"] if a["account_id"] == account_id)
    return {line["symbol"]: line for line in account["lines"]}


def test_only_a_price_entered_by_hand_is_flagged(
    admin_client: TestClient,
    broker: str,  # noqa: F811  (the fixture imported above)
) -> None:
    valued, by_hand, by_provider, unpriced = _sym("V"), _sym("H"), _sym("P"), _sym("U")
    description = f"Stable Value Trust {uuid.uuid4().hex[:4]}"
    with db.get_sessionmaker()() as session:
        plan = _feed_account(session)
        on = _ago(1)
        _row(session, plan.id, on, valued, "10", "500")
        _row(session, plan.id, on, by_hand, "4", "0")
        _row(session, plan.id, on, by_provider, "2", None)
        _row(session, plan.id, on, unpriced, "1", "0")
        _row(session, plan.id, on, description, "3", "0")
        _close(session, by_hand, _ago(2), "12.5", source="manual")
        _close(session, by_provider, _ago(2), "30", source="tiingo")
        held = _sym("M")
        _close(session, held, _ago(3), "20", source="tiingo")
        typed = _sym("T")
        _close(session, typed, _ago(3), "8", source="manual")
        session.commit()
        plan_id = str(plan.id)
    _hold(broker, held, "5", days_ago=3)
    _hold(broker, typed, "2", days_ago=3)

    resp = admin_client.get("/holdings/portfolio")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    feed = _lines(body, plan_id)
    assert feed[valued]["manual_price"] is False
    assert Decimal(feed[valued]["price"]) == Decimal("50")  # 500 / 10, implied
    assert feed[by_hand]["manual_price"] is True
    assert Decimal(feed[by_hand]["market_value"]) == Decimal("50")
    assert feed[by_provider]["manual_price"] is False
    assert feed[unpriced]["manual_price"] is False and feed[unpriced]["no_price"]
    assert feed[description]["unpriceable"]

    manual = _lines(body, broker)
    assert manual[held]["manual_price"] is False
    assert Decimal(manual[held]["market_value"]) == Decimal("100")
    assert manual[typed]["manual_price"] is True
    assert Decimal(manual[typed]["market_value"]) == Decimal("16")

    assert body["no_price"] == 1 and body["unpriceable"] == 1
    assert body["lines"] == sum(len(a["lines"]) for a in body["accounts"])
    assert body["as_of"] == today().isoformat()


def test_totals_per_currency_and_closed_accounts_left_out(admin_client: TestClient) -> None:
    with db.get_sessionmaker()() as session:
        open_plan = _feed_account(session)
        _row(session, open_plan.id, _ago(1), _sym("A"), "2", "150.25")
        _row(session, open_plan.id, _ago(1), _sym("B"), "1", "49.75")
        closed = _feed_account(session)
        _row(session, closed.id, _ago(1), _sym("C"), "1", "999")
        session.get(Account, closed.id).closed_at = datetime.now(UTC)  # type: ignore[union-attr]
        session.commit()
        open_id, closed_id = str(open_plan.id), str(closed.id)

    body = admin_client.get("/holdings/portfolio").json()
    ids = [a["account_id"] for a in body["accounts"]]
    assert open_id in ids and closed_id not in ids
    account = next(a for a in body["accounts"] if a["account_id"] == open_id)
    assert Decimal(account["market_value"]) == Decimal("200")
    [usd] = [t for t in body["totals"] if t["currency"] == account["currency"]]
    assert Decimal(usd["market_value"]) == sum(
        (Decimal(a["market_value"]) for a in body["accounts"] if a["currency"] == usd["currency"]),
        Decimal(0),
    )
    assert Decimal(usd["total"]) == Decimal(usd["market_value"]) + Decimal(usd["cash"])


def test_read_scope_is_enough(admin_client: TestClient) -> None:
    minted = admin_client.post("/tokens", json={"name": "reader", "scopes": ["read"]}).json()
    headers = {"Authorization": f"Bearer {minted['token']}"}
    assert admin_client.get("/holdings/portfolio", headers=headers).status_code == 200
