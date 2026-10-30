"""An account's floor (`min_balance`) is editable after creation (#466).

The floor is what the nightly forecast scan checks before emitting
`forecast.shortfall` (DESIGN §3.4/§6). It was settable only by POST
/accounts; PATCH /accounts/{id} now sets and clears it (write scope), with
the same validation as creation, an audited before/after, and the next scan
honouring the change. The MCP side lives in tests/test_mcp.py.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from purser import schedules as schedules_service
from purser.models import AuditLog, Event


@pytest.fixture
def seeded(admin_client: TestClient) -> dict[str, str]:
    """A checking account without a floor, a card, a loan and two categories."""

    def make(endpoint: str, body: dict[str, str]) -> str:
        resp = admin_client.post(endpoint, json=body)
        assert resp.status_code == 201, resp.text
        return str(resp.json()["id"])

    ids = {
        "checking": make(
            "/accounts", {"name": "Checking", "type": "checking", "path": "assets.checking"}
        ),
        "card": make(
            "/accounts", {"name": "Card", "type": "credit_card", "path": "liabilities.card"}
        ),
        "loan": make(
            "/accounts", {"name": "Mortgage", "type": "loan", "path": "liabilities.mortgage"}
        ),
    }
    make("/categories", {"name": "Housing", "type": "category_expense", "path": "expenses"})
    make("/categories", {"name": "Salary", "type": "category_income", "path": "income"})
    return ids


def _patch(client: TestClient, account_id: str, body: dict[str, object], **kw: object) -> object:
    return client.patch(f"/accounts/{account_id}", json=body, **kw)  # type: ignore[arg-type]


def _floor_audits(db_session: Session, account_id: str) -> list[dict[str, dict[str, object]]]:
    rows = db_session.execute(
        select(AuditLog)
        .where(AuditLog.action == "account.update", AuditLog.entity_id == uuid.UUID(account_id))
        .order_by(AuditLog.at)
    ).scalars()
    return [row.before_after for row in rows]


class TestSetAndClear:
    def test_set_change_and_clear_with_explicit_null(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        checking = seeded["checking"]
        resp = _patch(client, checking, {"min_balance": "500.00"})
        assert resp.status_code == 200, resp.text  # type: ignore[attr-defined]
        assert Decimal(resp.json()["min_balance"]) == Decimal("500.00")  # type: ignore[attr-defined]
        # Absent leaves it unchanged...
        resp = _patch(client, checking, {"name": "Everyday"})
        assert Decimal(resp.json()["min_balance"]) == Decimal("500.00")  # type: ignore[attr-defined]
        # ...a new value replaces it (zero is a floor too)...
        resp = _patch(client, checking, {"min_balance": "0"})
        assert Decimal(resp.json()["min_balance"]) == Decimal("0")  # type: ignore[attr-defined]
        # ...and an explicit null clears it.
        resp = _patch(client, checking, {"min_balance": None})
        assert resp.status_code == 200  # type: ignore[attr-defined]
        assert resp.json()["min_balance"] is None  # type: ignore[attr-defined]
        assert client.get(f"/accounts/{checking}").json()["min_balance"] is None

    def test_a_card_takes_a_negative_floor(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        """Ledger sign: a card is negative when owed, so "warn me past 4,500
        owed" is -4500."""
        resp = _patch(client, seeded["card"], {"min_balance": "-4500.00"})
        assert resp.status_code == 200, resp.text  # type: ignore[attr-defined]
        assert Decimal(resp.json()["min_balance"]) == Decimal("-4500.00")  # type: ignore[attr-defined]

    def test_every_change_is_audited_and_reversed_by_restoring_before(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        """account.update carries min_balance on both sides, as a decimal
        string. An account edit is undone the way every account edit is: a
        PATCH back to the audited `before`, itself audited."""
        checking = seeded["checking"]
        assert _patch(client, checking, {"min_balance": "250.00"}).status_code == 200  # type: ignore[attr-defined]
        assert _patch(client, checking, {"min_balance": "750.5"}).status_code == 200  # type: ignore[attr-defined]
        first, second = _floor_audits(db_session, checking)
        assert (first["before"]["min_balance"], first["after"]["min_balance"]) == (
            None,
            "250.0000",
        )
        assert (second["before"]["min_balance"], second["after"]["min_balance"]) == (
            "250.0000",
            "750.5000",
        )
        # Reverse the last change from its audit row.
        restore = second["before"]["min_balance"]
        assert _patch(client, checking, {"min_balance": restore}).status_code == 200  # type: ignore[attr-defined]
        assert Decimal(client.get(f"/accounts/{checking}").json()["min_balance"]) == Decimal(
            "250.00"
        )
        third = _floor_audits(db_session, checking)[-1]
        assert (third["before"]["min_balance"], third["after"]["min_balance"]) == (
            "750.5000",
            "250.0000",
        )


class TestValidation:
    @pytest.mark.parametrize(
        "value",
        ["five hundred", "1.23456", "12345678901234567.00", "NaN", "Infinity"],
    )
    def test_malformed_values_are_refused(
        self, seeded: dict[str, str], client: TestClient, db_session: Session, value: str
    ) -> None:
        resp = _patch(client, seeded["checking"], {"min_balance": value})
        assert resp.status_code == 422, resp.text  # type: ignore[attr-defined]
        assert client.get(f"/accounts/{seeded['checking']}").json()["min_balance"] is None
        assert _floor_audits(db_session, seeded["checking"]) == []

    def test_a_floor_applies_only_to_cash_accounts_and_cards(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        resp = _patch(client, seeded["loan"], {"min_balance": "0"})
        assert resp.status_code == 422  # type: ignore[attr-defined]
        assert "checking, credit_card, savings" in resp.json()["detail"]  # type: ignore[attr-defined]
        # Clearing is always allowed, so a floor stored before this rule can go.
        assert _patch(client, seeded["loan"], {"min_balance": None}).status_code == 200  # type: ignore[attr-defined]
        # Creation follows the same rule.
        created = client.post(
            "/accounts",
            json={"name": "House", "type": "property", "path": "assets.house", "min_balance": "1"},
        )
        assert created.status_code == 422, created.text
        created = client.post(
            "/accounts",
            json={
                "name": "Savings",
                "type": "savings",
                "path": "assets.savings",
                "min_balance": "1000.00",
            },
        )
        assert created.status_code == 201, created.text
        assert Decimal(created.json()["min_balance"]) == Decimal("1000.00")

    def test_a_category_is_refused(self, seeded: dict[str, str], client: TestClient) -> None:
        category = next(c for c in client.get("/categories").json() if c["path"] == "expenses")
        resp = _patch(client, category["id"], {"min_balance": "0"})
        assert resp.status_code == 422  # type: ignore[attr-defined]
        assert "PATCH /categories" in resp.json()["detail"]  # type: ignore[attr-defined]

    def test_needs_write_scope(self, seeded: dict[str, str], client: TestClient) -> None:
        def bearer(scopes: list[str]) -> dict[str, str]:
            token = client.post("/tokens", json={"name": "agent", "scopes": scopes}).json()
            return {"Authorization": f"Bearer {token['token']}"}

        denied = _patch(
            client, seeded["checking"], {"min_balance": "500.00"}, headers=bearer(["read"])
        )
        assert denied.status_code == 403  # type: ignore[attr-defined]
        assert client.get(f"/accounts/{seeded['checking']}").json()["min_balance"] is None
        # Write scope is enough: the floor only governs warnings (not admin).
        allowed = _patch(
            client, seeded["checking"], {"min_balance": "500.00"}, headers=bearer(["write"])
        )
        assert allowed.status_code == 200, allowed.text  # type: ignore[attr-defined]


class TestShortfallScan:
    def _shortfalls(self, engine: Engine) -> list[dict[str, str]]:
        # Payloads are encrypted at rest (#137): read them through the model.
        with Session(engine) as session:
            return [
                dict(payload)
                for payload in session.execute(
                    select(Event.payload)
                    .where(Event.type == "forecast.shortfall")
                    .order_by(Event.at)
                ).scalars()
            ]

    def _expected(self, client: TestClient, amount: str, on: date) -> None:
        resp = client.post(
            "/transactions",
            json={
                "account": "assets.checking",
                "category": "expenses",
                "amount": amount,
                "date": on.isoformat(),
                "payee": "Lakeside Rent",
                "status": "expected",
            },
        )
        assert resp.status_code == 201, resp.text

    def test_a_floor_set_later_joins_the_next_scan_and_clearing_it_leaves(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        today = date.today()  # the forecast's own "today" (schedules.forecast_account)
        opening = client.post(
            "/transactions",
            json={
                "account": "assets.checking",
                "category": "income",
                "amount": "300.00",
                "date": (today - timedelta(days=20)).isoformat(),
                "status": "posted",
            },
        )
        assert opening.status_code == 201, opening.text
        self._expected(client, "-250.00", today + timedelta(days=5))  # projects 50.00

        # No floor: the account is not scanned.
        schedules_service.shortfall_scan_job()
        assert self._shortfalls(migrated_engine) == []

        # A floor set on the existing account is picked up by the next run.
        assert _patch(client, seeded["checking"], {"min_balance": "100.00"}).status_code == 200  # type: ignore[attr-defined]
        schedules_service.shortfall_scan_job()
        (fired,) = self._shortfalls(migrated_engine)
        assert fired["account"] == "assets.checking"
        assert fired["date"] == (today + timedelta(days=5)).isoformat()
        assert Decimal(fired["low"]) == Decimal("50.00")
        assert Decimal(fired["min_balance"]) == Decimal("100.00")

        # Cleared: a new, earlier dip below the old floor raises nothing.
        assert _patch(client, seeded["checking"], {"min_balance": None}).status_code == 200  # type: ignore[attr-defined]
        self._expected(client, "-275.00", today + timedelta(days=1))
        schedules_service.shortfall_scan_job()
        assert len(self._shortfalls(migrated_engine)) == 1
