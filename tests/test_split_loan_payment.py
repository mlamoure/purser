"""Split loan-payment confirmation (issue #269, DESIGN §3.3, decision 62).

A loan payment's two bank legs are unequal: the cash leg on checking includes
interest/fees, while the loan account shows principal only. An explicit
confirm_transfer with a remainder_category books ONE transaction — the exact
cash leg, the exact principal leg, and one balancing interest/fee posting —
and never a synthetic full-payment loan leg. Bounds: 0 < incoming < |outgoing|.

Synthetic data only; every date is fixed, so nothing here depends on today.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, text

from purser import crypto
from tests.test_matching import _imported

ON = date(2026, 7, 15)
CASH = Decimal("-612.40")  # what left checking
PRINCIPAL = Decimal("540.15")  # what the loan account shows as paid down
INTEREST = Decimal("72.25")  # the remainder: CASH paid beyond PRINCIPAL
CHECKING, LOAN, SAVINGS = "assets.checking", "liabilities.carloan", "assets.savings"
INTEREST_CATEGORY = "expenses.fees.interest"


@pytest.fixture
def seeded(admin_client: TestClient) -> None:
    for name, type_, path in (
        ("Checking", "checking", CHECKING),
        ("Car Loan", "loan", LOAN),
        ("Savings", "savings", SAVINGS),
    ):
        resp = admin_client.post("/accounts", json={"name": name, "type": type_, "path": path})
        assert resp.status_code == 201, resp.text
    for name, type_, path in (
        ("Fees", "category_expense", "expenses.fees"),
        ("Interest charges", "category_expense", INTEREST_CATEGORY),
        ("Salary", "category_income", "income.salary"),
    ):
        resp = admin_client.post("/categories", json={"name": name, "type": type_, "path": path})
        assert resp.status_code == 201, resp.text


def _pair(
    client: TestClient,
    engine: Engine,
    cash: Decimal = CASH,
    principal: Decimal = PRINCIPAL,
) -> tuple[str, str]:
    """The two one-sided bank rows of a loan payment, a day apart."""
    out_id = _imported(client, engine, CHECKING, str(cash), ON, payee="AUTO LOAN PAYMENT")
    in_id = _imported(
        client, engine, LOAN, str(principal), ON + timedelta(days=1), payee="PAYMENT RECEIVED"
    )
    return out_id, in_id


def _confirm(
    client: TestClient,
    out_id: str,
    in_id: str,
    remainder: str | None = INTEREST_CATEGORY,
    headers: dict[str, str] | None = None,
) -> dict:
    body = {"outgoing_id": out_id, "incoming_id": in_id}
    if remainder is not None:
        body["remainder_category"] = remainder
    resp = client.post("/matching/confirm-transfer", json=body, headers=headers or {})
    return {"status": resp.status_code, **resp.json()}


def _postings_sum(engine: Engine, tx_id: str) -> Decimal:
    with engine.connect() as conn:
        return Decimal(
            conn.execute(
                text("SELECT COALESCE(SUM(amount), 0) FROM postings WHERE transaction_id = :id"),
                {"id": tx_id},
            ).scalar_one()
        )


def _event_count(engine: Engine, type_: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT COUNT(*) FROM events WHERE type = :t"), {"t": type_}
            ).scalar_one()
        )


def _insert(engine: Engine, legs: list[tuple[str, str]], status: str = "posted") -> str:
    """A transaction written straight to the ledger, legs by account path —
    for shapes no write path produces today (and a schedule-style expected)."""
    with engine.begin() as conn:
        tx_id = conn.execute(
            text(
                "INSERT INTO transactions (date, description_enc, status, origin) "
                "VALUES (:d, :desc, :s, 'human') RETURNING id"
            ),
            {"d": ON, "desc": crypto.encrypt("synthetic row"), "s": status},
        ).scalar_one()
        for path, amount in legs:
            conn.execute(
                text(
                    "INSERT INTO postings (transaction_id, account_id, amount) "
                    "SELECT :tx, id, :amt FROM accounts WHERE path = :p"
                ),
                {"tx": tx_id, "amt": amount, "p": path},
            )
    return str(tx_id)


def _register_row(client: TestClient, tx_id: str, account: str | None = None) -> dict:
    params = {"limit": 200, **({"account": account} if account else {})}
    items = client.get("/transactions", params=params).json()["items"]
    return next(i for i in items if i["id"] == tx_id)


class TestConfirmSplit:
    def test_books_exact_legs_and_one_remainder_posting(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id, in_id = _pair(client, migrated_engine)
        result = _confirm(client, out_id, in_id)
        assert result["status"] == 200, result
        merged_id = result["merged_transaction_id"]
        assert Decimal(result["remainder"]) == INTEREST
        assert result["remainder_category"] == INTEREST_CATEGORY

        detail = client.get(f"/transactions/{merged_id}").json()
        postings = {p["account_path"]: Decimal(p["amount"]) for p in detail["postings"]}
        # Exactly three postings, one per account: the bank's cash leg, the
        # loan's principal leg, and the interest — no full-payment loan leg.
        assert len(detail["postings"]) == 3
        assert postings == {CHECKING: CASH, LOAN: PRINCIPAL, INTEREST_CATEGORY: INTEREST}
        assert _postings_sum(migrated_engine, merged_id) == 0
        assert detail["status"] == "posted"
        assert detail["date"] == ON.isoformat()  # min of the two bank dates
        for original in (out_id, in_id):
            row = client.get(f"/transactions/{original}").json()
            assert row["status"] == "void"
            assert row["superseded_by"] == merged_id

        # Audited and evented, with the split's numbers on both records.
        with migrated_engine.connect() as conn:
            audit_enc = conn.execute(
                text(
                    "SELECT before_after_enc FROM audit_log "
                    "WHERE action = 'transfer.confirm' AND entity_id = :id"
                ),
                {"id": merged_id},
            ).scalar_one()
            event_enc = conn.execute(
                text(
                    "SELECT payload_enc FROM events "
                    "WHERE type = 'transfer.confirmed' AND entity_id = :id"
                ),
                {"id": merged_id},
            ).scalar_one()
        after = crypto.decrypt_json(bytes(audit_enc))["after"]
        assert Decimal(after["amount"]) == -CASH
        assert Decimal(after["incoming_amount"]) == PRINCIPAL
        assert Decimal(after["remainder"]) == INTEREST
        assert after["remainder_category"] == INTEREST_CATEGORY
        payload = crypto.decrypt_json(bytes(event_enc))
        assert Decimal(payload["remainder"]) == INTEREST
        assert payload["remainder_category"] == INTEREST_CATEGORY
        assert payload["outgoing_status"] == payload["incoming_status"] == "posted"
        # A split proposes no transfer_link rule (#271): every automatic path
        # needs exactly opposite amounts, so a rule could never link it.
        assert "suggestion_id" not in result
        assert client.get("/suggestions", params={"kind": "transfer_link"}).json() == []

    def test_ordinary_transfer_shape_is_unchanged(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        """No remainder keys anywhere for an opposite-amount pair."""
        out_id = _imported(client, migrated_engine, CHECKING, "-500.00", ON, payee="XFER")
        in_id = _imported(client, migrated_engine, SAVINGS, "500.00", ON, payee="XFER")
        result = _confirm(client, out_id, in_id, remainder=None)
        assert result["status"] == 200, result
        # An ordinary confirm also proposes a transfer_link rule (#271); the
        # remainder keys stay split-only.
        assert set(result) == {"status", "merged_transaction_id", "suggestion_id"}
        row = _register_row(client, result["merged_transaction_id"], CHECKING)
        assert row["is_transfer"] is True
        assert row["category_path"] is None
        assert row["remainder_amount"] is None

    @pytest.mark.parametrize(
        ("cash", "principal", "message"),
        [
            pytest.param("-500.00", "500.00", "already cancel", id="equal"),
            # The review's example: booking -600.12 as "interest" is refused.
            pytest.param("-50.00", "650.12", "exceeds", id="greater"),
            pytest.param("-612.40", "0.00", "must be positive", id="zero"),
            pytest.param("-612.40", "-540.15", "must be positive", id="negative"),
        ],
    )
    def test_out_of_bounds_legs_are_refused_with_422(
        self,
        seeded: None,
        client: TestClient,
        migrated_engine: Engine,
        cash: str,
        principal: str,
        message: str,
    ) -> None:
        out_id, in_id = _pair(client, migrated_engine, Decimal(cash), Decimal(principal))
        result = _confirm(client, out_id, in_id)
        assert result["status"] == 422, result
        assert message in result["detail"]
        # Nothing was written: both bank rows stay live and one-sided.
        for original in (out_id, in_id):
            row = client.get(f"/transactions/{original}").json()
            assert row["status"] == "posted"
            assert row["superseded_by"] is None
        assert _event_count(migrated_engine, "transfer.confirmed") == 0

    def test_unequal_legs_without_remainder_name_the_split_path(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id, in_id = _pair(client, migrated_engine)
        result = _confirm(client, out_id, in_id, remainder=None)
        assert result["status"] == 422
        assert "opposite amounts" in result["detail"]
        assert "remainder_category" in result["detail"]

    def test_blank_remainder_category_means_none(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        """An agent's "" for the omitted optional string is no split request."""
        out_id = _imported(client, migrated_engine, CHECKING, "-500.00", ON, payee="XFER")
        in_id = _imported(client, migrated_engine, SAVINGS, "500.00", ON, payee="XFER")
        result = _confirm(client, out_id, in_id, remainder=" ")
        assert result["status"] == 200, result
        assert "remainder" not in result
        out_id, in_id = _pair(client, migrated_engine)
        result = _confirm(client, out_id, in_id, remainder="")
        assert result["status"] == 422
        assert "opposite amounts" in result["detail"]

    def test_out_of_bounds_legs_without_remainder_get_no_split_hint(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id, in_id = _pair(client, migrated_engine, Decimal("-50.00"), Decimal("650.12"))
        result = _confirm(client, out_id, in_id, remainder=None)
        assert result["status"] == 422
        assert "remainder_category" not in result["detail"]

    @pytest.mark.parametrize(
        ("category", "status", "message"),
        [
            pytest.param(SAVINGS, 422, "real account", id="real-account"),
            pytest.param("income.salary", 422, "not an expense category", id="income"),
            pytest.param("expenses.uncategorized", 422, "placeholder", id="uncategorized"),
            pytest.param("expenses.nope", 404, "not found", id="unknown"),
        ],
    )
    def test_remainder_category_must_be_a_specific_expense_category(
        self,
        seeded: None,
        client: TestClient,
        migrated_engine: Engine,
        category: str,
        status: int,
        message: str,
    ) -> None:
        out_id, in_id = _pair(client, migrated_engine)
        result = _confirm(client, out_id, in_id, remainder=category)
        assert result["status"] == status, result
        assert message in result["detail"]
        assert client.get(f"/transactions/{out_id}").json()["status"] == "posted"

    def test_requires_write_scope(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id, in_id = _pair(client, migrated_engine)
        agent = client.post("/tokens", json={"name": "agent", "scopes": ["read"]})
        headers = {"Authorization": f"Bearer {agent.json()['token']}"}
        result = _confirm(client, out_id, in_id, headers=headers)
        assert result["status"] == 403
        assert client.get(f"/transactions/{out_id}").json()["status"] == "posted"

    def test_edits_need_an_unlink_first(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id, in_id = _pair(client, migrated_engine)
        merged_id = _confirm(client, out_id, in_id)["merged_transaction_id"]
        for body in (
            {"category": "expenses.fees", "account": CHECKING},  # flatten it
            {"category": f"[{SAVINGS}]"},  # re-point it
        ):
            resp = client.patch(f"/transactions/{merged_id}", json=body)
            assert resp.status_code == 422, resp.text
            assert "unlink" in resp.json()["detail"]
        assert len(client.get(f"/transactions/{merged_id}").json()["postings"]) == 3


class TestZeroSumProperty:
    """Guardrail 3: postings per transaction sum to zero — for every split
    the confirm path can book, end to end through the database trigger."""

    @settings(
        max_examples=15, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(data=st.data())
    def test_every_split_is_balanced_with_exact_bank_legs(
        self, seeded: None, client: TestClient, migrated_engine: Engine, data: st.DataObject
    ) -> None:
        cash = data.draw(
            st.decimals(min_value=Decimal("0.02"), max_value=Decimal("99999.99"), places=2)
        )
        principal = data.draw(
            st.decimals(min_value=Decimal("0.01"), max_value=cash - Decimal("0.01"), places=2)
        )
        out_id, in_id = _pair(client, migrated_engine, -cash, principal)
        result = _confirm(client, out_id, in_id)
        assert result["status"] == 200, result
        merged_id = result["merged_transaction_id"]
        assert _postings_sum(migrated_engine, merged_id) == 0
        postings = {
            p["account_path"]: Decimal(p["amount"])
            for p in client.get(f"/transactions/{merged_id}").json()["postings"]
        }
        assert postings == {CHECKING: -cash, LOAN: principal, INTEREST_CATEGORY: cash - principal}
        assert Decimal(result["remainder"]) == cash - principal


class TestUnlink:
    def test_unlink_restores_both_originals_exactly(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id, in_id = _pair(client, migrated_engine)
        originals = {i: client.get(f"/transactions/{i}").json() for i in (out_id, in_id)}
        merged_id = _confirm(client, out_id, in_id)["merged_transaction_id"]

        resp = client.post(f"/transactions/{merged_id}/unlink-transfer")
        assert resp.status_code == 200, resp.text
        result = resp.json()
        assert result["transfer_voided"] is True
        assert len(result["resurrected_ids"]) == 2
        clones = {
            c["account_path"]: c
            for c in (client.get(f"/transactions/{i}").json() for i in result["resurrected_ids"])
        }
        for original in originals.values():
            clone = clones[original["account_path"]]
            # Each bank row comes back exactly as it was: amount, date, payee,
            # category, status — a one-sided row again, not a transfer.
            for field in ("amount", "date", "payee", "category_path", "status"):
                assert clone[field] == original[field], field
            assert clone["is_transfer"] is False
            assert clone["remainder_amount"] is None
            assert _postings_sum(migrated_engine, clone["id"]) == 0

        merged = client.get(f"/transactions/{merged_id}").json()
        assert merged["status"] == "void"
        # The interest posting went with it: nothing live books to the category.
        interest = client.get("/transactions", params={"category": INTEREST_CATEGORY}).json()
        assert interest["total"] == 0
        # Balances are back to the two one-sided rows.
        assert Decimal(_register_row(client, clones[CHECKING]["id"], CHECKING)["balance"]) == CASH
        assert Decimal(_register_row(client, clones[LOAN]["id"], LOAN)["balance"]) == PRINCIPAL
        assert _event_count(migrated_engine, "transfer.unlinked") == 1


class TestRegisterRepresentation:
    def test_register_shows_counterparty_and_remainder_from_each_side(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id, in_id = _pair(client, migrated_engine)
        merged_id = _confirm(client, out_id, in_id)["merged_transaction_id"]

        checking = _register_row(client, merged_id, CHECKING)
        assert checking["is_transfer"] is True
        assert Decimal(checking["amount"]) == CASH
        assert checking["transfer_account_path"] == LOAN
        assert checking["category_path"] == INTEREST_CATEGORY
        assert Decimal(checking["remainder_amount"]) == INTEREST
        assert checking["has_linked_originals"] is True
        # Stored running balances: each account carries only its own bank leg.
        assert Decimal(checking["balance"]) == CASH

        loan = _register_row(client, merged_id, LOAN)
        assert loan["is_transfer"] is True
        assert Decimal(loan["amount"]) == PRINCIPAL
        assert loan["transfer_account_path"] == CHECKING
        assert loan["category_path"] == INTEREST_CATEGORY
        assert Decimal(loan["remainder_amount"]) == INTEREST
        assert Decimal(loan["balance"]) == PRINCIPAL  # principal only, never the full payment

        # No perspective: the historical shape (outgoing side, positive amount).
        flat = _register_row(client, merged_id)
        assert Decimal(flat["amount"]) == -CASH
        assert flat["account_path"] == CHECKING
        assert flat["transfer_account_path"] == LOAN
        assert Decimal(flat["remainder_amount"]) == INTEREST

        # The interest is ordinary spending in its category.
        interest = client.get("/transactions", params={"category": INTEREST_CATEGORY}).json()
        assert [i["id"] for i in interest["items"]] == [merged_id]


MULTI_POSTING_ROWS: dict[str, list[tuple[str, str]]] = {
    "two-real-two-categories": [
        (CHECKING, "-100.00"),
        (LOAN, "60.00"),
        (INTEREST_CATEGORY, "30.00"),
        ("expenses.fees", "10.00"),
    ],
    "paycheck": [
        (CHECKING, "2000.00"),
        (SAVINGS, "500.00"),
        ("income.salary", "-2500.00"),
    ],
    "three-real": [(CHECKING, "-100.00"), (SAVINGS, "60.00"), (LOAN, "40.00")],
    # The review's bad split, had it ever been written: not a transfer.
    "negative-remainder": [
        (CHECKING, "-50.00"),
        (SAVINGS, "650.12"),
        (INTEREST_CATEGORY, "-600.12"),
    ],
}


class TestClassification:
    """#245 multi-posting templates will put rows with two real accounts and
    several categories into the ledger. Only Purser's own transfer shapes are
    transfers; anything else is displayed and handled as what it is."""

    @pytest.mark.parametrize("shape", sorted(MULTI_POSTING_ROWS))
    def test_multi_posting_rows_are_not_transfers(
        self, seeded: None, client: TestClient, migrated_engine: Engine, shape: str
    ) -> None:
        tx_id = _insert(migrated_engine, MULTI_POSTING_ROWS[shape])
        assert _postings_sum(migrated_engine, tx_id) == 0
        for row in (
            client.get(f"/transactions/{tx_id}").json(),
            _register_row(client, tx_id, CHECKING),
            _register_row(client, tx_id),
        ):
            assert row["is_transfer"] is False
            assert row["transfer_account_path"] is None
            assert row["remainder_amount"] is None
        resp = client.post(f"/transactions/{tx_id}/unlink-transfer")
        assert resp.status_code == 422
        assert "only transfers" in resp.json()["detail"]


class TestMatcherSeesTheSplit:
    def test_expected_loan_payment_matches_from_the_cash_side(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        """A loan-payment schedule's instance is a two-leg expected transfer
        for the full cash amount (decision 56). It settles against the split
        through checking's leg, which is exactly the bank's cash."""
        expected_id = _insert(
            migrated_engine, [(CHECKING, str(CASH)), (LOAN, str(-CASH))], status="expected"
        )
        out_id, in_id = _pair(client, migrated_engine)
        merged_id = _confirm(client, out_id, in_id)["merged_transaction_id"]

        stats = client.post("/matching/run").json()
        assert stats["auto_matched"] == 1
        expected = client.get(f"/transactions/{expected_id}").json()
        assert expected["status"] == "void"
        assert expected["superseded_by"] == merged_id
        with migrated_engine.connect() as conn:
            variance = conn.execute(
                text("SELECT variance FROM matches WHERE expected_id = :e"), {"e": expected_id}
            ).scalar_one()
        assert Decimal(variance) == 0
