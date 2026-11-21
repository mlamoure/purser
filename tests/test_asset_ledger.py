"""Hand-valued assets as ledgers (#566, decision 113).

A property account's value is its postings; a change in value is an entry
against the system account equity.revaluation; "worth X on D" is a reconcile
whose difference posts the same way.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_mcp import LiveServer, _mcp_call
from tests.test_mcp import live_server as live_server

TODAY = datetime.now(UTC).date()


def _account(client: TestClient, type_: str = "property", name: str = "House") -> str:
    path = f"assets.{name.lower()}_{uuid.uuid4().hex[:8]}"
    resp = client.post("/accounts", json={"name": name, "type": type_, "path": path})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _change(client: TestClient, account: str, amount: str, days_ago: int = 0, **extra: Any) -> Any:
    return client.post(
        f"/accounts/{account}/value-changes",
        json={"as_of": (TODAY - timedelta(days=days_ago)).isoformat(), "amount": amount, **extra},
    )


def _target(client: TestClient, account: str, value: str, days_ago: int = 0, **extra: Any) -> Any:
    return client.post(
        f"/accounts/{account}/reconcile",
        json={
            "as_of": (TODAY - timedelta(days=days_ago)).isoformat(),
            "statement_balance": value,
            **extra,
        },
    )


def _balance(client: TestClient, account: str, days_ago: int | None = None) -> Decimal:
    if days_ago is None:
        return Decimal(client.get(f"/accounts/{account}").json()["balance"])
    on = (TODAY - timedelta(days=days_ago)).isoformat()
    return Decimal(
        client.get(f"/accounts/{account}/balance", params={"as_of": on}).json()["balance"]
    )


def _net_worth(client: TestClient, days_ago: int = 0) -> Decimal:
    on = (TODAY - timedelta(days=days_ago)).isoformat()
    return Decimal(client.get("/networth", params={"as_of": on}).json()["net_worth"])


class TestValueChanges:
    def test_a_change_moves_the_asset_and_net_worth_but_not_spending(
        self, admin_client: TestClient
    ) -> None:
        house = _account(admin_client)
        resp = _change(admin_client, house, "400000.00", days_ago=30, note="Purchase price")
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["created"] is True and body["reversed"] is False
        assert Decimal(body["amount"]) == Decimal("400000.00")
        assert Decimal(body["balance"]) == Decimal("400000.00")
        assert _change(admin_client, house, "-12500.00", days_ago=5).status_code == 201

        assert _balance(admin_client, house) == Decimal("387500.00")
        assert _balance(admin_client, house, days_ago=10) == Decimal("400000.00")
        assert _net_worth(admin_client) == Decimal("387500.00")
        assert _net_worth(admin_client, days_ago=31) == Decimal("0")
        account = admin_client.get(f"/accounts/{house}").json()
        assert account["balance_method"] == "postings"

        # The other leg is a system account, never a category: no spending.
        categories = {c["path"] for c in admin_client.get("/categories").json()}
        assert "equity.revaluation" not in categories
        rows = admin_client.get("/transactions", params={"account": house}).json()["items"]
        assert {r["value_change"] for r in rows} == {True}
        assert {r["securities_trade"] for r in rows} == {False}

    def test_a_backdated_change_moves_every_balance_after_it(
        self, admin_client: TestClient
    ) -> None:
        house = _account(admin_client)
        _change(admin_client, house, "100.00", days_ago=60)
        _change(admin_client, house, "20.00", days_ago=10)
        assert _balance(admin_client, house, days_ago=5) == Decimal("120.00")
        _change(admin_client, house, "10.00", days_ago=30)
        assert _balance(admin_client, house, days_ago=5) == Decimal("130.00")
        assert _balance(admin_client, house, days_ago=40) == Decimal("100.00")

    def test_a_repeated_reference_posts_nothing(self, admin_client: TestClient) -> None:
        house = _account(admin_client)
        first = _change(admin_client, house, "5000.00", reference="valuation-2026-12")
        assert first.status_code == 201, first.text
        again = _change(admin_client, house, "5000.00", reference="valuation-2026-12")
        assert again.status_code == 200, again.text
        assert again.json()["created"] is False
        assert again.json()["transaction_id"] == first.json()["transaction_id"]
        assert _balance(admin_client, house) == Decimal("5000.00")
        # The same reference on another asset is its own.
        car = _account(admin_client, name="Car")
        assert (
            _change(admin_client, car, "100.00", reference="valuation-2026-12").status_code == 201
        )

    @pytest.mark.parametrize(
        ("amount", "extra", "reason"),
        [
            ("0", {}, "nonzero"),
            ("1.00", {"reference": "has spaces in it"}, "reference must be"),
        ],
    )
    def test_bad_input_is_refused(
        self, admin_client: TestClient, amount: str, extra: dict[str, str], reason: str
    ) -> None:
        house = _account(admin_client)
        resp = _change(admin_client, house, amount, **extra)
        assert resp.status_code == 422, resp.text
        assert reason in resp.json()["detail"]

    def test_a_future_date_is_refused(self, admin_client: TestClient) -> None:
        house = _account(admin_client)
        resp = _change(admin_client, house, "1.00", days_ago=-2)
        assert resp.status_code == 422
        assert "future" in resp.json()["detail"]

    @pytest.mark.parametrize("type_", ["checking", "loan", "brokerage"])
    def test_other_account_types_are_refused(self, admin_client: TestClient, type_: str) -> None:
        account = _account(admin_client, type_=type_, name=type_)
        resp = _change(admin_client, account, "1.00")
        assert resp.status_code == 422
        assert "hand-valued assets" in resp.json()["detail"]

    def test_a_change_is_undone_by_its_reversal_and_never_edited(
        self, admin_client: TestClient
    ) -> None:
        house = _account(admin_client)
        _change(admin_client, house, "1000.00", days_ago=20)
        wrong = _change(admin_client, house, "999999.00", days_ago=3).json()["transaction_id"]

        edit = admin_client.patch(f"/transactions/{wrong}", json={"amount": "1.00"})
        assert edit.status_code == 422
        assert "reverse" in edit.json()["detail"]
        recategorize = admin_client.patch(
            f"/transactions/{wrong}", json={"category": "expenses.dining"}
        )
        assert recategorize.status_code in (404, 422)

        undone = admin_client.post(f"/value-changes/{wrong}/reverse")
        assert undone.status_code == 200, undone.text
        assert undone.json()["reversed"] is True
        assert Decimal(undone.json()["amount"]) == Decimal("-999999.00")
        assert _balance(admin_client, house) == Decimal("1000.00")
        # The reversal is the history: the original stays, corrected.
        again = admin_client.post(f"/value-changes/{wrong}/reverse")
        assert again.status_code == 422

    def test_reversing_an_ordinary_row_is_refused(self, admin_client: TestClient) -> None:
        checking = _account(admin_client, type_="checking", name="Checking")
        admin_client.post(
            "/categories",
            json={"name": "Dining", "type": "category_expense", "path": "expenses.dining"},
        )
        tx = admin_client.post(
            "/transactions",
            json={
                "account": checking,
                "category": "expenses.dining",
                "amount": "-5.00",
                "date": TODAY.isoformat(),
                "status": "posted",
            },
        ).json()["id"]
        resp = admin_client.post(f"/value-changes/{tx}/reverse")
        assert resp.status_code == 422
        assert "not a change in an asset's value" in resp.json()["detail"]

    def test_the_system_account_is_never_a_category_you_choose(
        self, admin_client: TestClient
    ) -> None:
        checking = _account(admin_client, type_="checking", name="Checking")
        house = _account(admin_client)
        _change(admin_client, house, "1.00")
        resp = admin_client.post(
            "/transactions",
            json={
                "account": checking,
                "category": "equity.revaluation",
                "amount": "-5.00",
                "date": TODAY.isoformat(),
            },
        )
        assert resp.status_code == 422
        assert "record_value_change" in resp.json()["detail"]

    def test_a_closed_asset_takes_no_change(self, admin_client: TestClient) -> None:
        house = _account(admin_client)
        assert admin_client.post(f"/accounts/{house}/close", json={}).status_code == 200
        resp = _change(admin_client, house, "1.00")
        assert resp.status_code == 422
        assert "closed" in resp.json()["detail"]

    def test_every_change_is_audited_and_evented(self, admin_client: TestClient) -> None:
        house = _account(admin_client)
        _change(admin_client, house, "250.00", reference="ref-1")
        audit = admin_client.get("/audit", params={"action": "asset.value_change"}).json()
        assert len(audit["items"]) == 1
        events = admin_client.get("/events", params={"type": "asset.value_changed"}).json()
        assert len(events) == 1


class TestValueOnADate:
    def test_reconcile_sets_the_value_and_posts_the_difference(
        self, admin_client: TestClient
    ) -> None:
        house = _account(admin_client)
        _change(admin_client, house, "400000.00", days_ago=90)
        resp = _target(admin_client, house, "452300.00", days_ago=30, note="Estimate midpoint")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert Decimal(body["adjustment"]) == Decimal("52300.00")
        assert body["category"] == "equity.revaluation"
        assert body["created"] is True
        assert _balance(admin_client, house) == Decimal("452300.00")
        tx = admin_client.get(f"/transactions/{body['adjustment_tx_id']}").json()
        assert tx["value_change"] is True
        assert tx["description"] == "Estimate midpoint"

        # The same figure again is a no-op.
        again = _target(admin_client, house, "452300.00", days_ago=30)
        assert again.status_code == 200 and again.json()["created"] is False
        # Today's value, posted once.
        today = _target(admin_client, house, "460000.00")
        assert Decimal(today.json()["adjustment"]) == Decimal("7700.00")
        repeat = _target(admin_client, house, "460000.00")
        assert repeat.json()["created"] is False
        assert _balance(admin_client, house) == Decimal("460000.00")

    def test_a_target_before_the_latest_is_refused_but_a_backdated_change_is_not(
        self, admin_client: TestClient
    ) -> None:
        house = _account(admin_client)
        assert _target(admin_client, house, "300000.00", days_ago=10).status_code == 200
        early = _target(admin_client, house, "290000.00", days_ago=40)
        assert early.status_code == 409
        assert early.json()["detail"]["code"] == "reconciliation_out_of_order"
        assert _change(admin_client, house, "1500.00", days_ago=40).status_code == 201
        # The recorded target now reads out of balance by the backdated change.
        (latest,) = admin_client.get(f"/accounts/{house}/reconciliations").json()[:1]
        assert latest["in_balance"] is False
        assert Decimal(latest["difference"]) == Decimal("1500.00")

    def test_a_reversed_target_change_shows_on_the_claim(self, admin_client: TestClient) -> None:
        house = _account(admin_client)
        body = _target(admin_client, house, "1000.00").json()
        undone = admin_client.post(f"/value-changes/{body['adjustment_tx_id']}/reverse")
        assert undone.status_code == 200, undone.text
        assert _balance(admin_client, house) == Decimal("0")
        (latest,) = admin_client.get(f"/accounts/{house}/reconciliations").json()[:1]
        assert latest["in_balance"] is False

    def test_a_card_reconcile_still_plugs_the_adjustments_category(
        self, admin_client: TestClient
    ) -> None:
        card = _account(admin_client, type_="credit_card", name="Card")
        resp = _target(admin_client, card, "25.00", note="Statement")
        assert resp.status_code == 200, resp.text
        assert resp.json()["category"] == "expenses.reconciliation_adjustments"


class TestLedgerInvariants:
    @settings(
        max_examples=20,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        st.lists(
            st.tuples(
                st.sampled_from(["change", "target"]),
                st.integers(min_value=-(10**9), max_value=10**9),
                st.integers(min_value=0, max_value=60),
            ),
            min_size=1,
            max_size=8,
        )
    )
    def test_changes_and_targets_keep_the_books_balanced(
        self, admin_client: TestClient, db_session: Session, ops: list[tuple[str, int, int]]
    ) -> None:
        house = _account(admin_client)
        latest_target: int | None = None
        recorded: set[int] = set()
        for kind, cents, days in ops:
            figure = str(Decimal(cents) / 100)
            if kind == "change":
                if cents == 0:
                    continue
                assert _change(admin_client, house, figure, days_ago=days).status_code == 201
                continue
            resp = _target(admin_client, house, figure, days_ago=days)
            if days in recorded:
                # One value per date: the same figure is a no-op while it holds; a
                # different one, or one a later change moved, is a conflict.
                assert resp.status_code in (200, 409), resp.text
                if resp.status_code == 409:
                    assert resp.json()["detail"]["code"] in (
                        "reconciliation_conflict",
                        "reconciliation_moved",
                    )
                else:
                    assert resp.json()["created"] is False
                continue
            if latest_target is not None and days > latest_target:
                assert resp.status_code == 409
                assert resp.json()["detail"]["code"] == "reconciliation_out_of_order"
                continue
            assert resp.status_code == 200, resp.text
            recorded.add(days)
            latest_target = days
            # Right after a target, the value on its date is the target.
            assert _balance(admin_client, house, days_ago=days) == Decimal(figure)
        db_session.expire_all()
        unbalanced = db_session.execute(
            text(
                "SELECT t.id FROM transactions t JOIN postings p ON p.transaction_id = t.id "
                "WHERE t.status IN ('pending', 'posted') "
                "GROUP BY t.id, p.currency HAVING SUM(p.amount) <> 0"
            )
        ).all()
        assert unbalanced == []
        # The asset's value is the sum of its live entries; equity.revaluation
        # holds exactly the other side.
        totals = db_session.execute(
            text(
                "SELECT a.path, COALESCE(SUM(p.amount), 0) FROM postings p "
                "JOIN transactions t ON t.id = p.transaction_id "
                "JOIN accounts a ON a.id = p.account_id "
                "WHERE t.status IN ('pending', 'posted') "
                "AND p.transaction_id IN (SELECT transaction_id FROM postings "
                "  WHERE account_id = CAST(:h AS uuid)) GROUP BY a.path"
            ),
            {"h": house},
        ).all()
        sums = {path: Decimal(total) for path, total in totals}
        assert sum(sums.values()) == 0
        assert _balance(admin_client, house) == sums.get(
            next((p for p in sums if p != "equity.revaluation"), ""), Decimal(0)
        )


class TestValuationsAreForLoans:
    def test_a_property_valuation_is_refused_with_its_two_ways(
        self, admin_client: TestClient
    ) -> None:
        house = _account(admin_client)
        resp = admin_client.post(
            f"/accounts/{house}/valuations",
            json={"as_of": TODAY.isoformat(), "value": "1.00"},
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "record_value_change" in detail and "reconcile_account" in detail


class TestMcp:
    def test_record_and_reverse_through_mcp(
        self, admin_client: TestClient, live_server: LiveServer
    ) -> None:
        house = _account(admin_client)
        path = admin_client.get(f"/accounts/{house}").json()["path"]
        agent = admin_client.post("/tokens", json={"name": "agent", "scopes": ["read", "write"]})
        token = agent.json()["token"]
        (recorded, repeated) = _mcp_call(
            live_server.url,
            token,
            [
                (
                    "record_value_change",
                    {
                        "account": path,
                        "as_of": TODAY.isoformat(),
                        "amount": "8000.00",
                        "reference": "mcp-1",
                    },
                ),
                (
                    "record_value_change",
                    {
                        "account": path,
                        "as_of": TODAY.isoformat(),
                        "amount": "8000.00",
                        "reference": "mcp-1",
                    },
                ),
            ],
        )
        assert recorded["created"] is True
        assert Decimal(recorded["balance"]) == Decimal("8000.00")
        assert repeated["created"] is False
        (target, reversed_) = _mcp_call(
            live_server.url,
            token,
            [
                (
                    "reconcile_account",
                    {
                        "account": path,
                        "as_of": TODAY.isoformat(),
                        "statement_balance": "9000.00",
                        "note": "Appraisal",
                    },
                ),
                ("reverse_value_change", {"transaction_id": recorded["transaction_id"]}),
            ],
        )
        assert target["category"] == "equity.revaluation"
        assert Decimal(target["adjustment"]) == Decimal("1000.00")
        assert reversed_["reversed"] is True
        assert Decimal(reversed_["balance"]) == Decimal("1000.00")
        (refused,) = _mcp_call(
            live_server.url,
            token,
            [("record_valuation", {"account": path, "as_of": TODAY.isoformat(), "value": "1"})],
        )
        assert "record_value_change" in refused["error"]


class TestFeedMappedAsset:
    def test_the_reported_balance_moves_the_asset_by_a_value_change(
        self, admin_client: TestClient, db_session: Session
    ) -> None:
        from types import SimpleNamespace

        from purser import ledger
        from purser.feed import importer

        house_id = _account(admin_client)
        account = ledger.get_account(db_session, uuid.UUID(house_id))
        fetch = SimpleNamespace(fetched_at=datetime.now(UTC))
        importer._record_balance_valuation(db_session, fetch, {"balance": "410000.00"}, account)  # type: ignore[arg-type]
        db_session.commit()
        assert _balance(admin_client, house_id) == Decimal("410000.00")
        importer._record_balance_valuation(db_session, fetch, {"balance": "410000.00"}, account)  # type: ignore[arg-type]
        importer._record_balance_valuation(db_session, fetch, {"balance": "415000.00"}, account)  # type: ignore[arg-type]
        db_session.commit()
        assert _balance(admin_client, house_id) == Decimal("415000.00")
        rows = admin_client.get("/transactions", params={"account": house_id}).json()["items"]
        assert len(rows) == 2
        assert {r["origin"] for r in rows} == {"import"}
