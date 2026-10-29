"""REST read/write call-row capture (issue #122): the AuditMiddleware matrix."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from purser.models import AuditLog


def _call_rows(session: Session, kind: str | None = None) -> list[AuditLog]:
    stmt = select(AuditLog).where(AuditLog.kind.in_(["read", "write"]))
    if kind:
        stmt = select(AuditLog).where(AuditLog.kind == kind)
    return list(session.execute(stmt.order_by(AuditLog.at)).scalars())


def test_authenticated_get_writes_read_call_row(
    admin_client: TestClient, db_session: Session
) -> None:
    resp = admin_client.get("/accounts", params={"include_balances": "true"})
    assert resp.status_code == 200

    rows = _call_rows(db_session, "read")
    assert len(rows) == 1
    row = rows[0]
    assert row.action == "GET /accounts"
    assert row.entity_type == "account"
    assert row.actor_token_id is not None
    assert row.request_id is not None
    assert row.detail is not None
    assert row.detail["method"] == "GET"
    assert row.detail["path"] == "/accounts"
    assert row.detail["status"] == 200
    assert row.detail["rows"] == 0  # clean instance: empty account list
    assert row.detail["query"] == {"include_balances": "true"}
    assert "ms" in row.detail


def test_unauthenticated_and_public_requests_produce_nothing(
    clean_db: None, client: TestClient, db_session: Session
) -> None:
    assert client.get("/accounts").status_code == 401
    assert client.get("/auth/setup/status").status_code == 200
    assert client.get("/healthz").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/llms.txt").status_code == 200
    assert _call_rows(db_session) == []


def test_forbidden_requests_are_logged(admin_client: TestClient, db_session: Session) -> None:
    minted = admin_client.post("/tokens", json={"name": "reader", "scopes": ["read"]}).json()
    resp = admin_client.get("/audit", headers={"Authorization": f"Bearer {minted['token']}"})
    assert resp.status_code == 403

    denied = [
        r
        for r in _call_rows(db_session, "read")
        if r.actor_token_id == uuid.UUID(minted["id"]) and r.detail["path"] == "/audit"
    ]
    assert len(denied) == 1
    assert denied[0].detail["status"] == 403


def test_write_call_row_shares_request_id_with_change_rows(
    admin_client: TestClient, db_session: Session
) -> None:
    resp = admin_client.put("/config", json={"transfer_auto_link": False})
    assert resp.status_code == 200

    calls = [r for r in _call_rows(db_session, "write") if r.action == "PUT /config"]
    assert len(calls) == 1
    call = calls[0]
    assert call.detail["status"] == 200
    changes = list(
        db_session.execute(
            select(AuditLog).where(AuditLog.kind == "change", AuditLog.action == "config.update")
        ).scalars()
    )
    assert len(changes) == 1
    assert changes[0].request_id == call.request_id


def test_entity_id_captured_from_single_uuid_path_param(
    admin_client: TestClient, db_session: Session
) -> None:
    minted = admin_client.post("/tokens", json={"name": "temp", "scopes": ["read"]}).json()
    assert admin_client.delete(f"/tokens/{minted['id']}").status_code in (200, 204)

    deletes = [r for r in _call_rows(db_session, "write") if "DELETE /tokens" in r.action]
    assert len(deletes) == 1
    assert deletes[0].entity_id == uuid.UUID(minted["id"])
    assert deletes[0].detail["path"] == "/tokens/{token_id}"


def test_audit_failure_never_fails_the_request(
    admin_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from purser import audit

    def boom(**_: object) -> None:
        raise RuntimeError("audit exploded")

    monkeypatch.setattr(audit, "record_call", boom)
    resp = admin_client.get("/accounts")
    assert resp.status_code == 200
