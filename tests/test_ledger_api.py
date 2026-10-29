"""Ledger API invariants, end to end against real Postgres.

The three required ledger properties are exercised through the public API:
zero-sum persistence, posted immutability via reversal pairs, and (in #4)
import idempotency.
"""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, text

api_amounts = st.decimals(
    min_value=Decimal("-99999.99"),
    max_value=Decimal("99999.99"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
).filter(lambda d: d != 0)


@pytest.fixture
def seeded(admin_client: TestClient) -> dict[str, str]:
    """Admin session + a small chart of accounts; returns name -> path."""

    def acct(name: str, type_: str, parent_id: str | None = None, path: str | None = None) -> dict:
        body: dict = {"name": name, "type": type_}
        if parent_id:
            body["parent_id"] = parent_id
        if path:
            body["path"] = path
        endpoint = "/categories" if type_.startswith("category_") else "/accounts"
        resp = admin_client.post(endpoint, json=body)
        assert resp.status_code == 201, resp.text
        return resp.json()

    checking = acct("Checking", "checking", path="assets.checking")
    savings = acct("Savings", "savings", path="assets.savings")
    expenses = acct("Expenses", "category_expense")
    dining = acct("Dining", "category_expense", parent_id=expenses["id"])
    coffee = acct("Coffee", "category_expense", parent_id=dining["id"])
    groceries = acct("Groceries", "category_expense", parent_id=expenses["id"])
    income = acct("Income", "category_income")
    salary = acct("Salary", "category_income", parent_id=income["id"])
    return {
        "checking": checking["path"],
        "savings": savings["path"],
        "expenses": expenses["path"],
        "dining": dining["path"],
        "coffee": coffee["path"],
        "groceries": groceries["path"],
        "salary": salary["path"],
    }


def _postings_sum(engine: Engine, tx_id: str) -> Decimal:
    with engine.connect() as conn:
        return Decimal(
            conn.execute(
                text("SELECT COALESCE(SUM(amount), 0) FROM postings WHERE transaction_id = :id"),
                {"id": tx_id},
            ).scalar_one()
        )


class TestZeroSum:
    @settings(
        max_examples=20, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(amount=api_amounts)
    def test_flat_create_persists_balanced(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine, amount: Decimal
    ) -> None:
        resp = client.post(
            "/transactions",
            json={
                "account": seeded["checking"],
                "category": seeded["dining"],
                "amount": str(amount),
                "date": "2026-07-01",
                "payee": "Chipotle",
            },
        )
        assert resp.status_code == 201, resp.text
        detail = resp.json()
        assert _postings_sum(migrated_engine, detail["id"]) == 0
        assert Decimal(detail["amount"]) == amount
        assert detail["category_path"] == seeded["dining"]

    def test_zero_amount_rejected(self, seeded: dict[str, str], client: TestClient) -> None:
        resp = client.post(
            "/transactions",
            json={
                "account": seeded["checking"],
                "category": seeded["dining"],
                "amount": "0",
                "date": "2026-07-01",
            },
        )
        assert resp.status_code == 422


class TestTransfers:
    def test_transfer_is_one_transaction_two_real_postings(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        resp = client.post(
            "/transactions/transfer",
            json={
                "from_account": seeded["checking"],
                "to_account": seeded["savings"],
                "amount": "500.00",
                "date": "2026-07-01",
            },
        )
        assert resp.status_code == 201, resp.text
        detail = resp.json()
        assert detail["is_transfer"] is True
        assert len(detail["postings"]) == 2
        paths = {p["account_path"] for p in detail["postings"]}
        assert paths == {seeded["checking"], seeded["savings"]}
        assert _postings_sum(migrated_engine, detail["id"]) == 0

    def test_transfer_perspective_serialization(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        """A register view (`?account=`) presents the account's own signed leg
        and names the other side; the global view keeps the historical
        outgoing-side presentation with a positive amount."""
        resp = client.post(
            "/transactions/transfer",
            json={
                "from_account": seeded["checking"],
                "to_account": seeded["savings"],
                "amount": "500.00",
                "date": "2026-07-01",
            },
        )
        assert resp.status_code == 201, resp.text

        (from_side,) = client.get("/transactions", params={"account": seeded["checking"]}).json()[
            "items"
        ]
        assert Decimal(from_side["amount"]) == Decimal("-500.00")
        assert from_side["account_path"] == seeded["checking"]
        assert from_side["transfer_account_path"] == seeded["savings"]
        assert from_side["is_transfer"] is True

        (to_side,) = client.get("/transactions", params={"account": seeded["savings"]}).json()[
            "items"
        ]
        assert Decimal(to_side["amount"]) == Decimal("500.00")
        assert to_side["account_path"] == seeded["savings"]
        assert to_side["transfer_account_path"] == seeded["checking"]

        (globally,) = client.get("/transactions").json()["items"]
        assert Decimal(globally["amount"]) == Decimal("500.00")
        assert globally["account_path"] == seeded["checking"]
        assert globally["transfer_account_path"] == seeded["savings"]

    def test_flat_row_perspective_matches_global(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        resp = client.post(
            "/transactions",
            json={
                "account": seeded["checking"],
                "category": seeded["dining"],
                "amount": "-14.00",
                "date": "2026-07-01",
                "payee": "Chipotle",
            },
        )
        assert resp.status_code == 201, resp.text
        (row,) = client.get("/transactions", params={"account": seeded["checking"]}).json()["items"]
        assert Decimal(row["amount"]) == Decimal("-14.00")
        assert row["category_path"] == seeded["dining"]
        assert row["transfer_account_path"] is None
        assert row["is_transfer"] is False

    def test_search_unknown_account_is_404(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        assert client.get("/transactions", params={"account": "assets.nope"}).status_code == 404

    def test_transfer_to_category_rejected(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        resp = client.post(
            "/transactions/transfer",
            json={
                "from_account": seeded["checking"],
                "to_account": seeded["dining"],
                "amount": "50.00",
                "date": "2026-07-01",
            },
        )
        assert resp.status_code == 422


class TestPostedImmutability:
    def _post_txn(self, client: TestClient, seeded: dict[str, str], amount: str) -> dict:
        resp = client.post(
            "/transactions",
            json={
                "account": seeded["checking"],
                "category": seeded["dining"],
                "amount": amount,
                "date": "2026-07-01",
                "payee": "Chipotle",
                "status": "posted",
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["status"] == "posted"  # admin session has commit
        return resp.json()

    @settings(
        max_examples=10, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(amount=api_amounts, corrected=api_amounts)
    def test_correction_is_linked_reversal_pair(
        self,
        seeded: dict[str, str],
        client: TestClient,
        migrated_engine: Engine,
        amount: Decimal,
        corrected: Decimal,
    ) -> None:
        original = self._post_txn(client, seeded, str(amount))
        resp = client.patch(
            f"/transactions/{original['id']}",
            json={"amount": str(corrected), "category": seeded["groceries"]},
        )
        assert resp.status_code == 200, resp.text
        replacement = resp.json()

        # The original is untouched: still posted, same amount.
        after = client.get(f"/transactions/{original['id']}").json()
        assert after["status"] == "posted"
        assert Decimal(after["amount"]) == amount

        # The replacement is a new posted row with the corrected values,
        # linked to a reversal that negates the original exactly.
        assert replacement["id"] != original["id"]
        assert Decimal(replacement["amount"]) == corrected
        assert replacement["category_path"] == seeded["groceries"]
        reversal = client.get(f"/transactions/{replacement['reverses_id']}").json()
        assert reversal["reverses_id"] == original["id"]
        assert Decimal(reversal["amount"]) == -amount
        assert _postings_sum(migrated_engine, reversal["id"]) == 0

    def test_flat_view_collapses_reversal_chain(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        original = self._post_txn(client, seeded, "-40.00")
        replacement = client.patch(
            f"/transactions/{original['id']}", json={"amount": "-45.00"}
        ).json()

        flat = client.get("/transactions", params={"account": seeded["checking"]}).json()
        ids = {t["id"] for t in flat["items"]}
        assert replacement["id"] in ids
        assert original["id"] not in ids
        assert replacement["reverses_id"] not in ids

        full = client.get(
            "/transactions",
            params={"account": seeded["checking"], "include_collapsed": True},
        ).json()
        assert {original["id"], replacement["id"], replacement["reverses_id"]} <= {
            t["id"] for t in full["items"]
        }

    def test_balance_reflects_correction(self, seeded: dict[str, str], client: TestClient) -> None:
        original = self._post_txn(client, seeded, "-40.00")
        client.patch(f"/transactions/{original['id']}", json={"amount": "-45.00"})
        accounts = client.get("/accounts", params={"include_balances": True}).json()
        checking = next(a for a in accounts if a["path"] == seeded["checking"])
        assert Decimal(checking["balance"]) == Decimal("-45.00")

    def test_double_correction_rejected(self, seeded: dict[str, str], client: TestClient) -> None:
        original = self._post_txn(client, seeded, "-40.00")
        first = client.patch(f"/transactions/{original['id']}", json={"amount": "-45.00"})
        assert first.status_code == 200

        # Correcting the same original again must fail — the chain continues
        # from the replacement, never forks from the original.
        second = client.patch(f"/transactions/{original['id']}", json={"amount": "-50.00"})
        assert second.status_code == 422
        assert "already corrected" in second.json()["detail"]

        # Balance still reflects exactly one correction.
        accounts = client.get("/accounts", params={"include_balances": True}).json()
        checking = next(a for a in accounts if a["path"] == seeded["checking"])
        assert Decimal(checking["balance"]) == Decimal("-45.00")

        # The replacement remains correctable.
        replacement = first.json()
        third = client.patch(f"/transactions/{replacement['id']}", json={"amount": "-50.00"})
        assert third.status_code == 200
        accounts = client.get("/accounts", params={"include_balances": True}).json()
        checking = next(a for a in accounts if a["path"] == seeded["checking"])
        assert Decimal(checking["balance"]) == Decimal("-50.00")

    def test_zero_amount_correction_rejected(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        original = self._post_txn(client, seeded, "-40.00")
        resp = client.patch(f"/transactions/{original['id']}", json={"amount": "0"})
        assert resp.status_code == 422
        assert "nonzero" in resp.json()["detail"]

        # Nothing persisted: no reversal, balance unchanged.
        after = client.get(f"/transactions/{original['id']}").json()
        assert after["status"] == "posted"
        assert Decimal(after["amount"]) == Decimal("-40.00")
        accounts = client.get("/accounts", params={"include_balances": True}).json()
        checking = next(a for a in accounts if a["path"] == seeded["checking"])
        assert Decimal(checking["balance"]) == Decimal("-40.00")
        # Original is not marked corrected — a real correction still works.
        assert (
            client.patch(f"/transactions/{original['id']}", json={"amount": "-41.00"}).status_code
            == 200
        )


class TestScopeEnforcement:
    def _agent(self, client: TestClient, scopes: list[str]) -> dict[str, str]:
        resp = client.post("/tokens", json={"name": "agent", "scopes": scopes})
        return {"Authorization": f"Bearer {resp.json()['token']}"}

    def test_write_scope_gets_the_status_it_asks_for(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        """#436: effective_status is gone — an agent's requested status is
        honoured; the row stays attributed to the agent."""
        headers = self._agent(client, ["read", "write"])
        resp = client.post(
            "/transactions",
            json={
                "account": seeded["checking"],
                "category": seeded["dining"],
                "amount": "-12.00",
                "date": "2026-07-02",
                "status": "posted",
            },
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["status"] == "posted"
        assert resp.json()["origin"] == "agent"

    def test_session_write_is_human_origin(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        created = client.post(
            "/transactions",
            json={
                "account": seeded["checking"],
                "category": seeded["dining"],
                "amount": "-12.00",
                "date": "2026-07-02",
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["origin"] == "human"

    def test_session_named_token_still_writes_agent_origin(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        # Regression: origin keys off the server-controlled kind column — a
        # client naming its token like a session must not be classified human.
        resp = client.post("/tokens", json={"name": "session:evil", "scopes": ["read", "write"]})
        headers = {"Authorization": f"Bearer {resp.json()['token']}"}
        created = client.post(
            "/transactions",
            json={
                "account": seeded["checking"],
                "category": seeded["dining"],
                "amount": "-12.00",
                "date": "2026-07-02",
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
        assert created.json()["origin"] == "agent"

    def test_write_scope_posts_and_read_scope_cannot(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        headers = self._agent(client, ["read", "write"])
        created = client.post(
            "/transactions",
            json={
                "account": seeded["checking"],
                "category": seeded["dining"],
                "amount": "-12.00",
                "date": "2026-07-02",
            },
            headers=headers,
        ).json()
        reader = self._agent(client, ["read"])
        assert client.post(f"/transactions/{created['id']}/post", headers=reader).status_code == 403
        resp = client.post(f"/transactions/{created['id']}/post", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "posted"

    def test_write_scope_corrects_a_posted_row_by_reversal(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        posted = client.post(
            "/transactions",
            json={
                "account": seeded["checking"],
                "category": seeded["dining"],
                "amount": "-30.00",
                "date": "2026-07-02",
                "status": "posted",
            },
        ).json()
        headers = self._agent(client, ["read", "write"])
        resp = client.patch(
            f"/transactions/{posted['id']}", json={"amount": "-35.00"}, headers=headers
        )
        assert resp.status_code == 200, resp.text
        # Undo, not pre-approval: the original is kept, reversed and replaced.
        replacement = resp.json()
        assert replacement["id"] != posted["id"]
        assert Decimal(replacement["amount"]) == Decimal("-35.00")
        original = client.get(f"/transactions/{posted['id']}").json()
        assert Decimal(original["amount"]) == Decimal("-30.00")


class TestLifecycle:
    def test_pending_to_posted_then_bare_void_rejected(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        created = client.post(
            "/transactions",
            json={
                "account": seeded["checking"],
                "category": seeded["dining"],
                "amount": "-10.00",
                "date": "2026-07-01",
            },
        ).json()
        assert created["status"] == "pending"
        posted = client.post(f"/transactions/{created['id']}/post").json()
        assert posted["status"] == "posted"
        # Bare posted->void erases balance effect with no lineage (DESIGN §4).
        resp = client.post(f"/transactions/{created['id']}/void")
        assert resp.status_code == 422
        assert "cannot be voided directly" in resp.json()["detail"]
        assert client.get(f"/transactions/{created['id']}").json()["status"] == "posted"

    def test_pending_to_void_is_terminal(self, seeded: dict[str, str], client: TestClient) -> None:
        created = client.post(
            "/transactions",
            json={
                "account": seeded["checking"],
                "category": seeded["dining"],
                "amount": "-10.00",
                "date": "2026-07-01",
            },
        ).json()
        voided = client.post(f"/transactions/{created['id']}/void").json()
        assert voided["status"] == "void"
        # void is terminal
        assert client.post(f"/transactions/{created['id']}/post").status_code == 422

    def test_pending_edit_answers_with_the_new_category_and_payee(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        """#383: an in-place edit of a pending row re-pointed the category leg
        and the payee by foreign key, so the PATCH response still carried the
        old category_path and payee while a later GET was right."""
        created = client.post(
            "/transactions",
            json={
                "account": seeded["checking"],
                "category": seeded["dining"],
                "amount": "-10.00",
                "date": "2026-07-01",
                "payee": "Example Bistro",
            },
        ).json()
        resp = client.patch(
            f"/transactions/{created['id']}",
            json={"category": seeded["groceries"], "payee": "Example Market"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["category_path"] == seeded["groceries"]
        assert resp.json()["payee"] == "Example Market"
        fetched = client.get(f"/transactions/{created['id']}").json()
        assert fetched["category_path"] == seeded["groceries"]
        assert fetched["payee"] == "Example Market"

    def test_posted_events_emitted(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        client.post(
            "/transactions",
            json={
                "account": seeded["checking"],
                "category": seeded["dining"],
                "amount": "-10.00",
                "date": "2026-07-01",
                "status": "posted",
            },
        )
        with migrated_engine.connect() as conn:
            count = conn.execute(
                text("SELECT COUNT(*) FROM events WHERE type = 'transaction.posted'")
            ).scalar_one()
        assert count >= 1


class TestCategoryRollup:
    def test_subtree_rollup_single_query(self, seeded: dict[str, str], client: TestClient) -> None:
        def spend(category: str, amount: str) -> None:
            resp = client.post(
                "/transactions",
                json={
                    "account": seeded["checking"],
                    "category": category,
                    "amount": amount,
                    "date": "2026-07-01",
                    "status": "posted",
                },
            )
            assert resp.status_code == 201

        spend(seeded["dining"], "-20.00")
        spend(seeded["coffee"], "-5.00")  # child of dining
        spend(seeded["groceries"], "-80.00")  # sibling, outside subtree

        rollup = client.get(f"/categories/{seeded['dining']}/rollup").json()
        assert Decimal(rollup["total"]) == Decimal("25.00")  # dining + coffee only
        assert rollup["transaction_count"] == 2

        parent = client.get(f"/categories/{seeded['expenses']}/rollup").json()
        assert Decimal(parent["total"]) == Decimal("105.00")

    def test_search_by_category_subtree(self, seeded: dict[str, str], client: TestClient) -> None:
        for category, amount in [(seeded["coffee"], "-5.00"), (seeded["groceries"], "-80.00")]:
            client.post(
                "/transactions",
                json={
                    "account": seeded["checking"],
                    "category": category,
                    "amount": amount,
                    "date": "2026-07-01",
                },
            )
        page = client.get("/transactions", params={"category": seeded["dining"]}).json()
        assert page["total"] == 1
        assert page["items"][0]["category_path"] == seeded["coffee"]


def test_unauthenticated_rejected(clean_db: None, client: TestClient) -> None:
    assert client.get("/transactions").status_code == 401
    assert client.post("/transactions", json={}).status_code == 401
    assert client.post("/accounts", json={"name": "X", "type": "checking"}).status_code == 401


def test_account_creation_requires_write(admin_client: TestClient) -> None:
    resp = admin_client.post("/tokens", json={"name": "agent", "scopes": ["read"]})
    headers = {"Authorization": f"Bearer {resp.json()['token']}"}
    denied = admin_client.post(
        "/accounts", json={"name": "Sneaky", "type": "checking"}, headers=headers
    )
    assert denied.status_code == 403


def test_uuid_and_path_refs_both_work(admin_client: TestClient) -> None:
    checking = admin_client.post(
        "/accounts", json={"name": "Checking", "type": "checking", "path": "assets.checking"}
    ).json()
    dining = admin_client.post(
        "/categories",
        json={"name": "Dining", "type": "category_expense", "path": "expenses.dining"},
    ).json()
    resp = admin_client.post(
        "/transactions",
        json={
            "account": checking["id"],  # by uuid
            "category": "expenses.dining",  # by path
            "amount": "-14.00",
            "date": "2026-07-01",
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["account_path"] == "assets.checking"
    assert resp.json()["category_path"] == dining["path"]
