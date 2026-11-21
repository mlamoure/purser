"""Asset kinds, the loan link, equity and money put in (#567)."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_mcp import LiveServer, _mcp_call
from tests.test_mcp import live_server as live_server

TODAY = datetime.now(UTC).date()


def _create(client: TestClient, type_: str, name: str, **extra: Any) -> dict[str, Any]:
    path = f"{'liabilities' if type_ == 'loan' else 'assets'}.{name.lower()}_{uuid.uuid4().hex[:6]}"
    resp = client.post("/accounts", json={"name": name, "type": type_, "path": path, **extra})
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def _get(client: TestClient, account_id: str) -> dict[str, Any]:
    return dict(client.get(f"/accounts/{account_id}").json())


class TestAssetDetails:
    def test_an_asset_is_created_with_its_kind_date_and_description(
        self, admin_client: TestClient, db_session: Session
    ) -> None:
        house = _create(
            admin_client,
            "property",
            "House",
            asset_kind="house",
            acquired_on="2016-12-15",
            asset_description="Synthetic Lane 7",
        )
        assert {k: house["asset"][k] for k in ("kind", "acquired_on", "description")} == {
            "kind": "house",
            "acquired_on": "2016-12-15",
            "description": "Synthetic Lane 7",
        }
        # The description is narrative: encrypted at rest.
        stored = db_session.execute(
            text("SELECT description_enc FROM asset_details WHERE account_id = CAST(:a AS uuid)"),
            {"a": house["id"]},
        ).scalar_one()
        assert b"Synthetic Lane" not in bytes(stored)

    def test_every_asset_has_details_and_other_accounts_have_none(
        self, admin_client: TestClient
    ) -> None:
        plain = _create(admin_client, "property", "Ring")
        assert plain["asset"]["kind"] == "other"
        checking = _create(admin_client, "checking", "Checking")
        assert checking["asset"] is None
        refused = admin_client.post(
            "/accounts",
            json={"name": "X", "type": "checking", "path": "assets.x_kind", "asset_kind": "car"},
        )
        assert refused.status_code == 422

    def test_details_are_edited_and_cleared(self, admin_client: TestClient) -> None:
        car = _create(admin_client, "property", "Car", asset_kind="car", acquired_on="2024-03-01")
        resp = admin_client.patch(
            f"/accounts/{car['id']}",
            json={"asset_kind": "rv", "acquired_on": None, "asset_description": "Synthetic VIN"},
        )
        assert resp.status_code == 200, resp.text
        asset = resp.json()["asset"]
        assert {k: asset[k] for k in ("kind", "acquired_on", "description")} == {
            "kind": "rv",
            "acquired_on": None,
            "description": "Synthetic VIN",
        }
        audit = admin_client.get("/audit", params={"action": "asset.update"}).json()["items"]
        assert len(audit) >= 1
        bad = admin_client.patch(f"/accounts/{car['id']}", json={"asset_kind": "spaceship"})
        assert bad.status_code == 422
        future = admin_client.patch(
            f"/accounts/{car['id']}", json={"acquired_on": (TODAY + timedelta(days=5)).isoformat()}
        )
        assert future.status_code == 422


class TestEquity:
    def test_a_secured_loan_gives_the_asset_its_equity(self, admin_client: TestClient) -> None:
        house = _create(admin_client, "property", "House", asset_kind="house")
        loan = _create(admin_client, "loan", "Mortgage")
        admin_client.post(
            f"/accounts/{house['id']}/value-changes",
            json={"as_of": TODAY.isoformat(), "amount": "500000.00", "kind": "capital"},
        )
        admin_client.post(
            f"/accounts/{loan['id']}/valuations",
            json={"as_of": TODAY.isoformat(), "value": "-320000.00"},
        )
        linked = admin_client.patch(f"/accounts/{loan['id']}", json={"secured_by_id": house["id"]})
        assert linked.status_code == 200, linked.text
        assert linked.json()["secured_by_id"] == house["id"]

        detail = _get(admin_client, house["id"])
        assert Decimal(detail["equity"]) == Decimal("180000.00")
        assert detail["secured_loan_ids"] == [loan["id"]]
        listed = admin_client.get("/accounts", params={"include_balances": True}).json()
        (row,) = [a for a in listed if a["id"] == house["id"]]
        assert Decimal(row["equity"]) == Decimal("180000.00")

        cleared = admin_client.patch(f"/accounts/{loan['id']}", json={"secured_by_id": None})
        assert cleared.json()["secured_by_id"] is None
        assert _get(admin_client, house["id"])["secured_loan_ids"] == []

    def test_only_a_loan_is_secured_and_only_by_an_asset(self, admin_client: TestClient) -> None:
        house = _create(admin_client, "property", "House")
        checking = _create(admin_client, "checking", "Checking")
        loan = _create(admin_client, "loan", "Loan")
        not_a_loan = admin_client.patch(
            f"/accounts/{checking['id']}", json={"secured_by_id": house["id"]}
        )
        assert not_a_loan.status_code == 422
        not_an_asset = admin_client.patch(
            f"/accounts/{loan['id']}", json={"secured_by_id": checking["id"]}
        )
        assert not_an_asset.status_code == 422


class TestMoneyPutIn:
    def test_capital_is_an_entry_against_equity_capital(self, admin_client: TestClient) -> None:
        house = _create(admin_client, "property", "House")
        resp = admin_client.post(
            f"/accounts/{house['id']}/value-changes",
            json={
                "as_of": (TODAY - timedelta(days=400)).isoformat(),
                "amount": "450000.00",
                "kind": "capital",
                "note": "Purchase price",
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["kind"] == "capital"
        rows = admin_client.get("/transactions", params={"account": house["id"]}).json()["items"]
        (row,) = rows
        assert row["value_change"] is True and row["money_put_in"] is True
        tx = admin_client.get(f"/transactions/{row['id']}").json()
        assert tx["category_path"] == "equity.capital"
        undone = admin_client.post(f"/value-changes/{row['id']}/reverse")
        assert undone.status_code == 200, undone.text
        assert undone.json()["kind"] == "capital"
        assert Decimal(_get(admin_client, house["id"])["balance"]) == Decimal("0")

    def test_an_unknown_kind_is_refused(self, admin_client: TestClient) -> None:
        house = _create(admin_client, "property", "House")
        resp = admin_client.post(
            f"/accounts/{house['id']}/value-changes",
            json={"as_of": TODAY.isoformat(), "amount": "1.00", "kind": "gift"},
        )
        assert resp.status_code == 422


class TestMcp:
    def test_kind_link_and_listing_through_mcp(
        self, admin_client: TestClient, live_server: LiveServer
    ) -> None:
        house = _create(admin_client, "property", "House")
        loan = _create(admin_client, "loan", "Mortgage")
        admin_client.post(
            f"/accounts/{loan['id']}/valuations",
            json={"as_of": TODAY.isoformat(), "value": "-100.00"},
        )
        token = admin_client.post(
            "/tokens", json={"name": "agent", "scopes": ["read", "write"]}
        ).json()["token"]
        kind, link, change, listed = _mcp_call(
            live_server.url,
            token,
            [
                ("update_account", {"account": house["path"], "asset_kind": "house"}),
                ("update_account", {"account": loan["path"], "secured_by": house["path"]}),
                (
                    "record_value_change",
                    {
                        "account": house["path"],
                        "as_of": TODAY.isoformat(),
                        "amount": "1000.00",
                        "kind": "capital",
                    },
                ),
                ("list_accounts", {}),
            ],
        )
        assert kind["asset_kind"] == "house"
        assert link["secured_by"] == house["path"]
        assert change["kind"] == "capital"
        rows = {r["path"]: r for r in (listed if isinstance(listed, list) else listed["result"])}
        assert rows[house["path"]]["asset_kind"] == "house"
        assert Decimal(rows[house["path"]]["equity"]) == Decimal("900.00")
        assert rows[house["path"]]["secured_loans"] == [loan["path"]]
        assert rows[loan["path"]]["secured_by"] == house["path"]


class TestDelete:
    def test_deleting_an_asset_unsecures_its_loans(self, admin_client: TestClient) -> None:
        from purser.admin import account_delete
        from purser.db import get_sessionmaker

        house = _create(admin_client, "property", "House")
        loan = _create(admin_client, "loan", "Mortgage")
        admin_client.patch(f"/accounts/{loan['id']}", json={"secured_by_id": house["id"]})
        with get_sessionmaker()() as session:
            from purser import ledger

            account = ledger.get_account(session, uuid.UUID(house["id"]))
            summary = account_delete.execute_delete(session, account, actor_token_id=None)
            session.commit()
        assert summary["loans_unsecured"] == 1
        assert _get(admin_client, loan["id"])["secured_by_id"] is None
