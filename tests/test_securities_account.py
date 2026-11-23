"""equity.securities is virtual but not a category (issue #390 phase 1b,
decision 103). Every surface that reads ``is_virtual`` to mean "a category"
must leave it out, and every path that lets someone CHOOSE a category must
refuse it. One pin per surface; synthetic data only.
"""

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from purser import ledger
from purser.core import ledger as core
from purser.holdings import service
from purser.ledger import LedgerError
from purser.models import Account
from tests.conftest import today


def _weekday_before(days: int):
    day = today() - timedelta(days=days)
    while day.weekday() >= 4:
        day -= timedelta(days=1)
    return day


@pytest.fixture
def trade(admin_client: TestClient) -> dict[str, str]:
    """A manual brokerage account with one sale whose cash Purser posted."""
    account = admin_client.post(
        "/accounts", json={"name": "Plan", "type": "brokerage", "path": "assets.plan"}
    ).json()["id"]
    admin_client.patch(f"/accounts/{account}", json={"holdings_source": "manual"})
    position = admin_client.post(
        f"/accounts/{account}/positions",
        json={
            "symbol": "ACME",
            "quantity": "10",
            "as_of": (today() - timedelta(days=60)).isoformat(),
        },
    ).json()["id"]
    resp = admin_client.post(
        f"/positions/{position}/events",
        json={
            "kind": "sell",
            "occurred_on": _weekday_before(20).isoformat(),
            "quantity": "2",
            "price": "62.50",
        },
    )
    assert resp.status_code == 201, resp.text
    admin_client.post(
        "/categories",
        json={"name": "Groceries", "type": "category_expense", "path": "expenses.groceries"},
    )
    securities = next(
        a
        for a in admin_client.get("/accounts", params={"include_virtual": True}).json()
        if a["path"] == "equity.securities"
    )
    return {
        "account": account,
        "position": position,
        "cash": resp.json()["event"]["transaction_id"],
        "securities": securities["id"],
    }


class TestListingsHideIt:
    def test_categories_endpoint_and_accounts(
        self, trade: dict[str, str], admin_client: TestClient
    ) -> None:
        paths = {c["path"] for c in admin_client.get("/categories").json()}
        assert "expenses.groceries" in paths and "equity.securities" not in paths
        listed = {
            a["path"]: a
            for a in admin_client.get("/accounts", params={"include_virtual": True}).json()
        }
        # The full account listing still shows it, as what it is.
        assert listed["equity.securities"]["type"] == "equity"
        assert listed["equity.securities"]["is_virtual"] is True

    def test_it_cannot_be_renamed_or_deleted_as_a_category(
        self, trade: dict[str, str], admin_client: TestClient
    ) -> None:
        resp = admin_client.patch(f"/categories/{trade['securities']}", json={"name": "Mine"})
        assert resp.status_code == 422 and "system account" in resp.text
        resp = admin_client.delete(f"/categories/{trade['securities']}")
        assert resp.status_code == 422 and "system account" in resp.text
        # Nor edited or moved as an account.
        for body in ({"name": "Mine"}, {"path": "assets.securities"}):
            resp = admin_client.patch(f"/accounts/{trade['securities']}", json=body)
            assert resp.status_code == 422 and "system account" in resp.text


class TestNothingChosenBooksToIt:
    def test_a_transaction_cannot_name_it(
        self, trade: dict[str, str], admin_client: TestClient
    ) -> None:
        resp = admin_client.post(
            "/transactions",
            json={
                "account": "assets.plan",
                "category": "equity.securities",
                "amount": "-1.00",
                "date": today().isoformat(),
                "status": "posted",
            },
        )
        assert resp.status_code == 422 and "system account" in resp.text

    def test_a_row_cannot_be_recategorized_into_it(
        self, trade: dict[str, str], admin_client: TestClient
    ) -> None:
        row = admin_client.post(
            "/transactions",
            json={
                "account": "assets.plan",
                "category": "expenses.groceries",
                "amount": "-3.00",
                "date": today().isoformat(),
                "status": "posted",
            },
        ).json()
        resp = admin_client.patch(
            f"/transactions/{row['id']}", json={"category": "equity.securities"}
        )
        assert resp.status_code == 422 and "system account" in resp.text

    def test_the_trade_row_itself_is_locked(
        self, trade: dict[str, str], admin_client: TestClient
    ) -> None:
        for body in ({"category": "expenses.groceries"}, {"description": "renamed"}):
            resp = admin_client.patch(f"/transactions/{trade['cash']}", json=body)
            assert resp.status_code == 422 and "securities trade" in resp.text
        resp = admin_client.patch(
            f"/transactions/{trade['cash']}", json={"category": "[assets.plan]"}
        )
        assert resp.status_code == 422

    def test_split_lines_and_close_adjustments_refuse_it(
        self, trade: dict[str, str], db_session: Session
    ) -> None:
        securities = ledger.securities_account(db_session)
        groceries = ledger.account_by_path(db_session, "expenses.groceries")
        with pytest.raises(LedgerError, match="system account"):
            ledger.create_split_transaction(
                db_session,
                account_ref="assets.plan",
                amount=Decimal("-10.00"),
                lines=[
                    core.SplitLine(category_id=securities.id, amount=Decimal("-4.00")),
                    core.SplitLine(category_id=groceries.id, amount=Decimal("-6.00")),
                ],
                date=today(),
                requested_status=core.Status.POSTED,
                origin=core.Origin.HUMAN,
                token=None,
            )
        with pytest.raises(LedgerError, match="system account"):
            ledger.ensure_category_path(db_session, "equity.securities", actor_token_id=None)

    def test_close_refuses_it_as_the_adjustment_category(
        self, trade: dict[str, str], admin_client: TestClient
    ) -> None:
        checking = admin_client.post(
            "/accounts", json={"name": "Chk", "type": "checking", "path": "assets.chk"}
        ).json()["id"]
        admin_client.post(
            "/transactions",
            json={
                "account": "assets.chk",
                "category": "expenses.groceries",
                "amount": "-5.00",
                "date": today().isoformat(),
                "status": "posted",
            },
        )
        for ref in ("equity.securities", trade["securities"]):
            resp = admin_client.post(
                f"/accounts/{checking}/close", json={"adjustment_category": ref}
            )
            assert resp.status_code == 422 and "system account" in resp.text

    def test_rules_cannot_target_it(self, trade: dict[str, str], admin_client: TestClient) -> None:
        resp = admin_client.post(
            "/rules",
            json={
                "type": "categorize",
                "match_field": "description",
                "match_mode": "contains",
                "match_text": "sale",
                "category_id": trade["securities"],
            },
        )
        assert resp.status_code in (400, 422) and "system account" in resp.text

    def test_schedules_cannot_target_it(
        self, trade: dict[str, str], admin_client: TestClient
    ) -> None:
        resp = admin_client.post(
            "/schedules",
            json={
                "name": "Synthetic sweep",
                "account": "assets.plan",
                "category": "equity.securities",
                "rrule": "FREQ=MONTHLY",
                "amount_type": "fixed",
                "amount": "10.00",
            },
        )
        assert resp.status_code == 422 and "system account" in resp.text


class TestTheEquitySubtreeIsTheSystems:
    """Refusing the exact system path is not enough: a category under
    equity.securities (or an equity root category) would sit in the same
    subtree, and reports by path would mix it with trade cash."""

    def test_nothing_is_created_under_equity(
        self, trade: dict[str, str], admin_client: TestClient
    ) -> None:
        cases = [
            (
                "/categories",
                {"name": "Mine", "type": "category_expense", "parent_id": trade["securities"]},
            ),
            ("/categories", {"name": "Mine", "type": "category_expense", "path": "equity.mine"}),
            ("/categories", {"name": "Equity", "type": "category_income", "path": "equity"}),
            ("/accounts", {"name": "Odd", "type": "checking", "path": "equity.odd"}),
            ("/accounts", {"name": "Odd", "type": "checking", "parent_id": trade["securities"]}),
        ]
        for url, body in cases:
            resp = admin_client.post(url, json=body)
            assert resp.status_code == 422, (url, body, resp.text)
            assert "Purser's system accounts" in resp.text
        paths = {
            a["path"]
            for a in admin_client.get("/accounts", params={"include_virtual": True}).json()
        }
        assert {p for p in paths if p.startswith("equity")} == {"equity.securities"}

    def test_no_account_moves_under_equity(
        self, trade: dict[str, str], admin_client: TestClient
    ) -> None:
        """PATCH /accounts/{id} with a path moves an account and its subtree
        (decision 72): never into the system subtree."""
        checking = admin_client.post(
            "/accounts", json={"name": "Chk", "type": "checking", "path": "assets.movable"}
        ).json()["id"]
        for path in ("equity.movable", "equity.securities.movable", "equity"):
            resp = admin_client.patch(f"/accounts/{checking}", json={"path": path})
            assert resp.status_code == 422, (path, resp.text)
            assert "Purser's system accounts" in resp.text
        assert admin_client.get(f"/accounts/{checking}").json()["path"] == "assets.movable"

    def test_ensure_category_path_and_close_refuse_the_subtree(
        self, trade: dict[str, str], admin_client: TestClient, db_session: Session
    ) -> None:
        for path in ("equity", "equity.mine", "equity.securities.mine"):
            with pytest.raises(LedgerError, match="system accounts"):
                ledger.ensure_category_path(db_session, path, actor_token_id=None)
        checking = admin_client.post(
            "/accounts", json={"name": "Chk", "type": "checking", "path": "assets.chk"}
        ).json()["id"]
        admin_client.post(  # a balance to zero at close
            "/transactions",
            json={
                "account": "assets.chk",
                "category": "expenses.groceries",
                "amount": "-5.00",
                "date": today().isoformat(),
                "status": "posted",
            },
        )
        resp = admin_client.post(
            f"/accounts/{checking}/close", json={"adjustment_category": "equity.adjustments"}
        )
        assert resp.status_code == 422 and "system accounts" in resp.text

    def test_reports_by_path_never_count_trade_cash(
        self, trade: dict[str, str], db_session: Session
    ) -> None:
        cash = ledger.get_transaction(db_session, uuid.UUID(trade["cash"]))
        for subtree in ("equity", "equity.securities"):
            rollup = ledger.category_rollup(db_session, subtree=subtree)
            assert (rollup["total"], rollup["transaction_count"]) == (0, 0)
            summary = ledger.spending_summary(
                db_session, group_by="category", category_subtree=subtree
            )
            assert summary["groups"] == [] and summary["grand_total"] == 0
            baseline = ledger.spending_baseline(
                db_session,
                period=cash.date.strftime("%Y-%m"),
                baseline="avg_3mo",
                category_subtree=subtree,
            )
            assert baseline["rows"] == [] and baseline["grand_total"] == "0.00"


class TestMachineryLeavesTradeCashAlone:
    def test_payee_memory_and_the_classifier_never_offer_it(
        self, trade: dict[str, str], db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from purser import categorize

        offered: list[list[str]] = []

        def suggest(settings: object, **kwargs: object) -> None:
            offered.append(list(kwargs["categories"]))  # type: ignore[call-overload]
            return None

        real = categorize.appsettings.effective
        monkeypatch.setattr(categorize.ai_providers, "suggest", suggest)
        monkeypatch.setattr(
            categorize.appsettings,
            "effective",
            lambda: real().model_copy(update={"ai_provider": "openai"}),
        )
        from purser.feed import importer

        uncategorized = importer._uncategorized(db_session, income=False)
        ledger.create_flat_transaction(
            db_session,
            account_ref="assets.plan",
            category_ref=str(uncategorized.id),
            amount=Decimal("-7.00"),
            date=today(),
            payee_name="Synthetic Store",
            requested_status=core.Status.PENDING,
            origin=core.Origin.IMPORT,
            token=None,
            content_hash=f"t-{uuid.uuid4().hex}",
        )
        categorize.run(db_session, token=None)
        assert offered
        assert all(
            "equity.securities" not in paths and "[equity.securities]" not in paths
            for paths in offered
        )

    def test_trade_cash_teaches_payee_memory_nothing(
        self, trade: dict[str, str], db_session: Session
    ) -> None:
        """A sale whose cash the bank sent carries the bank's payee once it
        is linked; its category leg is equity.securities. However often that
        payee's sales recur, memory never learns the system account."""
        from purser import categorize
        from purser.feed import importer

        account = ledger.account_by_path(db_session, "assets.plan")
        position = service.open_position(db_session, account.id, "ACME")
        assert position is not None
        income = importer._uncategorized(db_session, income=True)
        linked = []
        for days in (12, 8, 4):  # more than memory needs (two)
            day = _weekday_before(days)
            row = ledger.create_flat_transaction(
                db_session,
                account_ref=str(account.id),
                category_ref=str(income.id),
                amount=Decimal("10.00"),
                date=day,
                payee_name="Synthetic Plan Sale",
                requested_status=core.Status.POSTED,
                origin=core.Origin.IMPORT,
                token=None,
                content_hash=f"t-{uuid.uuid4().hex}",
            )
            event = service.record_event(
                db_session,
                position_id=position.id,
                kind="sell",
                occurred_on=day,
                quantity=Decimal("0.2"),
                price=Decimal("50"),
                transaction_id=row.id,
                token=None,
            )
            linked.append(ledger.get_transaction(db_session, event.transaction_id))
        payee_ids = {tx.payee_id for tx in linked}
        assert len(payee_ids) == 1 and None not in payee_ids  # the bank's payee, kept
        assert all(ledger.has_system_leg(tx.postings) for tx in linked)
        assert categorize.payee_memory(db_session, payee_ids.pop()) is None

    def test_the_trade_row_waits_for_review(
        self, trade: dict[str, str], admin_client: TestClient
    ) -> None:
        detail = admin_client.get(f"/transactions/{trade['cash']}").json()
        assert detail["origin"] == "system" and detail["needs_review"] is True
        assert detail["securities_trade"] is True

    def test_the_matcher_never_settles_a_bill_with_it(
        self, trade: dict[str, str], admin_client: TestClient, db_session: Session
    ) -> None:
        from purser import matching

        cash = ledger.get_transaction(db_session, uuid.UUID(trade["cash"]))
        candidates = matching._candidates_for(db_session, uuid.UUID(trade["account"]), cash.date)
        assert cash.id not in {c.id for c in candidates}

    def test_the_transfer_pool_leaves_a_linked_bank_row_out(self, db_session: Session) -> None:
        """link() books the system leg on the correction's REPLACEMENT; the
        bank's original stays posted and import-origin. It must leave the
        transfer pool, or the auto-linker merges it with an opposite row and
        the register counts the cash twice (the replacement and the merge)."""
        from purser import balances, matching, transfers
        from purser.feed import importer

        account = ledger.create_account(
            db_session,
            name="Plan",
            type_="brokerage",
            path=f"assets.plan_{uuid.uuid4().hex[:8]}",
            actor_token_id=None,
        )
        ledger.update_account(
            db_session, account_id=account.id, holdings_source="manual", actor_token_id=None
        )
        checking = ledger.create_account(
            db_session,
            name="Checking",
            type_="checking",
            path=f"assets.checking_{uuid.uuid4().hex[:8]}",
            actor_token_id=None,
        )
        position = service.record_position(
            db_session,
            account_ref=str(account.id),
            symbol="TEST",
            quantity=Decimal("10"),
            as_of=today() - timedelta(days=60),
            token=None,
        ).position
        day = _weekday_before(8)

        def bank_row(on: Account, amount: str, description: str):
            category = importer._uncategorized(db_session, income=Decimal(amount) > 0)
            return ledger.create_flat_transaction(
                db_session,
                account_ref=str(on.id),
                category_ref=str(category.id),
                amount=Decimal(amount),
                date=day,
                description=description,
                requested_status=core.Status.POSTED,
                origin=core.Origin.IMPORT,
                token=None,
                content_hash=f"t-{uuid.uuid4().hex}",
            )

        sold = bank_row(account, "250.00", "TRANSFER SOLD SHARES")
        bank_row(checking, "-250.00", "TRANSFER TO PLAN")  # an opposite, signalled row
        service.record_event(
            db_session,
            position_id=position.id,
            kind="sell",
            occurred_on=day,
            quantity=Decimal("5"),
            price=Decimal("50"),
            transaction_id=sold.id,
            token=None,
        )
        pool, _holds = matching.transfer_pool(db_session)
        assert sold.id not in {t.id for t in pool}

        transfers.auto_link_pass(db_session)
        db_session.refresh(sold)
        assert sold.status == "posted" and sold.superseded_by is None
        balances.recompute_running(db_session)
        assert balances.balance_asof(db_session, account.id, today()) == Decimal("250.0000")

    def test_a_conversion_never_merges_with_trade_cash(
        self, trade: dict[str, str], db_session: Session
    ) -> None:
        """Turning another account's row into a transfer to the brokerage
        account looks there for the transfer's other side: never a trade's
        cash, which would leave its trade without it."""
        from purser import transfers

        cash = ledger.get_transaction(db_session, uuid.UUID(trade["cash"]))
        checking = ledger.create_account(
            db_session,
            name="Checking",
            type_="checking",
            path=f"assets.checking_{uuid.uuid4().hex[:8]}",
            actor_token_id=None,
        )
        groceries = ledger.account_by_path(db_session, "expenses.groceries")
        row = ledger.create_flat_transaction(
            db_session,
            account_ref=str(checking.id),
            category_ref=str(groceries.id),
            amount=Decimal("-125.00"),
            date=cash.date,
            requested_status=core.Status.POSTED,
            origin=core.Origin.IMPORT,
            token=None,
            content_hash=f"t-{uuid.uuid4().hex}",
        )
        counterpart = transfers._counterpart_in(
            db_session, account_id=uuid.UUID(trade["account"]), amount=Decimal("125.00"), row=row
        )
        assert counterpart.chosen is None and not counterpart.refused  # #386's _Pick

    def test_spending_never_counts_it(self, trade: dict[str, str], db_session: Session) -> None:
        result = ledger.spending_summary(db_session, group_by="category")
        assert not any(str(g["key"]).startswith("equity") for g in result["groups"])
        income = ledger.spending_summary(db_session, group_by="category", category_subtree="income")
        assert not any(str(g["key"]).startswith("equity") for g in income["groups"])

    def test_schedule_detection_skips_trade_cash(self, db_session: Session) -> None:
        """A monthly sale whose cash the feed delivers (so it carries the
        bank's payee) and that is linked to its trade is not a bill: detection
        must not propose a schedule booking to equity.securities."""
        from purser import schedules
        from purser.feed import importer

        account = ledger.create_account(
            db_session,
            name="Plan",
            type_="brokerage",
            path=f"assets.plan_{uuid.uuid4().hex[:8]}",
            actor_token_id=None,
        )
        ledger.update_account(
            db_session, account_id=account.id, holdings_source="manual", actor_token_id=None
        )
        position = service.create_position(
            db_session,
            account_ref=str(account.id),
            symbol="TEST",
            opened_on=today() - timedelta(days=200),
            token=None,
        )
        service.record_event(
            db_session,
            position_id=position.id,
            kind="set_quantity",
            occurred_on=today() - timedelta(days=200),
            quantity=Decimal("100"),
            token=None,
        )
        income = importer._uncategorized(db_session, income=True)
        trade_rows = []
        for month in range(1, 7):  # a monthly sale of the same size
            day = _weekday_before(30 * month)
            feed = ledger.create_flat_transaction(
                db_session,
                account_ref=str(account.id),
                category_ref=str(income.id),
                amount=Decimal("25.00"),
                date=day,
                payee_name="Synthetic Plan Sale",
                requested_status=core.Status.POSTED,
                origin=core.Origin.IMPORT,
                token=None,
                content_hash=f"t-{uuid.uuid4().hex}",
            )
            event = service.record_event(
                db_session,
                position_id=position.id,
                kind="sell",
                occurred_on=day,
                quantity=Decimal("1"),
                price=Decimal("25"),
                transaction_id=feed.id,
                token=None,
            )
            trade_rows.append(ledger.get_transaction(db_session, event.transaction_id))
        # The trade rows themselves never form a recurrence group...
        assert all(r.payee is not None for r in trade_rows)  # they carry the bank's payee
        groups = schedules._group_history(
            db_session, trade_rows, only_account_id=None, only_counterparty=None
        )
        assert groups == {}
        # ...and neither do the corrected bank originals, still posted: the
        # monthly sale is not a recurring deposit, so detection drafts nothing
        # for the account — no schedule of the old category, none booking to
        # the system account.
        found = schedules.detect_schedules(db_session, actor_token_id=None)
        assert [s for s in found if s.subject_account_id == account.id] == []
        assert all(s.draft.get("category") != "equity.securities" for s in found)

    def test_it_is_seeded_and_never_a_category_type(self, db_session: Session) -> None:
        securities = ledger.securities_account(db_session)
        assert securities.type == "equity" and securities.is_virtual
        assert ledger.is_system_account(securities) and not ledger.is_category(securities)
        assert securities.type not in ledger.CATEGORY_TYPES
        again = ledger.securities_account(db_session)
        assert again.id == securities.id
        count = db_session.execute(
            select(Account).where(Account.path == ledger.SECURITIES_PATH)
        ).all()
        assert len(count) == 1
