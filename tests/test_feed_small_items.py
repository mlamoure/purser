"""SimpleFIN small items (#483): the available balance on discovery."""

from typing import Any

from tests import test_feed
from tests.test_feed import FeedHarness, account_of
from tests.test_feed_v2 import as_v2

feed_env = test_feed.feed_env
harness = test_feed.harness


def _account(account_id: str, balance: str, available: str | None) -> dict[str, Any]:
    account = account_of([], account_id=account_id, balance=balance)
    if available is not None:
        account["available-balance"] = available
    return account


def _discovered(harness: FeedHarness, connection_id: str) -> dict[str, dict[str, Any]]:
    rows = harness.client.get(f"/connections/{connection_id}/discovered").json()
    return {row["external_id"]: row for row in rows}


def test_available_balance_shows_on_cash_and_card_accounts_only(harness: FeedHarness) -> None:
    payload = as_v2(
        {
            "accounts": [
                _account("CARD", "-500.00", "4500.00"),  # credit left on a card
                _account("SAME", "100.00", "100.00"),  # equal: says nothing
                _account("BRK", "9000.00", "0.00"),  # a brokerage's 0.00 means nothing
                _account("NEW", "20.00", "15.00"),  # unmapped: type unknown, shown
                _account("NONE", "30.00", None),
            ]
        }
    )
    harness.use_payload(payload)
    connection_id = harness.connect()
    harness.sync(connection_id)
    for external_id, type_, path in [
        ("CARD", "credit_card", "liabilities.card"),
        ("SAME", "checking", "assets.same"),
        ("BRK", "brokerage", "assets.brokerage"),
    ]:
        resp = harness.client.post(
            f"/connections/{connection_id}/map",
            json={
                "external_id": external_id,
                "create": {"name": external_id, "type": type_, "path": path},
            },
        )
        assert resp.status_code == 200, resp.text

    rows = _discovered(harness, connection_id)
    assert rows["CARD"]["available_balance"] == "4500.00"
    assert rows["SAME"]["available_balance"] is None
    assert rows["BRK"]["available_balance"] is None
    assert rows["NEW"]["available_balance"] == "15.00"
    assert rows["NONE"]["available_balance"] is None
