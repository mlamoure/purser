"""Curated category set: shipped defaults, seeding, CRUD, classifier prompt
(DESIGN §3.2, issue #110)."""

import re
import uuid
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from purser.ai_providers import _prompt
from purser.categoryseed import seed_default_categories
from purser.core.categories import DEFAULT_CATEGORIES

LTREE_LABEL = re.compile(r"^[a-z0-9_]+$")


class TestDefaultSet:
    def test_paths_are_ltree_valid_and_unique(self) -> None:
        seen: set[str] = set()
        for spec in DEFAULT_CATEGORIES:
            assert spec.path not in seen, f"duplicate {spec.path}"
            seen.add(spec.path)
            for label in spec.path.split("."):
                assert LTREE_LABEL.match(label), f"bad ltree label in {spec.path}"

    def test_parents_precede_children(self) -> None:
        seen: set[str] = set()
        for spec in DEFAULT_CATEGORIES:
            if "." in spec.path:
                parent = spec.path.rsplit(".", 1)[0]
                assert parent in seen, f"{spec.path} appears before its parent"
            seen.add(spec.path)

    def test_every_entry_is_described_and_typed(self) -> None:
        for spec in DEFAULT_CATEGORIES:
            assert spec.description.strip(), f"{spec.path} has no description"
            root = spec.path.split(".")[0]
            expected = "category_income" if root == "income" else "category_expense"
            assert spec.type == expected, spec.path

    def test_no_transfer_categories_ship(self) -> None:
        """Transfers are structural in Purser (DESIGN §3.3) — the default set
        must never teach the classifier a 'transfer' bucket."""
        for spec in DEFAULT_CATEGORIES:
            assert "transfer" not in spec.path
            assert "transfer" not in spec.name.lower()


class TestSeeding:
    def test_seed_is_idempotent_and_links_parents(
        self, clean_db: None, migrated_engine: Engine
    ) -> None:
        with migrated_engine.begin() as conn:
            first = seed_default_categories(conn)
            second = seed_default_categories(conn)
        assert first == len(DEFAULT_CATEGORIES)
        assert second == 0
        with migrated_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT c.description, p.path AS parent_path"
                    " FROM accounts c JOIN accounts p ON p.id = c.parent_id"
                    " WHERE c.path = 'expenses.food.groceries'"
                )
            ).one()
        assert "farmers" in row.description
        assert row.parent_path == "expenses.food"

    def test_seed_skips_existing_paths(self, clean_db: None, migrated_engine: Engine) -> None:
        with migrated_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO accounts (name, type, is_virtual, path)"
                    " VALUES ('My Food', 'category_expense', true, 'expenses.food')"
                )
            )
            created = seed_default_categories(conn)
        assert created == len(DEFAULT_CATEGORIES) - 1
        with migrated_engine.connect() as conn:
            name, description = conn.execute(
                text("SELECT name, description FROM accounts WHERE path = 'expenses.food'")
            ).one()
            groceries_parent = conn.execute(
                text(
                    "SELECT p.name FROM accounts c JOIN accounts p ON p.id = c.parent_id"
                    " WHERE c.path = 'expenses.food.groceries'"
                )
            ).scalar_one()
        assert name == "My Food"  # user's row untouched
        assert description is None
        assert groceries_parent == "My Food"  # children still link to it


class TestCategoryCrud:
    def _category(self, client: TestClient, path: str = "expenses.dining") -> dict:
        resp = client.post(
            "/categories",
            json={
                "name": "Dining",
                "type": "category_expense",
                "path": path,
                "description": "Restaurants and bars.",
            },
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    def test_create_carries_description(self, admin_client: TestClient) -> None:
        created = self._category(admin_client)
        assert created["description"] == "Restaurants and bars."
        listed = admin_client.get("/categories").json()
        assert any(c["description"] == "Restaurants and bars." for c in listed)

    def test_patch_renames_and_redescribes(self, admin_client: TestClient) -> None:
        created = self._category(admin_client)
        resp = admin_client.patch(
            f"/categories/{created['id']}",
            json={"name": "Eating out", "description": "Only sit-down restaurants."},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "Eating out"
        assert resp.json()["description"] == "Only sit-down restaurants."
        assert resp.json()["path"] == "expenses.dining"  # paths never change here

    def test_patch_real_account_rejected(self, admin_client: TestClient) -> None:
        acct = admin_client.post(
            "/accounts", json={"name": "Checking", "type": "checking", "path": "assets.checking"}
        ).json()
        assert (
            admin_client.patch(f"/categories/{acct['id']}", json={"name": "x"}).status_code == 422
        )

    def test_delete_unused_ok(self, admin_client: TestClient) -> None:
        created = self._category(admin_client)
        assert admin_client.delete(f"/categories/{created['id']}").status_code == 204
        assert not any(c["id"] == created["id"] for c in admin_client.get("/categories").json())

    def test_delete_guards(self, admin_client: TestClient, migrated_engine: Engine) -> None:
        parent = self._category(admin_client, path="expenses.dining")
        child = admin_client.post(
            "/categories",
            json={"name": "Coffee", "type": "category_expense", "parent_id": parent["id"]},
        ).json()
        # children guard
        resp = admin_client.delete(f"/categories/{parent['id']}")
        assert resp.status_code == 422
        assert "child" in resp.json()["detail"]

        # postings guard
        admin_client.post(
            "/accounts", json={"name": "Checking", "type": "checking", "path": "assets.checking"}
        )
        admin_client.post(
            "/transactions",
            json={
                "account": "assets.checking",
                "category": child["path"],
                "amount": "-4.50",
                "date": "2026-07-01",
            },
        )
        resp = admin_client.delete(f"/categories/{child['id']}")
        assert resp.status_code == 422
        assert "transactions" in resp.json()["detail"]

        # schedule-template guard
        template = self._category(admin_client, path="expenses.utilities")
        with migrated_engine.begin() as conn:
            account_id = conn.execute(
                text("SELECT id FROM accounts WHERE path = 'assets.checking'")
            ).scalar_one()
            from purser import crypto

            conn.execute(
                text(
                    "INSERT INTO schedules"
                    " (name_enc, account_id, category_path, rrule, dtstart, amount_type,"
                    "  source, status)"
                    " VALUES (:name_enc, :a, 'expenses.utilities',"
                    "  'FREQ=MONTHLY;BYMONTHDAY=1', CURRENT_DATE, 'fixed', 'manual', 'active')"
                ),
                {"a": account_id, "name_enc": crypto.encrypt("Electric")},
            )
        resp = admin_client.delete(f"/categories/{template['id']}")
        assert resp.status_code == 422
        assert "schedule" in resp.json()["detail"]

    def test_delete_unknown_404(self, admin_client: TestClient) -> None:
        assert admin_client.delete(f"/categories/{uuid.uuid4()}").status_code == 404


class TestClassifierPrompt:
    def test_descriptions_ride_along(self) -> None:
        prompt = _prompt(
            "STARBUCKS #221",
            "STARBUCKS STORE 221",
            Decimal("-6.40"),
            ["expenses.food.coffee", "expenses.food.groceries"],
            {"expenses.food.coffee": "Coffee houses, cafes, and espresso or tea bars."},
        )
        assert "- expenses.food.coffee — Coffee houses, cafes, and espresso or tea bars." in prompt
        assert "- expenses.food.groceries" in prompt  # bare when undescribed

    def test_prompt_without_descriptions_unchanged_shape(self) -> None:
        prompt = _prompt("X", None, Decimal("-1"), ["expenses.a"], None)
        assert "- expenses.a" in prompt
        assert "—" not in prompt
