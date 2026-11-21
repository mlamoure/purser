"""Read-only feed inventory (#482): every delivered account by bank login,
connected or not, with freshness and issues; ids and account numbers
withheld from non-admin API tokens; the MCP summary stays small."""

import json
import uuid
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

    def test_login_issue_named_without_the_id_prefix(self, harness: FeedHarness) -> None:
        """#559: the error list may name a login by its id without the
        provider prefix the connection list carries."""
        payload = _payload()
        payload["errlist"][0]["conn_id"] = "DEMO-2"  # the login is CON-DEMO-2
        harness.use_payload(payload)
        connection_id = harness.connect()
        _only(connection_id)
        harness.sync(connection_id)
        (connection,) = harness.client.get("/connections/inventory").json()
        first, other = connection["logins"]
        assert first["issue"] is None
        assert other["conn_id"] == OTHER["conn_id"]
        assert other["issue"] == {"code": "con.auth", "message": "Log in again"}

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


class TestIgnoredFeedAccount:
    """#560: an unmapped feed account an admin chose not to track."""

    def _ignore(self, harness: FeedHarness, connection_id: str, **body: Any) -> Any:
        return harness.client.post(f"/connections/{connection_id}/ignore", json=body)

    def test_ignored_account_is_not_reported_as_waiting(self, harness: FeedHarness) -> None:
        connection_id = _ready(harness)
        assert _attention_names() == {"Brokerage", "New Card"}
        resp = self._ignore(harness, connection_id, external_id="ACT-NEW")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"external_id": "ACT-NEW", "ignored": True}

        rows = harness.client.get(f"/connections/{connection_id}/discovered").json()
        assert {r["external_id"]: r["ignored"] for r in rows} == {
            "ACT-CHK": False,
            "ACT-BRK": False,
            "ACT-NEW": True,
        }
        (connection,) = harness.client.get("/connections/inventory").json()
        new_card = connection["logins"][1]["accounts"][0]
        assert (new_card["connected"], new_card["ignored"]) == (False, True)
        assert _attention_names() == {"Brokerage"}  # still stale; the card is by choice

    def test_ignore_is_audited_once_and_reversible(self, harness: FeedHarness) -> None:
        connection_id = _ready(harness)
        for _ in range(2):  # a repeat changes nothing and records nothing
            assert self._ignore(harness, connection_id, external_id="ACT-NEW").status_code == 200
        assert _audit_actions(connection_id) == ["feed_account.ignore"]
        resp = self._ignore(harness, connection_id, external_id="ACT-NEW", ignored=False)
        assert resp.json()["ignored"] is False
        assert _audit_actions(connection_id) == ["feed_account.ignore", "feed_account.unignore"]
        assert "New Card" in _attention_names()

    def test_only_a_delivered_unmapped_account_is_ignored(self, harness: FeedHarness) -> None:
        connection_id = _ready(harness)
        mapped = self._ignore(harness, connection_id, external_id="ACT-CHK")
        assert mapped.status_code == 422 and "assets.checking" in mapped.json()["detail"]
        assert self._ignore(harness, connection_id, external_id="ACT-NONE").status_code == 422

    def test_mapping_an_ignored_account_tracks_it(self, harness: FeedHarness) -> None:
        connection_id = _ready(harness)
        self._ignore(harness, connection_id, external_id="ACT-NEW")
        harness.map_new(connection_id, "ACT-NEW", "liabilities.new_card")
        rows = harness.client.get(f"/connections/{connection_id}/discovered").json()
        card = next(r for r in rows if r["external_id"] == "ACT-NEW")
        assert (card["ignored"], card["mapped_account_path"]) == (False, "liabilities.new_card")
        with db.get_sessionmaker()() as session:
            from purser.feed import service

            assert service.ignored_external_ids(session, uuid.UUID(connection_id)) == set()

    def test_ignore_needs_admin(self, harness: FeedHarness) -> None:
        connection_id = _ready(harness)
        token = _token(harness, ["read", "write"])
        resp = harness.client.post(
            f"/connections/{connection_id}/ignore",
            json={"external_id": "ACT-NEW"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403, resp.text


def _attention_names() -> set[str]:
    with db.get_sessionmaker()() as session:
        return {a["name"] for a in inventory.feed_status(session)["attention"]}


def _audit_actions(connection_id: str) -> list[str]:
    from sqlalchemy import text

    with db.get_engine().connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT action FROM audit_log WHERE entity_id = :id "
                    "AND action LIKE 'feed_account.%' ORDER BY at, id"
                ),
                {"id": connection_id},
            ).scalars()
        )


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
