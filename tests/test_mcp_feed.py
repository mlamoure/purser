"""The feed MCP tools over a live MCP session (#491): feed_status and
refetch_feed called with real scoped tokens.

The SimpleFIN client is stubbed at its own boundary (``fetch_accounts``,
``claim_access_url``) rather than by patching ``httpx.Client`` the way the
feed harness does: the MCP test client speaks HTTP through httpx too."""

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text

from purser.feed import simplefin
from tests import test_mcp
from tests.test_feed import ACCESS_URL, SETUP_TOKEN, account_of
from tests.test_feed_v2 import as_v2
from tests.test_mcp import LiveServer, _mcp_call

live_server = test_mcp.live_server

ACCOUNT = "MCP Checking"
PATH = "assets.mcp_checking"


def _row(days_ago: int, amount: str, desc: str) -> dict[str, Any]:
    posted = int((datetime.now(UTC) - timedelta(days=days_ago)).timestamp())
    return {"id": f"m-{days_ago}", "posted": posted, "amount": amount, "description": desc}


class StubBridge:
    """Serves the rows inside each request's window (end-date exclusive)."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []

    def fetch(self, access_url: str, **kwargs: Any) -> dict[str, Any]:
        assert access_url == ACCESS_URL  # decrypted from the store, never logged
        start: date | None = kwargs.get("start")
        end: date | None = kwargs.get("end")
        self.calls.append({"start": start, "end": end, "accounts": kwargs.get("accounts")})
        served = [
            row
            for row in self.rows
            if (start is None or _day(row) >= start) and (end is None or _day(row) < end)
        ]
        return as_v2({"accounts": [account_of(served, account_id=ACCOUNT, balance_date=_now())]})


def _day(row: dict[str, Any]) -> date:
    return datetime.fromtimestamp(int(row["posted"])).date()


def _now() -> int:
    return int(datetime.now(UTC).timestamp())


@pytest.fixture
def bridge(monkeypatch: pytest.MonkeyPatch) -> Iterator[StubBridge]:
    stub = StubBridge()
    monkeypatch.setattr(simplefin, "claim_access_url", lambda token, client=None: ACCESS_URL)
    monkeypatch.setattr(simplefin, "fetch_accounts", stub.fetch)
    yield stub


@pytest.fixture
def connected(admin_client: TestClient, bridge: StubBridge, migrated_engine: Engine) -> str:
    resp = admin_client.post("/connections", json={"setup_token": SETUP_TOKEN})
    assert resp.status_code == 201, resp.text
    connection_id = str(resp.json()["id"])
    # Connections outlive a test (raw_fetches is append-only): keep only ours.
    with migrated_engine.begin() as conn:
        conn.execute(
            text("UPDATE connections SET status = 'disabled' WHERE id <> :id"),
            {"id": connection_id},
        )
    assert admin_client.post(f"/connections/{connection_id}/sync").status_code == 200
    resp = admin_client.post(
        f"/connections/{connection_id}/map",
        json={
            "external_id": ACCOUNT,
            "create": {"name": "MCP Checking", "type": "checking", "path": PATH},
        },
    )
    assert resp.status_code == 200, resp.text
    return connection_id


def _token(admin_client: TestClient, name: str, scopes: list[str]) -> str:
    resp = admin_client.post("/tokens", json={"name": name, "scopes": scopes})
    assert resp.status_code in (200, 201), resp.text
    return str(resp.json()["token"])


class TestFeedToolsOverMcp:
    def test_feed_status_reads_with_a_read_token(
        self, live_server: LiveServer, admin_client: TestClient, connected: str
    ) -> None:
        token = _token(admin_client, "reader", ["read"])
        (status,) = _mcp_call(live_server.url, token, [("feed_status", {})])
        assert [login["status"] for login in status["logins"]] == ["ok"]
        assert status["logins"][0]["connected"] == 1
        assert status["attention"] == []
        assert ACCOUNT not in str(status)  # SimpleFIN ids never reach an agent

    def test_refetch_needs_write_and_is_audited(
        self,
        live_server: LiveServer,
        admin_client: TestClient,
        connected: str,
        bridge: StubBridge,
        migrated_engine: Engine,
    ) -> None:
        bridge.rows = [_row(30, "-30.00", "Missed row")]
        start = (date.today() - timedelta(days=40)).isoformat()
        end = (date.today() - timedelta(days=20)).isoformat()
        args = {"start": start, "end": end, "accounts": [PATH]}

        reader = _token(admin_client, "reader2", ["read"])
        (refused,) = _mcp_call(live_server.url, reader, [("refetch_feed", args)])
        assert "error" in refused

        writer = _token(admin_client, "writer", ["read", "write"])
        calls_before = len(bridge.calls)
        first, again = _mcp_call(
            live_server.url, writer, [("refetch_feed", args), ("refetch_feed", args)]
        )
        assert (first["imported"], first["accounts"]) == (1, [PATH])
        assert again["imported"] == 0  # idempotent
        assert all(call["accounts"] == [ACCOUNT] for call in bridge.calls[calls_before:])

        from purser import db
        from purser.models import AuditLog

        with db.get_sessionmaker()() as session:
            rows = session.execute(
                select(AuditLog).where(AuditLog.action == "connection.refetch")
            ).scalars()
            mine = [r for r in rows if str(r.entity_id) == connected]
        assert len(mine) == 2
        assert all(r.actor_token_id is not None for r in mine)
