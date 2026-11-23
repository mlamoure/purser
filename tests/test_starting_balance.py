"""An account's starting balance from the bank (issue #585, DESIGN §7.5,
decision 123).

The property tests state the invariants; the rest drive the surfaces: the
mapping option, the REST routes, the MCP tool, the import hooks and the
refusals. All data is synthetic.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from purser import audit, crypto, db, ledger, reconciliation, starting_balance
from purser.core import ledger as core
from purser.feed import drift
from purser.models import (
    Account,
    AccountStartingBalance,
    AuditLog,
    Connection,
    Posting,
    RawFetch,
    Transaction,
)
from tests import test_feed, test_mcp
from tests.conftest import now, today
from tests.test_feed import FeedHarness, _old_tx, account_of, multi_payload_of, payload_of


def _earliest() -> date:
    return today() - timedelta(days=20)


harness = test_feed.harness
feed_env = test_feed.feed_env
live_server = test_mcp.live_server
ZERO = Decimal("0.0000")


# --------------------------------------------------------------------------
# Service level: one session, rolled back by the fixture
# --------------------------------------------------------------------------


def _fed(
    session: Session,
    *,
    bank_balance: str | None,
    rows: list[tuple[int, Decimal, core.Status]],
    type_: str = "checking",
    balance_age_days: int = 0,
) -> tuple[Account, Account]:
    """A mapped account whose bank reports ``bank_balance`` in the latest
    fetch, holding ``rows`` (days ago, amount, status). Returns it and a
    category for further rows."""
    suffix = uuid.uuid4().hex[:10]
    connection = Connection(provider="simplefin", status="active")
    session.add(connection)
    session.flush()
    root = "liabilities" if type_ == "credit_card" else "assets"
    account = ledger.create_account(
        session,
        name=f"Acct {suffix}",
        type_=type_,
        path=f"{root}.acct_{suffix}",
        connection_id=connection.id,
        external_id=f"EXT-{suffix}",
        actor_token_id=None,
    )
    category = ledger.ensure_category_path(session, f"expenses.seed_{suffix}", actor_token_id=None)
    for days_ago, amount, status in rows:
        _row(session, account, category, amount, today() - timedelta(days=days_ago), status)
    bank: dict[str, Any] = {"id": account.external_id, "name": "Synthetic", "currency": "USD"}
    if bank_balance is not None:
        bank["balance"] = bank_balance
        bank["balance-date"] = int((now() - timedelta(days=balance_age_days)).timestamp())
    session.add(
        RawFetch(
            connection_id=connection.id,
            content_hash=uuid.uuid4().hex,
            payload_enc=crypto.encrypt_json({"accounts": [bank]}),
        )
    )
    session.flush()
    return account, category


def _row(
    session: Session,
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
        origin=core.Origin.IMPORT,
        token=None,
    )


def _total(session: Session, account: Account, basis: str = drift.PENDING_INCLUSIVE) -> Decimal:
    posted, pending, _ = drift._ledger_sums(session, [account.id]).get(
        account.id, (Decimal(0), Decimal(0), None)
    )
    return core.quantize(posted if basis == drift.POSTED_ONLY else pending)


def _at(session: Session, account: Account, on: date) -> Decimal:
    return reconciliation.ledger_balance(
        session, account.id, on, reconciliation.BASIS_PENDING_INCLUSIVE
    )


def _observe(session: Session, account: Account, *, steady: str) -> None:
    """Three sync audit rows whose drift blocks show the account's ``steady``
    basis holding still while the other moves with a pending charge: the
    evidence the drift check learns a bank's basis from."""
    moving = [Decimal("-15.00"), Decimal("-40.00"), Decimal("0.00")]
    for pendings in moving:
        posted = Decimal("100.00")
        pending = posted + pendings
        reported = posted if steady == drift.POSTED_ONLY else Decimal("60.00")
        audit.record(
            session,
            actor_token_id=None,
            action="connection.sync",
            entity_type="connection",
            entity_id=account.connection_id,
            after={
                "balance_drift": {
                    str(account.id): {
                        "reported": str(reported),
                        "posted": str(posted),
                        "pending": str(pending),
                        "basis": steady,
                        "learned": steady,
                        "pinned": False,
                    }
                }
            },
        )
    session.flush()


_amounts = st.decimals(
    min_value=Decimal("-500.00"),
    max_value=Decimal("500.00"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
).filter(lambda d: d != 0)
_statuses = st.sampled_from([core.Status.POSTED, core.Status.PENDING])
_bank = st.decimals(
    min_value=Decimal("-50000.00"),
    max_value=Decimal("50000.00"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
)


class TestProperties:
    @settings(
        max_examples=25,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        type_=st.sampled_from(["checking", "savings", "cash", "credit_card"]),
        bank=_bank,
        rows=st.lists(st.tuples(st.integers(0, 80), _amounts, _statuses), max_size=6),
    )
    def test_the_account_then_agrees_with_the_bank_and_a_dry_run_says_the_same(
        self,
        db_session: Session,
        type_: str,
        bank: Decimal,
        rows: list[tuple[int, Decimal, core.Status]],
    ) -> None:
        account, _ = _fed(db_session, bank_balance=str(bank), rows=rows, type_=type_)
        before = _total(db_session, account)

        dry = starting_balance.set_from_bank(
            db_session, account_id=account.id, actor_token_id=None, dry_run=True
        )
        assert starting_balance.get(db_session, account.id) is None
        assert _total(db_session, account) == before
        done = starting_balance.set_from_bank(
            db_session, account_id=account.id, actor_token_id=None
        )

        assert (dry.amount, dry.basis, dry.entry_date) == (
            done.amount,
            done.basis,
            done.entry_date,
        )
        # The feed's own sign: no flip for a card, whatever the figure.
        assert done.amount == core.quantize(bank) - before
        assert _total(db_session, account) == core.quantize(bank)
        if done.amount == 0:
            assert done.transaction_id is None and done.entry_date is None
        else:
            tx = db_session.get(Transaction, done.transaction_id)
            assert tx is not None and tx.status == "posted" and tx.origin == "system"
            earliest = min((today() - timedelta(days=d) for d, _, _ in rows), default=today())
            assert tx.date == earliest - timedelta(days=1)
            # Zero-sum against the system account, which is not a category.
            assert sum(Decimal(p.amount) for p in tx.postings) == 0
            assert ledger.is_starting_balance(tx.postings)
        again = starting_balance.set_from_bank(
            db_session, account_id=account.id, actor_token_id=None
        )
        assert again.status == "unchanged" and again.amount == done.amount

    @settings(
        max_examples=25,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        bank=_bank,
        rows=st.lists(st.tuples(st.integers(0, 80), _amounts), min_size=1, max_size=5),
        older=st.lists(st.tuples(st.integers(1, 400), _amounts), min_size=1, max_size=6),
    )
    def test_older_history_never_moves_a_balance_on_or_after_the_horizon(
        self,
        db_session: Session,
        bank: Decimal,
        rows: list[tuple[int, Decimal]],
        older: list[tuple[int, Decimal]],
    ) -> None:
        account, category = _fed(
            db_session,
            bank_balance=str(bank),
            rows=[(d, a, core.Status.POSTED) for d, a in rows],
        )
        starting_balance.set_from_bank(db_session, account_id=account.id, actor_token_id=None)
        marker = starting_balance.get(db_session, account.id)
        assert marker is not None
        horizon = marker.horizon
        probes = [horizon, horizon + timedelta(days=3), today()]
        before = [_at(db_session, account, on) for on in probes]

        for days_before, amount in older:
            _row(db_session, account, category, amount, horizon - timedelta(days=days_before))
        outcome = starting_balance.reseat(db_session, account.id, skip_locked=False)
        assert outcome is not None

        assert [_at(db_session, account, on) for on in probes] == before
        assert _total(db_session, account) == core.quantize(bank)
        # It gave way by exactly the rows that arrived, and sits before them.
        first = core.quantize(bank) - sum((a for _, a in rows), Decimal(0))
        assert outcome.amount == core.quantize(first - sum((a for _, a in older), Decimal(0)))
        oldest = horizon - timedelta(days=max(d for d, _ in older))
        if outcome.transaction_id is not None:
            tx = db_session.get(Transaction, outcome.transaction_id)
            assert tx is not None and tx.date == oldest - timedelta(days=1)
        # A second pass with nothing new changes nothing.
        heads = _entry_rows(db_session, account)
        assert starting_balance.reseat(db_session, account.id, skip_locked=False) is not None
        assert _entry_rows(db_session, account) == heads


def _entry_rows(session: Session, account: Account) -> int:
    opening = ledger.opening_balances_account(session)
    mine = select(Posting.transaction_id).where(Posting.account_id == account.id)
    return len(
        session.execute(
            select(Posting.id).where(
                Posting.account_id == opening.id, Posting.transaction_id.in_(mine)
            )
        ).all()
    )


class TestBasis:
    def test_without_history_a_new_account_is_set_pending_inclusive(
        self, db_session: Session
    ) -> None:
        account, _ = _fed(
            db_session,
            bank_balance="1000.00",
            rows=[
                (3, Decimal("-40.00"), core.Status.POSTED),
                (1, Decimal("-15.00"), core.Status.PENDING),
            ],
        )
        outcome = starting_balance.set_from_bank(
            db_session, account_id=account.id, actor_token_id=None
        )
        assert outcome.basis == drift.PENDING_INCLUSIVE
        assert outcome.amount == Decimal("1055.0000")

    def test_it_follows_the_basis_the_drift_check_learns(self, db_session: Session) -> None:
        account, _ = _fed(
            db_session,
            bank_balance="1000.00",
            rows=[
                (3, Decimal("-40.00"), core.Status.POSTED),
                (1, Decimal("-15.00"), core.Status.PENDING),
            ],
        )
        starting_balance.set_from_bank(db_session, account_id=account.id, actor_token_id=None)
        _observe(db_session, account, steady=drift.POSTED_ONLY)

        outcome = starting_balance.reseat(db_session, account.id, skip_locked=False)

        # The bank's figure left the pending charge out, so the account held
        # 1040.00 before its first row, and its posted total is the bank's.
        assert outcome is not None and outcome.basis == drift.POSTED_ONLY
        assert outcome.amount == Decimal("1040.0000")
        assert _total(db_session, account, drift.POSTED_ONLY) == Decimal("1000.0000")
        # Both writes share one transaction's timestamp, so compare as a set.
        changes = db_session.execute(
            select(AuditLog).where(
                AuditLog.action == "account.starting_balance", AuditLog.entity_id == account.id
            )
        ).scalars()
        assert {row.before_after["after"]["reason"] for row in changes} == {
            starting_balance.SET,
            starting_balance.BASIS,
        }

    def test_one_observation_is_not_evidence(self, db_session: Session) -> None:
        account, _ = _fed(
            db_session,
            bank_balance="1000.00",
            rows=[
                (3, Decimal("-40.00"), core.Status.POSTED),
                (1, Decimal("-15.00"), core.Status.PENDING),
            ],
        )
        starting_balance.set_from_bank(db_session, account_id=account.id, actor_token_id=None)
        # One sync whose per-sync rule happened to say posted-only.
        audit.record(
            db_session,
            actor_token_id=None,
            action="connection.sync",
            entity_type="connection",
            entity_id=account.connection_id,
            after={
                "balance_drift": {
                    str(account.id): {
                        "reported": "1000.00",
                        "posted": "1015.00",
                        "pending": "1000.00",
                        "basis": drift.POSTED_ONLY,
                        "learned": drift.POSTED_ONLY,
                        "pinned": False,
                    }
                }
            },
        )
        db_session.flush()

        outcome = starting_balance.reseat(db_session, account.id, skip_locked=False)
        assert outcome is not None and outcome.status == "unchanged"
        assert outcome.basis == drift.PENDING_INCLUSIVE

    def test_a_pin_decides_at_once(self, db_session: Session) -> None:
        account, _ = _fed(
            db_session,
            bank_balance="1000.00",
            rows=[
                (3, Decimal("-40.00"), core.Status.POSTED),
                (1, Decimal("-15.00"), core.Status.PENDING),
            ],
        )
        account.drift_basis_override = drift.POSTED_ONLY
        db_session.flush()
        outcome = starting_balance.set_from_bank(
            db_session, account_id=account.id, actor_token_id=None
        )
        assert outcome.basis == drift.POSTED_ONLY and outcome.amount == Decimal("1040.0000")

    def test_a_reconciled_account_keeps_its_basis(self, db_session: Session) -> None:
        account, _ = _fed(
            db_session,
            bank_balance="1000.00",
            rows=[
                (3, Decimal("-40.00"), core.Status.POSTED),
                (1, Decimal("-15.00"), core.Status.PENDING),
            ],
        )
        starting_balance.set_from_bank(db_session, account_id=account.id, actor_token_id=None)
        account.reconciled_through = today()
        db_session.flush()
        _observe(db_session, account, steady=drift.POSTED_ONLY)

        outcome = starting_balance.reseat(db_session, account.id, skip_locked=False)

        assert outcome is not None and outcome.status == "unchanged"
        assert outcome.basis == drift.PENDING_INCLUSIVE and outcome.amount == Decimal("1055.0000")


class TestRefusals:
    def _refused(self, session: Session, account: Account) -> str:
        with pytest.raises(starting_balance.StartingBalanceError) as caught:
            starting_balance.set_from_bank(session, account_id=account.id, actor_token_id=None)
        assert starting_balance.get(session, account.id) is None
        return caught.value.code

    def test_a_bank_that_reported_no_balance(self, db_session: Session) -> None:
        account, _ = _fed(db_session, bank_balance=None, rows=[])
        assert self._refused(db_session, account) == "no_bank_balance"

    def test_a_stale_balance(self, db_session: Session) -> None:
        account, _ = _fed(db_session, bank_balance="10.00", rows=[], balance_age_days=45)
        assert self._refused(db_session, account) == "stale_balance"

    def test_an_account_whose_balance_is_not_its_postings(self, db_session: Session) -> None:
        account, _ = _fed(db_session, bank_balance="10.00", rows=[], type_="brokerage")
        assert self._refused(db_session, account) == "account_type"

    def test_an_account_no_bank_feeds(self, db_session: Session) -> None:
        account = ledger.create_account(
            db_session,
            name="By hand",
            type_="checking",
            path=f"assets.manual_{uuid.uuid4().hex[:8]}",
            actor_token_id=None,
        )
        assert self._refused(db_session, account) == "not_connected"

    def test_a_reconciled_account_that_differs_from_the_bank(self, db_session: Session) -> None:
        account, _ = _fed(
            db_session, bank_balance="900.00", rows=[(2, Decimal("100.00"), core.Status.POSTED)]
        )
        account.reconciled_through = today()
        db_session.flush()
        assert self._refused(db_session, account) == "reconciled"

    def test_a_reconciled_account_that_agrees_gets_one_with_nothing_posted(
        self, db_session: Session
    ) -> None:
        account, category = _fed(
            db_session, bank_balance="100.00", rows=[(2, Decimal("100.00"), core.Status.POSTED)]
        )
        account.reconciled_through = today()
        db_session.flush()

        outcome = starting_balance.set_from_bank(
            db_session, account_id=account.id, actor_token_id=None
        )
        assert outcome.amount == ZERO and outcome.transaction_id is None
        # It still gives way to older history, which keeps the reconciled
        # balance where the statement put it.
        _row(db_session, account, category, Decimal("-30.00"), today() - timedelta(days=200))
        after = starting_balance.reseat(db_session, account.id, skip_locked=False)
        assert after is not None and after.amount == Decimal("30.0000")
        assert _at(db_session, account, today()) == Decimal("100.0000")


class TestRemove:
    def test_remove_reverses_the_entry_and_stops_keeping_it(self, db_session: Session) -> None:
        account, category = _fed(
            db_session, bank_balance="500.00", rows=[(4, Decimal("-20.00"), core.Status.POSTED)]
        )
        starting_balance.set_from_bank(db_session, account_id=account.id, actor_token_id=None)
        assert _total(db_session, account) == Decimal("500.0000")

        dry = starting_balance.remove(
            db_session, account_id=account.id, actor_token_id=None, dry_run=True
        )
        assert dry.status == "removed" and _total(db_session, account) == Decimal("500.0000")
        outcome = starting_balance.remove(db_session, account_id=account.id, actor_token_id=None)

        assert outcome.status == "removed"
        assert _total(db_session, account) == Decimal("-20.0000")
        assert starting_balance.get(db_session, account.id) is None
        _row(db_session, account, category, Decimal("-5.00"), today() - timedelta(days=300))
        assert starting_balance.reseat(db_session, account.id) is None
        assert _total(db_session, account) == Decimal("-25.0000")
        again = starting_balance.remove(db_session, account_id=account.id, actor_token_id=None)
        assert again.status == "none"


# --------------------------------------------------------------------------
# REST, the mapping option, the import hooks, MCP
# --------------------------------------------------------------------------


def _rows() -> list[dict[str, Any]]:
    """Three posted rows summing to ROWS_SUM, dated back from the test's day
    (built per test, #533)."""
    return [
        _old_tx(20, "-40.00", "Hardware Store"),
        _old_tx(10, "900.00", "Acme Payroll"),
        _old_tx(2, "-12.34", "Coffee"),
    ]


ROWS_SUM = Decimal("847.66")
BANK = Decimal("5000.00")


def _mapped(harness: FeedHarness, *, starting: bool | None) -> tuple[str, dict[str, Any]]:
    """Connect, sync, and map "Demo Checking" (bank balance 5000.00, three
    rows). Returns the connection id and the map response."""
    harness.use_payload(payload_of(_rows(), balance=str(BANK)))
    connection_id = harness.connect()
    harness.sync(connection_id)
    body: dict[str, Any] = {
        "external_id": "Demo Checking",
        "create": {"name": "Checking", "type": "checking", "path": "assets.checking"},
    }
    if starting is not None:
        body["starting_balance"] = starting
    resp = harness.client.post(f"/connections/{connection_id}/map", json=body)
    assert resp.status_code == 200, resp.text
    return connection_id, resp.json()


def _balance(client: TestClient, account_id: str, **params: Any) -> dict[str, Any]:
    resp = client.get(f"/accounts/{account_id}/balance", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _register(client: TestClient) -> list[dict[str, Any]]:
    page = client.get("/transactions", params={"account": "assets.checking"}).json()
    return list(page["items"])


def _starting_rows(client: TestClient) -> list[dict[str, Any]]:
    return [row for row in _register(client) if row["starting_balance"]]


class TestMapping:
    def test_the_option_starts_the_account_from_the_bank(self, harness: FeedHarness) -> None:
        _, mapped = _mapped(harness, starting=True)

        assert Decimal(mapped["starting_balance"]) == BANK - ROWS_SUM
        balance = _balance(harness.client, mapped["account_id"])
        assert Decimal(balance["balance"]) == BANK
        assert Decimal(balance["bank_snapshot"]["comparison"]["difference"]) == 0
        [entry] = _starting_rows(harness.client)
        assert entry["payee"] == "Starting balance" and entry["origin"] == "system"
        assert entry["date"] == (_earliest() - timedelta(days=1)).isoformat()
        assert Decimal(entry["amount"]) == BANK - ROWS_SUM
        assert entry["category_path"] == ledger.OPENING_PATH
        # Every day from the first feed row on reads as the bank would have.
        first_day = _balance(harness.client, mapped["account_id"], as_of=_earliest().isoformat())
        assert Decimal(first_day["balance"]) == BANK - ROWS_SUM - Decimal("40.00")

    def test_it_is_neither_income_nor_spending(self, harness: FeedHarness) -> None:
        _mapped(harness, starting=True)
        month = (_earliest() - timedelta(days=1)).strftime("%Y-%m")
        summary = harness.client.get("/reports/spending", params={"period": month})
        if summary.status_code == 200:  # the route's shape is its own test's business
            assert "opening_balances" not in summary.text
        with db.get_sessionmaker()() as session:
            opening = ledger.opening_balances_account(session)
            assert opening.type == "equity" and ledger.is_system_account(opening)
            assert not ledger.is_category(opening)

    def test_without_the_option_nothing_is_posted(self, harness: FeedHarness) -> None:
        _, mapped = _mapped(harness, starting=None)

        assert "starting_balance" not in mapped
        assert _starting_rows(harness.client) == []
        assert Decimal(_balance(harness.client, mapped["account_id"])["balance"]) == ROWS_SUM

    def test_an_account_type_it_does_not_apply_to_maps_without_one(
        self, harness: FeedHarness
    ) -> None:
        harness.use_payload(payload_of([], account_id="Loan X", balance="-250000.00"))
        connection_id = harness.connect()
        harness.sync(connection_id)
        resp = harness.client.post(
            f"/connections/{connection_id}/map",
            json={
                "external_id": "Loan X",
                "create": {"name": "Mortgage", "type": "loan", "path": "liabilities.mortgage"},
                "starting_balance": True,
            },
        )
        assert resp.status_code == 200, resp.text
        assert "starting_balance" not in resp.json()

    def test_a_card_keeps_the_feeds_sign(self, harness: FeedHarness) -> None:
        charges = [_old_tx(9, "-60.00", "Bookshop"), _old_tx(4, "-15.00", "Cafe")]
        harness.use_payload(payload_of(charges, account_id="Card 1111", balance="-400.00"))
        connection_id = harness.connect()
        harness.sync(connection_id)
        resp = harness.client.post(
            f"/connections/{connection_id}/map",
            json={
                "external_id": "Card 1111",
                "create": {"name": "Card", "type": "credit_card", "path": "liabilities.card"},
                "starting_balance": True,
            },
        )
        assert resp.status_code == 200, resp.text
        # 325.00 was already owed before the first charge the feed delivered.
        assert Decimal(resp.json()["starting_balance"]) == Decimal("-325.00")
        balance = _balance(harness.client, resp.json()["account_id"])
        assert Decimal(balance["balance"]) == Decimal("-400.00")
        assert Decimal(balance["bank_snapshot"]["comparison"]["difference"]) == 0


class TestRoutes:
    def test_set_is_idempotent_and_remove_puts_the_ledger_back(self, harness: FeedHarness) -> None:
        _, mapped = _mapped(harness, starting=None)
        url = f"/accounts/{mapped['account_id']}/starting-balance"

        dry = harness.client.post(url, json={"dry_run": True})
        assert dry.status_code == 200, dry.text
        assert dry.json()["status"] == "set" and dry.json()["dry_run"] is True
        assert _starting_rows(harness.client) == []

        done = harness.client.post(url, json={}).json()
        assert done["status"] == "set"
        assert Decimal(done["amount"]) == BANK - ROWS_SUM
        assert done["entry_date"] == (_earliest() - timedelta(days=1)).isoformat()
        assert Decimal(done["bank_balance"]) == BANK
        assert done["basis"] == "pending_inclusive"
        again = harness.client.post(url, json={}).json()
        assert again["status"] == "unchanged" and again["transaction_id"] == done["transaction_id"]
        assert len(_starting_rows(harness.client)) == 1

        removed = harness.client.delete(url).json()
        assert removed["status"] == "removed"
        assert Decimal(_balance(harness.client, mapped["account_id"])["balance"]) == ROWS_SUM
        assert harness.client.delete(url).json()["status"] == "none"

    def test_it_needs_write_scope(self, harness: FeedHarness) -> None:
        _, mapped = _mapped(harness, starting=None)
        raw = harness.client.post("/tokens", json={"name": "reader", "scopes": ["read"]}).json()
        headers = {"Authorization": f"Bearer {raw['token']}"}
        url = f"/accounts/{mapped['account_id']}/starting-balance"
        # A bearer token on a client that also holds the admin session cookie:
        # the token decides.
        harness.client.cookies.clear()
        assert harness.client.post(url, json={}, headers=headers).status_code == 403
        assert harness.client.delete(url, headers=headers).status_code == 403

    def test_a_refusal_names_its_reason(self, harness: FeedHarness) -> None:
        created = harness.client.post(
            "/accounts", json={"name": "By hand", "type": "checking", "path": "assets.by_hand"}
        ).json()
        resp = harness.client.post(f"/accounts/{created['id']}/starting-balance", json={})
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "not_connected"

    def test_the_entry_is_not_edited_and_review_names_it(self, harness: FeedHarness) -> None:
        _mapped(harness, starting=True)
        [entry] = _starting_rows(harness.client)

        resp = harness.client.patch(
            f"/transactions/{entry['id']}", json={"category": "expenses.uncategorized"}
        )
        assert resp.status_code == 422
        assert "starting balance" in resp.text and "set_starting_balance" in resp.text
        resp = harness.client.patch(f"/transactions/{entry['id']}", json={"amount": "1.00"})
        assert resp.status_code == 422
        with db.get_sessionmaker()() as session:
            from purser import review

            tx = session.get(Transaction, uuid.UUID(entry["id"]))
            assert tx is not None
            payload = review._row_payload(session, tx, claimed=None)
        assert "starting_balance" in payload["flags"] and "locked" not in payload


class TestOlderHistory:
    QIF = (
        "!Type:Bank\n"
        "D2/1/2024\nT-40.00\nPHardware Store\nLHome\n^\n"
        "D2/3/2024\nT900.00\nPAcme Payroll\nLSalary\n^\n"
    )

    def test_a_file_import_lowers_it_by_what_it_brought(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        _, mapped = _mapped(harness, starting=True)
        first_day = _balance(harness.client, mapped["account_id"], as_of=_earliest().isoformat())

        resp = harness.client.post(
            "/import/qif",
            json={"content": self.QIF, "account_map": {"__default__": "assets.checking"}},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["imported"] == 2

        [entry] = _starting_rows(harness.client)
        assert Decimal(entry["amount"]) == BANK - ROWS_SUM - Decimal("860.00")
        assert entry["date"] == "2024-01-31"
        balance = _balance(harness.client, mapped["account_id"])
        assert Decimal(balance["balance"]) == BANK
        assert Decimal(balance["bank_snapshot"]["comparison"]["difference"]) == 0
        assert (
            _balance(harness.client, mapped["account_id"], as_of=_earliest().isoformat())["balance"]
            == first_day["balance"]
        )
        with db.get_sessionmaker()() as session:
            changes = session.execute(
                select(AuditLog)
                .where(AuditLog.action == "account.starting_balance")
                .order_by(AuditLog.at, AuditLog.id)
            ).scalars()
            reasons = [row.before_after["after"]["reason"] for row in changes]
            imported = session.execute(
                select(AuditLog).where(AuditLog.action == "qif.import")
            ).scalar_one()
            noted = imported.before_after["after"].get("starting_balances")
        with migrated_engine.connect() as conn:
            events = conn.execute(
                text("SELECT COUNT(*) FROM events WHERE type = 'account.starting_balance_set'")
            ).scalar_one()
        assert reasons == ["set", "older_history"]
        assert noted and noted[0]["account_id"] == mapped["account_id"]
        assert events == 2

    def test_a_backdated_row_is_taken_up_by_the_next_sync(self, harness: FeedHarness) -> None:
        connection_id, mapped = _mapped(harness, starting=True)
        with db.get_sessionmaker()() as session:
            account = session.get(Account, uuid.UUID(mapped["account_id"]))
            assert account is not None
            category = ledger.ensure_category_path(session, "expenses.cash", actor_token_id=None)
            _row(session, account, category, Decimal("-50.00"), _earliest() - timedelta(days=30))
            session.commit()
        assert Decimal(_balance(harness.client, mapped["account_id"])["balance"]) == BANK - 50

        harness.sync(connection_id)

        assert Decimal(_balance(harness.client, mapped["account_id"])["balance"]) == BANK
        [entry] = _starting_rows(harness.client)
        assert Decimal(entry["amount"]) == BANK - ROWS_SUM + Decimal("50.00")
        assert entry["date"] == (_earliest() - timedelta(days=31)).isoformat()

    def test_a_row_inside_the_feeds_window_is_left_to_the_drift_check(
        self, harness: FeedHarness
    ) -> None:
        connection_id, mapped = _mapped(harness, starting=True)
        with db.get_sessionmaker()() as session:
            account = session.get(Account, uuid.UUID(mapped["account_id"]))
            assert account is not None
            category = ledger.ensure_category_path(session, "expenses.cash", actor_token_id=None)
            _row(session, account, category, Decimal("-50.00"), _earliest() + timedelta(days=1))
            session.commit()

        harness.sync(connection_id)

        # Not older history: the starting balance stays, and the comparison
        # with the bank reports the difference as for any account.
        [entry] = _starting_rows(harness.client)
        assert Decimal(entry["amount"]) == BANK - ROWS_SUM
        balance = _balance(harness.client, mapped["account_id"])
        assert Decimal(balance["bank_snapshot"]["comparison"]["difference"]) == Decimal("50.00")


class TestSeveralAccounts:
    def test_one_connection_two_accounts_each_from_its_own_balance(
        self, harness: FeedHarness
    ) -> None:
        harness.use_payload(
            multi_payload_of(
                [
                    account_of(_rows(), account_id="Demo Checking", balance="5000.00"),
                    account_of(
                        [_old_tx(7, "25.00", "Interest")],
                        account_id="Demo Savings",
                        balance="80.00",
                    ),
                ]
            )
        )
        connection_id = harness.connect()
        harness.sync(connection_id)
        ids = {}
        for external_id, type_, path in [
            ("Demo Checking", "checking", "assets.checking"),
            ("Demo Savings", "savings", "assets.savings"),
        ]:
            resp = harness.client.post(
                f"/connections/{connection_id}/map",
                json={
                    "external_id": external_id,
                    "create": {"name": external_id, "type": type_, "path": path},
                    "starting_balance": True,
                },
            )
            assert resp.status_code == 200, resp.text
            ids[path] = resp.json()["account_id"]
        assert Decimal(_balance(harness.client, ids["assets.checking"])["balance"]) == BANK
        assert Decimal(_balance(harness.client, ids["assets.savings"])["balance"]) == Decimal("80")
        with db.get_sessionmaker()() as session:
            assert len(session.execute(select(AccountStartingBalance)).all()) == 2


class TestMcp:
    def test_the_tool_sets_repeats_and_removes(
        self, harness: FeedHarness, live_server: test_mcp.LiveServer
    ) -> None:
        _mapped(harness, starting=None)
        raw = harness.client.post(
            "/tokens", json={"name": "agent", "scopes": ["read", "write"]}
        ).json()["token"]
        args = {"account": "assets.checking"}

        dry, done, again, balance, removed = test_mcp._mcp_call(
            live_server.url,
            raw,
            [
                ("set_starting_balance", {**args, "dry_run": True}),
                ("set_starting_balance", args),
                ("set_starting_balance", args),
                ("account_balance", args),
                ("set_starting_balance", {**args, "remove": True}),
            ],
        )

        assert dry["status"] == "set" and dry["dry_run"] is True
        assert done["status"] == "set" and Decimal(done["amount"]) == BANK - ROWS_SUM
        assert done["account"] == "assets.checking"
        assert again["status"] == "unchanged"
        assert Decimal(balance["bank_snapshot"]["comparison"]["difference"]) == 0
        assert removed["status"] == "removed"

    def test_a_reader_is_refused(
        self, harness: FeedHarness, live_server: test_mcp.LiveServer
    ) -> None:
        _mapped(harness, starting=None)
        raw = harness.client.post("/tokens", json={"name": "reader", "scopes": ["read"]}).json()[
            "token"
        ]
        [denied] = test_mcp._mcp_call(
            live_server.url, raw, [("set_starting_balance", {"account": "assets.checking"})]
        )
        assert "write" in str(denied).lower()
        assert _starting_rows(harness.client) == []
