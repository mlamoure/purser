"""Read-only feed inventory (#482): every delivered account by bank login,
connected or not, with freshness and issues; ids and account numbers
withheld from non-admin API tokens; the MCP summary stays small."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from purser import db
from purser.feed import inventory
from tests import test_feed
from tests.test_feed import FeedHarness, account_of
from tests.test_feed_v2 import CONN, as_v2

feed_env = test_feed.feed_env
harness = test_feed.harness

OTHER = dict(CONN, conn_id="CON-DEMO-2", name="Other Bank")


def _days_ago(days: int) -> int:
    return int((datetime.now(UTC) - timedelta(days=days)).timestamp())


def _payload() -> dict[str, Any]:
    checking = account_of([], account_id="ACT-CHK", balance_date=_days_ago(0))
    checking["name"] = "Everyday Checking (1111)"
    brokerage = account_of([], account_id="ACT-BRK", balance_date=_days_ago(9))
    brokerage["name"] = "Brokerage *2222"
    brokerage["holdings"] = [{"symbol": "SYNTH", "shares": "1.0", "market_value": "10.00"}]
    card = account_of([], account_id="ACT-NEW", balance_date=_days_ago(0))
    card["name"] = "New Card (3333)"
    payload = as_v2({"accounts": [checking, brokerage, card]})
    payload["connections"].append(OTHER)
    payload["accounts"][2]["conn_id"] = OTHER["conn_id"]
    payload["errlist"] = [
        {"code": "con.auth", "msg": "Log in again", "conn_id": OTHER["conn_id"]},
        {"code": "act.missingdata", "msg": "Try later", "account_id": "ACT-BRK"},
    ]
    return payload


def _only(connection_id: str) -> None:
    """Feed tests keep earlier tests' connections (raw_fetches is
    append-only); disable them so the inventory shows this test's alone."""
    from sqlalchemy import text

    with db.get_engine().begin() as conn:
        conn.execute(
            text("UPDATE connections SET status = 'disabled' WHERE id <> :id"),
            {"id": connection_id},
        )


def _ready(harness: FeedHarness) -> str:
    harness.use_payload(_payload())
    connection_id = harness.connect()
    _only(connection_id)
    harness.sync(connection_id)
    harness.map_new(connection_id, "ACT-CHK", "assets.checking")
    harness.map_new(connection_id, "ACT-BRK", "assets.brokerage")
    harness.sync(connection_id)
    return connection_id


def _token(harness: FeedHarness, scopes: list[str]) -> str:
    resp = harness.client.post("/tokens", json={"name": f"t-{'-'.join(scopes)}", "scopes": scopes})
    assert resp.status_code in (200, 201), resp.text
    return str(resp.json()["token"])


class TestInventory:
    def test_session_sees_every_account_with_ids(self, harness: FeedHarness) -> None:
        connection_id = _ready(harness)
        (connection,) = harness.client.get("/connections/inventory").json()
        assert connection["connection_id"] == connection_id
        assert connection["protocol_version"] == 2
        first, other = connection["logins"]
        assert (first["institution"], first["conn_id"], first["issue"]) == (
            CONN["name"],
            CONN["conn_id"],
            None,
        )
        assert other["issue"] == {"code": "con.auth", "message": "Log in again"}
        checking, brokerage = first["accounts"]
        assert checking["name"] == "Everyday Checking (1111)"
        assert checking["external_id"] == "ACT-CHK"
        assert (checking["account"], checking["connected"], checking["stale"]) == (
            "assets.checking",
            True,
            False,
        )
        assert brokerage["stale"] and brokerage["days_since_balance"] == 9
        assert brokerage["holdings"] == 1
        assert brokerage["issue"]["code"] == "act.missingdata"
        (new_card,) = other["accounts"]
        assert (new_card["account"], new_card["connected"]) == (None, False)

    def test_api_tokens_get_no_ids_or_account_numbers(self, harness: FeedHarness) -> None:
        _ready(harness)
        token = _token(harness, ["read"])
        resp = harness.client.get(
            "/connections/inventory", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200, resp.text
        text = resp.text
        for secret in ("ACT-CHK", "ACT-NEW", "CON-DEMO", "1111", "2222", "3333"):
            assert secret not in text
        (connection,) = resp.json()
        assert connection["connection_id"] is None
        names = [a["name"] for login in connection["logins"] for a in login["accounts"]]
        assert names == ["Everyday Checking", "Brokerage", "New Card"]

    def test_v1_fetch_groups_by_org(self, harness: FeedHarness) -> None:
        harness.use_payload(test_feed.payload_of([]))
        connection_id = harness.connect()
        _only(connection_id)
        harness.sync(connection_id)
        (connection,) = harness.client.get("/connections/inventory").json()
        (login,) = connection["logins"]
        assert login["institution"] == "Demo Bank" and login["conn_id"] is None
        assert connection["protocol_version"] == 1


class TestFeedStatusSummary:
    def test_lists_logins_and_only_accounts_needing_attention(self, harness: FeedHarness) -> None:
        _ready(harness)
        with db.get_sessionmaker()() as session:
            status = inventory.feed_status(session)
        assert [(row["institution"], row["status"]) for row in status["logins"]] == [
            (CONN["name"], "ok"),
            (OTHER["name"], "needs_login"),
        ]
        assert status["logins"][0]["connected"] == 2
        attention = {row["name"]: row["problems"] for row in status["attention"]}
        assert attention == {
            "Brokerage": ["stale 9d", "act.missingdata"],
            "New Card": ["not_connected"],
        }
        rendered = json.dumps(status)
        assert "ACT-" not in rendered and "1111" not in rendered
        assert len(rendered) < 2000  # well inside the tool token budget

    def test_redact_name(self) -> None:
        assert inventory.redact_name("Rewards Card (1234)") == "Rewards Card"
        assert inventory.redact_name("CHECKING\xa0\xa0*5678 (5678)") == "CHECKING"
        assert inventory.redact_name("Cash Card-4321 (4321)") == "Cash Card"
        assert inventory.redact_name("Plan 401(k) Savings (_000)") == "Plan 401(k) Savings"
        assert inventory.redact_name(None) is None
