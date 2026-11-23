"""Selling or disposing of a hand-valued asset; money put in vs gain (#569)."""

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import today


def _day(days_ago: int) -> str:
    return (today() - timedelta(days=days_ago)).isoformat()


def _account(client: TestClient, type_: str, name: str) -> dict[str, Any]:
    root = "assets"
    resp = client.post(
        "/accounts",
        json={"name": name, "type": type_, "path": f"{root}.{name.lower()}_{uuid.uuid4().hex[:6]}"},
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def _house_with_history(client: TestClient) -> dict[str, Any]:
    house = _account(client, "property", "House")
    resp = client.post(
        f"/accounts/{house['id']}/value-changes",
        json={"as_of": _day(400), "amount": "500000.00", "kind": "capital"},
    )
    assert resp.status_code == 201, resp.text
    resp = client.post(
        f"/accounts/{house['id']}/reconcile",
        json={"as_of": _day(30), "statement_balance": "600000.00"},
    )
    assert resp.status_code == 200, resp.text
    return house


def _get(client: TestClient, account_id: str) -> dict[str, Any]:
    return dict(client.get(f"/accounts/{account_id}").json())


class TestGain:
    def test_money_put_in_and_gain_add_up_to_the_value(self, admin_client: TestClient) -> None:
        house = _house_with_history(admin_client)
        asset = _get(admin_client, house["id"])["asset"]
        assert Decimal(asset["money_put_in"]) == Decimal("500000.00")
        assert Decimal(asset["gain"]) == Decimal("100000.00")

    def test_an_improvement_transfer_is_money_put_in(self, admin_client: TestClient) -> None:
        house = _house_with_history(admin_client)
        checking = _account(admin_client, "checking", "Checking")
        # An improvement paid from checking: a transfer into the asset.
        resp = admin_client.post(
            "/transactions/transfer",
            json={
                "from_account": checking["path"],
                "to_account": house["path"],
                "amount": "20000.00",
                "date": _day(10),
                "status": "posted",
            },
        )
        assert resp.status_code == 201, resp.text
        asset = _get(admin_client, house["id"])["asset"]
        assert Decimal(asset["money_put_in"]) == Decimal("520000.00")
        assert Decimal(asset["gain"]) == Decimal("100000.00")


class TestDispose:
    def test_a_sale_closes_the_asset_at_zero_with_its_gain(self, admin_client: TestClient) -> None:
        house = _house_with_history(admin_client)
        checking = _account(admin_client, "checking", "Checking")
        # The proceeds, recorded as a transfer out of the house before the sale call:
        # the bank's deposit, then converted into a transfer from the asset.
        admin_client.post(
            "/categories",
            json={"name": "Misc", "type": "category_income", "path": "income.misc"},
        )
        tx = admin_client.post(
            "/transactions",
            json={
                "account": checking["path"],
                "category": "income.misc",
                "amount": "640000.00",
                "date": _day(2),
                "status": "posted",
            },
        )
        assert tx.status_code == 201, tx.text
        converted = admin_client.patch(
            f"/transactions/{tx.json()['id']}", json={"category": f"[{house['path']}]"}
        )
        assert converted.status_code == 200, converted.text
        assert Decimal(_get(admin_client, house["id"])["balance"]) == Decimal("-40000.00")

        dry = admin_client.post(
            f"/accounts/{house['id']}/dispose", json={"as_of": _day(1), "dry_run": True}
        )
        assert dry.status_code == 200, dry.text
        assert dry.json()["dry_run"] is True
        assert Decimal(dry.json()["value_before"]) == Decimal("-40000.00")
        assert Decimal(dry.json()["gain"]) == Decimal("140000.00")
        assert _get(admin_client, house["id"])["closed_at"] is None

        done = admin_client.post(
            f"/accounts/{house['id']}/dispose", json={"as_of": _day(1), "note": "Sold"}
        )
        assert done.status_code == 200, done.text
        after = _get(admin_client, house["id"])
        assert after["closed_at"] is not None
        assert Decimal(after["balance"]) == Decimal("0")
        assert Decimal(after["asset"]["gain"]) == Decimal("140000.00")
        # Net worth: the cash stays, the house is gone, and spending saw nothing.
        nw = Decimal(admin_client.get("/networth").json()["net_worth"])
        assert nw == Decimal("640000.00")
        assert all(
            c["path"] != "expenses.adjustments" for c in admin_client.get("/categories").json()
        )

    def test_a_write_off_takes_the_whole_value(self, admin_client: TestClient) -> None:
        car = _account(admin_client, "property", "Car")
        admin_client.post(
            f"/accounts/{car['id']}/value-changes",
            json={"as_of": _day(100), "amount": "9000.00", "kind": "capital"},
        )
        done = admin_client.post(f"/accounts/{car['id']}/dispose", json={"as_of": _day(5)})
        assert done.status_code == 200, done.text
        assert Decimal(done.json()["closing_change"]) == Decimal("-9000.00")
        assert Decimal(done.json()["gain"]) == Decimal("-9000.00")
        assert Decimal(
            admin_client.get("/networth", params={"as_of": _day(10)}).json()["net_worth"]
        ) == Decimal("9000.00")
        assert Decimal(
            admin_client.get("/networth", params={"as_of": _day(5)}).json()["net_worth"]
        ) == Decimal("0")

    def test_a_date_before_the_latest_entry_is_refused(self, admin_client: TestClient) -> None:
        house = _house_with_history(admin_client)
        resp = admin_client.post(f"/accounts/{house['id']}/dispose", json={"as_of": _day(60)})
        assert resp.status_code == 422
        assert "latest entry" in resp.json()["detail"]

    def test_closing_an_asset_posts_its_leftover_as_a_change_in_value(
        self, admin_client: TestClient
    ) -> None:
        house = _house_with_history(admin_client)
        resp = admin_client.post(f"/accounts/{house['id']}/close", json={})
        assert resp.status_code == 200, resp.text
        tx = admin_client.get(f"/transactions/{resp.json()['adjustment_tx_id']}").json()
        assert tx["value_change"] is True and tx["category_path"] == "equity.revaluation"

    def test_only_an_asset_is_disposed(self, admin_client: TestClient) -> None:
        checking = _account(admin_client, "checking", "Checking")
        resp = admin_client.post(f"/accounts/{checking['id']}/dispose", json={"as_of": _day(0)})
        assert resp.status_code == 422
