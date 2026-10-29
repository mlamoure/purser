"""Account lifecycle: rename, hide, close, reopen (issue #124, DESIGN §3.4).

Exercised end-to-end against real Postgres. The close invariant — an account
carries exactly zero balance into closure via a guided adjustment posted as an
ordinary transaction — gets a property-based test; read-only enforcement is
proven across every write path; the DB trigger is the structural backstop.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session

from purser import ledger, matching, networth, transfers
from purser.core import ledger as core
from purser.feed import service as feed_service
from purser.models import Account, ApiToken, AuditLog, Connection, User, Valuation

# --------------------------------------------------------------------------
# API-level fixture (mirrors tests/test_ledger_api.py)
# --------------------------------------------------------------------------


@pytest.fixture
def seeded(admin_client: TestClient) -> dict[str, str]:
    """Admin session + a small chart of accounts; returns name -> id."""

    def acct(name: str, type_: str, path: str, parent_id: str | None = None) -> dict:
        body: dict = {"name": name, "type": type_, "path": path}
        if parent_id:
            body["parent_id"] = parent_id
        endpoint = "/categories" if type_.startswith("category_") else "/accounts"
        resp = admin_client.post(endpoint, json=body)
        assert resp.status_code == 201, resp.text
        return resp.json()

    checking = acct("Checking", "checking", "assets.checking")
    savings = acct("Savings", "savings", "assets.savings")
    expenses = acct("Expenses", "category_expense", "expenses")
    dining = acct("Dining", "category_expense", "expenses.dining", expenses["id"])
    income = acct("Income", "category_income", "income")
    salary = acct("Salary", "category_income", "income.salary", income["id"])
    return {
        "checking": checking["id"],
        "savings": savings["id"],
        "checking_path": checking["path"],
        "dining": dining["path"],
        "salary": salary["path"],
    }


def _post_balance(client: TestClient, account_id: str, category: str, amount: str) -> str:
    """Create then post a transaction so the account carries a POSTED balance
    (posted, not pending — pending would block a close)."""
    resp = client.post(
        "/transactions",
        json={
            "account": account_id,
            "category": category,
            "amount": amount,
            "date": date.today().isoformat(),
            "payee": "Seed",
        },
    )
    assert resp.status_code == 201, resp.text
    tx_id = resp.json()["id"]
    assert client.post(f"/transactions/{tx_id}/post").status_code == 200
    return tx_id


# --------------------------------------------------------------------------
# db_session helpers (service-level tests)
# --------------------------------------------------------------------------


def _commit_token(session: Session, suffix: str) -> ApiToken:
    user = User(email=f"user_{suffix}@example.com")
    session.add(user)
    session.flush()
    token = ApiToken(
        user_id=user.id, name=f"tok_{suffix}", token_hash=uuid.uuid4().hex, scopes=["write"]
    )
    session.add(token)
    session.flush()
    return token


# --------------------------------------------------------------------------
# Property: closing zeroes any balance via the guided adjustment
# --------------------------------------------------------------------------

_balances_2dp = st.decimals(
    min_value=Decimal("-9999.99"),
    max_value=Decimal("9999.99"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
)


class TestCloseZeroesBalance:
    @settings(
        max_examples=15,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(balance=_balances_2dp)
    def test_close_drives_balance_to_exactly_zero(
        self, db_session: Session, balance: Decimal
    ) -> None:
        suffix = uuid.uuid4().hex[:10]
        token = _commit_token(db_session, suffix)
        account = ledger.create_account(
            db_session,
            name=f"Acct {suffix}",
            type_="checking",
            path=f"assets.acct_{suffix}",
            actor_token_id=token.id,
        )
        seed = ledger.ensure_category_path(db_session, "income.seed", actor_token_id=token.id)
        if balance != 0:
            ledger.create_flat_transaction(
                db_session,
                account_ref=str(account.id),
                category_ref=str(seed.id),
                amount=balance,
                date=date.today(),
                requested_status=core.Status.POSTED,
                token=token,
            )
        assert ledger.account_balances(db_session, [account.id]).get(
            account.id, Decimal(0)
        ) == core.quantize(balance)

        result = ledger.close_account(
            db_session,
            account_id=account.id,
            adjustment_category="expenses.adjustments",
            token=token,
        )
        final = ledger.account_balances(db_session, [account.id]).get(account.id, Decimal(0))
        assert final == Decimal("0")
        if balance != 0:
            assert result["adjustment_tx_id"] is not None
            adj = ledger.get_transaction(db_session, result["adjustment_tx_id"])
            real_leg = next(p for p in adj.postings if not p.account.is_virtual)
            assert Decimal(real_leg.amount) == core.quantize(-balance)
            assert adj.status == core.Status.POSTED.value
        else:
            assert result["adjustment_tx_id"] is None


# --------------------------------------------------------------------------
# Read-only enforcement: no new posting on a closed account
# --------------------------------------------------------------------------


class TestClosedIsReadOnly:
    def _closed_account(self, client: TestClient, seeded: dict[str, str]) -> str:
        """Give checking a posted balance, then close it (with adjustment)."""
        _post_balance(client, seeded["checking"], seeded["dining"], "-25.00")
        resp = client.post(
            f"/accounts/{seeded['checking']}/close",
            json={"adjustment_category": "expenses.adjustments"},
        )
        assert resp.status_code == 200, resp.text
        return seeded["checking"]

    def test_flat_create_blocked(self, seeded: dict[str, str], client: TestClient) -> None:
        acct = self._closed_account(client, seeded)
        resp = client.post(
            "/transactions",
            json={
                "account": acct,
                "category": seeded["dining"],
                "amount": "-5.00",
                "date": date.today().isoformat(),
            },
        )
        assert resp.status_code == 422
        assert "closed" in resp.json()["detail"]

    def test_transfer_create_blocked(self, seeded: dict[str, str], client: TestClient) -> None:
        acct = self._closed_account(client, seeded)
        resp = client.post(
            "/transactions/transfer",
            json={
                "from_account": acct,
                "to_account": seeded["savings"],
                "amount": "10.00",
                "date": date.today().isoformat(),
            },
        )
        assert resp.status_code == 422
        assert "closed" in resp.json()["detail"]

    def test_edit_of_closed_rows_blocked(self, seeded: dict[str, str], client: TestClient) -> None:
        """A row on the account can't be edited once the account is closed."""
        tx_id = _post_balance(client, seeded["checking"], seeded["dining"], "-25.00")
        assert (
            client.post(
                f"/accounts/{seeded['checking']}/close",
                json={"adjustment_category": "expenses.adjustments"},
            ).status_code
            == 200
        )
        resp = client.patch(f"/transactions/{tx_id}", json={"amount": "-30.00"})
        assert resp.status_code == 422
        assert "closed" in resp.json()["detail"]

    def test_status_transition_blocked(self, db_session: Session) -> None:
        """transition_status refuses before anything else once the account is
        closed (the closed guard runs ahead of the posted-void check)."""
        suffix = uuid.uuid4().hex[:10]
        token = _commit_token(db_session, suffix)
        account = ledger.create_account(
            db_session,
            name="Chk",
            type_="checking",
            path=f"assets.tr_{suffix}",
            actor_token_id=token.id,
        )
        seed = ledger.ensure_category_path(db_session, "income.seed3", actor_token_id=token.id)
        tx = ledger.create_flat_transaction(
            db_session,
            account_ref=str(account.id),
            category_ref=str(seed.id),
            amount=Decimal("-10.00"),
            date=date.today(),
            requested_status=core.Status.POSTED,
            token=token,
        )
        ledger.close_account(
            db_session,
            account_id=account.id,
            adjustment_category="expenses.adjustments",
            token=token,
        )
        with pytest.raises(ledger.LedgerError, match="closed"):
            ledger.transition_status(
                db_session, tx_id=tx.id, new_status=core.Status.VOID, token=token
            )

    def test_raw_insert_trigger_backstop(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        """Even raw SQL can't insert a posting onto a closed account."""
        acct = self._closed_account(client, seeded)  # committed by the API client
        tx_id = db_session.execute(
            text(
                "INSERT INTO transactions (date, status, origin) "
                "VALUES (:d, 'pending', 'system') RETURNING id"
            ),
            {"d": date.today()},
        ).scalar_one()
        with pytest.raises((IntegrityError, ProgrammingError)) as exc:
            db_session.execute(
                text(
                    "INSERT INTO postings (transaction_id, account_id, amount) "
                    "VALUES (:tx, :acct, 1)"
                ),
                {"tx": tx_id, "acct": acct},
            )
            db_session.flush()
        assert "closed" in str(exc.value).lower()
        db_session.rollback()


# --------------------------------------------------------------------------
# Close precondition matrix
# --------------------------------------------------------------------------


class TestClosePreconditions:
    def test_nonzero_balance_without_category_422(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        _post_balance(client, seeded["checking"], seeded["dining"], "-40.00")
        resp = client.post(f"/accounts/{seeded['checking']}/close", json={})
        assert resp.status_code == 422
        assert "-40" in resp.json()["detail"]

    def test_live_pending_blocks_close(self, seeded: dict[str, str], client: TestClient) -> None:
        # A pending (unposted) row on the account.
        assert (
            client.post(
                "/transactions",
                json={
                    "account": seeded["checking"],
                    "category": seeded["dining"],
                    "amount": "-9.00",
                    "date": date.today().isoformat(),
                },
            ).status_code
            == 201
        )
        resp = client.post(
            f"/accounts/{seeded['checking']}/close",
            json={"adjustment_category": "expenses.adjustments"},
        )
        assert resp.status_code == 422
        assert "pending" in resp.json()["detail"]

    def test_active_schedule_blocks_close_and_names_it(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        sched = client.post(
            "/schedules",
            json={
                "name": "Rent",
                "account": seeded["checking"],
                "rrule": "FREQ=MONTHLY;BYMONTHDAY=1",
                "amount_type": "fixed",
                "amount": "-1000.00",
                "category_path": seeded["dining"],
            },
        )
        assert sched.status_code == 201, sched.text
        resp = client.post(
            f"/accounts/{seeded['checking']}/close",
            json={"adjustment_category": "expenses.adjustments"},
        )
        assert resp.status_code == 422
        assert "Rent" in resp.json()["detail"]

    def test_expected_instances_voided_on_close(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        # An expected row on the account (no active schedule) gets voided.
        exp = client.post(
            "/transactions",
            json={
                "account": seeded["checking"],
                "category": seeded["dining"],
                "amount": "-15.00",
                "date": (date.today() + timedelta(days=5)).isoformat(),
                "status": "expected",
            },
        )
        assert exp.status_code == 201, exp.text
        exp_id = exp.json()["id"]
        assert (
            client.post(
                f"/accounts/{seeded['checking']}/close",
                json={"adjustment_category": "expenses.adjustments"},
            ).status_code
            == 200
        )
        detail = client.get(f"/transactions/{exp_id}").json()
        assert detail["status"] == "void"


# --------------------------------------------------------------------------
# SimpleFIN unmap on close + map refusal (service-level, db_session)
# --------------------------------------------------------------------------


class TestCloseUnmapsFeed:
    def test_close_unmaps_and_audits(self, db_session: Session) -> None:
        suffix = uuid.uuid4().hex[:10]
        token = _commit_token(db_session, suffix)
        conn = Connection()
        db_session.add(conn)
        db_session.flush()
        account = ledger.create_account(
            db_session,
            name="Mapped",
            type_="checking",
            path=f"assets.mapped_{suffix}",
            actor_token_id=token.id,
        )
        account.connection_id = conn.id
        account.external_id = "ext-123"
        db_session.flush()

        ledger.close_account(
            db_session, account_id=account.id, adjustment_category=None, token=token
        )
        assert account.connection_id is None
        assert account.external_id is None
        unmap = db_session.execute(
            select(AuditLog).where(
                AuditLog.action == "account.unmap", AuditLog.entity_id == account.id
            )
        ).scalar_one()
        assert unmap.before_after["after"]["reason"] == "closed"

        # Discovery would now see it as unmapped: no account holds that pair.
        holder = db_session.execute(
            select(Account).where(
                Account.connection_id == conn.id, Account.external_id == "ext-123"
            )
        ).scalar_one_or_none()
        assert holder is None

    def test_map_onto_closed_refused(self, db_session: Session) -> None:
        suffix = uuid.uuid4().hex[:10]
        token = _commit_token(db_session, suffix)
        conn = Connection()
        db_session.add(conn)
        db_session.flush()
        account = ledger.create_account(
            db_session,
            name="Closed",
            type_="checking",
            path=f"assets.closed_{suffix}",
            actor_token_id=token.id,
        )
        ledger.close_account(
            db_session, account_id=account.id, adjustment_category=None, token=token
        )
        with pytest.raises(feed_service.FeedError, match="closed"):
            feed_service.map_account(
                db_session,
                connection_id=conn.id,
                external_id="ext-9",
                account_id=account.id,
                actor_token_id=token.id,
            )

    def test_reopen_then_remap_works(self, db_session: Session) -> None:
        suffix = uuid.uuid4().hex[:10]
        token = _commit_token(db_session, suffix)
        conn = Connection()
        db_session.add(conn)
        db_session.flush()
        account = ledger.create_account(
            db_session,
            name="Reopenable",
            type_="checking",
            path=f"assets.reopen_{suffix}",
            actor_token_id=token.id,
        )
        ledger.close_account(
            db_session, account_id=account.id, adjustment_category=None, token=token
        )
        ledger.reopen_account(db_session, account_id=account.id, token=token)
        assert account.closed_at is None
        mapped = feed_service.map_account(
            db_session,
            connection_id=conn.id,
            external_id="ext-remap",
            account_id=account.id,
            actor_token_id=token.id,
        )
        assert mapped.external_id == "ext-remap"


# --------------------------------------------------------------------------
# Hide (list-endpoint-only) + rename
# --------------------------------------------------------------------------


class TestHide:
    def test_hide_filters_only_default_listing(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        assert (
            client.patch(f"/accounts/{seeded['checking']}", json={"hidden": True}).status_code
            == 200
        )

        default = client.get("/accounts").json()
        assert seeded["checking"] not in [a["id"] for a in default]
        with_hidden = client.get("/accounts", params={"include_hidden": True}).json()
        assert seeded["checking"] in [a["id"] for a in with_hidden]
        # Single-account GET still works.
        assert client.get(f"/accounts/{seeded['checking']}").status_code == 200

    def test_hide_does_not_change_net_worth(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        _post_balance(client, seeded["checking"], seeded["salary"], "500.00")
        before = client.get("/networth").json()["net_worth"]
        assert (
            client.patch(f"/accounts/{seeded['checking']}", json={"hidden": True}).status_code
            == 200
        )
        after = client.get("/networth").json()["net_worth"]
        assert Decimal(before) == Decimal(after)

    def test_hide_virtual_422(self, seeded: dict[str, str], client: TestClient) -> None:
        cat = client.get("/categories").json()
        dining_id = next(c["id"] for c in cat if c["path"] == seeded["dining"])
        resp = client.patch(f"/accounts/{dining_id}", json={"hidden": True})
        assert resp.status_code == 422
        assert "PATCH /categories" in resp.json()["detail"]


class TestRename:
    def test_rename_and_describe_keeps_path(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        resp = client.patch(
            f"/accounts/{seeded['checking']}",
            json={"name": "Everyday Checking", "description": "primary"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["name"] == "Everyday Checking"
        assert body["path"] == seeded["checking_path"]  # path immutable
        audit_row = db_session.execute(
            select(AuditLog).where(
                AuditLog.action == "account.update",
                AuditLog.entity_id == uuid.UUID(seeded["checking"]),
            )
        ).scalar_one()
        assert audit_row.before_after["after"]["name"] == "Everyday Checking"

    def test_rename_virtual_422(self, seeded: dict[str, str], client: TestClient) -> None:
        cat = client.get("/categories").json()
        dining_id = next(c["id"] for c in cat if c["path"] == seeded["dining"])
        resp = client.patch(f"/accounts/{dining_id}", json={"name": "X"})
        assert resp.status_code == 422

    def test_rename_requires_write_scope(self, seeded: dict[str, str], client: TestClient) -> None:
        reader = client.post("/tokens", json={"name": "agent", "scopes": ["read"]})
        headers = {"Authorization": f"Bearer {reader.json()['token']}"}
        resp = client.patch(f"/accounts/{seeded['checking']}", json={"name": "X"}, headers=headers)
        assert resp.status_code == 403


class TestFeedStart:
    """feed_start set/clear via PATCH /accounts (#154, decision 40): explicit
    null clears, absent leaves unchanged, and the field is admin-gated while
    the rest of the update surface stays commit-scope."""

    def test_set_and_clear_feed_start(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        resp = client.patch(f"/accounts/{seeded['checking']}", json={"feed_start": "2026-04-15"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["feed_start"] == "2026-04-15"
        audit_row = db_session.execute(
            select(AuditLog).where(
                AuditLog.action == "account.update",
                AuditLog.entity_id == uuid.UUID(seeded["checking"]),
            )
        ).scalar_one()
        assert audit_row.before_after["before"]["feed_start"] is None
        assert audit_row.before_after["after"]["feed_start"] == "2026-04-15"

        # Absent field leaves it unchanged...
        resp = client.patch(f"/accounts/{seeded['checking']}", json={"name": "Everyday"})
        assert resp.status_code == 200
        assert resp.json()["feed_start"] == "2026-04-15"
        # ...explicit null clears it.
        resp = client.patch(f"/accounts/{seeded['checking']}", json={"feed_start": None})
        assert resp.status_code == 200
        assert resp.json()["feed_start"] is None

    def test_feed_start_requires_admin_scope(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        commit = client.post("/tokens", json={"name": "agent", "scopes": ["read", "write"]})
        headers = {"Authorization": f"Bearer {commit.json()['token']}"}
        # feed_start is feed configuration — commit scope is not enough...
        resp = client.patch(
            f"/accounts/{seeded['checking']}", json={"feed_start": "2026-04-15"}, headers=headers
        )
        assert resp.status_code == 403
        assert "admin" in resp.json()["detail"]
        # ...even as an explicit clear...
        resp = client.patch(
            f"/accounts/{seeded['checking']}", json={"feed_start": None}, headers=headers
        )
        assert resp.status_code == 403
        # ...while the same token still renames (commit surface unchanged).
        resp = client.patch(
            f"/accounts/{seeded['checking']}", json={"name": "Still Mine"}, headers=headers
        )
        assert resp.status_code == 200, resp.text

    def test_feed_start_virtual_422(self, seeded: dict[str, str], client: TestClient) -> None:
        cat = client.get("/categories").json()
        dining_id = next(c["id"] for c in cat if c["path"] == seeded["dining"])
        resp = client.patch(f"/accounts/{dining_id}", json={"feed_start": "2026-04-15"})
        assert resp.status_code == 422
        assert "PATCH /categories" in resp.json()["detail"]


# --------------------------------------------------------------------------
# Net worth valuation/holdings cutoff at the close date
# --------------------------------------------------------------------------


class TestNetWorthCutoff:
    def test_closed_property_drops_out_after_close_date(self, db_session: Session) -> None:
        suffix = uuid.uuid4().hex[:10]
        token = _commit_token(db_session, suffix)
        account = ledger.create_account(
            db_session,
            name="House",
            type_="property",
            path=f"assets.house_{suffix}",
            actor_token_id=token.id,
        )
        # The cutoff compares `as_of` against `closed_at.date()`, and
        # `close_account` stamps `closed_at` in UTC. Driving the assertions off
        # a LOCAL `date.today()` fails deterministically on an ET host between
        # 20:00 and 24:00, when the local date lags UTC by one day and the
        # close date is therefore "tomorrow" (issue #140). Stay on the clock
        # the code under test uses.
        today = datetime.now(UTC).date()
        as_of_val = today - timedelta(days=30)
        db_session.add(
            Valuation(
                account_id=account.id, as_of=as_of_val, value=Decimal("500000"), source="manual"
            )
        )
        db_session.flush()

        # Before close: valuation counts.
        before = networth.net_worth_at(db_session, today - timedelta(days=1))
        assert account.id in {b["account_id"] for b in before["breakdown"]}

        ledger.close_account(
            db_session, account_id=account.id, adjustment_category=None, token=token
        )
        # Read the stamp back instead of recomputing "today": it is the exact
        # value the cutoff compares against, so the assertions hold even if the
        # run straddles a UTC midnight.
        assert account.closed_at is not None
        close_date = account.closed_at.date()
        # From the close date onward: the property drops out (postings are 0).
        after = networth.net_worth_at(db_session, close_date)
        assert account.id not in {b["account_id"] for b in after["breakdown"]}
        # History before the close date is untouched.
        historical = networth.net_worth_at(db_session, close_date - timedelta(days=1))
        assert account.id in {b["account_id"] for b in historical["breakdown"]}


# --------------------------------------------------------------------------
# System pools skip closed accounts (matching smoke)
# --------------------------------------------------------------------------


class TestPoolHygiene:
    def test_closed_one_sider_leaves_the_transfer_pool(self, db_session: Session) -> None:
        suffix = uuid.uuid4().hex[:10]
        token = _commit_token(db_session, suffix)
        checking = ledger.create_account(
            db_session,
            name="Chk",
            type_="checking",
            path=f"assets.chk_{suffix}",
            actor_token_id=token.id,
        )
        savings = ledger.create_account(
            db_session,
            name="Sav",
            type_="savings",
            path=f"assets.sav_{suffix}",
            actor_token_id=token.id,
        )
        cat = ledger.ensure_category_path(db_session, "income.seed2", actor_token_id=token.id)
        # Import-origin one-sided rows forming a transfer pair.
        ledger.create_flat_transaction(
            db_session,
            account_ref=str(checking.id),
            category_ref=str(cat.id),
            amount=Decimal("-100.00"),
            date=date.today(),
            description="transfer to savings",
            requested_status=core.Status.POSTED,
            origin=core.Origin.IMPORT,
            token=token,
        )
        ledger.create_flat_transaction(
            db_session,
            account_ref=str(savings.id),
            category_ref=str(cat.id),
            amount=Decimal("100.00"),
            date=date.today(),
            description="transfer from checking",
            requested_status=core.Status.POSTED,
            origin=core.Origin.IMPORT,
            token=token,
        )
        # Both open: checking's row is in the pool.
        pool_open = {
            matching._real_leg(t).account_id  # type: ignore[union-attr]
            for t in matching._one_sided_import_rows(db_session)
        }
        assert checking.id in pool_open and savings.id in pool_open

        ledger.close_account(
            db_session,
            account_id=checking.id,
            adjustment_category="expenses.adjustments",
            token=token,
        )
        pool_closed = {
            matching._real_leg(t).account_id  # type: ignore[union-attr]
            for t in matching._one_sided_import_rows(db_session)
        }
        assert checking.id not in pool_closed  # filtered out
        assert savings.id in pool_closed
        # The auto-linker runs without crashing and links nothing.
        stats = transfers.auto_link_pass(db_session)
        assert stats["transfers_auto_linked"] == 0


# --------------------------------------------------------------------------
# Reopen round-trip
# --------------------------------------------------------------------------


def test_reopen_makes_account_writable_again(seeded: dict[str, str], client: TestClient) -> None:
    _post_balance(client, seeded["checking"], seeded["dining"], "-12.00")
    assert (
        client.post(
            f"/accounts/{seeded['checking']}/close",
            json={"adjustment_category": "expenses.adjustments"},
        ).status_code
        == 200
    )
    # Closed: a write is refused.
    blocked = client.post(
        "/transactions",
        json={
            "account": seeded["checking"],
            "category": seeded["dining"],
            "amount": "-3.00",
            "date": date.today().isoformat(),
        },
    )
    assert blocked.status_code == 422
    # Reopen, then the same write succeeds.
    reopened = client.post(f"/accounts/{seeded['checking']}/reopen")
    assert reopened.status_code == 200
    assert reopened.json()["closed_at"] is None
    ok = client.post(
        "/transactions",
        json={
            "account": seeded["checking"],
            "category": seeded["dining"],
            "amount": "-3.00",
            "date": date.today().isoformat(),
        },
    )
    assert ok.status_code == 201, ok.text


def test_close_result_and_audit_event(
    seeded: dict[str, str], client: TestClient, db_session: Session
) -> None:
    _post_balance(client, seeded["checking"], seeded["dining"], "-20.00")
    resp = client.post(
        f"/accounts/{seeded['checking']}/close",
        json={"adjustment_category": "expenses.adjustments"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["closed_at"] is not None
    assert body["adjustment_tx_id"] is not None
    # account.close audit + account.closed event both landed.
    close_audit = db_session.execute(
        select(AuditLog).where(
            AuditLog.action == "account.close",
            AuditLog.entity_id == uuid.UUID(seeded["checking"]),
        )
    ).scalar_one()
    assert close_audit.before_after["after"]["unmapped"] is False
    from purser.models import Event

    event = db_session.execute(
        select(Event).where(
            Event.type == "account.closed", Event.entity_id == uuid.UUID(seeded["checking"])
        )
    ).scalar_one()
    assert event.payload["path"] == "assets.checking"


def test_already_closed_and_reopen_open_errors(seeded: dict[str, str], client: TestClient) -> None:
    assert (
        client.post(f"/accounts/{seeded['checking']}/close", json={}).status_code
        == 200  # zero balance -> no adjustment needed
    )
    # Second close: 422.
    assert client.post(f"/accounts/{seeded['checking']}/close", json={}).status_code == 422
    # Reopen an open account (savings) -> 422.
    assert client.post(f"/accounts/{seeded['savings']}/reopen").status_code == 422
