"""Synthetic Home scenario: unvested awards never become owned balances."""

import uuid
from datetime import timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from tests.conftest import now, today


def account(client: TestClient, path: str = "assets.plan") -> str:
    r = client.post("/accounts", json={"name": "Plan", "type": "brokerage", "path": path})
    assert r.status_code == 201, r.text
    aid = r.json()["id"]
    assert client.patch(f"/accounts/{aid}", json={"holdings_source": "manual"}).status_code == 200
    return aid


def grant(client: TestClient, aid: str, *, symbol: str = "SYN", known: bool = True) -> None:
    row = {
        "symbol": symbol,
        "grant_ref": f"SYN-{symbol}",
        "granted_quantity": "50",
        "granted_on": (today() - timedelta(days=100)).isoformat(),
    }
    if known:
        row["vested_quantity"] = "0"
    body = {
        "as_of": (today() - timedelta(days=1)).isoformat(),
        "captured_at": now().isoformat(),
        "completeness": "partial",
        "awards": [row],
    }
    preview = client.post(f"/accounts/{aid}/award-captures", json=body)
    assert preview.status_code == 200, preview.text
    result = client.post(
        f"/accounts/{aid}/award-captures",
        json={
            **body,
            "dry_run": False,
            "expect_hash": preview.json()["plan_hash"],
        },
    )
    assert result.status_code == 200 and result.json()["applied"], result.text


def price(client: TestClient, *, symbol: str = "SYN", age: int = 0) -> None:
    r = client.post(
        "/prices",
        json={
            "symbol": symbol,
            "close": "100.000001",
            "date": (today() - timedelta(days=age)).isoformat(),
        },
    )
    assert r.status_code in (200, 201), r.text


def test_future_totals_are_additive_without_changing_owned_balances(
    admin_client: TestClient,
) -> None:
    c = admin_client
    aid = account(c)
    r = c.post(
        f"/accounts/{aid}/positions",
        json={
            "symbol": "SYN",
            "quantity": "10",
            "as_of": today().isoformat(),
        },
    )
    assert r.status_code == 201, r.text
    grant(c, aid)
    price(c)
    owned = c.get("/networth").json()
    positions = c.get(f"/accounts/{aid}/positions").json()
    result = c.get("/networth/potential")
    assert result.status_code == 200, result.text
    d = result.json()
    assert d["complete"] and d["unvested_awards"] == 1
    assert Decimal(d["unvested_value"]) == Decimal("5000.0000")
    assert Decimal(d["potential_assets"]) == Decimal(owned["assets"]) + Decimal("5000")
    assert Decimal(d["potential_net_worth"]) == Decimal(owned["net_worth"]) + Decimal("5000")
    assert d["liabilities"] == owned["liabilities"]
    assert c.get("/networth").json() == owned
    assert c.get(f"/accounts/{aid}/positions").json() == positions
    assert c.patch(f"/accounts/{aid}", json={"hidden": True}).status_code == 200
    assert c.get("/networth/potential").json()["unvested_awards"] == 1


def test_missing_prices_and_unknown_zero_schedules_are_not_complete_zero(
    admin_client: TestClient,
) -> None:
    c = admin_client
    aid = account(c)
    grant(c, aid)
    grant(c, aid, symbol="UNKNOWN", known=False)
    d = c.get("/networth/potential").json()
    assert d["unvested_awards"] == 2 and d["unknown_quantity_awards"] == 1
    assert d["unpriced_symbols"] == ["SYN"]
    assert not d["complete"] and d["unvested_value"] is None
    assert d["potential_assets"] is None and d["potential_net_worth"] is None
    assert Decimal(d["known_unvested_value"]) == 0


def test_price_staleness_uses_runtime_setting(admin_client: TestClient) -> None:
    c = admin_client
    aid = account(c)
    grant(c, aid)
    price(c, age=2)
    before = c.get("/networth/potential").json()
    for threshold, expected in ((1, ["SYN"]), (3, [])):
        assert c.put("/config", json={"price_stale_days": threshold}).status_code == 200
        result = c.get("/networth/potential").json()
        assert result["stale_price_symbols"] == expected
        assert result["unvested_value"] == before["unvested_value"]


def test_stale_prices_are_identified_and_closed_accounts_excluded(
    admin_client: TestClient, migrated_engine: Engine
) -> None:
    c = admin_client
    aid = account(c)
    grant(c, aid)
    price(c, age=30)
    d = c.get("/networth/potential").json()
    assert d["complete"] and d["stale_price_symbols"] == ["SYN"]
    assert d["oldest_price_as_of"] == (today() - timedelta(days=30)).isoformat()
    with migrated_engine.begin() as conn:
        conn.execute(
            text("UPDATE accounts SET closed_at=now() WHERE id=:id"), {"id": uuid.UUID(aid)}
        )
    d = c.get("/networth/potential").json()
    assert d["complete"] and d["unvested_awards"] == 0
    assert Decimal(d["unvested_value"]) == 0
    assert not d["stale_price_symbols"]


def test_future_requires_read_scope_and_unsupported_currency_is_incomplete(
    admin_client: TestClient, client: TestClient, migrated_engine: Engine
) -> None:
    c = admin_client
    aid = account(c)
    grant(c, aid)
    price(c)
    with migrated_engine.begin() as conn:
        conn.execute(
            text("UPDATE accounts SET currency='EUR' WHERE id=:id"), {"id": uuid.UUID(aid)}
        )
    d = c.get("/networth/potential").json()
    assert not d["complete"] and d["unsupported_currency_awards"] == 1
    assert d["potential_net_worth"] is None
    token = c.post("/tokens", json={"name": "reader", "scopes": ["read"]}).json()["token"]
    c.cookies.clear()
    assert client.get("/networth/potential").status_code == 401
    assert (
        client.get("/networth/potential", headers={"Authorization": "Bearer " + token}).status_code
        == 200
    )


def test_recording_a_vest_moves_units_out_of_potential_without_double_counting(
    admin_client: TestClient,
) -> None:
    c = admin_client
    aid = account(c)
    grant(c, aid)
    price(c)
    award = c.get(f"/accounts/{aid}/awards").json()["awards"][0]
    before = c.get("/networth/potential").json()
    vested = c.post(
        f"/awards/{award['id']}/vests",
        json={
            "vest_date": today().isoformat(),
            "gross_quantity": "10",
            "withheld_quantity": "3",
            "fmv": "100.000001",
        },
    )
    assert vested.status_code == 200, vested.text
    after = c.get("/networth/potential").json()
    assert Decimal(after["owned_assets"]) == Decimal("700.0000")
    assert Decimal(after["unvested_value"]) == Decimal("4000.0000")
    assert Decimal(after["potential_assets"]) == Decimal(before["potential_assets"]) - Decimal(
        "300"
    )
