"""Stale-value reminder for hand-valued assets (#568)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from purser import assets, digest

TODAY = datetime.now(UTC).date()


def _asset(client: TestClient, **extra: Any) -> dict[str, Any]:
    resp = client.post(
        "/accounts",
        json={
            "name": "House",
            "type": "property",
            "path": f"assets.house_{uuid.uuid4().hex[:6]}",
            **extra,
        },
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def _change(client: TestClient, account: str, days_ago: int, amount: str = "1000.00") -> None:
    resp = client.post(
        f"/accounts/{account}/value-changes",
        json={"as_of": (TODAY - timedelta(days=days_ago)).isoformat(), "amount": amount},
    )
    assert resp.status_code == 201, resp.text


def _events(client: TestClient) -> list[dict[str, Any]]:
    return list(client.get("/events", params={"type": "asset.stale"}).json())


class TestStale:
    def test_an_old_value_is_stale_and_alerts_once(
        self, admin_client: TestClient, db_session: Session
    ) -> None:
        house = _asset(admin_client, revalue_after_days=35)
        _change(admin_client, house["id"], days_ago=50)
        detail = admin_client.get(f"/accounts/{house['id']}").json()["asset"]
        assert detail["revalue_after_days"] == 35
        assert detail["last_valued"] == (TODAY - timedelta(days=50)).isoformat()
        assert detail["stale"] is True

        assert assets.emit_stale(db_session) == 1
        assert assets.emit_stale(db_session) == 0  # same stale value: never repeats
        db_session.commit()
        (event,) = _events(admin_client)
        assert event["payload"]["account"] == house["path"] if "payload" in event else True
        brief = digest.whats_new(db_session)
        assert [s["account"] for s in brief["stale_assets"]] == [house["path"]]

        # A refreshed value clears it; going stale again alerts again.
        _change(admin_client, house["id"], days_ago=0)
        db_session.expire_all()
        assert admin_client.get(f"/accounts/{house['id']}").json()["asset"]["stale"] is False
        assert digest.whats_new(db_session)["stale_assets"] == []
        assert assets.emit_stale(db_session, today=TODAY + timedelta(days=40)) == 1

    def test_a_target_that_posts_nothing_still_refreshes(self, admin_client: TestClient) -> None:
        """The monthly estimate that did not move records a value and posts
        nothing; it still counts as the value's date."""
        house = _asset(admin_client, revalue_after_days=35)
        _change(admin_client, house["id"], days_ago=60, amount="500000.00")
        resp = admin_client.post(
            f"/accounts/{house['id']}/reconcile",
            json={"as_of": TODAY.isoformat(), "statement_balance": "500000.00"},
        )
        assert resp.status_code == 200 and resp.json()["adjustment_tx_id"] is None
        asset = admin_client.get(f"/accounts/{house['id']}").json()["asset"]
        assert asset["last_valued"] == TODAY.isoformat()
        assert asset["stale"] is False

    def test_a_reversed_change_does_not_count(self, admin_client: TestClient) -> None:
        house = _asset(admin_client, revalue_after_days=10)
        _change(admin_client, house["id"], days_ago=30)
        _change(admin_client, house["id"], days_ago=1)
        rows = admin_client.get("/transactions", params={"account": house["id"]}).json()["items"]
        newest = max(rows, key=lambda r: r["date"])
        admin_client.post(f"/value-changes/{newest['id']}/reverse")
        asset = admin_client.get(f"/accounts/{house['id']}").json()["asset"]
        assert asset["last_valued"] == (TODAY - timedelta(days=30)).isoformat()
        assert asset["stale"] is True

    def test_never_valued_with_a_reminder_is_stale_and_without_one_never(
        self, admin_client: TestClient
    ) -> None:
        reminded = _asset(admin_client, revalue_after_days=30)
        plain = _asset(admin_client)
        assert admin_client.get(f"/accounts/{reminded['id']}").json()["asset"]["stale"] is True
        assert admin_client.get(f"/accounts/{plain['id']}").json()["asset"]["stale"] is False

    def test_the_reminder_is_set_cleared_and_bounded(self, admin_client: TestClient) -> None:
        house = _asset(admin_client)
        set_ = admin_client.patch(f"/accounts/{house['id']}", json={"revalue_after_days": 90})
        assert set_.json()["asset"]["revalue_after_days"] == 90
        cleared = admin_client.patch(f"/accounts/{house['id']}", json={"revalue_after_days": None})
        assert cleared.json()["asset"]["revalue_after_days"] is None
        bad = admin_client.patch(f"/accounts/{house['id']}", json={"revalue_after_days": 0})
        assert bad.status_code == 422

    def test_a_closed_asset_is_never_stale(
        self, admin_client: TestClient, db_session: Session
    ) -> None:
        house = _asset(admin_client, revalue_after_days=5)
        assert admin_client.post(f"/accounts/{house['id']}/close", json={}).status_code == 200
        assert assets.emit_stale(db_session) == 0
