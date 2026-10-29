"""Bracket transfer conversion + unlink (DESIGN §3.3/§4/§5, issue #109).

category='[account]' turns a row into a transfer (absorb > merge > in-place);
a plain category on a transfer turns it back; unlink resurrects merged
originals with their content hashes MOVED onto the clones.
"""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, text

from tests.test_transfers import Harness, _event_count, two_account_payload

api_amounts = st.decimals(
    min_value=Decimal("-99999.99"),
    max_value=Decimal("99999.99"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
).filter(lambda d: d != 0)


@pytest.fixture
def seeded(admin_client: TestClient) -> dict[str, str]:
    for name, type_, path in (
        ("Checking", "checking", "assets.checking"),
        ("Savings", "savings", "assets.savings"),
        ("Dining", "category_expense", "expenses.dining"),
    ):
        endpoint = "/categories" if type_.startswith("category_") else "/accounts"
        resp = admin_client.post(endpoint, json={"name": name, "type": type_, "path": path})
        assert resp.status_code == 201, resp.text
    return {
        "checking": "assets.checking",
        "savings": "assets.savings",
        "dining": "expenses.dining",
    }


def _flat(
    client: TestClient,
    seeded: dict[str, str],
    amount: str,
    *,
    status: str = "pending",
    account: str = "checking",
) -> dict:
    resp = client.post(
        "/transactions",
        json={
            "account": seeded[account],
            "category": seeded["dining"],
            "amount": amount,
            "date": "2026-07-01",
            "payee": "Row",
            "status": status,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _postings_sum(engine: Engine, tx_id: str) -> Decimal:
    with engine.connect() as conn:
        return Decimal(
            conn.execute(
                text("SELECT COALESCE(SUM(amount), 0) FROM postings WHERE transaction_id = :id"),
                {"id": tx_id},
            ).scalar_one()
        )


class TestConvertInPlace:
    @settings(
        max_examples=15, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(amount=api_amounts)
    def test_pending_bracket_convert_stays_balanced(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine, amount: Decimal
    ) -> None:
        tx = _flat(client, seeded, str(amount))
        resp = client.patch(f"/transactions/{tx['id']}", json={"category": "[assets.savings]"})
        assert resp.status_code == 200, resp.text
        detail = resp.json()
        assert detail["id"] == tx["id"]  # pending converts in place
        assert detail["is_transfer"] is True
        assert detail["status"] == "pending"
        assert {p["account_path"] for p in detail["postings"]} == {
            seeded["checking"],
            seeded["savings"],
        }
        assert _postings_sum(migrated_engine, detail["id"]) == 0

    def test_pending_convert_back_to_category(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        tx = _flat(client, seeded, "-14.00")
        client.patch(f"/transactions/{tx['id']}", json={"category": "[assets.savings]"})
        resp = client.patch(
            f"/transactions/{tx['id']}",
            json={"category": seeded["dining"], "account": seeded["checking"]},
        )
        assert resp.status_code == 200, resp.text
        detail = resp.json()
        assert detail["is_transfer"] is False
        assert detail["category_path"] == seeded["dining"]
        assert Decimal(detail["amount"]) == Decimal("-14.00")
        assert detail["account_path"] == seeded["checking"]

    def test_recategorize_transfer_needs_account(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        tx = _flat(client, seeded, "-14.00")
        client.patch(f"/transactions/{tx['id']}", json={"category": "[assets.savings]"})
        resp = client.patch(f"/transactions/{tx['id']}", json={"category": seeded["dining"]})
        assert resp.status_code == 422
        assert "account" in resp.json()["detail"]

    def test_conversion_must_be_the_only_change(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        tx = _flat(client, seeded, "-14.00")
        resp = client.patch(
            f"/transactions/{tx['id']}",
            json={"category": "[assets.savings]", "amount": "-20.00"},
        )
        assert resp.status_code == 422

    def test_invalid_targets_rejected(self, seeded: dict[str, str], client: TestClient) -> None:
        tx = _flat(client, seeded, "-14.00")
        assert (
            client.patch(
                f"/transactions/{tx['id']}", json={"category": "[expenses.dining]"}
            ).status_code
            == 422
        )
        assert (
            client.patch(
                f"/transactions/{tx['id']}", json={"category": "[assets.checking]"}
            ).status_code
            == 422
        )
        assert (
            client.patch(
                f"/transactions/{tx['id']}", json={"category": "[assets.nope]"}
            ).status_code
            == 404
        )


class TestConvertPosted:
    def test_posted_bracket_convert_is_a_reversal_pair(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        tx = _flat(client, seeded, "-250.00", status="posted")
        resp = client.patch(f"/transactions/{tx['id']}", json={"category": "[assets.savings]"})
        assert resp.status_code == 200, resp.text
        replacement = resp.json()
        assert replacement["id"] != tx["id"]
        assert replacement["is_transfer"] is True
        assert replacement["status"] == "posted"
        assert _postings_sum(migrated_engine, replacement["id"]) == 0

        # Flat view collapses the chain to the replacement only.
        page = client.get("/transactions", params={"account": seeded["checking"]}).json()
        assert [item["id"] for item in page["items"]] == [replacement["id"]]
        assert page["items"][0]["transfer_account_path"] == seeded["savings"]

        # Balances: checking -250, savings +250, dining back to 0.
        accounts = client.get("/accounts", params={"include_balances": True}).json()
        savings = next(a for a in accounts if a["path"] == seeded["savings"])
        assert Decimal(savings["balance"]) == Decimal("250.00")

    def test_write_scope_converts_a_posted_row(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        """#436: an agent token acts with the user's authority, so a posted
        row converts through the same correction a person's edit gets."""
        tx = _flat(client, seeded, "-99.00", status="posted")
        token = client.post("/tokens", json={"name": "p", "scopes": ["read", "write"]}).json()[
            "token"
        ]
        resp = client.patch(
            f"/transactions/{tx['id']}",
            json={"category": "[assets.savings]"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["is_transfer"] is True

    def test_a_read_only_token_cannot_convert(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        tx = _flat(client, seeded, "-99.00", status="posted")
        token = client.post("/tokens", json={"name": "r", "scopes": ["read"]}).json()["token"]
        resp = client.patch(
            f"/transactions/{tx['id']}",
            json={"category": "[assets.savings]"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403

    def test_write_scope_converts_a_pending_row(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        tx = _flat(client, seeded, "-42.00")  # pending
        token = client.post("/tokens", json={"name": "p2", "scopes": ["read", "write"]}).json()[
            "token"
        ]
        resp = client.patch(
            f"/transactions/{tx['id']}",
            json={"category": "[assets.savings]"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["is_transfer"] is True


class TestConvertMergesAndAbsorbs:
    def test_convert_merges_live_counterpart(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        out_tx = _flat(client, seeded, "-500.00", status="posted")
        in_tx = _flat(client, seeded, "500.00", status="posted", account="savings")
        resp = client.patch(f"/transactions/{out_tx['id']}", json={"category": "[assets.savings]"})
        assert resp.status_code == 200, resp.text
        merged = resp.json()
        assert merged["is_transfer"] is True
        assert merged["id"] not in (out_tx["id"], in_tx["id"])
        for original in (out_tx["id"], in_tx["id"]):
            detail = client.get(f"/transactions/{original}").json()
            assert detail["status"] == "void"
            assert detail["superseded_by"] == merged["id"]
        assert merged["has_linked_originals"] is True

    def test_merged_transfer_refuses_recategorize_until_unlinked(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        out_tx = _flat(client, seeded, "-75.00", status="posted")
        _flat(client, seeded, "75.00", status="posted", account="savings")
        merged = client.patch(
            f"/transactions/{out_tx['id']}", json={"category": "[assets.savings]"}
        ).json()
        resp = client.patch(
            f"/transactions/{merged['id']}",
            json={"category": seeded["dining"], "account": seeded["checking"]},
        )
        assert resp.status_code == 422
        assert "unlink" in resp.json()["detail"]

        unlink = client.post(f"/transactions/{merged['id']}/unlink-transfer")
        assert unlink.status_code == 200, unlink.text
        assert unlink.json()["transfer_voided"] is True
        assert len(unlink.json()["resurrected_ids"]) == 2


class TestUnlinkWithFeed:
    @pytest.fixture
    def harness(
        self, clean_db: None, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> Harness:
        return Harness(client, monkeypatch)

    SIGNAL_PAYLOAD = two_account_payload(
        [
            {
                "id": "t-out",
                "posted": 1780617600,  # 2026-06-05
                "amount": "-500.00",
                "description": "TRANSFER TO SAVINGS",
            }
        ],
        [
            {
                "id": "t-in",
                "posted": 1780617600,
                "amount": "500.00",
                "description": "TRANSFER FROM CHECKING",
            }
        ],
    )

    def test_unlink_moves_hashes_and_suppresses_auto_relink(
        self, harness: Harness, client: TestClient, migrated_engine: Engine
    ) -> None:
        harness.use_payload(self.SIGNAL_PAYLOAD)
        connection_id = harness.connect_and_map_both()
        (merged,) = client.get("/transactions", params={"account": "assets.checking"}).json()[
            "items"
        ]
        assert merged["is_transfer"] is True
        assert merged["has_linked_originals"] is True

        resp = client.post(f"/transactions/{merged['id']}/unlink-transfer")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["transfer_voided"] is True
        resurrected = body["resurrected_ids"]
        assert len(resurrected) == 2

        with migrated_engine.connect() as conn:
            clone_hashes = conn.execute(
                text("SELECT content_hash, status FROM transactions WHERE id::text = ANY(:ids)"),
                {"ids": resurrected},
            ).all()
            void_hashes = conn.execute(
                text(
                    "SELECT content_hash FROM transactions"
                    " WHERE superseded_by = :m AND id::text != ALL(:ids)"
                ),
                {"m": merged["id"], "ids": resurrected},
            ).scalars()
            transfer_state = conn.execute(
                text("SELECT status, superseded_by::text FROM transactions WHERE id = :m"),
                {"m": merged["id"]},
            ).one()
        assert all(h is not None and s == "posted" for h, s in clone_hashes)
        assert all(h is None for h in void_hashes)
        assert transfer_state.status == "void"
        assert transfer_state.superseded_by in resurrected

        # Re-import: the moved hashes keep idempotency — zero new rows.
        result = harness.sync(connection_id)
        assert result["imported"] == 0
        assert result["skipped"] == 2
        # And the auto-linker does NOT redo what a human undid (the single
        # auto_linked event is the original pre-unlink merge).
        assert _event_count(migrated_engine, "transfer.auto_linked") == 1

        # Explicit confirm remains the escape hatch.
        rows = client.get("/transactions", params={"account": "assets.checking"}).json()["items"]
        outgoing = next(r for r in rows if Decimal(r["amount"]) < 0)
        incoming = client.get("/transactions", params={"account": "assets.savings"}).json()[
            "items"
        ][0]
        confirm = client.post(
            "/matching/confirm-transfer",
            json={"outgoing_id": outgoing["id"], "incoming_id": incoming["id"]},
        )
        assert confirm.status_code == 200, confirm.text

    def test_unlink_absorb_keeps_transfer_alive(
        self, harness: Harness, client: TestClient, migrated_engine: Engine
    ) -> None:
        harness.use_payload(two_account_payload([], []))
        connection_id = harness.connect_and_map_both()
        manual = client.post(
            "/transactions/transfer",
            json={
                "from_account": "assets.checking",
                "to_account": "assets.savings",
                "amount": "500.00",
                "date": "2026-06-15",
                "status": "posted",
            },
        ).json()
        harness.use_payload(
            two_account_payload(
                [],
                [
                    {
                        "id": "late-in",
                        "posted": 1781481600,  # 2026-06-15
                        "amount": "500.00",
                        "description": "TRANSFER FROM CHECKING",
                    }
                ],
            )
        )
        harness.sync(connection_id)
        assert _event_count(migrated_engine, "transfer.absorbed") == 1

        resp = client.post(f"/transactions/{manual['id']}/unlink-transfer")
        assert resp.status_code == 200, resp.text
        assert resp.json()["transfer_voided"] is False
        (resurrected_id,) = resp.json()["resurrected_ids"]

        # The manual transfer survives; the import row is back, posted, hashed.
        assert client.get(f"/transactions/{manual['id']}").json()["status"] == "posted"
        row = client.get(f"/transactions/{resurrected_id}").json()
        assert row["status"] == "posted"
        assert row["content_hash"] is not None

        # Auto paths stay suppressed across the next sync...
        harness.sync(connection_id)
        assert _event_count(migrated_engine, "transfer.absorbed") == 1
        # ...but the explicit bracket convert re-absorbs on request.
        convert = client.patch(
            f"/transactions/{resurrected_id}", json={"category": "[assets.checking]"}
        )
        assert convert.status_code == 200, convert.text
        assert convert.json()["id"] == manual["id"]
        assert client.get(f"/transactions/{resurrected_id}").json()["status"] == "void"

    def test_unlink_without_linked_originals_rejected(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        manual = client.post(
            "/transactions/transfer",
            json={
                "from_account": seeded["checking"],
                "to_account": seeded["savings"],
                "amount": "10.00",
                "date": "2026-07-01",
            },
        ).json()
        resp = client.post(f"/transactions/{manual['id']}/unlink-transfer")
        assert resp.status_code == 422
        assert "nothing to unlink" in resp.json()["detail"]
