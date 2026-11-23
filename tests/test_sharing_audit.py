# ruff: noqa: F811 -- pytest fixture re-export
"""Sharing evidence is about returned data, not requested access (#525)."""

import asyncio
import csv
import io
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from purser import audit, sharing
from purser.models import ApiToken, AuditLog, AuditReceipt
from tests.test_mcp import LiveServer, live_server  # noqa: F401


def mint(client: TestClient, name: str = "budget-assistant") -> dict:
    result = client.post("/tokens", json={"name": name, "scopes": ["read"]})
    assert result.status_code == 201, result.text
    return result.json()


def headers(token: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {token['token']}"}


def test_inventory_contains_fields_not_values() -> None:
    summary = sharing.summarize(
        {
            "items": [
                {
                    "account": "assets.checking",
                    "date": "2026-10-02",
                    "payee": "py_synthetic",
                    "amount": "12.34",
                    "memo": "[withheld]",
                    "withheld": ["description"],
                }
            ],
            "next_cursor": "opaque",
        },
        requested={"since": "2026-10-01", "q": "private search", "account": "assets.other"},
    )
    assert summary["record_count"] == 1
    assert summary["accounts"] == ["assets.checking"]
    assert summary["requested_accounts"] == ["assets.other"]
    assert summary["returned_dates"] == {"from": "2026-10-02", "to": "2026-10-02"}
    assert summary["sensitive_fields"] == ["amount"]
    assert summary["withheld"] == ["description"]
    assert summary["response_has_more"]
    assert "12.34" not in json.dumps(summary)
    assert "private search" not in json.dumps(summary)
    assert "py_synthetic" not in json.dumps(summary)
    assert "Merchant references" in summary["data_types"]


def test_empty_duplicate_mcp_tiers_and_aggregate_counts() -> None:
    from fastmcp.tools import ToolResult
    from mcp.types import TextContent

    result = ToolResult(
        content=[TextContent(type="text", text="[]")], structured_content={"result": []}
    )
    assert sharing.summarize(result)["empty"]
    assert sharing.summarize({"total": "12.34", "currency": "USD"})["record_count"] is None
    assert not sharing.summarize({"items": [], "total": "12.34"})["empty"]
    assert sharing.summarize("unstructured narrative")["partial"]
    assert "private" not in json.dumps(sharing.summarize({"token": "private"}))


def test_rest_summary_identity_and_server_filters(
    admin_client: TestClient, db_session: Session
) -> None:
    account = admin_client.post(
        "/accounts", json={"name": "Checking", "type": "checking", "path": "assets.checking"}
    ).json()
    token = mint(admin_client)
    other = mint(admin_client, "other")
    assert admin_client.get("/accounts", headers=headers(token)).status_code == 200
    assert admin_client.get("/accounts", headers=headers(other)).status_code == 200
    params = {"integration_id": token["integration_id"], "account_id": account["id"]}
    response = admin_client.get("/audit/activity", params=params)
    assert response.status_code == 200, response.text
    rows = response.json()["items"]
    assert len(rows) == 1
    entry = rows[0]
    assert entry["outcome"] == "shared"
    assert entry["identity"]["name"] == "budget-assistant"
    assert entry["identity"]["historical"]
    assert entry["identity"]["authorization"]["granted_by"] == "admin@example.com"
    assert entry["sharing"]["record_count"] == 1
    assert entry["sharing"]["accounts"] == ["assets.checking"]
    assert (
        admin_client.get(
            "/audit/activity", params={**params, "data_type": "Individual amounts"}
        ).json()["items"]
        == []
    )
    stored = db_session.get(AuditLog, uuid.UUID(entry["id"]))
    assert stored is not None and stored.account_ids == [uuid.UUID(account["id"])]
    assert db_session.get(AuditReceipt, stored.id) is not None


def test_empty_denied_and_legacy_are_distinct(
    admin_client: TestClient, db_session: Session
) -> None:
    token = mint(admin_client)
    admin_client.get("/accounts", headers=headers(token))
    admin_client.post(
        "/accounts",
        json={"name": "Denied", "type": "checking", "path": "assets.denied"},
        headers=headers(token),
    )
    db_session.add(
        AuditLog(
            actor_token_id=uuid.UUID(token["id"]),
            kind="read",
            action="GET /accounts",
            entity_type="account",
            disclosed=["payee"],
        )
    )
    db_session.commit()
    params = {"integration_id": token["integration_id"]}
    entries = admin_client.get("/audit/activity", params=params).json()["items"]
    assert {x["outcome"] for x in entries} == {"empty", "denied", "legacy"}
    old = next(x for x in entries if x["outcome"] == "legacy")
    assert old["sharing"] is None
    assert old["identity"]["historical"] is False
    assert old["extra_fields"] == []
    assert old["requested_extra"] == ["payee"]
    assert (
        admin_client.get("/audit/activity", params={**params, "extra": True}).json()["items"] == []
    )
    denied = admin_client.get("/audit/activity", params={**params, "outcome": "denied"}).json()[
        "items"
    ]
    assert len(denied) == 1


def test_response_withheld_when_capture_fails(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = mint(admin_client)

    def unavailable(**kwargs):
        assert kwargs["strict"]
        raise audit.CaptureUnavailable("response data withheld")

    monkeypatch.setattr(audit, "record_call", unavailable)
    response = admin_client.get("/accounts", headers=headers(token))
    assert response.status_code == 503
    assert response.json() == {"detail": "response data withheld"}


def test_missing_receipt_is_uncertain(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = mint(admin_client)
    monkeypatch.setattr(audit, "record_receipt", lambda *args: None)
    assert admin_client.get("/accounts", headers=headers(token)).status_code == 200
    rows = admin_client.get(
        "/audit/activity", params={"integration_id": token["integration_id"]}
    ).json()["items"]
    assert rows[0]["outcome"] == "uncertain"


def test_oversized_response_is_withheld(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = mint(admin_client)
    admin_client.post(
        "/accounts", json={"name": "Private account", "type": "checking", "path": "assets.test"}
    )
    monkeypatch.setattr(sharing, "MAX_BODY", 8)
    response = admin_client.get("/accounts", headers=headers(token))
    assert response.status_code == 413
    assert "Private account" not in response.text
    entries = admin_client.get(
        "/audit/activity", params={"integration_id": token["integration_id"]}
    ).json()["items"]
    assert entries[0]["outcome"] == "failed"
    assert entries[0]["sharing"]["partial"]


def test_commit_acknowledgment_retry_does_not_duplicate(
    admin_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = mint(admin_client)
    original = Session.commit
    lost_ack = True

    def commit(session):
        nonlocal lost_ack
        original(session)
        if lost_ack:
            lost_ack = False
            raise OSError("simulated lost commit acknowledgment")

    monkeypatch.setattr(Session, "commit", commit)
    request_id = uuid.uuid4()
    event = audit.record_call(
        actor_token_id=uuid.UUID(token["id"]),
        kind="read",
        action="test.retry",
        entity_type="account",
        entity_id=None,
        request_id=request_id,
        detail={},
        strict=True,
    )
    from sqlalchemy import select

    rows = db_session.scalars(select(AuditLog).where(AuditLog.request_id == request_id)).all()
    assert len(rows) == 1 and rows[0].id == event


def test_large_browser_response_does_not_claim_it_was_withheld(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin_client.post(
        "/accounts", json={"name": "Checking", "type": "checking", "path": "assets.test"}
    )
    monkeypatch.setattr(sharing, "MAX_BODY", 8)
    response = admin_client.get("/accounts")
    assert response.status_code == 200 and len(response.json()) == 1
    entries = admin_client.get("/audit/activity", params={"view": "technical"}).json()["items"]
    read = next(e for e in entries if e["action"] == "GET /accounts")
    assert read["outcome"] == "shared"
    assert read["sharing"]["partial"]
    assert "withheld" not in read["technical"]["error"]


def test_replacement_preserves_integration_and_old_snapshot(
    admin_client: TestClient, db_session: Session
) -> None:
    first = mint(admin_client)
    admin_client.get("/accounts", headers=headers(first))
    second = admin_client.post(
        "/tokens",
        json={"name": "new-credential", "scopes": ["read"], "replaces_token_id": first["id"]},
    ).json()
    assert second["integration_id"] == first["integration_id"]
    admin_client.get("/accounts", headers=headers(second))
    row = db_session.get(ApiToken, uuid.UUID(first["id"]))
    assert row is not None
    row.name = "renamed-after-call"
    db_session.commit()
    entries = admin_client.get(
        "/audit/activity", params={"integration_id": first["integration_id"]}
    ).json()["items"]
    assert {e["identity"]["name"] for e in entries} == {"budget-assistant", "new-credential"}
    assert admin_client.get("/accounts", headers=headers(first)).status_code == 200
    assert (
        admin_client.post(
            "/tokens", json={"name": "bad", "replaces_token_id": str(uuid.uuid4())}
        ).status_code
        == 422
    )
    admin_client.delete(f"/tokens/{first['id']}")
    access = admin_client.get(
        "/audit/activity", params={"view": "access", "integration_id": first["integration_id"]}
    ).json()["items"]
    assert {e["entity_id"] for e in access} == {first["id"], second["id"]}
    assert {e["action"] for e in access if e["kind"] == "change"} == {
        "token.create",
        "token.revoke",
    }
    assert all(e["identity"]["kind"] == "session" for e in access)


def test_paging_and_export_beyond_500_with_equal_timestamps(
    admin_client: TestClient, db_session: Session
) -> None:
    token = mint(admin_client, "=unsafe-name")
    stamp = datetime.now(UTC) - timedelta(days=1)
    for _ in range(503):
        db_session.add(
            AuditLog(
                at=stamp,
                actor_token_id=uuid.UUID(token["id"]),
                kind="read",
                action="GET /accounts",
                entity_type="account",
            )
        )
    db_session.commit()
    params = {"integration_id": token["integration_id"], "limit": 200}
    seen = []
    while True:
        response = admin_client.get("/audit/activity", params=params)
        assert response.status_code == 200, response.text
        page = response.json()
        seen.extend(x["id"] for x in page["items"])
        params["until"] = page["until"]
        if not page["next_cursor"]:
            break
        params["cursor"] = page["next_cursor"]
    assert len(seen) == len(set(seen)) == 503
    exported = admin_client.get(
        "/audit/export", params={"integration_id": token["integration_id"]}
    ).json()
    assert {x["id"] for x in exported["items"]} == set(seen)
    response = admin_client.get(
        "/audit/export", params={"format": "csv", "integration_id": token["integration_id"]}
    )
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 503
    assert rows[0]["integrator"] == "'=unsafe-name"


def test_new_audit_routes_admin_only_and_bad_filters(admin_client: TestClient) -> None:
    token = mint(admin_client)
    for path in ("/audit/activity", "/audit/options", "/audit/export"):
        assert admin_client.get(path, headers=headers(token)).status_code == 403
    assert (
        admin_client.get("/audit/activity", params={"since": "2026-11-01T00:00:00"}).status_code
        == 422
    )
    assert (
        admin_client.get(
            "/audit/activity",
            params={"since": "2026-11-02T00:00:00Z", "until": "2026-11-01T00:00:00Z"},
        ).status_code
        == 422
    )
    assert admin_client.get("/audit/activity", params={"cursor": "bad"}).status_code == 422
    options = admin_client.get("/audit/options").json()
    assert options["retention_days"] == 90
    assert options["retained_since"] is not None
    assert token["integration_id"] in {x["id"] for x in options["integrations"]}


def test_real_mcp_direct_resource_and_tool_have_one_record_each(
    admin_client: TestClient, live_server: LiveServer
) -> None:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    token = mint(admin_client)
    admin_client.post(
        "/accounts", json={"name": "Checking", "type": "checking", "path": "assets.checking"}
    )
    uri = "purser://forecast/assets.checking/30d"

    async def run() -> None:
        async with (
            httpx2.AsyncClient(headers=headers(token)) as http,
            streamable_http_client(f"{live_server.url}/mcp", http_client=http) as (read, write),
            ClientSession(read, write) as client,
        ):
            await client.initialize()
            result = await client.read_resource(uri)
            assert result.contents
            tool = await client.call_tool("read_series", {"uri": uri})
            assert not tool.is_error
            empty = await client.call_tool("search_transactions", {"withhold": ["payee"]})
            assert not empty.is_error

    asyncio.run(run())
    entries = admin_client.get(
        "/audit/activity", params={"integration_id": token["integration_id"]}
    ).json()["items"]
    assert len(entries) == 3
    assert {x["channel"] for x in entries} == {"resource", "mcp"}
    direct = next(x for x in entries if x["channel"] == "resource")
    assert direct["sharing"]["accounts"] == ["assets.checking"]
    assert direct["outcome"] == "shared"
    empty = next(x for x in entries if x["action"] == "mcp.search_transactions")
    assert empty["outcome"] == "empty"
    assert empty["extra_fields"] == []
