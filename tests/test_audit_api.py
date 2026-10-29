"""/audit filters + keyset pagination and /audit/sessions gap-splitting."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

T0 = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def _seed(session: Session, *, at: datetime, kind: str, action: str, request_id=None) -> None:
    session.execute(
        text(
            "INSERT INTO audit_log (at, actor_token_id, action, entity_type, kind, request_id)"
            " VALUES (:at, NULL, :action, 'system', :kind, :request_id)"
        ),
        {"at": at, "action": action, "kind": kind, "request_id": request_id},
    )


def test_kind_defaults_to_change_and_check_rejects_garbage(
    clean_db: None, db_session: Session
) -> None:
    db_session.execute(
        text("INSERT INTO audit_log (action, entity_type) VALUES ('legacy.row', 'system')")
    )
    kind = db_session.execute(
        text("SELECT kind FROM audit_log WHERE action = 'legacy.row'")
    ).scalar_one()
    assert kind == "change"
    with pytest.raises(Exception, match="audit_log_kind_valid"):
        db_session.execute(
            text("INSERT INTO audit_log (action, entity_type, kind) VALUES ('x', 'y', 'bogus')")
        )
    db_session.rollback()


def test_filters(admin_client: TestClient, db_session: Session) -> None:
    request_id = str(uuid.uuid4())
    _seed(db_session, at=T0, kind="read", action="GET /accounts", request_id=request_id)
    _seed(db_session, at=T0 + timedelta(minutes=1), kind="write", action="POST /transactions")
    db_session.commit()

    def items(**params: object) -> list[dict]:
        resp = admin_client.get("/audit", params=params)  # type: ignore[arg-type]
        assert resp.status_code == 200, resp.text
        return resp.json()["items"]

    assert {i["action"] for i in items(kind="read", unattributed=True)} == {"GET /accounts"}
    assert {i["action"] for i in items(kind="write", unattributed=True)} == {"POST /transactions"}
    assert items(request_id=request_id)[0]["action"] == "GET /accounts"
    assert items(action="POST /transactions", unattributed=True)[0]["kind"] == "write"
    assert (
        items(since=(T0 + timedelta(seconds=30)).isoformat(), until=T0.isoformat()) == []
    )  # inverted window matches nothing
    assert {i["entity_type"] for i in items(entity_type="system", unattributed=True)} == {"system"}


def test_keyset_pagination_is_stable_under_inserts(
    admin_client: TestClient, db_session: Session
) -> None:
    for n in range(5):
        _seed(db_session, at=T0 + timedelta(minutes=n), kind="read", action=f"seed.{n}")
    db_session.commit()

    seen: list[str] = []
    cursor: str | None = None
    for page in range(3):
        params: dict[str, object] = {
            "unattributed": True,
            "order": "asc",
            "limit": 2,
            "since": T0.isoformat(),
            "until": (T0 + timedelta(hours=1)).isoformat(),
        }
        if cursor:
            params["cursor"] = cursor
        resp = admin_client.get("/audit", params=params)  # type: ignore[arg-type]
        body = resp.json()
        seen += [i["action"] for i in body["items"]]
        if page == 0:
            # a row landing mid-pagination must not shift later pages
            _seed(db_session, at=T0 + timedelta(minutes=2, seconds=30), kind="read", action="mid")
            db_session.commit()
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert [a for a in seen if a.startswith("seed.")] == [f"seed.{n}" for n in range(5)]
    assert "mid" in seen  # picked up in order, not skipped

    resp = admin_client.get("/audit", params={"cursor": "garbage"})
    assert resp.status_code == 422


def test_sessions_gap_split_and_counts(admin_client: TestClient, db_session: Session) -> None:
    # burst 1: two rows 5 minutes apart; burst 2 after a 90-minute gap
    _seed(db_session, at=T0, kind="read", action="a")
    _seed(db_session, at=T0 + timedelta(minutes=5), kind="write", action="b")
    _seed(db_session, at=T0 + timedelta(minutes=95), kind="change", action="c")
    db_session.commit()

    # bound the window: setup/login bookkeeping writes unattributed change
    # rows at "now" that would otherwise appear as a third System session
    resp = admin_client.get(
        "/audit/sessions",
        params={
            "since": (T0 - timedelta(days=1)).isoformat(),
            "until": (T0 + timedelta(hours=2)).isoformat(),
        },
    )
    assert resp.status_code == 200, resp.text
    system = [s for s in resp.json()["items"] if s["actor_token_id"] is None]
    assert len(system) == 2
    assert all(s["token_name"] == "System" for s in system)
    newest, oldest = system[0], system[1]  # ordered by ended_at desc
    assert (oldest["reads"], oldest["writes"], oldest["changes"]) == (1, 1, 0)
    assert (newest["reads"], newest["writes"], newest["changes"]) == (0, 0, 1)


def test_audit_requires_admin(admin_client: TestClient) -> None:
    minted = admin_client.post("/tokens", json={"name": "reader", "scopes": ["read"]}).json()
    headers = {"Authorization": f"Bearer {minted['token']}"}
    assert admin_client.get("/audit", headers=headers).status_code == 403
    assert admin_client.get("/audit/sessions", headers=headers).status_code == 403
