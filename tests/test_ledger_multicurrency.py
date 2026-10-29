"""Non-USD transactions through the service layer (#278).

An account's currency is set when it is created (``create_account(currency=)``,
``POST /accounts``/``POST /categories`` ``currency``) and every posting takes
its account's currency. The service has no FX: a transaction whose accounts
differ in currency is refused. A correction of a posted row (reversal +
replacement) must keep the original's currency on every leg.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from purser import ledger
from purser.core import ledger as core
from purser.db import get_sessionmaker
from purser.ledger import LedgerError
from purser.models import Account, ApiToken, Posting, Transaction, User

ON = date(2026, 7, 1)

_amounts = st.decimals(
    min_value=Decimal("-99999.99"),
    max_value=Decimal("99999.99"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
).filter(lambda d: d != 0)


def _commit_token(session: Session) -> ApiToken:
    user = User(email=f"multi_{uuid.uuid4().hex[:8]}@example.com")
    session.add(user)
    session.flush()
    token = ApiToken(
        user_id=user.id, name="multi", token_hash=uuid.uuid4().hex, scopes=["read", "write"]
    )
    session.add(token)
    session.flush()
    return token


def _account(session: Session, token: ApiToken, type_: str, currency: str) -> Account:
    label = f"{type_}_{currency.lower()}_{uuid.uuid4().hex[:6]}"
    root = "expenses" if type_ == "category_expense" else "assets"
    return ledger.create_account(
        session,
        name=label,
        type_=type_,
        path=f"{root}.{label}",
        currency=currency,
        actor_token_id=token.id,
    )


def _per_currency_sums(
    session: Session, tx_ids: list[uuid.UUID]
) -> dict[tuple[uuid.UUID, str], Decimal]:
    rows = session.execute(
        text(
            "SELECT transaction_id, currency, sum(amount) FROM postings"
            " WHERE transaction_id = ANY(:ids) GROUP BY transaction_id, currency"
        ),
        {"ids": tx_ids},
    ).all()
    return {(tx_id, currency): total for tx_id, currency, total in rows}


class TestCorrectionKeepsCurrency:
    @pytest.mark.parametrize("currency", ["EUR", "JPY", "CAD"])
    @settings(max_examples=10, deadline=None)
    @given(original_amount=_amounts, edited_amount=_amounts)
    def test_amount_edit_of_posted_row(
        self,
        migrated_engine: Engine,
        currency: str,
        original_amount: Decimal,
        edited_amount: Decimal,
    ) -> None:
        # No per-example reset (a TRUNCATE costs more than the example): every
        # name here is unique and every check is scoped to this chain's ids.
        with get_sessionmaker()() as session:
            token = _commit_token(session)
            account = _account(session, token, "checking", currency)
            category = _account(session, token, "category_expense", currency)
            original = ledger.create_flat_transaction(
                session,
                account_ref=str(account.id),
                category_ref=str(category.id),
                amount=original_amount,
                date=ON,
                requested_status=core.Status.POSTED,
                token=token,
            )
            session.commit()
            assert original.status == core.Status.POSTED

            replacement = ledger.update_transaction(
                session, tx_id=original.id, token=token, amount=edited_amount
            )
            session.commit()

            assert replacement.reverses_id is not None
            reversal = session.get(Transaction, replacement.reverses_id)
            assert reversal is not None
            assert reversal.reverses_id == original.id
            chain = [original.id, reversal.id, replacement.id]

            postings = session.execute(
                select(Posting).where(Posting.transaction_id.in_(chain))
            ).scalars()
            assert {p.currency.strip() for p in postings} == {currency}
            sums = _per_currency_sums(session, chain)
            assert set(sums) == {(tx_id, currency) for tx_id in chain}
            assert all(total == 0 for total in sums.values())

            real_leg = session.execute(
                select(Posting.amount).where(
                    Posting.transaction_id == replacement.id, Posting.account_id == account.id
                )
            ).scalar_one()
            assert real_leg == edited_amount


class TestMixedCurrencyRefused:
    def test_flat_between_currencies(self, clean_db: None, db_session: Session) -> None:
        token = _commit_token(db_session)
        account = _account(db_session, token, "checking", "EUR")
        category = _account(db_session, token, "category_expense", "USD")
        with pytest.raises(LedgerError, match="currency mismatch"):
            ledger.create_flat_transaction(
                db_session,
                account_ref=str(account.id),
                category_ref=str(category.id),
                amount=Decimal("-42.00"),
                date=ON,
                token=token,
            )

    def test_transfer_between_currencies(self, clean_db: None, db_session: Session) -> None:
        token = _commit_token(db_session)
        checking = _account(db_session, token, "checking", "EUR")
        savings = _account(db_session, token, "savings", "CAD")
        with pytest.raises(LedgerError, match="currency mismatch"):
            ledger.create_transfer(
                db_session,
                from_account_ref=str(checking.id),
                to_account_ref=str(savings.id),
                amount=Decimal("250.00"),
                date=ON,
                token=token,
            )


def _change_rows(session: Session, tx_id: str) -> int:
    """Mutation audit rows about this transaction (the request's own call row
    is kind='write' and is written whatever the outcome)."""
    return int(
        session.execute(
            text(
                "SELECT count(*) FROM audit_log WHERE kind = 'change'"
                " AND (entity_id = :id OR replacement_id = :id)"
            ),
            {"id": tx_id},
        ).scalar_one()
    )


@pytest.fixture
def euro_book(admin_client: TestClient) -> dict[str, str]:
    """A EUR checking account, two EUR categories and a USD one."""
    ids: dict[str, str] = {}
    resp = admin_client.post(
        "/accounts",
        json={
            "name": "Euro Checking",
            "type": "checking",
            "path": "assets.euro_checking",
            "currency": "EUR",
        },
    )
    assert resp.status_code == 201, resp.text
    ids["assets.euro_checking"] = resp.json()["id"]
    for name, path, currency in [
        ("Euro Dining", "expenses.euro_dining", "EUR"),
        ("Euro Groceries", "expenses.euro_groceries", "EUR"),
        ("Dollar Dining", "expenses.dollar_dining", "USD"),
    ]:
        resp = admin_client.post(
            "/categories",
            json={"name": name, "type": "category_expense", "path": path, "currency": currency},
        )
        assert resp.status_code == 201, resp.text
        ids[path] = resp.json()["id"]
    return ids


def _euro_row(client: TestClient, status: str) -> str:
    resp = client.post(
        "/transactions",
        json={
            "account": "assets.euro_checking",
            "category": "expenses.euro_dining",
            "amount": "-42.00",
            "date": ON.isoformat(),
            "payee": "Example Bistro",
            "status": status,
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


class TestRecategorizeKeepsCurrency:
    """#361: creating a flat row refused a category of another currency, but
    recategorizing re-pointed the category leg without asking, so a USD
    category ended up holding a EUR posting that every rollup summed as
    dollars. Every recategorize path now applies the create path's rule."""

    @pytest.mark.parametrize("status", ["pending", "posted"])
    def test_another_currency_is_refused_and_nothing_is_written(
        self,
        admin_client: TestClient,
        db_session: Session,
        euro_book: dict[str, str],
        status: str,
    ) -> None:
        tx_id = _euro_row(admin_client, status)
        changes = _change_rows(db_session, tx_id)

        resp = admin_client.patch(
            f"/transactions/{tx_id}", json={"category": "expenses.dollar_dining"}
        )
        assert resp.status_code == 422, resp.text
        assert "currency mismatch" in resp.json()["detail"]

        row = admin_client.get(f"/transactions/{tx_id}").json()
        assert (row["status"], row["category_path"]) == (status, "expenses.euro_dining")
        db_session.expire_all()
        legs = db_session.execute(
            select(Posting.account_id, Posting.currency).where(
                Posting.transaction_id == uuid.UUID(tx_id)
            )
        ).all()
        assert {(str(a), c.strip()) for a, c in legs} == {
            (euro_book["assets.euro_checking"], "EUR"),
            (euro_book["expenses.euro_dining"], "EUR"),
        }
        corrections = db_session.execute(
            select(Transaction.id).where(Transaction.reverses_id == uuid.UUID(tx_id))
        ).all()
        assert corrections == []
        assert _change_rows(db_session, tx_id) == changes

    @pytest.mark.parametrize("status", ["pending", "posted"])
    def test_the_same_currency_recategorizes_as_before(
        self,
        admin_client: TestClient,
        db_session: Session,
        euro_book: dict[str, str],
        status: str,
    ) -> None:
        tx_id = _euro_row(admin_client, status)

        resp = admin_client.patch(
            f"/transactions/{tx_id}", json={"category": "expenses.euro_groceries"}
        )
        assert resp.status_code == 200, resp.text
        head = resp.json()["id"]
        # A posted row is corrected by a reversal + replacement; pending edits in place.
        assert (head == tx_id) is (status == "pending")
        assert admin_client.get(f"/transactions/{head}").json()["category_path"] == (
            "expenses.euro_groceries"
        )
        currencies = db_session.execute(
            select(Posting.currency).where(Posting.transaction_id == uuid.UUID(head))
        ).scalars()
        assert {c.strip() for c in currencies} == {"EUR"}

    def test_a_transfer_converted_back_to_a_category_is_checked(
        self, admin_client: TestClient, db_session: Session, euro_book: dict[str, str]
    ) -> None:
        """A plain category on a transfer converts it back to a flat row
        (transfers.convert_to_category) — the third recategorize path."""
        resp = admin_client.post(
            "/accounts",
            json={
                "name": "Euro Savings",
                "type": "savings",
                "path": "assets.euro_savings",
                "currency": "EUR",
            },
        )
        assert resp.status_code == 201, resp.text
        resp = admin_client.post(
            "/transactions/transfer",
            json={
                "from_account": "assets.euro_checking",
                "to_account": "assets.euro_savings",
                "amount": "100.00",
                "date": ON.isoformat(),
            },
        )
        assert resp.status_code == 201, resp.text
        tx_id = resp.json()["id"]
        changes = _change_rows(db_session, tx_id)

        refused = admin_client.patch(
            f"/transactions/{tx_id}",
            json={"category": "expenses.dollar_dining", "account": "assets.euro_checking"},
        )
        assert refused.status_code == 422, refused.text
        assert "currency mismatch" in refused.json()["detail"]
        assert admin_client.get(f"/transactions/{tx_id}").json()["is_transfer"] is True
        assert _change_rows(db_session, tx_id) == changes

        converted = admin_client.patch(
            f"/transactions/{tx_id}",
            json={"category": "expenses.euro_dining", "account": "assets.euro_checking"},
        )
        assert converted.status_code == 200, converted.text
        row = admin_client.get(f"/transactions/{tx_id}").json()
        assert (row["is_transfer"], row["category_path"]) == (False, "expenses.euro_dining")

    @pytest.mark.parametrize("status", ["pending", "posted"])
    def test_machine_categorization_is_refused_before_writing(
        self, clean_db: None, db_session: Session, status: str
    ) -> None:
        """The categorize pass writes the pending leg itself and corrects a
        posted row through ledger.recategorize_posted; both refuse."""
        from purser import categorize

        token = _commit_token(db_session)
        account = _account(db_session, token, "checking", "EUR")
        euro = _account(db_session, token, "category_expense", "EUR")
        dollar = _account(db_session, token, "category_expense", "USD")
        tx = ledger.create_flat_transaction(
            db_session,
            account_ref=str(account.id),
            category_ref=str(euro.id),
            amount=Decimal("-42.00"),
            date=ON,
            requested_status=core.Status(status),
            token=token,
        )
        db_session.commit()

        assert categorize.fits_currency(db_session, tx, euro.id)
        assert not categorize.fits_currency(db_session, tx, dollar.id)
        with pytest.raises(LedgerError, match="currency mismatch"):
            categorize._apply_category(
                db_session,
                tx,
                dollar.id,
                provenance={"source": "rule"},
                actor_token_id=None,
                token=None,
            )
        assert {p.account_id for p in tx.postings} == {account.id, euro.id}
        assert not db_session.new and not db_session.dirty


class TestAutomationSkipsAnotherCurrency:
    """The automated passes ask before applying, so a rule whose category is
    in another currency leaves the row alone instead of failing the pass,
    and apply-history's dry run still equals its commit."""

    def test_categorize_pass_and_apply_history(
        self, admin_client: TestClient, euro_book: dict[str, str]
    ) -> None:
        # Both USD (the default); the rule's category is EUR.
        resp = admin_client.post(
            "/categories",
            json={
                "name": "Uncategorized",
                "type": "category_expense",
                "path": "expenses.uncategorized",
            },
        )
        assert resp.status_code == 201, resp.text
        resp = admin_client.post(
            "/accounts", json={"name": "Checking", "type": "checking", "path": "assets.checking"}
        )
        assert resp.status_code == 201, resp.text
        resp = admin_client.post(
            "/transactions",
            json={
                "account": "assets.checking",
                "category": "expenses.uncategorized",
                "amount": "-18.00",
                "date": ON.isoformat(),
                "payee": "Example Bistro",
            },
        )
        assert resp.status_code == 201, resp.text
        tx_id = resp.json()["id"]
        rule = admin_client.post(
            "/rules",
            json={
                "type": "categorize",
                "match_field": "payee",
                "match_mode": "contains",
                "match_text": "example bistro",
                "category_id": euro_book["expenses.euro_dining"],
                "activate": True,
            },
        )
        assert rule.status_code == 201, rule.text

        stats = admin_client.post("/categorize/run").json()
        assert (stats["rule"], stats["unresolved"]) == (0, 1)
        row = admin_client.get(f"/transactions/{tx_id}").json()
        assert row["category_path"] == "expenses.uncategorized"

        rule_id = rule.json()["id"]
        dry = admin_client.post(f"/rules/{rule_id}/apply-history", json={"dry_run": True})
        wet = admin_client.post(f"/rules/{rule_id}/apply-history", json={"dry_run": False})
        assert dry.status_code == wet.status_code == 200, wet.text
        for body in (dry.json(), wet.json()):
            assert (body["matched"], body["pending"], body["skipped_currency"]) == (0, 0, 1)
        row = admin_client.get(f"/transactions/{tx_id}").json()
        assert row["category_path"] == "expenses.uncategorized"
