"""Securities trades a person marks, rules that mark them, the rule a mark
proposes, and the brokerage balance true-up (issue #591, decision 119).

Synthetic data only: made-up symbols, amounts and descriptions.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from purser import crypto, ledger, rules, suggestions
from purser.core import ledger as core
from purser.feed import trueup
from purser.models import (
    Account,
    AuditLog,
    BrokerageTrueUp,
    Connection,
    Event,
    HoldingsSnapshot,
    Position,
    RawFetch,
    Rule,
    Suggestion,
    Transaction,
)
from tests.conftest import now, reset_db, today
from tests.test_feed import feed_env as feed_env
from tests.test_mcp import LiveServer, _mcp_call
from tests.test_mcp import live_server as live_server

BROKER = "assets.broker"


def _post(
    client: TestClient,
    account: str,
    amount: str,
    *,
    payee: str,
    status: str = "posted",
    on: date | None = None,
    category: str = "expenses.uncategorized",
) -> dict[str, Any]:
    resp = client.post(
        "/transactions",
        json={
            "account": account,
            "category": category,
            "amount": amount,
            "date": (on or today()).isoformat(),
            "payee": payee,
            "description": f"{payee} 10 SHARES OF EXAMPLE FUND",
            "status": status,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
def broker(admin_client: TestClient) -> dict[str, str]:
    """A feed-holdings brokerage account (the default source), a checking
    account and a hand-tracked brokerage account, plus the categories."""
    ids = {}
    for name, type_, path in [
        ("Broker", "brokerage", BROKER),
        ("Checking", "checking", "assets.checking"),
        ("Plan", "brokerage", "assets.plan"),
    ]:
        resp = admin_client.post("/accounts", json={"name": name, "type": type_, "path": path})
        assert resp.status_code == 201, resp.text
        ids[path] = resp.json()["id"]
    resp = admin_client.patch(f"/accounts/{ids['assets.plan']}", json={"holdings_source": "manual"})
    assert resp.status_code == 200, resp.text
    for path, name, type_ in [
        ("expenses.uncategorized", "Uncategorized", "category_expense"),
        ("expenses.fees", "Fees", "category_expense"),
    ]:
        admin_client.post("/categories", json={"name": name, "type": type_, "path": path})
    return ids


def _mark(client: TestClient, tx_id: str, **extra: Any) -> Any:
    return client.patch(f"/transactions/{tx_id}", json={"securities_trade": True, **extra})


class TestMarkAndUnmark:
    def test_a_pending_row_is_marked_in_place_and_changed_back(
        self, broker: dict[str, str], admin_client: TestClient
    ) -> None:
        row = _post(admin_client, BROKER, "-250.00", payee="YOU BOUGHT", status="pending")
        resp = _mark(admin_client, row["id"])
        assert resp.status_code == 200, resp.text
        marked = resp.json()
        assert marked["id"] == row["id"]
        assert marked["securities_trade"] is True and marked["securities_trade_locked"] is False
        assert marked["category_path"] == "equity.securities"
        assert marked["suggestion_id"] is not None
        back = admin_client.patch(
            f"/transactions/{row['id']}", json={"category": "expenses.fees"}
        ).json()
        assert back["id"] == row["id"] and back["category_path"] == "expenses.fees"
        assert back["securities_trade"] is False

    def test_a_posted_row_is_corrected_and_corrected_back(
        self, broker: dict[str, str], admin_client: TestClient, db_session: Session
    ) -> None:
        row = _post(admin_client, BROKER, "-250.00", payee="YOU BOUGHT")
        marked = _mark(admin_client, row["id"]).json()
        assert marked["id"] != row["id"]  # the correction pair's replacement
        assert marked["reverses_id"] is not None
        assert marked["securities_trade"] is True
        # Spending reports never count it.
        summary = ledger.spending_summary(db_session, group_by="category")
        assert "equity" not in str(summary)
        back = admin_client.patch(
            f"/transactions/{marked['id']}", json={"category": "expenses.fees"}
        )
        assert back.status_code == 200, back.text
        assert back.json()["category_path"] == "expenses.fees"
        # The ledger keeps both corrections, audited.
        actions = list(
            db_session.execute(
                select(AuditLog.action).where(AuditLog.action == "transaction.correct")
            ).scalars()
        )
        assert len(actions) == 2
        # The account's balance never moved: only the category side did.
        accounts = admin_client.get("/accounts", params={"include_balances": True}).json()
        (acct,) = [a for a in accounts if a["path"] == BROKER]
        assert Decimal(acct["balance"]) == Decimal("-250.00")

    def test_an_amount_edit_keeps_the_mark(
        self, broker: dict[str, str], admin_client: TestClient
    ) -> None:
        row = _post(admin_client, BROKER, "-250.00", payee="YOU BOUGHT", status="pending")
        _mark(admin_client, row["id"])
        resp = admin_client.patch(f"/transactions/{row['id']}", json={"amount": "-260.00"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["securities_trade"] is True
        assert Decimal(resp.json()["amount"]) == Decimal("-260.00")


class TestRefusals:
    def test_a_checking_row_is_refused(
        self, broker: dict[str, str], admin_client: TestClient
    ) -> None:
        row = _post(admin_client, "assets.checking", "-25.00", payee="SHOP")
        resp = _mark(admin_client, row["id"])
        assert resp.status_code == 422 and "brokerage" in resp.text

    def test_a_hand_tracked_brokerage_row_is_refused(
        self, broker: dict[str, str], admin_client: TestClient
    ) -> None:
        row = _post(admin_client, "assets.plan", "-25.00", payee="YOU BOUGHT")
        resp = _mark(admin_client, row["id"])
        assert resp.status_code == 422 and "record the trade" in resp.text

    def test_a_transfer_is_refused(self, broker: dict[str, str], admin_client: TestClient) -> None:
        resp = admin_client.post(
            "/transactions/transfer",
            json={
                "from_account": "assets.checking",
                "to_account": BROKER,
                "amount": "100.00",
                "date": today().isoformat(),
            },
        )
        assert resp.status_code == 201, resp.text
        out = _mark(admin_client, resp.json()["id"])
        assert out.status_code == 422 and "transfer" in out.text

    def test_a_split_row_is_refused(
        self, broker: dict[str, str], admin_client: TestClient, db_session: Session
    ) -> None:
        fees = ledger.account_by_path(db_session, "expenses.fees")
        other = ledger.account_by_path(db_session, "expenses.uncategorized")
        tx = ledger.create_split_transaction(
            db_session,
            account_ref=BROKER,
            amount=Decimal("-10.00"),
            lines=[
                core.SplitLine(category_id=fees.id, amount=Decimal("-4.00")),
                core.SplitLine(category_id=other.id, amount=Decimal("-6.00")),
            ],
            date=today(),
            requested_status=core.Status.POSTED,
            origin=core.Origin.HUMAN,
            token=None,
        )
        db_session.commit()
        resp = _mark(admin_client, str(tx.id))
        assert resp.status_code == 422 and "split" in resp.text

    def test_the_category_path_itself_is_still_refused(
        self, broker: dict[str, str], admin_client: TestClient
    ) -> None:
        _mark(admin_client, _post(admin_client, BROKER, "-1.00", payee="SEED")["id"])
        row = _post(admin_client, BROKER, "-25.00", payee="YOU BOUGHT")
        resp = admin_client.patch(
            f"/transactions/{row['id']}", json={"category": "equity.securities"}
        )
        assert resp.status_code == 422 and "system account" in resp.text
        resp = admin_client.post(
            "/transactions",
            json={
                "account": BROKER,
                "category": "equity.securities",
                "amount": "-1.00",
                "date": today().isoformat(),
            },
        )
        assert resp.status_code == 422 and "system account" in resp.text

    def test_both_fields_and_false_without_a_category_are_refused(
        self, broker: dict[str, str], admin_client: TestClient
    ) -> None:
        row = _post(admin_client, BROKER, "-25.00", payee="YOU BOUGHT")
        resp = _mark(admin_client, row["id"], category="expenses.fees")
        assert resp.status_code == 422
        resp = admin_client.patch(f"/transactions/{row['id']}", json={"securities_trade": False})
        assert resp.status_code == 422 and "category" in resp.text

    def test_payee_memory_never_learns_the_mark(
        self, broker: dict[str, str], admin_client: TestClient, db_session: Session
    ) -> None:
        from purser import categorize

        for _ in range(3):
            _mark(admin_client, _post(admin_client, BROKER, "-20.00", payee="YOU BOUGHT")["id"])
        payee = ledger.resolve_payee(db_session, "YOU BOUGHT")
        assert categorize.payee_memory(db_session, payee.id) is None

    def test_a_marked_row_cannot_become_a_transfer_until_changed_back(
        self, broker: dict[str, str], admin_client: TestClient
    ) -> None:
        row = _post(admin_client, BROKER, "-25.00", payee="YOU BOUGHT", status="pending")
        _mark(admin_client, row["id"])
        resp = admin_client.patch(
            f"/transactions/{row['id']}", json={"category": "[assets.checking]"}
        )
        assert resp.status_code == 422 and "change it back" in resp.text.lower()


class TestRules:
    def _rule(self, client: TestClient, account_ids: list[str] | None, **extra: Any) -> Any:
        body: dict[str, Any] = {
            "type": "categorize",
            "match_field": "payee_or_description",
            "match_mode": "prefix",
            "match_text": "YOU BOUGHT",
            "securities_trade": True,
            **extra,
        }
        if account_ids is not None:
            body["account_ids"] = account_ids
        return client.post("/rules", json=body)

    def test_scope_must_be_feed_brokerage_accounts(
        self, broker: dict[str, str], admin_client: TestClient
    ) -> None:
        assert self._rule(admin_client, None).status_code == 422
        assert self._rule(admin_client, [broker["assets.checking"]]).status_code == 422
        resp = self._rule(admin_client, [broker[BROKER], broker["assets.plan"]])
        assert resp.status_code == 422 and "entered in Purser" in resp.text
        resp = self._rule(admin_client, [broker[BROKER]])
        assert resp.status_code == 201, resp.text
        out = resp.json()
        assert out["securities_trade"] is True and out["state"] == "draft"
        assert "Securities trade" in out["action"]
        # Widening the scope to a checking account later is refused too.
        resp = admin_client.patch(
            f"/rules/{out['id']}", json={"account_ids": [broker["assets.checking"]]}
        )
        assert resp.status_code == 422
        resp = self._rule(admin_client, [broker[BROKER]], category_id=broker["assets.checking"])
        assert resp.status_code == 422

    def test_apply_history_marks_matching_rows_dry_run_first(
        self, broker: dict[str, str], admin_client: TestClient
    ) -> None:
        rows = [_post(admin_client, BROKER, f"-{n}0.00", payee="YOU BOUGHT") for n in (1, 2)]
        other = _post(admin_client, BROKER, "-5.00", payee="ADVISORY FEE")
        rule_id = self._rule(admin_client, [broker[BROKER]], activate=True).json()["id"]
        dry = admin_client.post(
            f"/rules/{rule_id}/apply-history", json={"dry_run": True, "posted": "correct"}
        ).json()
        assert dry["matched"] == 2 and dry["posted"] == 2
        real = admin_client.post(
            f"/rules/{rule_id}/apply-history", json={"dry_run": False, "posted": "correct"}
        ).json()
        assert real["posted"] == dry["posted"]
        listed = admin_client.get("/transactions", params={"account": BROKER}).json()["items"]
        trades = [t for t in listed if t["securities_trade"]]
        assert len(trades) == 2 and all(not t["securities_trade_locked"] for t in trades)
        assert other["id"] in {t["id"] for t in listed if not t["securities_trade"]}
        assert {t["id"] for t in trades}.isdisjoint({r["id"] for r in rows})  # corrected

    def test_the_nightly_pass_marks_uncategorized_rows(
        self, broker: dict[str, str], admin_client: TestClient, db_session: Session
    ) -> None:
        from purser import categorize

        self._rule(admin_client, [broker[BROKER]], activate=True)
        _post(admin_client, BROKER, "-30.00", payee="YOU BOUGHT", status="pending")
        stats = categorize.run(db_session, token=None)
        db_session.commit()
        assert stats["rule"] == 1
        listed = admin_client.get("/transactions", params={"account": BROKER}).json()["items"]
        assert [t["securities_trade"] for t in listed] == [True]

    def test_a_rule_on_an_account_switched_to_manual_answers_nothing(
        self, broker: dict[str, str], admin_client: TestClient, db_session: Session
    ) -> None:
        rule_id = self._rule(admin_client, [broker[BROKER]], activate=True).json()["id"]
        _post(admin_client, BROKER, "-30.00", payee="YOU BOUGHT", status="pending")
        admin_client.patch(f"/accounts/{broker[BROKER]}", json={"holdings_source": "manual"})
        dry = admin_client.post(f"/rules/{rule_id}/apply-history", json={"dry_run": True}).json()
        assert dry["matched"] == 0 and dry["skipped_not_trade_account"] == 1


class TestSuggestion:
    def test_a_mark_proposes_one_card_refreshed_by_repeat_marks(
        self, broker: dict[str, str], admin_client: TestClient
    ) -> None:
        first = _mark(admin_client, _post(admin_client, BROKER, "-10.00", payee="YOU BOUGHT")["id"])
        second = _mark(
            admin_client, _post(admin_client, BROKER, "-20.00", payee="YOU BOUGHT")["id"]
        )
        sid = first.json()["suggestion_id"]
        assert sid and second.json()["suggestion_id"] == sid
        card = admin_client.get(f"/suggestions/{sid}").json()
        assert card["kind"] == "trade_rule" and card["source"] == "mark"
        assert card["seen_count"] == 2
        assert card["draft"]["match_text"] == "YOU BOUGHT"
        assert card["draft"]["direction"] == "debit"
        assert "YOU BOUGHT" not in card["headline"]  # bank text stays in the draft
        assert len(card["evidence"]["occurrences"]) == 2
        counts = admin_client.get("/suggestions/counts").json()
        assert counts["trade_rule"] == 1

    def test_accept_creates_a_draft_rule_with_its_projected_count(
        self, broker: dict[str, str], admin_client: TestClient
    ) -> None:
        _post(admin_client, BROKER, "-15.00", payee="YOU BOUGHT")
        sid = _mark(
            admin_client, _post(admin_client, BROKER, "-10.00", payee="YOU BOUGHT")["id"]
        ).json()["suggestion_id"]
        out = admin_client.post(
            f"/suggestions/{sid}/decide",
            json={"decision": "edit_then_accept", "edits": {"match_mode": "contains"}},
        )
        assert out.status_code == 200, out.text
        body = out.json()
        assert body["rule"]["state"] == "draft" and body["rule"]["securities_trade"] is True
        assert body["projected_matches"] == 1  # the unmarked row; the marked one is a trade
        assert body["projected_links"] is None
        assert body["suggestion"]["status"] == "accepted"
        # The accepted rule covers the next mark: no new card.
        again = _mark(admin_client, _post(admin_client, BROKER, "-12.00", payee="YOU BOUGHT")["id"])
        assert again.json()["suggestion_id"] is None
        # Activation is the ordinary categorize activation.
        state = admin_client.post(f"/rules/{body['rule']['id']}/state", json={"state": "active"})
        assert state.status_code == 200 and state.json()["state"] == "active"

    def test_an_edit_outside_the_draft_is_refused(
        self, broker: dict[str, str], admin_client: TestClient
    ) -> None:
        sid = _mark(
            admin_client, _post(admin_client, BROKER, "-10.00", payee="YOU BOUGHT")["id"]
        ).json()["suggestion_id"]
        out = admin_client.post(
            f"/suggestions/{sid}/decide",
            json={"decision": "edit_then_accept", "edits": {"account_id": str(uuid.uuid4())}},
        )
        assert out.status_code == 422 and "account" in out.text

    def test_rejection_is_remembered(
        self, broker: dict[str, str], admin_client: TestClient
    ) -> None:
        sid = _mark(
            admin_client, _post(admin_client, BROKER, "-10.00", payee="YOU BOUGHT")["id"]
        ).json()["suggestion_id"]
        admin_client.post(f"/suggestions/{sid}/decide", json={"decision": "reject"})
        again = _mark(admin_client, _post(admin_client, BROKER, "-9.00", payee="YOU BOUGHT")["id"])
        assert again.json()["suggestion_id"] is None
        # Another wording on the account still proposes, until "never for this account".
        other = _mark(admin_client, _post(admin_client, BROKER, "9.00", payee="YOU SOLD")["id"])
        other_id = other.json()["suggestion_id"]
        assert other_id is not None and other_id != sid
        admin_client.post(
            f"/suggestions/{other_id}/decide",
            json={"decision": "reject", "reject_scope": "counterparty"},
        )
        third = _mark(
            admin_client, _post(admin_client, BROKER, "-9.00", payee="REINVESTMENT")["id"]
        )
        assert third.json()["suggestion_id"] is None

    def test_fingerprint_holds_no_bank_text(self, migrated_engine: Engine) -> None:
        # The blind index reads the key from the database (crypto._load_dek).
        account = uuid.uuid4()
        fp = suggestions.trade_rule_fingerprint(account, "YOU BOUGHT", "debit")
        assert "BOUGHT" not in fp.upper()
        assert fp == suggestions.trade_rule_fingerprint(account, "YOU BOUGHT", "debit")
        assert fp != suggestions.trade_rule_fingerprint(account, "YOU BOUGHT", "credit")


class TestRecordedTradeStaysLocked:
    def test_a_recorded_sale_cash_is_locked_and_flagged(
        self, broker: dict[str, str], admin_client: TestClient
    ) -> None:
        position = admin_client.post(
            f"/accounts/{broker['assets.plan']}/positions",
            json={
                "symbol": "EXFND",
                "quantity": "10",
                "as_of": (today() - timedelta(days=30)).isoformat(),
            },
        ).json()["id"]
        sale = admin_client.post(
            f"/positions/{position}/events",
            json={
                "kind": "sell",
                "occurred_on": (today() - timedelta(days=10)).isoformat(),
                "quantity": "1",
                "price": "40",
            },
        ).json()
        cash = sale["event"]["transaction_id"]
        detail = admin_client.get(f"/transactions/{cash}").json()
        assert detail["securities_trade"] is True and detail["securities_trade_locked"] is True
        resp = admin_client.patch(f"/transactions/{cash}", json={"category": "expenses.fees"})
        assert resp.status_code == 422 and "securities trade" in resp.text


# --------------------------------------------------------------------------
# The balance true-up
# --------------------------------------------------------------------------


def _fetch(
    session: Session, connection: Connection, *, fetched_at: datetime | None = None
) -> RawFetch:
    raw = RawFetch(
        connection_id=connection.id,
        content_hash=f"test-{uuid.uuid4()}",
        payload_enc=crypto.encrypt("{}"),
    )
    if fetched_at is not None:
        raw.fetched_at = fetched_at
    session.add(raw)
    session.flush()
    session.refresh(raw)
    return raw


def _feed_broker(session: Session, *, external_id: str = "BRK-1") -> tuple[Account, Connection]:
    connection = Connection(provider="simplefin", status="active")
    session.add(connection)
    session.flush()
    account = ledger.create_account(
        session,
        name="Feed Broker",
        type_="brokerage",
        path=f"assets.fb_{uuid.uuid4().hex[:8]}",
        external_id=external_id,
        connection_id=connection.id,
        actor_token_id=None,
    )
    assert account.balance_true_up is True  # on by default for feed holdings
    return account, connection


def _deposit(session: Session, account: Account, amount: str, on: date) -> None:
    category = ledger.ensure_category_path(session, "income.other", actor_token_id=None)
    ledger.create_flat_transaction(
        session,
        account_ref=str(account.id),
        category_ref=str(category.id),
        amount=Decimal(amount),
        date=on,
        requested_status=core.Status.POSTED,
        origin=core.Origin.IMPORT,
        token=None,
    )


Held = list[tuple[str, str | None]]  # (symbol, market value), ten shares each


def _snapshot(session: Session, account: Account, raw: RawFetch, holdings: Held) -> None:
    for symbol, value in holdings:
        session.add(
            HoldingsSnapshot(
                account_id=account.id,
                as_of=raw.fetched_at.date(),
                symbol=symbol,
                quantity=Decimal("10"),
                market_value=Decimal(value) if value is not None else None,
                raw_fetch_id=raw.id,
            )
        )
    session.flush()


def _sf(
    balance: str, *, on: date | None, holdings: Held, external_id: str = "BRK-1"
) -> dict[str, Any]:
    """The fetch's account: the same holdings ``_snapshot`` stores."""
    out: dict[str, Any] = {
        "id": external_id,
        "balance": balance,
        "holdings": [
            {"symbol": symbol, "shares": "10", **({"market_value": value} if value else {})}
            for symbol, value in holdings
        ],
    }
    if on is not None:
        out["balance-date"] = int(datetime(on.year, on.month, on.day, 12, tzinfo=UTC).timestamp())
    return out


def _postings(session: Session, account: Account) -> Decimal:
    return ledger.account_balances(session, [account.id]).get(account.id, Decimal(0))


class TestTrueUp:
    def test_fires_once_with_the_difference_and_net_worth_equals_the_report(
        self, clean_db: None, db_session: Session
    ) -> None:
        account, connection = _feed_broker(db_session)
        # A deposit of 1500 whose buy row (1000) the broker never sent.
        _deposit(db_session, account, "1500.00", today() - timedelta(days=3))
        raw = _fetch(db_session, connection)
        _snapshot(db_session, account, raw, [("S0", "1000.00")])
        sf = _sf("1500.00", on=raw.fetched_at.date(), holdings=[("S0", "1000.00")])
        out = trueup.true_up(
            db_session, account=account, sf_account=sf, raw_fetch=raw, stale_days=4
        )
        assert out.status == trueup.ADJUSTED and out.amount == Decimal("-1000.0000")
        assert _postings(db_session, account) + Decimal("1000.00") == Decimal("1500.00")
        tx = db_session.get(Transaction, out.transaction_id)
        assert tx is not None and tx.origin == "system" and tx.status == "posted"
        assert ledger.is_securities_trade(tx.postings)
        assert not ledger.held_by_trade(db_session, tx)  # not a recorded trade's cash
        # Audited and evented.
        assert db_session.execute(
            select(AuditLog).where(AuditLog.action == "account.balance_true_up")
        ).scalar_one()
        assert db_session.execute(
            select(Event).where(Event.type == "account.balance_adjusted")
        ).scalar_one()
        # Never twice for the same fetch.
        again = trueup.true_up(
            db_session, account=account, sf_account=sf, raw_fetch=raw, stale_days=4
        )
        assert again.status == "already_adjusted"
        db_session.commit()
        from purser import networth

        reported = networth.reported_balances(db_session, [account.id])[account.id]
        assert reported.balance == Decimal("1500.00")
        assert reported.method == "postings+holdings"

    def test_a_later_fetch_in_step_posts_nothing(self, clean_db: None, db_session: Session) -> None:
        account, connection = _feed_broker(db_session)
        _deposit(db_session, account, "200.00", today() - timedelta(days=5))
        yesterday = now() - timedelta(days=1)
        first = _fetch(db_session, connection, fetched_at=yesterday)
        _snapshot(db_session, account, first, [("S0", "150.00")])
        trueup.true_up(
            db_session,
            account=account,
            sf_account=_sf("150.00", on=yesterday.date(), holdings=[("S0", "150.00")]),
            raw_fetch=first,
            stale_days=4,
        )
        second = _fetch(db_session, connection)
        _snapshot(db_session, account, second, [("S0", "150.00")])
        out = trueup.true_up(
            db_session,
            account=account,
            sf_account=_sf("150.00", on=today(), holdings=[("S0", "150.00")]),
            raw_fetch=second,
            stale_days=4,
        )
        assert out.status == trueup.IN_STEP

    @pytest.mark.parametrize(
        ("case", "status"),
        [
            ("no_balance_date", "no_balance_date"),
            ("future_balance", "balance_date_mismatch"),
            ("stale", "stale_balance"),
            ("other_content", "holdings_mismatch"),
            ("symbol_dropped", "holdings_mismatch"),
            ("hand_tracked_row", "holdings_mismatch"),
            ("unpriced", "unpriced_holdings"),
            ("partial", "partial_holdings"),
            ("no_holdings", "no_holdings"),
            ("closed", "closed"),
            ("manual", "manual_holdings"),
            ("off", "off"),
        ],
    )
    def test_never_fires_on_untrusted_data(
        self, clean_db: None, db_session: Session, case: str, status: str
    ) -> None:
        account, connection = _feed_broker(db_session)
        _deposit(db_session, account, "900.00", today() - timedelta(days=10))
        raw = _fetch(db_session, connection)
        on = raw.fetched_at.date()
        holdings: Held = [("S0", "400.00")]
        sf = _sf("400.00", on=on, holdings=holdings)
        if case == "no_balance_date":
            sf = _sf("400.00", on=None, holdings=holdings)
        elif case == "future_balance":
            sf = _sf("400.00", on=on + timedelta(days=1), holdings=holdings)
        elif case == "stale":
            sf = _sf("400.00", on=on - timedelta(days=10), holdings=holdings)
        elif case == "other_content":
            # An earlier fetch the same day stored the day's set at another
            # value; this fetch's balance goes with its own value.
            earlier = _fetch(db_session, connection)
            _snapshot(db_session, account, earlier, [("S0", "390.00")])
            holdings = []
        elif case == "symbol_dropped":
            earlier = _fetch(db_session, connection)
            _snapshot(db_session, account, earlier, [("S0", "400.00"), ("S1", "5.00")])
            holdings = []
        elif case == "hand_tracked_row":
            # A manual tombstone ending a hand-tracked position on the day a
            # manual period ended (decision 88) is not what the fetch reports.
            position = Position(account_id=account.id, symbol="S9", opened_on=on)
            db_session.add(position)
            db_session.flush()
            db_session.add(
                HoldingsSnapshot(
                    account_id=account.id,
                    as_of=on,
                    symbol="S9",
                    quantity=Decimal(0),
                    market_value=Decimal(0),
                    source="manual",
                    position_id=position.id,
                )
            )
        elif case == "unpriced":
            holdings = [("S0", None)]
            sf = _sf("400.00", on=on, holdings=holdings)
        elif case == "partial":
            sf = _sf("400.00", on=on, holdings=[("S0", "400.00"), ("S0", "400.00")])
        elif case == "no_holdings":
            sf = _sf("400.00", on=on, holdings=[])
        elif case == "closed":
            account.closed_at = now()
        elif case == "manual":
            ledger.update_account(
                db_session, account_id=account.id, holdings_source="manual", actor_token_id=None
            )
            assert account.balance_true_up is False  # off with hand-tracked holdings
        elif case == "off":
            ledger.update_account(
                db_session, account_id=account.id, balance_true_up=False, actor_token_id=None
            )
        if holdings:
            _snapshot(db_session, account, raw, holdings)
        db_session.flush()
        before = _postings(db_session, account)
        out = trueup.true_up(
            db_session, account=account, sf_account=sf, raw_fetch=raw, stale_days=4
        )
        assert out.status == status
        assert _postings(db_session, account) == before
        assert db_session.execute(select(BrokerageTrueUp)).first() is None

    def test_the_adjustment_is_no_copy_of_trade_cash_and_no_rule_candidate(
        self, clean_db: None, db_session: Session
    ) -> None:
        from purser.holdings import cash as trade_cash

        account, connection = _feed_broker(db_session)
        _deposit(db_session, account, "300.00", today() - timedelta(days=4))
        raw = _fetch(db_session, connection)
        _snapshot(db_session, account, raw, [("S0", "300.00")])
        out = trueup.true_up(
            db_session,
            account=account,
            sf_account=_sf("300.00", on=raw.fetched_at.date(), holdings=[("S0", "300.00")]),
            raw_fetch=raw,
            stale_days=4,
        )
        assert out.status == trueup.ADJUSTED and out.amount == Decimal("-300.0000")
        # The double-count probe (decision 103) never mistakes it for a
        # broker's copy of a sale or buy's cash.
        found = trade_cash.probe(
            db_session,
            account,
            amount=Decimal("-300.00"),
            start=today() - timedelta(days=10),
            end=today() + timedelta(days=1),
        )
        assert found is None
        tx = db_session.get(Transaction, out.transaction_id)
        assert tx is not None
        assert rules._candidate_of(tx) is None  # rules never see it
        assert ledger.trade_cash_lineage_set(db_session) >= {tx.id}  # out of matcher/transfers

    def test_it_can_be_recategorized_by_the_user(self, clean_db: None, db_session: Session) -> None:
        account, connection = _feed_broker(db_session)
        _deposit(db_session, account, "50.00", today() - timedelta(days=4))
        raw = _fetch(db_session, connection)
        _snapshot(db_session, account, raw, [("S0", "40.00")])
        out = trueup.true_up(
            db_session,
            account=account,
            sf_account=_sf("45.00", on=raw.fetched_at.date(), holdings=[("S0", "40.00")]),
            raw_fetch=raw,
            stale_days=4,
        )
        assert out.amount == Decimal("-45.0000")  # cash 45.00 - 40.00 = 5.00; it held 50.00
        fees = ledger.ensure_category_path(db_session, "expenses.fees", actor_token_id=None)
        from purser.models import ApiToken, User

        user = User(email=f"trueup_{uuid.uuid4().hex[:8]}@example.com")
        db_session.add(user)
        db_session.flush()
        token = ApiToken(
            user_id=user.id, name="t", token_hash=uuid.uuid4().hex, scopes=["read", "write"]
        )
        db_session.add(token)
        db_session.flush()
        replacement = ledger.update_transaction(
            db_session, tx_id=out.transaction_id, token=token, category_ref=str(fees.id)
        )
        assert not ledger.is_securities_trade(replacement.postings)


class TestTrueUpMidDay:
    """The importer keeps the day's first holdings set (decision 88), so the
    true-up trusts a stored set by its content, not its fetch (decision 130):
    a deploy, a newly mapped account or the switch turned on mid-day is
    adjusted at the next fetch whose holdings still match."""

    def _import(self, session: Session, account: Account, raw: RawFetch, sf: dict) -> None:
        from purser.feed import importer

        importer._import_holdings(session, raw, sf, account)

    def test_a_later_fetch_the_same_day_adjusts_when_its_holdings_match(
        self, clean_db: None, db_session: Session
    ) -> None:
        account, connection = _feed_broker(db_session)
        _deposit(db_session, account, "1500.00", today() - timedelta(days=3))
        held: Held = [("S0", "1000.00")]
        # The day's first fetch stored the set; no true-up ran for it (the
        # code that does was deployed since).
        first = _fetch(db_session, connection)
        self._import(db_session, account, first, _sf("1500.00", on=today(), holdings=held))
        later = _fetch(db_session, connection)
        sf = _sf("1500.00", on=later.fetched_at.date(), holdings=held)
        self._import(db_session, account, later, sf)  # adds nothing: the day has a set
        assert {r.raw_fetch_id for r in db_session.execute(select(HoldingsSnapshot)).scalars()} == {
            first.id
        }
        out = trueup.true_up(
            db_session, account=account, sf_account=sf, raw_fetch=later, stale_days=4
        )
        assert out.status == trueup.ADJUSTED and out.amount == Decimal("-1000.0000")
        third = _fetch(db_session, connection)
        out = trueup.true_up(
            db_session, account=account, sf_account=sf, raw_fetch=third, stale_days=4
        )
        assert out.status == trueup.IN_STEP
        assert _postings(db_session, account) == Decimal("500.00")

    def test_the_switch_turned_on_mid_day_adjusts_at_the_next_fetch(
        self, clean_db: None, db_session: Session
    ) -> None:
        account, connection = _feed_broker(db_session)
        ledger.update_account(
            db_session, account_id=account.id, balance_true_up=False, actor_token_id=None
        )
        _deposit(db_session, account, "700.00", today() - timedelta(days=2))
        held: Held = [("S0", "300.00")]
        first = _fetch(db_session, connection)
        sf = _sf("700.00", on=first.fetched_at.date(), holdings=held)
        self._import(db_session, account, first, sf)
        out = trueup.true_up(
            db_session, account=account, sf_account=sf, raw_fetch=first, stale_days=4
        )
        assert out.status == "off"
        ledger.update_account(
            db_session, account_id=account.id, balance_true_up=True, actor_token_id=None
        )
        later = _fetch(db_session, connection)
        self._import(db_session, account, later, sf)
        out = trueup.true_up(
            db_session, account=account, sf_account=sf, raw_fetch=later, stale_days=4
        )
        assert out.status == trueup.ADJUSTED and out.amount == Decimal("-300.0000")

    def test_a_symbol_bought_mid_day_joins_the_set_and_a_moved_value_does_not(
        self, clean_db: None, db_session: Session
    ) -> None:
        account, connection = _feed_broker(db_session)
        _deposit(db_session, account, "1000.00", today() - timedelta(days=2))
        first = _fetch(db_session, connection)
        self._import(
            db_session, account, first, _sf("1000.00", on=today(), holdings=[("S0", "400.00")])
        )
        # A later fetch also reports a new position: the importer adds its
        # row from that fetch, so the set mixes two fetches and says exactly
        # what the later one reports.
        later = _fetch(db_session, connection)
        both: Held = [("S0", "400.00"), ("S1", "50.00")]
        sf = _sf("1000.00", on=later.fetched_at.date(), holdings=both)
        self._import(db_session, account, later, sf)
        out = trueup.true_up(
            db_session, account=account, sf_account=sf, raw_fetch=later, stale_days=4
        )
        assert out.status == trueup.ADJUSTED and out.amount == Decimal("-450.0000")
        # The price moved by the next fetch: the day's set keeps 400.00, so
        # this fetch's balance cannot be split against it.
        moved = _fetch(db_session, connection)
        sf = _sf(
            "1010.00", on=moved.fetched_at.date(), holdings=[("S0", "410.00"), ("S1", "50.00")]
        )
        self._import(db_session, account, moved, sf)
        before = _postings(db_session, account)
        out = trueup.true_up(
            db_session, account=account, sf_account=sf, raw_fetch=moved, stale_days=4
        )
        assert out.status == trueup.HOLDINGS_MISMATCH
        assert _postings(db_session, account) == before

    def test_figures_finer_than_the_columns_match_what_was_stored(
        self, clean_db: None, db_session: Session
    ) -> None:
        account, connection = _feed_broker(db_session)
        _deposit(db_session, account, "600.00", today() - timedelta(days=2))
        raw = _fetch(db_session, connection)
        sf: dict[str, Any] = {
            "id": "BRK-1",
            "balance": "600.00",
            "balance-date": int(now().timestamp()),
            "holdings": [
                {"symbol": "S0", "shares": "12.3456789", "market_value": "250.12345"},
                {"description": "PLAN FUND", "shares": "3", "market_value": "49.87645"},
            ],
        }
        self._import(db_session, account, raw, sf)
        stored = {r.symbol: r for r in db_session.execute(select(HoldingsSnapshot)).scalars()}
        # Postgres rounds half away from zero to the columns' scales.
        assert stored["S0"].quantity == Decimal("12.345679")
        assert stored["S0"].market_value == Decimal("250.1235")
        assert stored["PLAN FUND"].market_value == Decimal("49.8765")
        out = trueup.true_up(
            db_session, account=account, sf_account=sf, raw_fetch=raw, stale_days=4
        )
        assert out.status == trueup.ADJUSTED and out.amount == Decimal("-300.0000")


_cents = st.decimals(min_value=Decimal("0.00"), max_value=Decimal("99999.99"), places=2)


@settings(
    max_examples=12, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(deposit=_cents, held=_cents, cash=_cents)
def test_after_a_true_up_postings_plus_holdings_equal_the_report(
    migrated_engine: Engine, deposit: Decimal, held: Decimal, cash: Decimal
) -> None:
    """Whatever the ledger held, the broker's balance less its holdings is the
    account's cash after one true-up, and a second run of the fetch adds
    nothing."""
    from purser import db

    reset_db(migrated_engine)
    with db.get_sessionmaker()() as session:
        account, connection = _feed_broker(session)
        if deposit:
            _deposit(session, account, str(deposit), today() - timedelta(days=2))
        raw = _fetch(session, connection)
        if held:
            _snapshot(session, account, raw, [("S0", str(held))])
            sf = _sf(str(held + cash), on=raw.fetched_at.date(), holdings=[("S0", str(held))])
        else:
            sf = _sf(str(cash), on=raw.fetched_at.date(), holdings=[])
        out = trueup.true_up(session, account=account, sf_account=sf, raw_fetch=raw, stale_days=4)
        if not held:
            assert out.status == "no_holdings"
            return
        assert _postings(session, account) == cash
        assert out.status == (trueup.IN_STEP if deposit == cash else trueup.ADJUSTED)
        again = trueup.true_up(session, account=account, sf_account=sf, raw_fetch=raw, stale_days=4)
        assert again.status in {"already_adjusted", trueup.IN_STEP}
        assert _postings(session, account) == cash
        session.rollback()


class TestTheSwitch:
    def test_patch_toggles_it_on_feed_brokerage_only(
        self, broker: dict[str, str], admin_client: TestClient, db_session: Session
    ) -> None:
        listed = {a["path"]: a for a in admin_client.get("/accounts").json()}
        assert listed[BROKER]["balance_true_up"] is True
        assert listed["assets.plan"]["balance_true_up"] is False
        assert listed["assets.checking"]["balance_true_up"] is False
        off = admin_client.patch(f"/accounts/{broker[BROKER]}", json={"balance_true_up": False})
        assert off.status_code == 200 and off.json()["balance_true_up"] is False
        for path in ("assets.checking", "assets.plan"):
            resp = admin_client.patch(f"/accounts/{broker[path]}", json={"balance_true_up": True})
            assert resp.status_code == 422, resp.text
        audit_after = [
            row.before_after["after"]
            for row in db_session.execute(
                select(AuditLog).where(AuditLog.action == "account.update")
            ).scalars()
        ]
        assert any(a.get("balance_true_up") is False for a in audit_after)
        # Back on the feed, the default comes back.
        admin_client.patch(f"/accounts/{broker[BROKER]}", json={"holdings_source": "manual"})
        connection = Connection(provider="simplefin", status="active")
        db_session.add(connection)
        db_session.flush()
        db_session.execute(
            Account.__table__.update()
            .where(Account.id == uuid.UUID(broker[BROKER]))
            .values(connection_id=connection.id, external_id="X-1")
        )
        db_session.commit()
        back = admin_client.patch(f"/accounts/{broker[BROKER]}", json={"holdings_source": "feed"})
        assert back.status_code == 200, back.text
        assert back.json()["balance_true_up"] is True

    def test_a_write_token_may_switch_it(
        self, broker: dict[str, str], admin_client: TestClient
    ) -> None:
        token = admin_client.post("/tokens", json={"name": "w", "scopes": ["write"]}).json()[
            "token"
        ]
        resp = admin_client.patch(
            f"/accounts/{broker[BROKER]}",
            json={"balance_true_up": False},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.text

    def test_a_read_token_may_neither_mark_nor_switch(
        self, broker: dict[str, str], admin_client: TestClient
    ) -> None:
        token = admin_client.post("/tokens", json={"name": "r", "scopes": ["read"]}).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        row = _post(admin_client, BROKER, "-25.00", payee="YOU BOUGHT")
        resp = admin_client.patch(
            f"/transactions/{row['id']}", json={"securities_trade": True}, headers=headers
        )
        assert resp.status_code == 403
        resp = admin_client.patch(
            f"/accounts/{broker[BROKER]}", json={"balance_true_up": False}, headers=headers
        )
        assert resp.status_code == 403


class TestMcp:
    def test_mark_switch_and_rule_over_mcp(
        self,
        broker: dict[str, str],
        admin_client: TestClient,
        live_server: LiveServer,
        db_session: Session,
    ) -> None:
        token = admin_client.post(
            "/tokens", json={"name": "agent", "scopes": ["read", "write"]}
        ).json()["token"]
        row = _post(admin_client, BROKER, "-40.00", payee="YOU BOUGHT", status="pending")
        checking = _post(admin_client, "assets.checking", "-4.00", payee="SHOP")
        marked, refused, switched, proposed = _mcp_call(
            live_server.url,
            token,
            [
                ("update_transaction", {"transaction_id": row["id"], "securities_trade": True}),
                (
                    "update_transaction",
                    {"transaction_id": checking["id"], "securities_trade": True},
                ),
                ("update_account", {"account": BROKER, "balance_true_up": False}),
                (
                    "propose_rule",
                    {
                        "type": "categorize",
                        "securities_trade": True,
                        "account": BROKER,
                        "match_field": "payee",
                        "match_mode": "prefix",
                        "match_text": "YOU SOLD",
                    },
                ),
            ],
        )
        assert marked["securities_trade"] is True and marked["suggestion_id"]
        assert "error" in refused and "brokerage" in refused["error"]
        assert switched["balance_true_up"] is False
        assert proposed["state"] == "draft" and "Securities trade" in proposed["then"]
        listed = _mcp_call(live_server.url, token, [("list_suggestions", {"kind": "trade_rule"})])[
            0
        ]
        assert listed["returned"] == 1
        assert listed["items"][0]["kind"] == "trade_rule"
        rule = db_session.execute(select(Rule).where(Rule.state == "draft")).scalar_one()
        assert rule.category_id == ledger.securities_account(db_session).id


class TestSeedCategories:
    def test_the_two_income_categories_seed_once_under_an_existing_parent(
        self, admin_client: TestClient, migrated_engine: Engine
    ) -> None:
        from purser.categoryseed import seed_categories
        from purser.core.categories import DEFAULT_CATEGORIES

        specs = {spec.path: spec for spec in DEFAULT_CATEGORIES}
        assert specs["income.gifts"].name == "Gifts received"
        assert "401(k)" in specs["income.employer_contributions"].description
        new = ["income.gifts", "income.employer_contributions"]
        with migrated_engine.begin() as conn:
            # No income root (a person deleted it): nothing seeded as an orphan.
            assert seed_categories(conn, new) == 0
            assert seed_categories(conn, ["income", *new]) == 3
            assert seed_categories(conn, ["income", *new]) == 0  # once
        paths = {c["path"]: c for c in admin_client.get("/categories").json()}
        assert paths["income.employer_contributions"]["name"] == "Employer contributions"
        # A later seed of other paths never brings a deleted one back.
        gifts = paths["income.gifts"]
        assert admin_client.delete(f"/categories/{gifts['id']}").status_code in (200, 204)
        with migrated_engine.begin() as conn:
            assert seed_categories(conn, ["income.employer_contributions"]) == 0
        assert "income.gifts" not in {c["path"] for c in admin_client.get("/categories").json()}
        with pytest.raises(ValueError), migrated_engine.begin() as conn:
            seed_categories(conn, ["income.nope"])


def test_suggestions_kind_check_admits_trade_rule(clean_db: None, db_session: Session) -> None:
    row = Suggestion(
        kind="trade_rule",
        fingerprint=f"fp-{uuid.uuid4()}",
        source="mark",
        draft={},
    )
    db_session.add(row)
    db_session.flush()


class TestAfterASync:
    def test_a_sync_trues_up_a_feed_brokerage_account_once(
        self,
        feed_env: None,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        migrated_engine: Engine,
    ) -> None:
        from tests.test_feed import FeedHarness, account_of, multi_payload_of

        harness = FeedHarness(client, monkeypatch)
        bare = account_of(
            [], account_id="BRK-9", balance="0.00", balance_date=int(now().timestamp())
        )
        harness.use_payload(multi_payload_of([bare]))
        connection_id = harness.connect()
        harness.sync(connection_id)
        resp = client.post(
            f"/connections/{connection_id}/map",
            json={
                "external_id": "BRK-9",
                "create": {"name": "Feed Broker", "type": "brokerage", "path": "assets.fb"},
            },
        )
        assert resp.status_code == 200, resp.text
        # The broker sends a 1500.00 deposit, holdings worth 1000.00 and a
        # balance of 1500.00: the 1000.00 buy row never came.
        deposit = {
            "id": "dep-1",
            "posted": int((now() - timedelta(days=1)).timestamp()),
            "amount": "1500.00",
            "description": "DEPOSIT FROM BANK",
        }
        funded = account_of(
            [deposit], account_id="BRK-9", balance="1500.00", balance_date=int(now().timestamp())
        )
        funded["holdings"] = [
            {"id": "h-1", "symbol": "EXFND", "shares": "10", "market_value": "1000.00"}
        ]
        harness.use_payload(multi_payload_of([funded]))
        result = harness.sync(connection_id)
        (adjusted,) = result["balance_true_ups"]
        assert Decimal(adjusted["amount"]) == Decimal("-1000.00")
        accounts = client.get("/accounts", params={"include_balances": True}).json()
        (fb,) = [a for a in accounts if a["path"] == "assets.fb"]
        assert Decimal(fb["balance"]) == Decimal("1500.00")
        rows = client.get("/transactions", params={"account": "assets.fb"}).json()["items"]
        (label,) = [t for t in rows if t["balance_adjustment"]]
        assert label["payee"] == trueup.PAYEE and label["securities_trade_locked"] is False
        # A second sync the same day keeps the first fetch's holdings, which
        # still say what it reports: in step, recorded on the sync's audit row.
        again = harness.sync(connection_id)
        assert again["balance_true_ups"] == []
        from purser import db

        with db.get_sessionmaker()() as session:
            audits = [
                row.before_after["after"].get("balance_true_up")
                for row in session.execute(
                    select(AuditLog)
                    .where(AuditLog.action == "connection.sync")
                    .order_by(AuditLog.at)
                ).scalars()
            ]
        assert audits[-1] == [{"account_id": fb["id"], "status": trueup.IN_STEP}]
        # Purser's own true-up never needs review (decision 141).
        assert client.get(f"/transactions/{label['id']}").json()["needs_review"] is False

    def test_an_account_mapped_mid_day_is_adjusted_at_the_next_sync(
        self,
        feed_env: None,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        migrated_engine: Engine,
    ) -> None:
        """Mapping re-imports the archived fetch, which stores the day's
        holdings without a true-up; the next sync that day reports the same
        holdings and is adjusted (it waited for the next UTC day before
        decision 130)."""
        from tests.test_feed import FeedHarness, account_of, multi_payload_of

        harness = FeedHarness(client, monkeypatch)
        deposit = {
            "id": "dep-7",
            "posted": int((now() - timedelta(days=1)).timestamp()),
            "amount": "800.00",
            "description": "DEPOSIT FROM BANK",
        }
        funded = account_of(
            [deposit], account_id="BRK-7", balance="800.00", balance_date=int(now().timestamp())
        )
        funded["holdings"] = [
            {"id": "h-7", "symbol": "EXFND", "shares": "4", "market_value": "500.00"}
        ]
        harness.use_payload(multi_payload_of([funded]))
        connection_id = harness.connect()
        harness.sync(connection_id)
        resp = client.post(
            f"/connections/{connection_id}/map",
            json={
                "external_id": "BRK-7",
                "create": {"name": "Feed Broker", "type": "brokerage", "path": "assets.fb7"},
            },
        )
        assert resp.status_code == 200, resp.text
        result = harness.sync(connection_id)
        (adjusted,) = result["balance_true_ups"]
        assert Decimal(adjusted["amount"]) == Decimal("-500.00")
        accounts = client.get("/accounts", params={"include_balances": True}).json()
        (fb,) = [a for a in accounts if a["path"] == "assets.fb7"]
        assert Decimal(fb["balance"]) == Decimal("800.00")
