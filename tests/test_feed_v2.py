"""SimpleFIN v2 through the feed pipeline (#479): the request asks for v2,
v1 and v2 deliveries of the same data are one ledger, structured issues set
the status, and an id two logins share is never imported."""

import uuid
from typing import Any

import httpx
from hypothesis import HealthCheck, given, settings
from sqlalchemy import Engine, text

from tests import test_feed
from tests.test_feed import (
    ACCESS_URL,
    FeedHarness,
    _epoch,
    _last_sync_after,
    account_of,
    payload_of,
    sf_transactions,
)

# The feed harness fixtures, re-exported by assignment (not import) so the
# test parameters that take them by name do not read as redefinitions.
feed_env = test_feed.feed_env
harness = test_feed.harness

CONN = {
    "conn_id": "CON-DEMO-1",
    "name": "Demo Bank - Pat",
    "org_id": "ORG-DEMO",
    "org_name": "Demo Bank",
    "org_url": "https://bank.example.com",
    "sfin_url": "https://sfin.example.com",
}


def as_v2(v1: dict[str, Any], errlist: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The same AccountSet in the v2 shape: org -> conn_id, errors -> errlist."""
    accounts = []
    for account in v1["accounts"]:
        moved = {k: v for k, v in account.items() if k != "org"}
        moved["conn_id"] = CONN["conn_id"]
        accounts.append(moved)
    return {"errlist": errlist or [], "connections": [CONN], "accounts": accounts}


def _import_count(engine: Engine) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT COUNT(*) FROM transactions WHERE origin = 'import'")
        ).scalar_one()


def _hashes(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        return sorted(
            conn.execute(
                text("SELECT content_hash FROM transactions WHERE origin = 'import'")
            ).scalars()
        )


class TestRequest:
    def test_fetch_asks_for_version_2(self, harness: FeedHarness) -> None:
        seen: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/claim/TEST123"):
                return httpx.Response(200, text=ACCESS_URL)
            seen.append(request.url.params.get("version"))
            return httpx.Response(200, json=as_v2(payload_of([])))

        harness.use_transport(httpx.MockTransport(handler))
        harness.sync(harness.connect())
        assert seen == ["2"]


class TestOneLedgerAcrossVersions:
    @settings(
        max_examples=10, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(transactions=sf_transactions)
    def test_v1_then_v2_of_the_same_rows_adds_nothing(
        self,
        harness: FeedHarness,
        migrated_engine: Engine,
        transactions: list[dict[str, Any]],
    ) -> None:
        harness.use_payload(payload_of(transactions))
        connection_id = harness.connect()
        harness.sync(connection_id)
        harness.map_new(connection_id, "Demo Checking", f"assets.c{uuid.uuid4().hex[:8]}")
        before = _hashes(migrated_engine)
        assert before

        harness.use_payload(as_v2(payload_of(transactions)))
        result = harness.sync(connection_id)
        assert result["imported"] == 0
        assert _hashes(migrated_engine) == before

    def test_v2_first_then_v1_adds_nothing(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        rows = [
            {"id": "a", "posted": _epoch(3), "amount": "-12.34", "description": "Coffee"},
            {"id": "b", "posted": _epoch(4), "amount": "250.00", "description": "Refund"},
        ]
        harness.use_payload(as_v2(payload_of(rows)))
        connection_id = harness.connect()
        harness.sync(connection_id)
        harness.map_new(connection_id, "Demo Checking", "assets.checking")
        assert _import_count(migrated_engine) == 2
        harness.use_payload(payload_of(rows))
        assert harness.sync(connection_id)["imported"] == 0
        assert _import_count(migrated_engine) == 2


class TestIssuesAndStatus:
    def test_connection_issue_flags_and_is_structured(self, harness: FeedHarness) -> None:
        errlist = [{"code": "con.auth", "msg": "Log in again", "conn_id": CONN["conn_id"]}]
        harness.use_payload(as_v2(payload_of([]), errlist))
        connection_id = harness.connect()
        result = harness.sync(connection_id)
        assert result["errors"] == ["Log in again"]
        assert result["issues"] == [
            {
                "code": "con.auth",
                "msg": "Log in again",
                "conn_id": CONN["conn_id"],
                "account_id": None,
            }
        ]
        health = harness.health_of(connection_id)
        assert health["status"] == "warning"
        assert health["last_errors"] == ["Log in again"]
        assert health["last_issues"][0]["code"] == "con.auth"
        assert health["protocol_version"] == 2
        after = _last_sync_after()
        assert after["issues"][0]["code"] == "con.auth"
        assert after["protocol_version"] == 2

    def test_bridge_usage_warning_is_listed_but_not_flagged(self, harness: FeedHarness) -> None:
        errlist = [{"code": "gen.api", "msg": "Requested date range exceeds recommended range"}]
        harness.use_payload(as_v2(payload_of([]), errlist))
        connection_id = harness.connect()
        harness.sync(connection_id)
        health = harness.health_of(connection_id)
        assert health["status"] == "active"
        assert health["last_issues"][0]["code"] == "gen.api"

    def test_recovery_clears_the_flag(self, harness: FeedHarness) -> None:
        errlist = [{"code": "act.failed", "msg": "Try later", "account_id": "Demo Checking"}]
        harness.use_payload(as_v2(payload_of([]), errlist))
        connection_id = harness.connect()
        harness.sync(connection_id)
        assert harness.health_of(connection_id)["status"] == "warning"
        harness.use_payload(as_v2(payload_of([])))
        harness.sync(connection_id)
        health = harness.health_of(connection_id)
        assert health["status"] == "active" and health["last_issues"] == []

    def test_v1_error_still_flags(self, harness: FeedHarness) -> None:
        payload = payload_of([])
        payload["errors"] = ["Connection to Demo Bank may need attention. Auth required"]
        harness.use_payload(payload)
        connection_id = harness.connect()
        harness.sync(connection_id)
        health = harness.health_of(connection_id)
        assert health["status"] == "warning"
        assert health["last_issues"][0]["code"] == "gen."
        assert health["protocol_version"] == 1


class TestDiscovery:
    def test_v2_discovery_names_the_login(self, harness: FeedHarness) -> None:
        harness.use_payload(as_v2(payload_of([])))
        connection_id = harness.connect()
        harness.sync(connection_id)
        (row,) = harness.client.get(f"/connections/{connection_id}/discovered").json()
        assert row["org"] == "Demo Bank - Pat"
        assert row["conn_id"] == CONN["conn_id"]
        assert row["ambiguous"] is False

    def test_v1_discovery_keeps_the_org_name(self, harness: FeedHarness) -> None:
        harness.use_payload(payload_of([]))
        connection_id = harness.connect()
        harness.sync(connection_id)
        (row,) = harness.client.get(f"/connections/{connection_id}/discovered").json()
        assert row["org"] == "Demo Bank" and row["conn_id"] is None


class TestSharedIdAcrossLogins:
    def test_an_id_two_logins_share_is_never_imported(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        row = {"id": "x", "posted": _epoch(5), "amount": "-9.99", "description": "Thing"}
        first = account_of([row])
        second = account_of([dict(row, amount="-1.00")])
        payload = as_v2({"accounts": [first, second]})
        payload["connections"].append(dict(CONN, conn_id="CON-DEMO-2", name="Demo Bank - Sam"))
        payload["accounts"][1]["conn_id"] = "CON-DEMO-2"
        harness.use_payload(payload)
        connection_id = harness.connect()
        harness.sync(connection_id)
        harness.map_new(connection_id, "Demo Checking", "assets.checking")
        assert _import_count(migrated_engine) == 0
        result = harness.sync(connection_id)
        assert result["skipped_ambiguous_accounts"] == 2
        assert result["imported"] == 0
        rows = harness.client.get(f"/connections/{connection_id}/discovered").json()
        assert [r["ambiguous"] for r in rows] == [True, True]
        assert {r["conn_id"] for r in rows} == {"CON-DEMO-1", "CON-DEMO-2"}
