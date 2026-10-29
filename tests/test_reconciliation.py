"""Account reconciliation (issue #268, DESIGN §3.7, decision 61).

Every seeded date is relative to today (UTC) and in the past, so nothing here
depends on the day of the month (#262).
"""

import os
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import purser.config
import purser.db
from purser import balances, crypto, ledger, matching, reconciliation, review
from purser.admin import account_delete
from purser.core import ledger as core
from purser.models import (
    Account,
    AccountReconciliation,
    ApiToken,
    AuditLog,
    Event,
    Match,
    Transaction,
    User,
)
from tests.conftest import TEST_ENCRYPTION_KEY

TODAY = datetime.now(UTC).date()
OPENED = TODAY - timedelta(days=20)  # opening rows
STATEMENT = TODAY - timedelta(days=10)  # the first statement date
LATER = TODAY - timedelta(days=5)


# --------------------------------------------------------------------------
# REST fixture
# --------------------------------------------------------------------------


@pytest.fixture
def seeded(admin_client: TestClient) -> dict[str, str]:
    """Admin session; checking holding 100.00 and a card owing 400.00, both
    from posted rows dated OPENED."""
    ids: dict[str, str] = {}
    for key, name, type_, path in [
        ("checking", "Checking", "checking", "assets.checking"),
        ("card", "Card", "credit_card", "liabilities.card"),
        ("loan", "Loan", "loan", "liabilities.loan"),
    ]:
        resp = admin_client.post("/accounts", json={"name": name, "type": type_, "path": path})
        assert resp.status_code == 201, resp.text
        ids[key] = resp.json()["id"]
    resp = admin_client.post(
        "/categories", json={"name": "Seed", "type": "category_expense", "path": "expenses.seed"}
    )
    assert resp.status_code == 201, resp.text
    ids["category"] = resp.json()["id"]
    _post(admin_client, "assets.checking", "100.00", OPENED)
    _post(admin_client, "liabilities.card", "-400.00", OPENED)
    return ids


def _post(
    client: TestClient, account: str, amount: str, on: date, *, status: str = "posted"
) -> str:
    resp = client.post(
        "/transactions",
        json={
            "account": account,
            "category": "expenses.seed",
            "amount": amount,
            "date": on.isoformat(),
            "payee": "Synthetic Payee",
            "status": status,
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _reconcile(client: TestClient, account_id: str, **body: Any) -> Any:
    payload = {"as_of": STATEMENT.isoformat(), **body}
    return client.post(f"/accounts/{account_id}/reconcile", json=payload)


def _count(engine: Engine, sql: str, **params: Any) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(sql), params).scalar_one())


# --------------------------------------------------------------------------
# REST: the happy path, retries, and what a person sees
# --------------------------------------------------------------------------


def test_reconcile_posts_one_adjustment_and_exact_retry_is_idempotent(
    seeded: dict[str, str], client: TestClient, migrated_engine: Engine
) -> None:
    checking = seeded["checking"]
    first = _reconcile(client, checking, statement_balance="125.00")
    assert first.status_code == 200, first.text
    result = first.json()
    assert result["created"] is True and result["dry_run"] is False
    assert result["basis"] == "posted"
    assert Decimal(result["statement_balance"]) == Decimal("125.00")
    assert Decimal(result["target_balance"]) == Decimal("125.00")
    assert Decimal(result["ledger_balance"]) == Decimal("100.00")
    assert Decimal(result["adjustment"]) == Decimal("25.00")
    assert result["category"] == reconciliation.CATEGORY_PATH
    assert result["reconciled_through"] == STATEMENT.isoformat()

    retry = _reconcile(client, checking, statement_balance="125.00")
    assert retry.status_code == 200, retry.text
    assert retry.json() == {**result, "created": False}

    account = client.get(f"/accounts/{checking}").json()
    assert account["reconciled_through"] == STATEMENT.isoformat()
    status = account["reconciliation"]
    assert status["id"] == result["reconciliation_id"]
    assert status["in_balance"] is True
    assert Decimal(status["difference"]) == 0
    assert Decimal(status["ledger_balance"]) == Decimal("125.00")
    balance = client.get(
        f"/accounts/{checking}/balance", params={"as_of": STATEMENT.isoformat()}
    ).json()
    assert Decimal(balance["balance"]) == Decimal("125.00")

    # The adjustment: one posted, system-origin row in the system category,
    # still in the review set so a person sees it.
    detail = client.get(f"/transactions/{result['adjustment_tx_id']}").json()
    assert detail["status"] == "posted"
    assert detail["origin"] == "system"
    assert detail["date"] == STATEMENT.isoformat()
    assert detail["payee"] == reconciliation.PAYEE
    assert detail["category_path"] == reconciliation.CATEGORY_PATH
    assert sum(Decimal(p["amount"]) for p in detail["postings"]) == 0
    assert detail["needs_review"] is True
    unreviewed = client.get("/transactions", params={"reviewed": "false", "limit": 200}).json()
    assert result["adjustment_tx_id"] in {row["id"] for row in unreviewed["items"]}

    assert (
        _count(
            migrated_engine,
            "SELECT COUNT(*) FROM account_reconciliations WHERE account_id = :a",
            a=checking,
        )
        == 1
    )
    assert (
        _count(migrated_engine, "SELECT COUNT(*) FROM audit_log WHERE action = 'account.reconcile'")
        == 1
    )
    assert (
        _count(migrated_engine, "SELECT COUNT(*) FROM events WHERE type = 'account.reconciled'")
        == 1
    )


def test_zero_difference_reconcile_is_recorded_without_an_adjustment(
    seeded: dict[str, str], client: TestClient, migrated_engine: Engine
) -> None:
    checking = seeded["checking"]
    transactions_before = _count(migrated_engine, "SELECT COUNT(*) FROM transactions")
    resp = _reconcile(client, checking, statement_balance="100.00")
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["created"] is True
    assert Decimal(result["adjustment"]) == 0
    assert result["adjustment_tx_id"] is None
    assert result["category"] is None
    assert _count(migrated_engine, "SELECT COUNT(*) FROM transactions") == transactions_before

    with migrated_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT adjustment, adjustment_tx_id FROM account_reconciliations"
                " WHERE account_id = :a"
            ),
            {"a": checking},
        ).one()
    assert row.adjustment == 0 and row.adjustment_tx_id is None
    account = client.get(f"/accounts/{checking}").json()
    assert account["reconciled_through"] == STATEMENT.isoformat()
    assert account["reconciliation"]["in_balance"] is True
    assert (
        _count(migrated_engine, "SELECT COUNT(*) FROM events WHERE type = 'account.reconciled'")
        == 1
    )
    # A retry of a clean reconcile is idempotent too.
    retry = _reconcile(client, checking, statement_balance="100.00")
    assert retry.status_code == 200 and retry.json() == {**result, "created": False}


def test_backdated_write_after_a_reconcile_is_reported_never_silent(
    seeded: dict[str, str], client: TestClient
) -> None:
    """Acceptance: reconcile, then a write dated before the reconciled date.
    The marker stays put, but every read reports the reconciliation stale with
    the exact difference, and a retry is a 409 rather than a quiet 200."""
    checking = seeded["checking"]
    assert _reconcile(client, checking, statement_balance="125.00").status_code == 200

    _post(client, "assets.checking", "10.00", STATEMENT - timedelta(days=1))

    account = client.get(f"/accounts/{checking}").json()
    assert account["reconciled_through"] == STATEMENT.isoformat()
    stale = account["reconciliation"]
    assert stale["in_balance"] is False
    assert Decimal(stale["difference"]) == Decimal("10.00")
    assert Decimal(stale["ledger_balance"]) == Decimal("135.00")
    assert Decimal(stale["target_balance"]) == Decimal("125.00")

    listed = client.get("/accounts").json()
    row = next(a for a in listed if a["id"] == checking)
    assert row["reconciliation"]["in_balance"] is False

    retry = _reconcile(client, checking, statement_balance="125.00")
    assert retry.status_code == 409, retry.text
    detail = retry.json()["detail"]
    assert detail["code"] == "reconciliation_moved"
    assert Decimal(detail["difference"]) == Decimal("10.00")
    assert Decimal(detail["ledger_balance"]) == Decimal("135.00")
    assert "10.0000" in detail["message"]
    # A dry run answers exactly what the commit would: the same 409.
    dry = _reconcile(client, checking, statement_balance="125.00", dry_run=True)
    assert dry.status_code == 409 and dry.json()["detail"] == detail

    # Reconciling a later date absorbs nothing silently: it is a new claim
    # (here already in balance), while the earlier one stays visibly stale.
    later = _reconcile(client, checking, as_of=LATER.isoformat(), statement_balance="135.00")
    assert later.status_code == 200, later.text
    assert Decimal(later.json()["adjustment"]) == 0
    history = client.get(f"/accounts/{checking}/reconciliations").json()
    assert [(h["as_of"], h["in_balance"]) for h in history] == [
        (LATER.isoformat(), True),
        (STATEMENT.isoformat(), False),
    ]
    assert client.get(f"/accounts/{checking}").json()["reconciled_through"] == LATER.isoformat()


def test_a_correction_before_the_reconciled_date_is_reported(
    seeded: dict[str, str], client: TestClient
) -> None:
    """Corrections are reversal+replacement pairs (§4): the live check reads
    chain heads, so an edited amount moves the reconciled balance exactly."""
    checking = seeded["checking"]
    row = _post(client, "assets.checking", "-20.00", STATEMENT - timedelta(days=2))
    assert _reconcile(client, checking, statement_balance="80.00").status_code == 200
    resp = client.patch(f"/transactions/{row}", json={"amount": "-23.50"})
    assert resp.status_code == 200, resp.text
    stale = client.get(f"/accounts/{checking}").json()["reconciliation"]
    assert stale["in_balance"] is False
    assert Decimal(stale["difference"]) == Decimal("-3.50")


def test_pending_basis_and_posted_basis_differ_by_the_pending_rows(
    seeded: dict[str, str], client: TestClient
) -> None:
    checking = seeded["checking"]
    pending = _post(
        client, "assets.checking", "-30.00", STATEMENT - timedelta(days=1), status="pending"
    )

    posted = _reconcile(client, checking, statement_balance="100.00", dry_run=True).json()
    assert Decimal(posted["ledger_balance"]) == Decimal("100.00")
    assert Decimal(posted["adjustment"]) == 0

    inclusive = _reconcile(
        client, checking, statement_balance="100.00", basis="pending_inclusive", dry_run=True
    ).json()
    assert Decimal(inclusive["ledger_balance"]) == Decimal("70.00")
    assert Decimal(inclusive["adjustment"]) == Decimal("30.00")

    recorded = _reconcile(
        client, checking, statement_balance="70.00", basis="pending_inclusive"
    ).json()
    assert recorded["created"] is True and Decimal(recorded["adjustment"]) == 0
    status = client.get(f"/accounts/{checking}").json()["reconciliation"]
    assert status["basis"] == "pending_inclusive" and status["in_balance"] is True

    # The pending row vanishes: the pending-inclusive balance on that date
    # moves back up by 30.00, and the read says so.
    assert client.post(f"/transactions/{pending}/void").status_code == 200
    stale = client.get(f"/accounts/{checking}").json()["reconciliation"]
    assert stale["in_balance"] is False
    assert Decimal(stale["difference"]) == Decimal("30.00")


def test_credit_card_statement_figure_is_converted_to_ledger_sign(
    seeded: dict[str, str], client: TestClient
) -> None:
    """A card statement prints the amount owed as a positive number; the
    ledger holds it negative. Passing the statement's 425.00 must not post a
    ~2x plug."""
    card = seeded["card"]
    resp = _reconcile(client, card, statement_balance="425.00")
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert Decimal(result["statement_balance"]) == Decimal("425.00")
    assert Decimal(result["target_balance"]) == Decimal("-425.00")
    assert Decimal(result["ledger_balance"]) == Decimal("-400.00")
    assert Decimal(result["adjustment"]) == Decimal("-25.00")
    balance = client.get(f"/accounts/{card}/balance", params={"as_of": STATEMENT.isoformat()})
    assert Decimal(balance.json()["balance"]) == Decimal("-425.00")
    status = client.get(f"/accounts/{card}").json()["reconciliation"]
    assert Decimal(status["statement_balance"]) == Decimal("425.00")
    assert status["in_balance"] is True


def test_dry_run_equals_commit_and_writes_nothing(
    seeded: dict[str, str], client: TestClient, migrated_engine: Engine
) -> None:
    checking = seeded["checking"]
    before = {
        table: _count(migrated_engine, f"SELECT COUNT(*) FROM {table}")
        for table in ("transactions", "account_reconciliations", "events")
    }
    changes_before = _count(migrated_engine, "SELECT COUNT(*) FROM audit_log WHERE kind = 'change'")

    dry = _reconcile(client, checking, statement_balance="112.34", dry_run=True)
    assert dry.status_code == 200, dry.text
    dry_result = dry.json()
    assert dry_result["dry_run"] is True and dry_result["created"] is False
    assert dry_result["reconciliation_id"] is None and dry_result["adjustment_tx_id"] is None
    for table, count in before.items():
        assert _count(migrated_engine, f"SELECT COUNT(*) FROM {table}") == count, table
    assert (
        _count(migrated_engine, "SELECT COUNT(*) FROM audit_log WHERE kind = 'change'")
        == changes_before
    )
    assert client.get(f"/accounts/{checking}").json()["reconciled_through"] is None

    committed = _reconcile(client, checking, statement_balance="112.34").json()
    computed = (
        "account_id",
        "as_of",
        "basis",
        "statement_balance",
        "target_balance",
        "ledger_balance",
        "adjustment",
        "category",
        "reconciled_through",
    )
    assert {k: dry_result[k] for k in computed} == {k: committed[k] for k in computed}
    assert Decimal(committed["adjustment"]) == Decimal("12.34")

    # A dry run of the recorded claim answers what its retry answers.
    replay = _reconcile(client, checking, statement_balance="112.34", dry_run=True).json()
    assert replay == {**committed, "created": False, "dry_run": True}


def test_future_dates_are_rejected_and_today_is_accepted(
    seeded: dict[str, str], client: TestClient
) -> None:
    checking = seeded["checking"]
    # Two days, not one: TODAY is fixed at import, so a run that crosses UTC
    # midnight must still be asking about a future date.
    future = TODAY + timedelta(days=2)
    resp = _reconcile(client, checking, as_of=future.isoformat(), statement_balance="100.00")
    assert resp.status_code == 422
    assert "future" in resp.json()["detail"]
    ok = _reconcile(client, checking, as_of=TODAY.isoformat(), statement_balance="100.00")
    assert ok.status_code == 200, ok.text


def test_conflicting_claims_are_409(seeded: dict[str, str], client: TestClient) -> None:
    checking = seeded["checking"]
    assert _reconcile(client, checking, statement_balance="125.00").status_code == 200

    other_target = _reconcile(client, checking, statement_balance="126.00")
    assert other_target.status_code == 409
    assert other_target.json()["detail"]["code"] == "reconciliation_conflict"

    other_basis = _reconcile(
        client, checking, statement_balance="125.00", basis="pending_inclusive"
    )
    assert other_basis.status_code == 409
    assert other_basis.json()["detail"]["code"] == "reconciliation_conflict"

    earlier = _reconcile(
        client,
        checking,
        as_of=(STATEMENT - timedelta(days=1)).isoformat(),
        statement_balance="100.00",
    )
    assert earlier.status_code == 409
    detail = earlier.json()["detail"]
    assert detail["code"] == "reconciliation_out_of_order"
    assert detail["reconciled_through"] == STATEMENT.isoformat()


def test_only_open_postings_balance_accounts_reconcile(
    seeded: dict[str, str], client: TestClient
) -> None:
    loan = _reconcile(client, seeded["loan"], statement_balance="1000.00")
    assert loan.status_code == 422 and "valuation" in loan.json()["detail"]
    category = _reconcile(client, seeded["category"], statement_balance="1.00")
    assert category.status_code == 422 and "category" in category.json()["detail"]
    missing = _reconcile(client, str(uuid.uuid4()), statement_balance="1.00")
    assert missing.status_code == 404
    bad_basis = _reconcile(client, seeded["checking"], statement_balance="1.00", basis="cleared")
    assert bad_basis.status_code == 422
    too_precise = _reconcile(client, seeded["checking"], statement_balance="1.00001")
    assert too_precise.status_code == 422

    resp = client.post(
        "/accounts", json={"name": "Wallet", "type": "cash", "path": "assets.wallet"}
    )
    assert resp.status_code == 201, resp.text
    wallet = resp.json()["id"]
    assert client.post(f"/accounts/{wallet}/close", json={}).status_code == 200
    closed = _reconcile(client, wallet, statement_balance="0.00")
    assert closed.status_code == 422 and "closed" in closed.json()["detail"]


def test_reconcile_requires_write_scope(seeded: dict[str, str], client: TestClient) -> None:
    tokens = {}
    for name, scopes in [("reader", ["read"]), ("agent", ["read"])]:
        resp = client.post("/tokens", json={"name": name, "scopes": scopes})
        tokens[name] = resp.json()["token"]
    for raw in tokens.values():
        resp = client.post(
            f"/accounts/{seeded['checking']}/reconcile",
            json={"as_of": STATEMENT.isoformat(), "statement_balance": "100.00", "dry_run": True},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 403
    # Reading the recorded reconciliations is read scope.
    assert _reconcile(client, seeded["checking"], statement_balance="100.00").status_code == 200
    listed = client.get(
        f"/accounts/{seeded['checking']}/reconciliations",
        headers={"Authorization": f"Bearer {tokens['reader']}"},
    )
    assert listed.status_code == 200 and len(listed.json()) == 1


def test_audit_rows_and_event_payload(
    seeded: dict[str, str], client: TestClient, migrated_engine: Engine
) -> None:
    result = _reconcile(client, seeded["card"], statement_balance="425.00").json()
    with Session(migrated_engine) as session:
        change = session.execute(
            select(AuditLog).where(
                AuditLog.action == "account.reconcile", AuditLog.kind == "change"
            )
        ).scalar_one()
        assert change.entity_id == uuid.UUID(seeded["card"])
        assert change.before_after["before"] == {"reconciled_through": None}
        after = change.before_after["after"]
        assert after["reconciled_through"] == STATEMENT.isoformat()
        assert after["reconciliation_id"] == result["reconciliation_id"]
        assert after["basis"] == "posted"
        assert Decimal(after["statement_balance"]) == Decimal("425.00")
        assert Decimal(after["adjustment"]) == Decimal("-25.00")
        assert after["adjustment_tx_id"] == result["adjustment_tx_id"]
        # The adjustment itself went through the create funnel's audit row.
        created = session.execute(
            select(AuditLog).where(
                AuditLog.action == "transaction.create",
                AuditLog.entity_id == uuid.UUID(result["adjustment_tx_id"]),
            )
        ).scalar_one()
        assert created.before_after["after"]["origin"] == "system"

        event = session.execute(
            select(Event).where(Event.type == "account.reconciled")
        ).scalar_one()
        assert event.entity_type == "account"
        assert event.entity_id == uuid.UUID(seeded["card"])
        assert event.payload == {
            "path": "liabilities.card",
            "currency": "USD",
            "reconciliation_id": result["reconciliation_id"],
            "as_of": STATEMENT.isoformat(),
            "basis": "posted",
            "statement_balance": "425.0000",
            "target_balance": "-425.0000",
            "ledger_balance": "-400.0000",
            "adjustment": "-25.0000",
            "adjustment_tx_id": result["adjustment_tx_id"],
        }


def test_adjustment_narrative_is_encrypted_at_rest(
    seeded: dict[str, str], client: TestClient, migrated_engine: Engine
) -> None:
    result = _reconcile(client, seeded["checking"], statement_balance="101.00").json()
    with migrated_engine.connect() as conn:
        encrypted = conn.execute(
            text("SELECT description_enc FROM transactions WHERE id = :t"),
            {"t": result["adjustment_tx_id"]},
        ).scalar_one()
    assert b"Reconciliation adjustment" not in bytes(encrypted)
    assert crypto.decrypt(bytes(encrypted)).startswith("Reconciliation adjustment")


# --------------------------------------------------------------------------
# Service level
# --------------------------------------------------------------------------


def _commit_token(session: Session, suffix: str) -> ApiToken:
    user = User(email=f"recon_{suffix}@example.com")
    session.add(user)
    session.flush()
    token = ApiToken(
        user_id=user.id, name=f"tok_{suffix}", token_hash=uuid.uuid4().hex, scopes=["write"]
    )
    session.add(token)
    session.flush()
    return token


def _setup(session: Session, *, type_: str = "checking") -> tuple[ApiToken, Account, Account]:
    suffix = uuid.uuid4().hex[:10]
    token = _commit_token(session, suffix)
    account = ledger.create_account(
        session,
        name=f"Acct {suffix}",
        type_=type_,
        path=f"assets.acct_{suffix}",
        actor_token_id=token.id,
    )
    category = ledger.ensure_category_path(
        session, f"expenses.seed_{suffix}", actor_token_id=token.id
    )
    return token, account, category


def _row(
    session: Session,
    token: ApiToken,
    account: Account,
    category: Account,
    amount: Decimal,
    on: date,
    status: core.Status = core.Status.POSTED,
) -> Transaction:
    return ledger.create_flat_transaction(
        session,
        account_ref=str(account.id),
        category_ref=str(category.id),
        amount=amount,
        date=on,
        payee_name="Synthetic Payee",
        requested_status=status,
        token=token,
    )


def test_statement_sign_conversion() -> None:
    to_ledger = reconciliation.to_ledger_sign
    assert to_ledger("checking", Decimal("10.00")) == Decimal("10.0000")
    assert to_ledger("checking", Decimal("-3.10")) == Decimal("-3.1000")
    assert to_ledger("credit_card", Decimal("10.00")) == Decimal("-10.0000")
    assert to_ledger("credit_card", Decimal("-10.00")) == Decimal("10.0000")  # credit balance
    assert to_ledger("loan", Decimal("1.00")) == Decimal("-1.0000")
    zero = to_ledger("credit_card", Decimal("0"))
    assert zero == 0 and str(zero) == "0.0000"  # never "-0.0000"
    for type_ in ("checking", "credit_card"):
        assert reconciliation.to_statement_sign(type_, to_ledger(type_, Decimal("7.25"))) == (
            Decimal("7.25")
        )


_amounts = st.decimals(
    min_value=Decimal("-500.00"),
    max_value=Decimal("500.00"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
).filter(lambda d: d != 0)


class TestReconcileProperties:
    @settings(
        max_examples=25,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        type_=st.sampled_from(["checking", "savings", "credit_card"]),
        rows=st.lists(
            st.tuples(
                st.integers(min_value=0, max_value=30),
                _amounts,
                st.sampled_from([core.Status.POSTED, core.Status.PENDING]),
            ),
            max_size=6,
        ),
        days_back=st.integers(min_value=0, max_value=30),
        statement=st.decimals(
            min_value=Decimal("-2000.00"),
            max_value=Decimal("2000.00"),
            allow_nan=False,
            allow_infinity=False,
            places=2,
        ),
        basis=st.sampled_from(reconciliation.BASES),
    )
    def test_dry_run_equals_commit_and_the_target_is_reached(
        self,
        db_session: Session,
        type_: str,
        rows: list[tuple[int, Decimal, core.Status]],
        days_back: int,
        statement: Decimal,
        basis: str,
    ) -> None:
        token, account, category = _setup(db_session, type_=type_)
        for back, amount, status in rows:
            _row(db_session, token, account, category, amount, TODAY - timedelta(days=back), status)
        as_of = TODAY - timedelta(days=days_back)
        kwargs: dict[str, Any] = {
            "account_id": account.id,
            "as_of": as_of,
            "statement_balance": statement,
            "basis": basis,
            "token": token,
            "today": TODAY,
        }

        dry = reconciliation.reconcile(db_session, dry_run=True, **kwargs)
        done = reconciliation.reconcile(db_session, **kwargs)
        computed = (
            "as_of",
            "basis",
            "statement_balance",
            "target_balance",
            "ledger_balance",
            "adjustment",
            "category_path",
            "reconciled_through",
        )
        assert {k: getattr(dry, k) for k in computed} == {k: getattr(done, k) for k in computed}
        assert dry.reconciliation_id is None and done.created

        target = reconciliation.to_ledger_sign(type_, statement)
        assert done.target_balance == target
        assert done.adjustment == target - done.ledger_balance
        # The target is exact in the asked basis, and the check agrees.
        assert reconciliation.ledger_balance(db_session, account.id, as_of, basis) == target
        row = db_session.get(AccountReconciliation, done.reconciliation_id)
        assert row is not None
        check = reconciliation.check(db_session, [row])[row.id]
        assert check.in_balance and check.statement_balance == core.quantize(statement)
        if done.adjustment == 0:
            assert done.adjustment_tx_id is None
        else:
            assert done.adjustment_tx_id is not None
            adjustment = ledger.get_transaction(db_session, done.adjustment_tx_id)
            assert adjustment.status == "posted" and adjustment.origin == "system"
            leg = next(p for p in adjustment.postings if p.account_id == account.id)
            assert Decimal(leg.amount) == done.adjustment
            assert sum(Decimal(p.amount) for p in adjustment.postings) == 0

        # The live pending-inclusive basis IS the stored running balance (§3.5).
        balances.recompute_running(db_session)
        assert reconciliation.ledger_balance(
            db_session, account.id, as_of, reconciliation.BASIS_PENDING_INCLUSIVE
        ) == balances.balance_asof(db_session, account.id, as_of)

        again = reconciliation.reconcile(db_session, **kwargs)
        assert not again.created and again.reconciliation_id == done.reconciliation_id


def test_ledger_balances_batch_matches_single_points(clean_db: None, db_session: Session) -> None:
    token, account, category = _setup(db_session)
    _, other, other_category = _setup(db_session)
    _row(db_session, token, account, category, Decimal("50.00"), TODAY - timedelta(days=9))
    _row(
        db_session,
        token,
        account,
        category,
        Decimal("-5.00"),
        TODAY - timedelta(days=4),
        core.Status.PENDING,
    )
    _row(db_session, token, other, other_category, Decimal("7.00"), TODAY - timedelta(days=2))
    points = [
        (account.id, TODAY - timedelta(days=10)),
        (account.id, TODAY - timedelta(days=9)),
        (account.id, TODAY),
        (other.id, TODAY),
    ]
    batch = reconciliation.ledger_balances(db_session, points)
    assert batch[(account.id, TODAY - timedelta(days=10))] == {
        "posted": Decimal("0"),
        "pending_inclusive": Decimal("0"),
    }
    assert batch[(account.id, TODAY - timedelta(days=9))]["posted"] == Decimal("50.00")
    assert batch[(account.id, TODAY)] == {
        "posted": Decimal("50.00"),
        "pending_inclusive": Decimal("45.00"),
    }
    assert batch[(other.id, TODAY)]["posted"] == Decimal("7.00")
    for account_id, as_of in points:
        for basis in reconciliation.BASES:
            single = reconciliation.ledger_balance(db_session, account_id, as_of, basis)
            assert single == batch[(account_id, as_of)][basis]


def test_an_adjustment_never_settles_an_expected_bill(clean_db: None, db_session: Session) -> None:
    """origin=system puts the adjustment in the review set, which would also
    put it in the matcher's pool (§5) — it is excluded explicitly there."""
    token, account, category = _setup(db_session)
    _row(db_session, token, account, category, Decimal("100.00"), OPENED)
    expected = _row(
        db_session, token, account, category, Decimal("-42.00"), STATEMENT, core.Status.EXPECTED
    )
    done = reconciliation.reconcile(
        db_session,
        account_id=account.id,
        as_of=STATEMENT,
        statement_balance=Decimal("58.00"),
        token=token,
        today=TODAY,
    )
    assert done.adjustment == Decimal("-42.00")  # same amount, same date as the bill
    matching.run_matching(db_session)
    assert (
        db_session.execute(select(Match).where(Match.posted_id == done.adjustment_tx_id)).first()
        is None
    )
    assert db_session.get(Transaction, expected.id).status == "expected"  # type: ignore[union-attr]
    assert not review.is_reviewed(db_session, done.adjustment_tx_id)  # type: ignore[arg-type]


def test_delete_account_removes_its_reconciliations(clean_db: None, db_session: Session) -> None:
    token, account, category = _setup(db_session)
    _row(db_session, token, account, category, Decimal("100.00"), OPENED)
    for as_of, figure in [(STATEMENT, "110.00"), (LATER, "110.00")]:
        reconciliation.reconcile(
            db_session,
            account_id=account.id,
            as_of=as_of,
            statement_balance=Decimal(figure),
            token=token,
            today=TODAY,
        )
    plan = account_delete.preflight(db_session, account)
    assert plan["reconciliations"] == 2
    summary = account_delete.execute_delete(db_session, account, actor_token_id=token.id)
    assert summary["reconciliations_deleted"] == 2
    remaining = db_session.execute(
        select(func.count())
        .select_from(AccountReconciliation)
        .where(AccountReconciliation.account_id == account.id)
    ).scalar_one()
    assert remaining == 0


def test_concurrent_reconciles_serialize_on_the_account(
    clean_db: None, migrated_engine: Engine
) -> None:
    """Two callers racing the same reconcile: the second waits on the
    account row lock, then sees the first's committed claim and answers
    created=false — one row, one adjustment."""
    sessionmaker = purser.db.get_sessionmaker()
    with sessionmaker() as setup:
        token, account, category = _setup(setup)
        _row(setup, token, account, category, Decimal("100.00"), OPENED)
        setup.commit()
        token_id, account_id = token.id, account.id

    request: dict[str, Any] = {
        "account_id": account_id,
        "as_of": STATEMENT,
        "statement_balance": Decimal("125.00"),
        "today": TODAY,
    }
    outcomes: dict[str, Any] = {}
    first = sessionmaker()
    try:
        outcomes["first"] = reconciliation.reconcile(
            first, token=first.get(ApiToken, token_id), **request
        )

        def contender() -> None:
            with sessionmaker() as second:
                outcomes["second"] = reconciliation.reconcile(
                    second, token=second.get(ApiToken, token_id), **request
                )
                second.commit()

        thread = threading.Thread(target=contender)
        thread.start()
        thread.join(timeout=1.0)
        assert thread.is_alive(), "the contender should be waiting on the account lock"
        first.commit()
    finally:
        first.close()
    thread.join(timeout=15)
    assert not thread.is_alive()

    assert outcomes["first"].created is True
    assert outcomes["second"].created is False
    assert outcomes["second"].reconciliation_id == outcomes["first"].reconciliation_id
    assert (
        _count(
            migrated_engine,
            "SELECT COUNT(*) FROM account_reconciliations WHERE account_id = :a",
            a=str(account_id),
        )
        == 1
    )
    assert (
        _count(
            migrated_engine,
            "SELECT COUNT(*) FROM transactions WHERE origin = 'system' AND date = :d",
            d=STATEMENT,
        )
        == 1
    )


# --------------------------------------------------------------------------
# Migration 0028 up/down (scratch database, never the suite's)
# --------------------------------------------------------------------------


@pytest.fixture
def scratch_db(database_url: str) -> Iterator[tuple[Config, sa.Engine]]:
    os.environ["PURSER_ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
    purser.config.get_settings.cache_clear()
    admin = sa.create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text("DROP DATABASE IF EXISTS purser_reconcile_mig WITH (FORCE)"))
        conn.execute(sa.text("CREATE DATABASE purser_reconcile_mig"))
    admin.dispose()
    scratch_url = database_url.rsplit("/", 1)[0] + "/purser_reconcile_mig"
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", scratch_url)
    engine = sa.create_engine(scratch_url)
    yield cfg, engine
    engine.dispose()


def _schema(engine: sa.Engine) -> tuple[set[str], set[str]]:
    with engine.connect() as conn:
        tables = set(
            conn.execute(
                sa.text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            ).scalars()
        )
        columns = set(
            conn.execute(
                sa.text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = 'accounts'"
                )
            ).scalars()
        )
    return tables, columns


def test_migration_0028_round_trips(scratch_db: tuple[Config, sa.Engine]) -> None:
    cfg, engine = scratch_db
    command.upgrade(cfg, "0028")
    tables, columns = _schema(engine)
    assert "account_reconciliations" in tables and "reconciled_through" in columns

    with engine.begin() as conn:
        account = conn.execute(
            sa.text(
                "INSERT INTO accounts (name, type, path) VALUES ('Checking', 'checking',"
                " 'assets.checking') RETURNING id"
            )
        ).scalar_one()
        conn.execute(
            sa.text(
                "INSERT INTO account_reconciliations (account_id, as_of, target_balance,"
                " adjustment) VALUES (:a, :d, 100, 0)"
            ),
            {"a": account, "d": STATEMENT},
        )
    # A nonzero adjustment must name its transaction (the shape CHECK) ...
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO account_reconciliations (account_id, as_of, target_balance,"
                " adjustment) VALUES (:a, :d, 100, 5)"
            ),
            {"a": account, "d": LATER},
        )
    # ... and one reconciliation per account and date, whatever the basis.
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO account_reconciliations (account_id, as_of, basis,"
                " target_balance, adjustment) VALUES (:a, :d, 'pending_inclusive', 100, 0)"
            ),
            {"a": account, "d": STATEMENT},
        )

    command.downgrade(cfg, "0027")
    tables, columns = _schema(engine)
    assert "account_reconciliations" not in tables and "reconciled_through" not in columns

    command.upgrade(cfg, "0028")
    tables, columns = _schema(engine)
    assert "account_reconciliations" in tables and "reconciled_through" in columns
