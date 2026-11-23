"""REST surface of lots, captures and awards (issue #393, decision 107):
scopes, reference redaction for agent tokens, the dry-run / expect_hash
contract, conflicts as 409s, lot-allocated events and realized gains.
Synthetic fixtures only."""

import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from purser import db
from purser.models import AuditLog
from tests.conftest import now, today


def _as_of() -> date:
    return today() - timedelta(days=15)


def _headers(client: TestClient, scopes: list[str]) -> dict[str, str]:
    resp = client.post("/tokens", json={"name": f"t-{uuid.uuid4().hex[:6]}", "scopes": scopes})
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


@pytest.fixture
def broker(admin_client: TestClient) -> str:
    resp = admin_client.post(
        "/accounts", json={"name": "Plan", "type": "brokerage", "path": "assets.plan"}
    )
    assert resp.status_code == 201, resp.text
    account = resp.json()["id"]
    resp = admin_client.patch(f"/accounts/{account}", json={"holdings_source": "manual"})
    assert resp.status_code == 200, resp.text
    return account


def _body(symbol: str, rows: list[dict], **kw: object) -> dict:
    return {
        "as_of": _as_of().isoformat(),
        "captured_at": now().isoformat(),
        "completeness": "complete",
        "source_note": "synthetic statement",
        "positions": [{"symbol": symbol, "lots": rows}],
        **kw,
    }


ROWS = [
    {
        "external_ref": "LOT-A",
        "acquired_on": "2023-02-06",
        "quantity": "12.5",
        "basis_per_share": "101.250000",
        "plan": "Monthly purchase",
        "acquisition": "plan_purchase",
    },
    {
        "external_ref": "LOT-B",
        "acquired_on": "2024-08-05",
        "quantity": "3",
        "plan": "Matching award",
        "acquisition": "rsu_vest",
    },
    {
        "external_ref": "LOT-C",
        "acquired_on": "2024-08-05",
        "quantity": "4",
        "plan": "Matching award",
        "acquisition": "rsu_vest",
        "cost_basis": "800.00",
    },
]


def _commit(client: TestClient, account: str, body: dict, headers=None) -> dict:
    preview = client.post(f"/accounts/{account}/lot-captures", json=body, headers=headers)
    assert preview.status_code == 200, preview.text
    commit = client.post(
        f"/accounts/{account}/lot-captures",
        json={**body, "dry_run": False, "expect_hash": preview.json()["plan_hash"]},
        headers=headers,
    )
    assert commit.status_code == 200, commit.text
    return commit.json()


def test_capture_end_to_end_and_redaction(admin_client: TestClient, broker: str) -> None:
    writer = _headers(admin_client, ["read", "write"])
    reader = _headers(admin_client, ["read"])
    body = _body("ACME", ROWS)
    # A read token cannot capture, even a dry run.
    assert (
        admin_client.post(f"/accounts/{broker}/lot-captures", json=body, headers=reader).status_code
        == 403
    )
    preview = admin_client.post(f"/accounts/{broker}/lot-captures", json=body, headers=writer)
    assert preview.status_code == 200, preview.text
    plan = preview.json()
    assert plan["dry_run"] and plan["conflicts"] == 0
    assert [c["kind"] for c in plan["symbols"][0]["changes"]].count("new_lot") == 3
    # A commit needs the hash.
    no_hash = admin_client.post(
        f"/accounts/{broker}/lot-captures", json={**body, "dry_run": False}, headers=writer
    )
    assert no_hash.status_code == 422
    done = _commit(admin_client, broker, body, writer)
    assert done["applied"] and len(done["created_lots"]) == 3
    again = admin_client.post(
        f"/accounts/{broker}/lot-captures",
        json={**body, "dry_run": False, "expect_hash": "stale"},
        headers=writer,
    )
    assert again.status_code == 200 and again.json()["retry"] is True

    positions = admin_client.get(f"/accounts/{broker}/positions", headers=reader).json()
    [acme] = positions
    assert acme["quantity"] == "19.500000" and acme["tracks_lots"]
    assert acme["cost_basis"] is None  # LOT-B has no basis
    assert acme["basis_unknown_quantity"] == "3.000000"
    page = admin_client.get(f"/positions/{acme['id']}/lots", headers=reader).json()
    assert page["summary"]["capture"]["as_of"] == _as_of().isoformat()
    assert page["total"] == 3
    # Agent tokens never see the references; the owner's session does.
    assert all(lot["external_ref"] is None and lot["external_ref_withheld"] for lot in page["lots"])
    mine = admin_client.get(f"/positions/{acme['id']}/lots").json()
    assert {lot["external_ref"] for lot in mine["lots"]} == {"LOT-A", "LOT-B", "LOT-C"}
    # Same-day, same-plan lots stay separate.
    assert sorted(lot["remaining"] for lot in mine["lots"]) == ["12.500000", "3.000000", "4.000000"]


def test_allocated_sale_and_realized_gains(admin_client: TestClient, broker: str) -> None:
    writer = _headers(admin_client, ["read", "write"])
    _commit(admin_client, broker, _body("ACME", ROWS), writer)
    position = admin_client.get(f"/accounts/{broker}/positions").json()[0]
    refused = admin_client.post(
        f"/positions/{position['id']}/events",
        json={
            "kind": "sell",
            "occurred_on": today().isoformat(),
            "quantity": "2",
            "price": "150",
            "proceeds_to": "none",
        },
        headers=writer,
    )
    assert refused.status_code == 422
    assert refused.json()["detail"]["code"] == "allocation_required"
    sold = admin_client.post(
        f"/positions/{position['id']}/events",
        json={
            "kind": "sell",
            "occurred_on": today().isoformat(),
            "quantity": "2",
            "price": "150",
            "proceeds_to": "none",
            "lots": [{"external_ref": "LOT-A", "quantity": "2"}],
        },
        headers=writer,
    )
    assert sold.status_code == 201, sold.text
    gains = admin_client.get(
        f"/accounts/{broker}/realized-gains", params={"year": today().year}, headers=writer
    ).json()
    assert gains["long"]["disposals"] == 1
    assert gains["long"]["proceeds"] == "300.0000"
    assert gains["long"]["cost_basis"] == "202.5000"
    assert gains["long"]["realized_gain"] == "97.5000"
    with db.get_sessionmaker()() as s:
        actions = set(s.execute(select(AuditLog.action)).scalars())
    assert {"capture.apply", "position.record"} <= actions


def test_conflicts_are_409(admin_client: TestClient, broker: str) -> None:
    _commit(admin_client, broker, _body("ACME", ROWS))
    later = (_as_of() + timedelta(days=2)).isoformat()
    body = _body("ACME", [ROWS[0]], as_of=later)
    preview = admin_client.post(f"/accounts/{broker}/lot-captures", json=body).json()
    assert {c["kind"] for c in preview["symbols"][0]["conflicts"]} == {"missing_from_capture"}
    resp = admin_client.post(
        f"/accounts/{broker}/lot-captures",
        json={**body, "dry_run": False, "expect_hash": preview["plan_hash"]},
    )
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "capture_conflicts"
    resp = admin_client.post(
        f"/accounts/{broker}/lot-captures",
        json={**body, "dry_run": False, "expect_hash": "not-the-hash"},
    )
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "preview_stale"


def test_awards_and_vest(admin_client: TestClient, broker: str) -> None:
    writer = _headers(admin_client, ["read", "write"])
    body = {
        "as_of": _as_of().isoformat(),
        "captured_at": now().isoformat(),
        "completeness": "complete",
        "awards": [
            {
                "grant_ref": "GRANT-1",
                "symbol": "ACME",
                "granted_on": "2024-03-01",
                "granted_quantity": "40",
                "schedule": [
                    {"vest_date": (_as_of() + timedelta(days=3)).isoformat(), "quantity": "10"},
                    {"vest_date": (today() + timedelta(days=200)).isoformat(), "quantity": "10"},
                ],
            }
        ],
    }
    preview = admin_client.post(f"/accounts/{broker}/award-captures", json=body, headers=writer)
    assert preview.status_code == 200, preview.text
    done = admin_client.post(
        f"/accounts/{broker}/award-captures",
        json={**body, "dry_run": False, "expect_hash": preview.json()["plan_hash"]},
        headers=writer,
    )
    assert done.status_code == 200 and done.json()["applied"], done.text
    listing = admin_client.get(f"/accounts/{broker}/awards", headers=writer).json()
    [award] = listing["awards"]
    assert award["grant_ref"] is None and award["grant_ref_withheld"]
    assert award["unvested_quantity"] == "20.000000"
    vest = {
        "vest_date": (_as_of() + timedelta(days=3)).isoformat(),
        "gross_quantity": "10",
        "withheld_quantity": "4",
        "fmv": "120",
    }
    missing = admin_client.post(
        f"/awards/{award['id']}/vests",
        json={k: v for k, v in vest.items() if k != "withheld_quantity"},
        headers=writer,
    )
    assert missing.status_code == 422 and "withheld_quantity" in missing.text
    assert admin_client.get(f"/accounts/{broker}/positions").json() == []
    dry = admin_client.post(
        f"/awards/{award['id']}/vests", json={**vest, "dry_run": True}, headers=writer
    )
    assert dry.status_code == 200 and dry.json()["dry_run"], dry.text
    assert admin_client.get(f"/accounts/{broker}/positions").json() == []
    first = admin_client.post(f"/awards/{award['id']}/vests", json=vest, headers=writer)
    assert first.status_code == 200, first.text
    assert first.json()["net_quantity"] == "6.000000"
    again = admin_client.post(f"/awards/{award['id']}/vests", json=vest, headers=writer)
    assert again.json()["already_recorded"] is True
    other = admin_client.post(
        f"/awards/{award['id']}/vests", json={**vest, "fmv": "121"}, headers=writer
    )
    assert other.status_code == 409 and other.json()["detail"]["code"] == "vest_conflict"
    [position] = admin_client.get(f"/accounts/{broker}/positions").json()
    assert position["quantity"] == "6.000000" and position["cost_basis"] == "720.0000"
    listing = admin_client.get(f"/accounts/{broker}/awards").json()
    assert listing["awards"][0]["unvested_quantity"] == "10.000000"
    assert listing["awards"][0]["grant_ref"] == "GRANT-1"  # the owner's session sees it
