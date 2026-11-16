"""Lots, captures and awards through the real MCP transport (issue #393,
decision 107): scopes, the dry-run / expect_hash contract, idempotent
retries, reference withholding, lot-aware sales, vests, unvested units
outside net worth, and the response budget. Synthetic fixtures only."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from tests.test_mcp import LiveServer, _mcp_call

TODAY = datetime.now(UTC).date()
AS_OF = TODAY - timedelta(days=10)
BUDGET = 1900


@pytest.fixture(scope="module")
def live_server(migrated_engine: Engine) -> Iterator[LiveServer]:
    server = LiveServer()
    server.start()
    yield server
    server.stop()


@pytest.fixture
def tokens(admin_client: TestClient) -> dict[str, str]:
    resp = admin_client.post(
        "/accounts", json={"name": "Plan", "type": "brokerage", "path": "assets.plan"}
    )
    assert resp.status_code == 201, resp.text
    resp = admin_client.patch(f"/accounts/{resp.json()['id']}", json={"holdings_source": "manual"})
    assert resp.status_code == 200, resp.text
    out = {}
    for name, scopes in (("reader", ["read"]), ("agent", ["read", "write"])):
        out[name] = admin_client.post("/tokens", json={"name": name, "scopes": scopes}).json()[
            "token"
        ]
    return out


def _lots(n: int) -> list[dict[str, Any]]:
    rows = []
    for i in range(n):
        rows.append(
            {
                "external_ref": f"SYN-{i:04d}",
                "acquired_on": (AS_OF - timedelta(days=30 * (i + 1))).isoformat(),
                "quantity": f"{(i % 7) + 1}.125",
                "basis_per_share": f"{50 + i}.123456",
                "plan": "Synthetic plan",
                "acquisition": "plan_purchase",
            }
        )
    # Two lots of one date and plan, told apart by their references.
    rows.append({**rows[0], "external_ref": "SYN-TWIN", "quantity": "2"})
    return rows


def _capture_args(rows: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    return {
        "account": "assets.plan",
        "as_of": AS_OF.isoformat(),
        "completeness": "complete",
        "captured_at": "2026-11-01T15:00:00+00:00",
        "source_note": "synthetic holdings page",
        "positions": [{"symbol": "SYNT", "lots": rows}],
        **kw,
    }


def test_hundred_lot_capture_end_to_end(live_server: LiveServer, tokens: dict[str, str]) -> None:
    rows = _lots(100)
    args = _capture_args(rows)
    [denied] = _mcp_call(live_server.url, tokens["reader"], [("upsert_lots", args)])
    assert "error" in denied and "write" in denied["error"]
    [preview] = _mcp_call(live_server.url, tokens["agent"], [("upsert_lots", args)])
    assert preview["dry_run"] and preview["counts"] == {"new_lot": 101, "recount": 1}
    assert len(json.dumps(preview)) <= BUDGET + 200  # withheld list etc.
    [done, retry] = _mcp_call(
        live_server.url,
        tokens["agent"],
        [
            ("upsert_lots", {**args, "dry_run": False, "expect_hash": preview["plan_hash"]}),
            ("upsert_lots", {**args, "dry_run": False, "expect_hash": preview["plan_hash"]}),
        ],
    )
    assert done["applied"] and not done["retry"]
    assert retry["retry"] and not retry["applied"]
    # Read again later: the same holdings, freshness only.
    later = {**args, "captured_at": "2026-11-02T15:00:00+00:00"}
    [again] = _mcp_call(live_server.url, tokens["agent"], [("upsert_lots", later)])
    assert again["counts"] == {} and again["conflicts"] == 0 and again["repeat_of"]
    expected = sum(float(r["quantity"]) for r in rows)  # display check only
    [page, revealed, holdings] = _mcp_call(
        live_server.url,
        tokens["agent"],
        [
            ("list_lots", {"account": "assets.plan", "symbol": "SYNT"}),
            (
                "list_lots",
                {"account": "assets.plan", "symbol": "SYNT", "include": ["external_ids"]},
            ),
            ("list_holdings", {"account": "assets.plan"}),
        ],
    )
    assert float(page["summary"]["quantity"]) == pytest.approx(expected)
    assert page["summary"]["unallocated"] == "0"
    assert page["summary"]["capture"]["as_of"] == AS_OF.isoformat()
    assert page["total"] == 101 and page["next_cursor"] is not None
    assert len(json.dumps(page)) <= BUDGET + 100
    assert all("external_ref" not in lot for lot in page["lots"])
    assert "external_ids" in page["withheld"]
    assert all(lot["external_ref"].startswith("SYN-") for lot in revealed["lots"])
    [line] = holdings["accounts"][0]["top"]
    assert line["cost_basis"] is not None  # every lot has a basis


def test_sale_by_reference_and_realized_gains(
    live_server: LiveServer, tokens: dict[str, str]
) -> None:
    args = _capture_args(_lots(3))
    [preview] = _mcp_call(live_server.url, tokens["agent"], [("upsert_lots", args)])
    _mcp_call(
        live_server.url,
        tokens["agent"],
        [("upsert_lots", {**args, "dry_run": False, "expect_hash": preview["plan_hash"]})],
    )
    sale = {
        "account": "assets.plan",
        "symbol": "SYNT",
        "quantity": "1",
        "price": "80",
        "trade_date": TODAY.isoformat(),
        "proceeds_to": "none",
    }
    [refused, sold, gains] = _mcp_call(
        live_server.url,
        tokens["agent"],
        [
            ("record_sale", sale),
            ("record_sale", {**sale, "lots": [{"lot": "SYN-0002", "quantity": "1"}]}),
            ("realized_gains", {"account": "assets.plan", "year": TODAY.year}),
        ],
    )
    assert "allocation_required" in refused["error"] or "names them" in refused["error"]
    assert sold["lots"]["dispositions"] == 1 and sold["lots"]["terms"] == ["short"]
    assert sold["lots"]["realized_gain"] == "27.8765"  # 80 - 52.123456 per share
    assert gains["short"]["disposals"] == 1 and gains["short"]["realized_gain"] == "27.88"


def test_stale_preview_is_refused(live_server: LiveServer, tokens: dict[str, str]) -> None:
    args = _capture_args(_lots(2))
    [preview] = _mcp_call(live_server.url, tokens["agent"], [("upsert_lots", args)])
    _mcp_call(
        live_server.url,
        tokens["agent"],
        [("upsert_lots", {**args, "dry_run": False, "expect_hash": preview["plan_hash"]})],
    )
    bigger = _capture_args(
        [*_lots(2), {"external_ref": "SYN-NEW", "acquired_on": AS_OF.isoformat(), "quantity": "1"}],
        captured_at="2026-11-03T15:00:00+00:00",
    )
    [plan] = _mcp_call(live_server.url, tokens["agent"], [("upsert_lots", bigger)])
    # Something changes in between: a transfer out of one lot.
    [_moved, stale] = _mcp_call(
        live_server.url,
        tokens["agent"],
        [
            (
                "record_transfer_out",
                {
                    "account": "assets.plan",
                    "symbol": "SYNT",
                    "quantity": "1",
                    "occurred_on": TODAY.isoformat(),
                    "lots": [{"lot": "SYN-0000", "quantity": "1"}],
                },
            ),
            ("upsert_lots", {**bigger, "dry_run": False, "expect_hash": plan["plan_hash"]}),
        ],
    )
    assert "preview_stale" in stale["error"]


def test_awards_vest_and_net_worth(live_server: LiveServer, tokens: dict[str, str]) -> None:
    vest_day = AS_OF + timedelta(days=2)
    awards = {
        "account": "assets.plan",
        "as_of": AS_OF.isoformat(),
        "captured_at": "2026-11-01T16:00:00+00:00",
        "completeness": "complete",
        "awards": [
            {
                "grant_ref": "SYN-GRANT-1",
                "symbol": "SYNT",
                "granted_on": "2025-02-01",
                "granted_quantity": "30",
                "plan": "Synthetic award plan",
                "schedule": [
                    {"vest_date": vest_day.isoformat(), "quantity": "10"},
                    {"vest_date": (TODAY + timedelta(days=120)).isoformat(), "quantity": "10"},
                    {"vest_date": (TODAY + timedelta(days=485)).isoformat(), "quantity": "10"},
                ],
            }
        ],
    }
    [worth_before, preview] = _mcp_call(
        live_server.url,
        tokens["agent"],
        [("net_worth", {}), ("upsert_awards", awards)],
    )
    assert preview["counts"] == {"new_award": 1, "new_vest": 3}
    vest = {
        "account": "assets.plan",
        "award": "SYN-GRANT-1",
        "vest_date": vest_day.isoformat(),
        "gross_quantity": "10",
        "withheld_quantity": "3.5",
        "fmv": "60",
    }
    [applied, worth_after, listing, recorded, again, conflict] = _mcp_call(
        live_server.url,
        tokens["agent"],
        [
            ("upsert_awards", {**awards, "dry_run": False, "expect_hash": preview["plan_hash"]}),
            ("net_worth", {}),
            ("list_awards", {"account": "assets.plan"}),
            ("record_vest", vest),
            ("record_vest", vest),
            ("record_vest", {**vest, "withheld_quantity": "3"}),
        ],
    )
    assert applied["applied"]
    assert worth_after["net_worth"] == worth_before["net_worth"]
    assert listing["unvested"][0]["units"] == "30"
    assert all("grant_ref" not in a for a in listing["awards"])
    assert recorded["net"] == "6.5" and recorded["unvested_after"] == "20"
    assert again["already_recorded"] is True
    assert "vest_conflict" in conflict["error"]
    [holdings] = _mcp_call(
        live_server.url, tokens["agent"], [("list_holdings", {"account": "assets.plan"})]
    )
    entry = holdings["accounts"][0]
    assert entry["unvested"][0]["units"] == "20"
    assert entry["top"][0]["quantity"] == "6.5"


def test_conflict_resolution_workflow(live_server: LiveServer, tokens: dict[str, str]) -> None:
    """The documented agent loop: a later capture shows fewer shares in a lot
    (a conflict, nothing applied); recording the sale naming that lot
    resolves it, and the same capture then applies cleanly."""
    rows = _lots(3)
    args = _capture_args(rows)
    [preview] = _mcp_call(live_server.url, tokens["agent"], [("upsert_lots", args)])
    _mcp_call(
        live_server.url,
        tokens["agent"],
        [("upsert_lots", {**args, "dry_run": False, "expect_hash": preview["plan_hash"]})],
    )
    sold_rows = [dict(r) for r in rows]
    sold_rows[1]["quantity"] = "1.125"  # SYN-0001 had 2.125
    later = _capture_args(
        sold_rows, as_of=TODAY.isoformat(), captured_at="2026-11-04T15:00:00+00:00"
    )
    [conflicted] = _mcp_call(live_server.url, tokens["agent"], [("upsert_lots", later)])
    assert conflicted["conflicts"] == 1
    assert conflicted["first_conflicts"][0]["kind"] == "quantity_decrease"
    sale = {
        "account": "assets.plan",
        "symbol": "SYNT",
        "quantity": "1",
        "price": "75",
        "trade_date": (TODAY - timedelta(days=1)).isoformat(),
        "proceeds_to": "none",
        "lots": [{"lot": "SYN-0001", "quantity": "1"}],
    }
    [_sold, clean] = _mcp_call(
        live_server.url, tokens["agent"], [("record_sale", sale), ("upsert_lots", later)]
    )
    assert clean["conflicts"] == 0 and clean["counts"] == {}
    [applied] = _mcp_call(
        live_server.url,
        tokens["agent"],
        [("upsert_lots", {**later, "dry_run": False, "expect_hash": clean["plan_hash"]})],
    )
    assert applied["applied"] and applied["repeat_of"] is None


def test_amount_privacy_withholds_lot_basis(
    live_server: LiveServer, tokens: dict[str, str], admin_client: TestClient
) -> None:
    """With single amounts withheld, a lot's basis is withheld whole: its
    per-share figure (times the remaining shares) would give it away, so
    neither appears in the structured answer nor in its text."""
    rows = _lots(2)
    args = _capture_args(rows)
    [preview] = _mcp_call(live_server.url, tokens["agent"], [("upsert_lots", args)])
    _mcp_call(
        live_server.url,
        tokens["agent"],
        [("upsert_lots", {**args, "dry_run": False, "expect_hash": preview["plan_hash"]})],
    )
    assert admin_client.put("/config", json={"privacy_amount": True}).status_code == 200
    from tests.test_mcp import _mcp_raw_call

    raw = _mcp_raw_call(
        live_server.url, tokens["agent"], "list_lots", {"account": "assets.plan", "symbol": "SYNT"}
    )
    structured = raw.structured_content
    text = " ".join(block.text for block in raw.content) + json.dumps(structured)
    lots = structured["lots"]
    assert lots and all(
        "basis_per_share" not in lot and "remaining_basis" not in lot for lot in lots
    )
    assert "amount" in structured["withheld"]
    for row in rows:
        per_share = row["basis_per_share"].rstrip("0").rstrip(".")
        assert per_share not in text


def test_brokerage_cash_reconciliation_mcp(live_server: LiveServer, tokens: dict[str, str]) -> None:
    args = {
        "account": "assets.plan",
        "as_of": TODAY.isoformat(),
        "statement_balance": "25",
        "component": "cash",
    }
    [denied] = _mcp_call(live_server.url, tokens["reader"], [("reconcile_account", args)])
    assert "error" in denied and "write" in denied["error"]
    implicit = {k: v for k, v in args.items() if k != "component"}
    bad, dry, done, retry, listing = _mcp_call(
        live_server.url,
        tokens["agent"],
        [
            ("reconcile_account", implicit),
            ("reconcile_account", {**args, "dry_run": True}),
            ("reconcile_account", args),
            ("reconcile_account", args),
            ("list_accounts", {}),
        ],
    )
    assert "error" in bad and "cash" in bad["error"]
    assert dry["component"] == "cash" and dry["reconciliation_id"] is None
    assert done["component"] == "cash" and done["created"]
    assert done["adjustment"] == "25.0000"
    assert retry == {**done, "created": False}
    plan = next(a for a in listing["result"] if a["path"] == "assets.plan")
    assert plan["reconciliation"]["component"] == "cash"
    assert plan["reconciliation"]["in_balance"]


def test_vest_requires_explicit_withholding(
    live_server: LiveServer, tokens: dict[str, str]
) -> None:
    [missing] = _mcp_call(
        live_server.url,
        tokens["agent"],
        [
            (
                "record_vest",
                {
                    "account": "assets.plan",
                    "award": "missing",
                    "vest_date": TODAY.isoformat(),
                    "gross_quantity": "10",
                },
            )
        ],
    )
    assert "error" in missing and "withheld_quantity" in missing["error"]
