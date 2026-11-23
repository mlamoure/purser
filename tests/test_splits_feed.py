"""Splits against real imports (issue #590, decision 118): re-importing after a
split or an un-split adds nothing, a pending backing row that later posts
under a new identity never counts twice, the "split like last time?" card,
and the MCP tools.

Synthetic data only. Feed rows are dated relative to today: the pending
lifecycle and the card window count from it.
"""

import uuid
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, text

import purser.db
from tests.conftest import now, today
from tests.test_feed import FeedHarness, _serve_accounts
from tests.test_mcp import LiveServer, _mcp_call

D = Decimal
CHECKING, RETIREMENT = "assets.checking", "assets.retirement"
SALARY, TAXES = "income.salary", "expenses.taxes"
PAYROLL = "EXAMPLE PAYROLL"


def _epoch(days_ago: int) -> int:
    return int((now() - timedelta(days=days_ago)).timestamp())


def _row(tx_id: str, days_ago: int, amount: str, desc: str, *, pending: bool = False) -> dict:
    row: dict[str, Any] = {
        "id": tx_id,
        "posted": _epoch(days_ago),
        "amount": amount,
        "description": desc,
        "payee": desc,
    }
    if pending:
        row["pending"] = True
    return row


@pytest.fixture
def harness(clean_db: None, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> FeedHarness:
    return FeedHarness(client, monkeypatch)


def _categories(client: TestClient) -> None:
    for name, type_, path in (
        ("Salary", "category_income", SALARY),
        ("Taxes", "category_expense", TAXES),
    ):
        resp = client.post("/categories", json={"name": name, "type": type_, "path": path})
        assert resp.status_code == 201, resp.text


def _map(harness: FeedHarness, cid: str, external_id: str, path: str, type_: str) -> None:
    resp = harness.client.post(
        f"/connections/{cid}/map",
        json={
            "external_id": external_id,
            "create": {"name": external_id, "type": type_, "path": path},
        },
    )
    assert resp.status_code == 200, resp.text


def _connect(harness: FeedHarness, accounts: dict[str, list[dict]]) -> str:
    _serve_accounts(harness, accounts)
    cid = harness.connect()
    _categories(harness.client)
    _map(harness, cid, "Demo Checking", CHECKING, "checking")
    _map(harness, cid, "Demo 401k", RETIREMENT, "savings")
    return cid


def _live(client: TestClient, path: str) -> list[dict[str, Any]]:
    return list(
        client.get(
            "/transactions",
            params={"account": path, "status": ["pending", "posted"], "limit": 200},
        ).json()["items"]
    )


def _balance(client: TestClient, path: str) -> Decimal:
    return sum((D(r["amount"]) for r in _live(client, path)), D(0))


def _count(engine: Engine, sql: str, **params: Any) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(sql), params).scalar())


def _lines(backing: list[str], amount: str = "-600.00", gross: str = "4000.00") -> list[dict]:
    return [
        {"category": SALARY, "amount": gross},
        {"category": TAXES, "amount": "-900.00"},
        {"category": f"[{RETIREMENT}]", "amount": amount, "backing_ids": backing},
    ]


class TestReimport:
    def test_split_and_unsplit_then_resync_adds_nothing(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        accounts = {
            "Demo Checking": [_row("pay", 5, "2500.00", PAYROLL)],
            "Demo 401k": [
                _row("fa", 4, "250.00", "FUND A", pending=True),
                _row("fb", 4, "350.00", "FUND B", pending=True),
            ],
        }
        cid = _connect(harness, accounts)
        harness.sync(cid)
        client = harness.client
        (pay,) = _live(client, CHECKING)
        funds = [r["id"] for r in _live(client, RETIREMENT)]
        rows_before = _count(migrated_engine, "SELECT COUNT(*) FROM transactions")
        resp = client.put(f"/transactions/{pay['id']}/split", json={"lines": _lines(funds)})
        assert resp.status_code == 200, resp.text
        head = resp.json()["transaction_id"]
        assert _balance(client, RETIREMENT) == D("600.00")

        stats = harness.sync(cid)
        assert stats["imported"] == 0, stats
        assert _balance(client, RETIREMENT) == D("600.00")
        assert _balance(client, CHECKING) == D("2500.00")

        assert client.delete(f"/transactions/{head}/split").status_code == 200
        stats = harness.sync(cid)
        assert stats["imported"] == 0, stats
        assert _balance(client, RETIREMENT) == D("600.00")
        assert _balance(client, CHECKING) == D("2500.00")
        # split: reversal + replacement; unsplit: reversal + replacement + 2 clones
        assert _count(migrated_engine, "SELECT COUNT(*) FROM transactions") == rows_before + 6

    def test_a_pending_backing_row_that_posts_under_a_new_identity_counts_once(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        pay = [_row("pay", 5, "2500.00", PAYROLL)]
        cid = _connect(
            harness,
            {"Demo Checking": pay, "Demo 401k": [_row("fa", 4, "600.00", "CONTRIB", pending=True)]},
        )
        harness.sync(cid)
        client = harness.client
        (pay_row,) = _live(client, CHECKING)
        (fund,) = _live(client, RETIREMENT)
        resp = client.put(
            f"/transactions/{pay_row['id']}/split", json={"lines": _lines([fund["id"]])}
        )
        head = resp.json()["transaction_id"]
        # The bank posts it with another date and text: a new identity.
        _serve_accounts(
            harness,
            {"Demo Checking": pay, "Demo 401k": [_row("fa2", 3, "600.00", "CONTRIB POSTED")]},
        )
        harness.sync(cid)
        assert _balance(client, RETIREMENT) == D("600.00")  # not 1200
        with migrated_engine.connect() as conn:
            posted_id, superseded = conn.execute(
                text(
                    "SELECT t.id, t.superseded_by FROM transactions t JOIN postings p "
                    "ON p.transaction_id = t.id JOIN accounts a ON a.id = p.account_id "
                    "WHERE a.path = :p AND t.origin = 'import' AND t.status = 'void' "
                    "AND t.id <> :fund"
                ),
                {"p": RETIREMENT, "fund": fund["id"]},
            ).one()
            pending_next = conn.execute(
                text("SELECT superseded_by FROM transactions WHERE id = :id"), {"id": fund["id"]}
            ).scalar()
        assert str(superseded) == head and pending_next == posted_id
        detail = client.get(f"/transactions/{head}").json()
        assert detail["split"]["lines"][2]["backing_ids"] == [str(posted_id)]
        # Un-split brings back the posted row, not the pending.
        body = client.delete(f"/transactions/{head}/split").json()
        (clone,) = body["restored_ids"]
        assert client.get(f"/transactions/{clone}").json()["status"] == "posted"
        assert _balance(client, RETIREMENT) == D("600.00")

    def test_a_backing_row_posted_under_the_same_identity_is_skipped(
        self, harness: FeedHarness
    ) -> None:
        pay = [_row("pay", 5, "2500.00", PAYROLL)]
        fund = _row("fa", 4, "600.00", "CONTRIB", pending=True)
        cid = _connect(harness, {"Demo Checking": pay, "Demo 401k": [fund]})
        harness.sync(cid)
        client = harness.client
        (pay_row,) = _live(client, CHECKING)
        (fund_row,) = _live(client, RETIREMENT)
        client.put(f"/transactions/{pay_row['id']}/split", json={"lines": _lines([fund_row["id"]])})
        posted = {k: v for k, v in fund.items() if k != "pending"}
        _serve_accounts(harness, {"Demo Checking": pay, "Demo 401k": [posted]})
        stats = harness.sync(cid)
        assert stats["imported"] == 0, stats
        assert _balance(client, RETIREMENT) == D("600.00")


def _qif(tag: int, pay: Decimal, contribution: Decimal) -> str:
    day = 1 + tag % 27
    return (
        "!Account\nNChecking\nTBank\n^\n!Type:Bank\n"
        f"D3/{day}'26\nT{pay}\nPSynthetic Payroll {tag}\nC*\n^\n"
        "!Account\nNRetirement\nTBank\n^\n!Type:Bank\n"
        f"D3/{day}'26\nT{contribution}\nPSynthetic Contribution {tag}\nC*\n^\n"
    )


class TestReimportProperty:
    @settings(
        max_examples=8,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    )
    @given(
        pay=st.integers(min_value=200_00, max_value=900_000).map(lambda c: D(c) / 100),
        contribution=st.integers(min_value=1_00, max_value=100_00).map(lambda c: D(c) / 100),
        unsplit=st.booleans(),
    )
    def test_reimport_is_idempotent_after_split_and_unsplit(
        self,
        admin_client: TestClient,
        pay: Decimal,
        contribution: Decimal,
        unsplit: bool,
    ) -> None:
        client = admin_client
        for name, type_, path in (
            ("Checking", "checking", CHECKING),
            ("Ret", "savings", RETIREMENT),
        ):
            client.post("/accounts", json={"name": name, "type": type_, "path": path})
        for name, type_, path in (("Salary", "category_income", SALARY),):
            client.post("/categories", json={"name": name, "type": type_, "path": path})
        tag = uuid.uuid4().int % 10_000
        body = {
            "content": _qif(tag, pay, contribution),
            "account_map": {"Checking": CHECKING, "Retirement": RETIREMENT},
        }
        assert client.post("/import/qif", json=body).status_code == 200
        payee = f"Synthetic Payroll {tag}"
        (pay_row,) = [r for r in _live(client, CHECKING) if r["payee"] == payee]
        (contrib,) = [
            r for r in _live(client, RETIREMENT) if r["payee"] == f"Synthetic Contribution {tag}"
        ]
        balances = (_balance(client, CHECKING), _balance(client, RETIREMENT))
        lines = [
            {"category": SALARY, "amount": str(pay + contribution)},
            {
                "category": f"[{RETIREMENT}]",
                "amount": str(-contribution),
                "backing_ids": [contrib["id"]],
            },
        ]
        resp = client.put(f"/transactions/{pay_row['id']}/split", json={"lines": lines})
        assert resp.status_code == 200, resp.text
        if unsplit:
            head = resp.json()["transaction_id"]
            assert client.delete(f"/transactions/{head}/split").status_code == 200
        again = client.post("/import/qif", json=body).json()
        assert again["imported"] == 0, again
        assert (_balance(client, CHECKING), _balance(client, RETIREMENT)) == balances


def _cards(client: TestClient, status: str = "all") -> list[dict[str, Any]]:
    resp = client.get(
        "/suggestions", params={"kind": "split", "status": status, "include_snoozed": True}
    )
    assert resp.status_code == 200, resp.text
    return list(resp.json())


class TestSplitLikeLastTime:
    def _first_split(self, harness: FeedHarness, extra: list[dict] | None = None) -> str:
        cid = _connect(
            harness,
            {
                "Demo Checking": [_row("pay1", 20, "2500.00", PAYROLL)],
                "Demo 401k": [],
            },
        )
        harness.sync(cid)
        (pay,) = _live(harness.client, CHECKING)
        resp = harness.client.put(f"/transactions/{pay['id']}/split", json={"lines": _lines([])})
        assert resp.status_code == 200, resp.text
        return cid

    def test_a_new_paycheck_gets_a_card_and_accepting_applies_it(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        cid = self._first_split(harness)
        client = harness.client
        assert _cards(client) == []
        _serve_accounts(
            harness,
            {
                "Demo Checking": [
                    _row("pay1", 20, "2500.00", PAYROLL),
                    _row("pay2", 6, "2550.00", PAYROLL),
                ],
                "Demo 401k": [_row("c2", 5, "600.00", "CONTRIB")],
            },
        )
        harness.sync(cid)
        (card,) = _cards(client, "open")
        draft = card["draft"]
        assert D(draft["amount"]) == D("2550.00")
        lines = {line["target"]: line for line in draft["lines"]}
        # The 50.00 difference goes to the largest category line.
        assert D(lines[SALARY]["amount"]) == D("4050.00")
        assert D(lines[TAXES]["amount"]) == D("-900.00")
        (contribution,) = [r for r in _live(client, RETIREMENT) if not r["split_account_path"]]
        assert lines[f"[{RETIREMENT}]"]["backing_ids"] == [contribution["id"]]
        assert "like last time" in card["headline"]
        # Never automatic: the row is still flat.
        assert client.get(f"/transactions/{draft['transaction_id']}").json()["split_lines"] == 0

        resp = client.post(f"/suggestions/{card['id']}/decide", json={"decision": "accept"})
        assert resp.status_code == 200, resp.text
        split_id = resp.json()["split_transaction_id"]
        assert client.get(f"/transactions/{split_id}").json()["split_lines"] == 3
        assert _balance(client, RETIREMENT) == D("1200.00")  # two paychecks, each counted once
        (decided,) = _cards(client)
        assert decided["status"] == "accepted" and decided["result_entity_id"] == split_id
        # A later sync does not propose it again.
        harness.sync(cid)
        assert len(_cards(client)) == 1

    def test_reject_is_remembered_and_counterparty_scope_stops_the_payee(
        self, harness: FeedHarness
    ) -> None:
        cid = self._first_split(harness)
        client = harness.client
        rows = [_row("pay1", 20, "2500.00", PAYROLL), _row("pay2", 6, "2500.00", PAYROLL)]
        _serve_accounts(harness, {"Demo Checking": rows, "Demo 401k": []})
        harness.sync(cid)
        (card,) = _cards(client, "open")
        resp = client.post(
            f"/suggestions/{card['id']}/decide",
            json={"decision": "reject", "reject_scope": "counterparty"},
        )
        assert resp.status_code == 200, resp.text
        rows.append(_row("pay3", 2, "2500.00", PAYROLL))
        _serve_accounts(harness, {"Demo Checking": rows, "Demo 401k": []})
        harness.sync(cid)
        assert [c["status"] for c in _cards(client)] == ["rejected"]

    def test_the_payees_alias_group_counts_as_the_payee(self, harness: FeedHarness) -> None:
        """A bank that words the payroll another way gets the card once a
        rename aliased the two payees (decision 46), and a rejection of the
        payee holds for the whole group (decision 130)."""
        from purser import rules as rules_service

        cid = self._first_split(harness)
        client = harness.client
        reworded = "EXAMPLE PAYROLL DIR DEP"
        rows = [_row("pay1", 20, "2500.00", PAYROLL), _row("pay2", 6, "2500.00", reworded)]
        _serve_accounts(harness, {"Demo Checking": rows, "Demo 401k": []})
        harness.sync(cid)
        assert _cards(client) == []  # another payee, not aliased yet

        def payee_of(description: str) -> uuid.UUID:
            (row,) = [r for r in _live(client, CHECKING) if r["description"] == description]
            with purser.db.get_sessionmaker()() as session:
                return uuid.UUID(
                    str(
                        session.execute(
                            text("SELECT payee_id FROM transactions WHERE id = :id"),
                            {"id": row["id"]},
                        ).scalar_one()
                    )
                )

        # What a rename's apply-history does: alias the reworded payee onto
        # the one the split was made under.
        with purser.db.get_sessionmaker()() as session:
            rules_service.alias_payee(
                session, source_id=payee_of(reworded), target_id=payee_of(PAYROLL), token=None
            )
            session.commit()
        harness.sync(cid)
        (card,) = _cards(client, "open")
        assert card["draft"]["transaction_id"] == next(
            r["id"] for r in _live(client, CHECKING) if r["description"] == reworded
        )
        resp = client.post(
            f"/suggestions/{card['id']}/decide",
            json={"decision": "reject", "reject_scope": "counterparty"},
        )
        assert resp.status_code == 200, resp.text
        # Rejected under the reworded payee, it stops the original wording too.
        rows.append(_row("pay3", 2, "2500.00", PAYROLL))
        _serve_accounts(harness, {"Demo Checking": rows, "Demo 401k": []})
        harness.sync(cid)
        assert [c["status"] for c in _cards(client)] == ["rejected"]

    def test_splitting_the_row_by_hand_withdraws_the_card(self, harness: FeedHarness) -> None:
        cid = self._first_split(harness)
        client = harness.client
        rows = [_row("pay1", 20, "2500.00", PAYROLL), _row("pay2", 6, "2500.00", PAYROLL)]
        _serve_accounts(harness, {"Demo Checking": rows, "Demo 401k": []})
        harness.sync(cid)
        (card,) = _cards(client, "open")
        row_id = card["draft"]["transaction_id"]
        head = client.get(f"/transactions/{row_id}").json()
        target = head["id"] if head["status"] == "posted" else row_id
        resp = client.put(
            f"/transactions/{target}/split",
            json={
                "lines": [
                    {"category": SALARY, "amount": "3000.00"},
                    {"category": TAXES, "amount": "-500.00"},
                ]
            },
        )
        assert resp.status_code == 200, resp.text
        (closed,) = _cards(client)
        assert closed["status"] == "withdrawn"

    def test_edit_then_accept_applies_edited_lines(self, harness: FeedHarness) -> None:
        cid = self._first_split(harness)
        client = harness.client
        rows = [_row("pay1", 20, "2500.00", PAYROLL), _row("pay2", 6, "2500.00", PAYROLL)]
        _serve_accounts(harness, {"Demo Checking": rows, "Demo 401k": []})
        harness.sync(cid)
        (card,) = _cards(client, "open")
        edited = [
            {"target": SALARY, "amount": "3100.00"},
            {"target": TAXES, "amount": "-600.00"},
        ]
        resp = client.post(
            f"/suggestions/{card['id']}/decide",
            json={"decision": "edit_then_accept", "edits": {"lines": edited}},
        )
        assert resp.status_code == 200, resp.text
        split_id = resp.json()["split_transaction_id"]
        assert client.get(f"/transactions/{split_id}").json()["split_lines"] == 2

    def test_a_qif_imported_last_split_proposes_nothing(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        from purser import ledger as ledger_service
        from purser.core import ledger as core

        cid = _connect(harness, {"Demo Checking": [], "Demo 401k": []})
        harness.sync(cid)
        with purser.db.get_sessionmaker()() as session:
            salary = ledger_service.resolve_account(session, SALARY)
            taxes = ledger_service.resolve_account(session, TAXES)
            ledger_service.create_split_transaction(
                session,
                account_ref=CHECKING,
                amount=D("2500.00"),
                lines=[
                    core.SplitLine(salary.id, D("3400.00")),
                    core.SplitLine(taxes.id, D("-900")),
                ],
                date=today() - timedelta(days=20),
                payee_name=PAYROLL,
                requested_status=core.Status.POSTED,
                origin=core.Origin.IMPORT,
                token=None,
            )
            session.commit()
        _serve_accounts(
            harness, {"Demo Checking": [_row("pay2", 6, "2500.00", PAYROLL)], "Demo 401k": []}
        )
        harness.sync(cid)
        assert _cards(harness.client) == []


@pytest.fixture(scope="module")
def live_server(migrated_engine: Engine) -> Iterator[LiveServer]:
    server = LiveServer()
    server.start()
    yield server
    server.stop()


class TestMcp:
    def test_split_and_unsplit_tools(
        self, harness: FeedHarness, live_server: LiveServer, migrated_engine: Engine
    ) -> None:
        cid = _connect(
            harness,
            {
                "Demo Checking": [_row("pay", 5, "2500.00", PAYROLL)],
                "Demo 401k": [_row("c", 4, "600.00", "CONTRIB")],
            },
        )
        harness.sync(cid)
        client = harness.client
        (pay,) = _live(client, CHECKING)
        (contribution,) = _live(client, RETIREMENT)
        operator = client.post("/tokens", json={"name": "operator", "scopes": ["read", "write"]})
        reader = client.post("/tokens", json={"name": "reader", "scopes": ["read"]})
        lines = [
            {"category": SALARY, "amount": "4000.00"},
            {"category": TAXES, "amount": "-900.00", "memo": "withholding"},
            {"category": f"[{RETIREMENT}]", "amount": "-600.00"},
        ]
        (denied,) = _mcp_call(
            live_server.url,
            reader.json()["token"],
            [("split_transaction", {"transaction_id": pay["id"], "lines": lines})],
        )
        assert "error" in denied
        dry, split = _mcp_call(
            live_server.url,
            operator.json()["token"],
            [
                (
                    "split_transaction",
                    {"transaction_id": pay["id"], "lines": lines, "dry_run": True},
                ),
                ("split_transaction", {"transaction_id": pay["id"], "lines": lines}),
            ],
        )
        assert dry["dry_run"] is True
        # The unbacked account line lists the exact candidate to back it.
        assert dry["lines"][2]["candidates"][0]["id"] == contribution["id"]
        head = split["transaction_id"]
        backed = [*lines[:2], {**lines[2], "backing_ids": [contribution["id"]]}]
        resplit, _detail, unsplit = _mcp_call(
            live_server.url,
            operator.json()["token"],
            [
                ("split_transaction", {"transaction_id": head, "lines": backed}),
                ("get_transaction", {"transaction_id": head}),
                ("unsplit_transaction", {"transaction_id": head}),
            ],
        )
        assert resplit["backing_added"] == [contribution["id"]]
        assert "error" in unsplit  # the old head was corrected by the re-split
        (detail2, unsplit2) = _mcp_call(
            live_server.url,
            operator.json()["token"],
            [
                ("get_transaction", {"transaction_id": resplit["transaction_id"]}),
                ("unsplit_transaction", {"transaction_id": resplit["transaction_id"]}),
            ],
        )
        assert detail2["split"]["account"] == CHECKING
        assert detail2["split"]["lines"][2] == {
            "category": f"[{RETIREMENT}]",
            "amount": "-600.0000",
            "backing_ids": [contribution["id"]],
        }
        assert len(unsplit2["restored_ids"]) == 1
        assert _balance(client, RETIREMENT) == D("600.00")
        assert (
            _count(
                migrated_engine,
                "SELECT COUNT(*) FROM audit_log WHERE action = 'transaction.split'",
            )
            == 2
        )
