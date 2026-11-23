"""Editable split transactions — category and [Account] lines, the Quicken
model (issue #590, DESIGN §3.3, decision 118).

A split is the row being split (its real leg keeps the bank's exact amount)
plus two or more lines, each a category or another real account; the lines
sum to the row's amount. An [Account] line's leg may be backed by that
account's own bank rows, merged in with lineage so nothing counts twice. A
split carries the split marker and is never a transfer. Un-split restores the
row and every backing row exactly.

Synthetic data only; fixed dates unless a test needs "recent".
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

import purser.db
from tests.test_matching import _imported

D = Decimal
ON = date(2026, 7, 15)
CHECKING, SAVINGS, RETIREMENT, LOAN = (
    "assets.checking",
    "assets.savings",
    "assets.retirement",
    "liabilities.carloan",
)
SALARY, TAXES, GROCERIES, HOUSEHOLD, INTEREST = (
    "income.salary",
    "expenses.taxes",
    "expenses.groceries",
    "expenses.household",
    "expenses.interest",
)
PAYCHECK_LINES = [
    {"category": SALARY, "amount": "4000.00"},
    {"category": TAXES, "amount": "-900.00", "memo": "withholding"},
    {"category": f"[{RETIREMENT}]", "amount": "-600.00"},
]


@pytest.fixture
def seeded(admin_client: TestClient) -> None:
    for name, type_, path in (
        ("Everyday", "checking", CHECKING),
        ("Reserve", "savings", SAVINGS),
        ("Example 401(k)", "brokerage", RETIREMENT),
        ("Car Loan", "loan", LOAN),
    ):
        resp = admin_client.post("/accounts", json={"name": name, "type": type_, "path": path})
        assert resp.status_code == 201, resp.text
    for name, type_, path in (
        ("Salary", "category_income", SALARY),
        ("Taxes", "category_expense", TAXES),
        ("Groceries", "category_expense", GROCERIES),
        ("Household", "category_expense", HOUSEHOLD),
        ("Interest", "category_expense", INTEREST),
    ):
        resp = admin_client.post("/categories", json={"name": name, "type": type_, "path": path})
        assert resp.status_code == 201, resp.text


def _split(client: TestClient, tx_id: str, lines: list[dict[str, Any]], **kw: Any) -> Any:
    return client.put(f"/transactions/{tx_id}/split", json={"lines": lines, **kw})


def _ok(resp: Any) -> dict[str, Any]:
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


def _balances(client: TestClient) -> dict[str, Decimal]:
    accounts = client.get("/accounts", params={"include_balances": True}).json()
    return {a["path"]: D(a["balance"]) for a in accounts if a.get("balance") is not None}


def _row(engine: Engine, tx_id: str) -> dict[str, Any]:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT status, superseded_by, content_hash, split_account_id, origin, "
                "reverses_id FROM transactions WHERE id = :id"
            ),
            {"id": tx_id},
        ).one()
    return dict(row._mapping)


def _postings(engine: Engine, tx_id: str) -> list[tuple[str, Decimal]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT a.path::text, p.amount FROM postings p JOIN accounts a "
                "ON a.id = p.account_id WHERE p.transaction_id = :id"
            ),
            {"id": tx_id},
        ).all()
    return sorted((path, D(amount)) for path, amount in rows)


def _paycheck(client: TestClient, engine: Engine, **kw: Any) -> str:
    return _imported(client, engine, CHECKING, "2500.00", ON, payee="EXAMPLE PAYROLL", **kw)


def _contribution(client: TestClient, engine: Engine, amount: str = "600.00", **kw: Any) -> str:
    return _imported(
        client, engine, RETIREMENT, amount, ON + timedelta(days=1), payee="CONTRIBUTION", **kw
    )


def _review(engine: Engine, tx_id: str) -> dict[str, Any] | None:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT reviewed_at, reviewed_by, via, carried_from FROM transaction_reviews "
                "WHERE transaction_id = :id"
            ),
            {"id": tx_id},
        ).one_or_none()
    return dict(row._mapping) if row is not None else None


def _events(engine: Engine, type_: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(text("SELECT COUNT(*) FROM events WHERE type = :t"), {"t": type_}).scalar()
        )


def _audit(engine: Engine, action: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT COUNT(*) FROM audit_log WHERE action = :a"), {"a": action}
            ).scalar()
        )


class TestSplitPostedRow:
    def test_reversal_and_replacement_keep_the_bank_amount_and_identity(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        tx_id = _paycheck(client, migrated_engine, content_hash="h-paycheck")
        body = _ok(_split(client, tx_id, PAYCHECK_LINES))
        head = body["transaction_id"]
        assert head != tx_id and body["replaces_id"] == tx_id
        assert body["status"] == "posted"
        # The original stays posted, corrected, and keeps the feed identity.
        original = _row(migrated_engine, tx_id)
        assert original["status"] == "posted" and original["content_hash"] == "h-paycheck"
        replacement = _row(migrated_engine, head)
        assert replacement["split_account_id"] is not None
        assert _postings(migrated_engine, head) == sorted(
            [
                (CHECKING, D("2500.0000")),
                (SALARY, D("-4000.0000")),
                (TAXES, D("900.0000")),
                (RETIREMENT, D("600.0000")),
            ]
        )
        detail = client.get(f"/transactions/{head}").json()
        assert detail["is_transfer"] is False
        assert detail["category_path"] is None
        assert detail["split_lines"] == 3 and detail["split_account_path"] == CHECKING
        assert D(detail["amount"]) == D("2500.00")
        lines = detail["split"]["lines"]
        assert [(line["target"], D(line["amount"])) for line in lines] == [
            (SALARY, D("4000.00")),
            (TAXES, D("-900.00")),
            (f"[{RETIREMENT}]", D("-600.00")),
        ]
        assert lines[1]["memo"] == "withholding"
        assert lines[2]["kind"] == "account" and lines[2]["backing_ids"] == []
        balances = _balances(client)
        assert balances[CHECKING] == D("2500.00")
        assert balances[RETIREMENT] == D("600.00")  # Purser's own leg, unbacked
        assert _audit(migrated_engine, "transaction.split") == 1
        assert _events(migrated_engine, "transaction.split") == 1

    def test_seen_from_the_line_account_the_row_shows_that_leg(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        head = _ok(_split(client, _paycheck(client, migrated_engine), PAYCHECK_LINES))[
            "transaction_id"
        ]
        items = client.get("/transactions", params={"account": RETIREMENT}).json()["items"]
        (row,) = [i for i in items if i["id"] == head]
        assert D(row["amount"]) == D("600.00")
        assert row["split_account_path"] == CHECKING and row["is_transfer"] is False
        assert row["has_linked_originals"] is False

    def test_a_loan_payment_shaped_split_is_not_a_transfer(
        self, seeded: None, client: TestClient, migrated_engine: Engine, db_session: Session
    ) -> None:
        from purser import transfers
        from purser.core import ledger as core
        from purser.models import Transaction

        out_id = _imported(client, migrated_engine, CHECKING, "-612.40", ON, payee="LOAN")
        head = _ok(
            _split(
                client,
                out_id,
                [
                    {"category": f"[{LOAN}]", "amount": "-540.15"},
                    {"category": INTEREST, "amount": "-72.25"},
                ],
            )
        )["transaction_id"]
        tx = db_session.get(Transaction, uuid.UUID(head))
        assert tx is not None
        legs = [core.Leg(p.account_id, D(p.amount), p.account.is_virtual) for p in tx.postings]
        # Decision 62's exact shape — the marker alone keeps it a split.
        assert core.transfer_shape(legs) is core.TransferShape.SPLIT
        assert transfers.transfer_shape(tx) is None and not transfers.is_transfer(tx)
        assert client.get(f"/transactions/{head}").json()["is_transfer"] is False
        resp = client.post(f"/transactions/{head}/unlink-transfer")
        assert resp.status_code == 422
        with migrated_engine.connect() as conn:
            view = conn.execute(
                text(
                    "SELECT is_transfer, category_path, account_path, amount "
                    "FROM readmodel.v2_transactions_flat WHERE id = :id"
                ),
                {"id": head},
            ).one()
        assert view.is_transfer is False and view.category_path is None
        assert view.account_path == CHECKING and D(view.amount) == D("-612.40")

    def test_dry_run_writes_nothing(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        tx_id = _paycheck(client, migrated_engine)
        backer = _contribution(client, migrated_engine)
        lines = [*PAYCHECK_LINES[:2], {**PAYCHECK_LINES[2], "backing_ids": [backer]}]
        body = _ok(_split(client, tx_id, lines, dry_run=True))
        assert body["dry_run"] is True and body["backing_added"] == [backer]
        assert _row(migrated_engine, tx_id)["split_account_id"] is None
        assert _row(migrated_engine, backer)["status"] == "posted"
        assert _audit(migrated_engine, "transaction.split") == 0

    def test_write_scope_is_required(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        tx_id = _paycheck(client, migrated_engine)
        reader = client.post("/tokens", json={"name": "reader", "scopes": ["read"]})
        headers = {"Authorization": f"Bearer {reader.json()['token']}"}
        resp = client.put(
            f"/transactions/{tx_id}/split", json={"lines": PAYCHECK_LINES}, headers=headers
        )
        assert resp.status_code == 403
        resp = client.delete(f"/transactions/{tx_id}/split", headers=headers)
        assert resp.status_code == 403
        resp = client.get(
            f"/transactions/{tx_id}/split/candidates",
            params={"account": RETIREMENT},
            headers=headers,
        )
        assert resp.status_code == 200


class TestBacking:
    def test_a_backed_line_counts_the_money_once(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        tx_id = _paycheck(client, migrated_engine)
        backer = _contribution(client, migrated_engine, content_hash="h-401k")
        assert _balances(client)[RETIREMENT] == D("600.00")
        lines = [*PAYCHECK_LINES[:2], {**PAYCHECK_LINES[2], "backing_ids": [backer]}]
        head = _ok(_split(client, tx_id, lines))["transaction_id"]
        assert _balances(client)[RETIREMENT] == D("600.00")  # not 1200
        merged = _row(migrated_engine, backer)
        assert merged["status"] == "void" and str(merged["superseded_by"]) == head
        detail = client.get(f"/transactions/{head}").json()
        assert detail["split"]["lines"][2]["backing_ids"] == [backer]
        assert detail["has_linked_originals"] is False

    def test_several_pending_rows_back_one_line(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        tx_id = _paycheck(client, migrated_engine)
        funds = [
            _contribution(client, migrated_engine, a, status="pending", content_hash=f"h-fund-{a}")
            for a in ("250.00", "350.00")
        ]
        lines = [*PAYCHECK_LINES[:2], {**PAYCHECK_LINES[2], "backing_ids": funds}]
        head = _ok(_split(client, tx_id, lines))["transaction_id"]
        assert _balances(client)[RETIREMENT] == D("600.00")
        assert all(_row(migrated_engine, f)["status"] == "void" for f in funds)
        body = _ok(client.delete(f"/transactions/{head}/split"))
        clones = body["restored_ids"]
        assert len(clones) == 2
        restored = sorted(
            (_row(migrated_engine, c)["status"], _row(migrated_engine, c)["content_hash"])
            for c in clones
        )
        assert restored == [("pending", "h-fund-250.00"), ("pending", "h-fund-350.00")]
        assert _balances(client)[RETIREMENT] == D("600.00")

    def test_candidates_list_exact_rows_near_the_date(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        tx_id = _paycheck(client, migrated_engine)
        exact = _contribution(client, migrated_engine)
        other = _contribution(client, migrated_engine, "75.00")
        _imported(client, migrated_engine, RETIREMENT, "600.00", ON + timedelta(days=30))
        found = client.get(
            f"/transactions/{tx_id}/split/candidates",
            params={"account": RETIREMENT, "amount": "600.00"},
        ).json()
        ids = [c["id"] for c in found["candidates"]]
        assert ids == [exact, other]  # nearest amount first; the far row is outside 7 days
        assert found["candidates"][0]["exact"] is True and found["window_days"] == 7

    @pytest.mark.parametrize(
        ("case", "message"),
        [
            ("wrong_account", "not on assets.retirement"),
            ("sum", "sum to"),
            ("twice", "back one line only"),
            ("category_line", "only an [Account] line is backed"),
            ("matched", "settles a scheduled transaction"),
            ("manual", "not a bank row"),
            ("split", "is a split"),
        ],
    )
    def test_refusals(
        self,
        seeded: None,
        client: TestClient,
        migrated_engine: Engine,
        case: str,
        message: str,
    ) -> None:
        tx_id = _paycheck(client, migrated_engine)
        backer = _contribution(client, migrated_engine)
        lines = [dict(line) for line in PAYCHECK_LINES]
        if case == "wrong_account":
            lines[2]["backing_ids"] = [
                _imported(client, migrated_engine, SAVINGS, "600.00", ON, payee="X")
            ]
        elif case == "sum":
            lines[2]["backing_ids"] = [_contribution(client, migrated_engine, "599.00")]
        elif case == "twice":
            lines = [
                {"category": SALARY, "amount": "4000.00"},
                {"category": TAXES, "amount": "-2300.00"},
                {"category": f"[{RETIREMENT}]", "amount": "-600.00", "backing_ids": [backer]},
                {"category": f"[{RETIREMENT}]", "amount": "-600.00", "backing_ids": [backer]},
                {"category": GROCERIES, "amount": "2000.00"},
            ]
        elif case == "category_line":
            lines[1]["backing_ids"] = [backer]
        elif case == "matched":
            expected = client.post(
                "/transactions",
                json={
                    "account": RETIREMENT,
                    "category": SALARY,
                    "amount": "600.00",
                    "date": (ON + timedelta(days=1)).isoformat(),
                    "payee": "CONTRIBUTION",
                    "status": "expected",
                },
            ).json()["id"]
            assert client.post(
                "/matching/match", json={"expected_id": expected, "posted_id": backer}
            ).status_code in (200, 201)
            lines[2]["backing_ids"] = [backer]
        elif case == "manual":
            manual = client.post(
                "/transactions",
                json={
                    "account": RETIREMENT,
                    "category": SALARY,
                    "amount": "600.00",
                    "date": ON.isoformat(),
                    "status": "posted",
                },
            ).json()["id"]
            lines[2]["backing_ids"] = [manual]
        elif case == "split":
            other = _imported(client, migrated_engine, RETIREMENT, "600.00", ON, payee="Y")
            other_head = _ok(
                _split(
                    client,
                    other,
                    [
                        {"category": SALARY, "amount": "500.00"},
                        {"category": GROCERIES, "amount": "100.00"},
                    ],
                )
            )["transaction_id"]
            lines[2]["backing_ids"] = [other_head]
        resp = _split(client, tx_id, lines)
        assert resp.status_code == 422, resp.text
        assert message in resp.json()["detail"]
        assert _row(migrated_engine, tx_id)["split_account_id"] is None


class TestRefusals:
    @pytest.mark.parametrize(
        ("lines", "message"),
        [
            (
                [{"category": SALARY, "amount": "2500.00"}, {"category": TAXES, "amount": "1"}],
                "sum",
            ),
            (
                [{"category": SALARY, "amount": "2500.00"}, {"category": TAXES, "amount": "0"}],
                "nonzero",
            ),
            (
                [
                    {"category": SALARY, "amount": "2000.00"},
                    {"category": f"[{CHECKING}]", "amount": "500.00"},
                ],
                "own account",
            ),
            (
                [
                    {"category": SALARY, "amount": "2000.00"},
                    {"category": f"[{TAXES}]", "amount": "500.00"},
                ],
                "write it without brackets",
            ),
            (
                [
                    {"category": SALARY, "amount": "2000.00"},
                    {"category": SAVINGS, "amount": "500.00"},
                ],
                "write it as [assets.savings]",
            ),
            (
                [
                    {"category": SALARY, "amount": "2000.00"},
                    {"category": "equity.securities", "amount": "500.00"},
                ],
                "",
            ),
        ],
    )
    def test_bad_lines(
        self,
        seeded: None,
        client: TestClient,
        migrated_engine: Engine,
        lines: list[dict[str, Any]],
        message: str,
    ) -> None:
        from purser import ledger as ledger_service

        with purser.db.get_sessionmaker()() as session:
            ledger_service.securities_account(session)
            session.commit()
        tx_id = _paycheck(client, migrated_engine)
        resp = _split(client, tx_id, lines)
        assert resp.status_code == 422, resp.text
        assert message in resp.json()["detail"]

    def test_one_line_is_refused_by_the_schema(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        tx_id = _paycheck(client, migrated_engine)
        assert _split(client, tx_id, [{"category": SALARY, "amount": "2500.00"}]).status_code == 422

    def test_rows_a_split_never_takes(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        pending = _paycheck(client, migrated_engine, status="pending")
        resp = _split(client, pending, PAYCHECK_LINES)
        assert resp.status_code == 422 and "once it posts" in resp.json()["detail"]
        expected = client.post(
            "/transactions",
            json={
                "account": CHECKING,
                "category": SALARY,
                "amount": "2500.00",
                "date": ON.isoformat(),
                "status": "expected",
            },
        ).json()["id"]
        resp = _split(client, expected, PAYCHECK_LINES)
        assert resp.status_code == 422 and "expected" in resp.json()["detail"]
        transfer = client.post(
            "/transactions/transfer",
            json={
                "from_account": SAVINGS,
                "to_account": CHECKING,
                "amount": "2500.00",
                "date": ON.isoformat(),
                "status": "posted",
            },
        ).json()["id"]
        resp = _split(client, transfer, PAYCHECK_LINES)
        assert resp.status_code == 422 and "transfer is not split" in resp.json()["detail"]
        posted = _paycheck(client, migrated_engine)
        _ok(_split(client, posted, PAYCHECK_LINES))
        resp = _split(client, posted, PAYCHECK_LINES)  # the corrected original
        assert resp.status_code == 422 and "corrected" in resp.json()["detail"]

    def test_closed_line_account_is_refused(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        tx_id = _paycheck(client, migrated_engine)
        savings = next(a for a in client.get("/accounts").json() if a["path"] == SAVINGS)
        assert client.post(f"/accounts/{savings['id']}/close", json={}).status_code == 200
        resp = _split(
            client,
            tx_id,
            [
                {"category": SALARY, "amount": "2600.00"},
                {"category": f"[{SAVINGS}]", "amount": "-100"},
            ],
        )
        assert resp.status_code == 422 and "closed" in resp.json()["detail"]

    def test_a_flat_edit_of_a_split_points_at_the_split_path(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        head = _ok(_split(client, _paycheck(client, migrated_engine), PAYCHECK_LINES))[
            "transaction_id"
        ]
        resp = client.patch(f"/transactions/{head}", json={"category": GROCERIES})
        assert resp.status_code == 422 and "split_transaction" in resp.json()["detail"]

    def test_a_backed_manual_pending_split_is_never_voided_by_hand(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        manual = client.post(
            "/transactions",
            json={
                "account": CHECKING,
                "category": SALARY,
                "amount": "2500.00",
                "date": ON.isoformat(),
                "status": "pending",
            },
        ).json()["id"]
        backer = _contribution(client, migrated_engine)
        lines = [*PAYCHECK_LINES[:2], {**PAYCHECK_LINES[2], "backing_ids": [backer]}]
        body = _ok(_split(client, manual, lines))
        assert body["transaction_id"] == manual and body["replaces_id"] is None  # in place
        resp = client.post(f"/transactions/{manual}/void")
        assert resp.status_code == 422 and "remove the split first" in resp.json()["detail"]


class TestReplaceAndUnsplit:
    def test_replace_restores_dropped_backers_and_keeps_named_ones(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        tx_id = _paycheck(client, migrated_engine)
        keep = _contribution(client, migrated_engine, "400.00", content_hash="h-keep")
        drop = _contribution(client, migrated_engine, "200.00", content_hash="h-drop")
        lines = [*PAYCHECK_LINES[:2], {**PAYCHECK_LINES[2], "backing_ids": [keep, drop]}]
        first = _ok(_split(client, tx_id, lines))["transaction_id"]
        second_lines = [
            {"category": SALARY, "amount": "4000.00"},
            {"category": TAXES, "amount": "-1100.00"},
            {"category": f"[{RETIREMENT}]", "amount": "-400.00", "backing_ids": [keep]},
        ]
        body = _ok(_split(client, first, second_lines))
        second = body["transaction_id"]
        assert body["replaces_id"] == first and body["backing_restored"] == [drop]
        (clone,) = body["restored_ids"]
        assert _row(migrated_engine, clone)["content_hash"] == "h-drop"
        assert _row(migrated_engine, clone)["status"] == "posted"
        assert str(_row(migrated_engine, keep)["superseded_by"]) == second
        assert _balances(client)[RETIREMENT] == D("600.00")  # 400 in the split + 200 back
        assert client.get(f"/transactions/{second}").json()["split"]["lines"][2]["backing_ids"] == [
            keep
        ]

    def test_unsplit_restores_the_row_and_its_backing_rows(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        tx_id = _paycheck(client, migrated_engine, content_hash="h-pay")
        before = _postings(migrated_engine, tx_id)
        backer = _contribution(client, migrated_engine, content_hash="h-401k")
        lines = [*PAYCHECK_LINES[:2], {**PAYCHECK_LINES[2], "backing_ids": [backer]}]
        head = _ok(_split(client, tx_id, lines))["transaction_id"]
        body = _ok(
            client.delete(
                f"/transactions/{head}/split", params={"category": "expenses.uncategorized"}
            )
        )
        flat = body["transaction_id"]
        assert _postings(migrated_engine, flat) == before
        assert _row(migrated_engine, flat)["split_account_id"] is None
        (clone,) = body["restored_ids"]
        assert _postings(migrated_engine, clone) == _postings(migrated_engine, backer)
        assert _row(migrated_engine, clone)["content_hash"] == "h-401k"
        assert _balances(client)[RETIREMENT] == D("600.00")
        assert _audit(migrated_engine, "transaction.unsplit") == 1
        assert _events(migrated_engine, "transaction.unsplit") == 1
        # The placeholder by sign when no category is given.
        head2 = _ok(_split(client, flat, PAYCHECK_LINES))["transaction_id"]
        flat2 = _ok(client.delete(f"/transactions/{head2}/split"))
        assert flat2["category"] == "income.uncategorized"

    def test_unsplit_keeps_the_rows_review_mark(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        """Removing a split restores the bank line, so the restored row keeps
        the split row's mark whole (who, how, when) instead of one stamped
        at the un-split, as each restored backing row keeps its own
        (decision 130)."""
        tx_id = _paycheck(client, migrated_engine)
        backer = _contribution(client, migrated_engine)
        assert _ok(client.post("/transactions/mark-reviewed", json={"transaction_ids": [backer]}))
        lines = [*PAYCHECK_LINES[:2], {**PAYCHECK_LINES[2], "backing_ids": [backer]}]
        head = _ok(_split(client, tx_id, lines))["transaction_id"]
        # The marks were made earlier than the un-split.
        with migrated_engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE transaction_reviews SET reviewed_at = :at, note_enc = NULL "
                    "WHERE transaction_id IN (:head, :backer)"
                ),
                {"at": "2026-07-16T09:30:00+00:00", "head": head, "backer": backer},
            )
        before = _review(migrated_engine, head)
        body = _ok(client.delete(f"/transactions/{head}/split"))
        flat, (clone,) = body["transaction_id"], body["restored_ids"]
        kept = _review(migrated_engine, flat)
        assert kept is not None and before is not None
        assert {k: kept[k] for k in ("reviewed_at", "reviewed_by", "via")} == {
            k: before[k] for k in ("reviewed_at", "reviewed_by", "via")
        }
        assert str(kept["carried_from"]) == head
        restored = _review(migrated_engine, clone)
        assert restored is not None
        assert restored["reviewed_at"].isoformat() == "2026-07-16T09:30:00+00:00"
        assert (
            client.get(f"/transactions/{flat}")
            .json()["reviewed_at"]
            .startswith("2026-07-16T09:30:00")
        )

    def test_unsplit_of_an_unmarked_split_is_the_review(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        tx_id = _paycheck(client, migrated_engine)
        head = _ok(_split(client, tx_id, PAYCHECK_LINES))["transaction_id"]
        with migrated_engine.begin() as conn:
            conn.execute(
                text("DELETE FROM transaction_reviews WHERE transaction_id = :id"), {"id": head}
            )
        flat = _ok(client.delete(f"/transactions/{head}/split"))["transaction_id"]
        mark = _review(migrated_engine, flat)
        assert mark is not None and mark["via"] == "update" and mark["carried_from"] is None

    def test_a_qif_split_is_editable_through_the_same_path(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        from purser import ledger as ledger_service
        from purser.core import ledger as core

        with purser.db.get_sessionmaker()() as session:
            salary = ledger_service.resolve_account(session, SALARY)
            taxes = ledger_service.resolve_account(session, TAXES)
            qif = ledger_service.create_split_transaction(
                session,
                account_ref=CHECKING,
                amount=D("2500.00"),
                lines=[
                    core.SplitLine(salary.id, D("3400.00")),
                    core.SplitLine(taxes.id, D("-900")),
                ],
                date=ON,
                requested_status=core.Status.POSTED,
                origin=core.Origin.IMPORT,
                token=None,
                content_hash="qif-hash",
            )
            session.commit()
            qif_id = str(qif.id)
        detail = client.get(f"/transactions/{qif_id}").json()
        assert detail["split_lines"] == 2 and detail["split"]["lines"][0]["target"] == SALARY
        head = _ok(_split(client, qif_id, PAYCHECK_LINES))["transaction_id"]
        assert client.get(f"/transactions/{head}").json()["split_lines"] == 3
        assert _row(migrated_engine, qif_id)["content_hash"] == "qif-hash"


class TestMatcherThroughTheSplit:
    def test_a_net_pay_schedule_matches_the_split_through_its_own_leg(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        expected = client.post(
            "/transactions",
            json={
                "account": CHECKING,
                "category": SALARY,
                "amount": "2500.00",
                "date": ON.isoformat(),
                "payee": "EXAMPLE PAYROLL",
                "status": "expected",
            },
        ).json()["id"]
        head = _ok(_split(client, _paycheck(client, migrated_engine), PAYCHECK_LINES))[
            "transaction_id"
        ]
        # An expected on the line's account never settles against the split.
        on_line = client.post(
            "/transactions",
            json={
                "account": RETIREMENT,
                "category": SALARY,
                "amount": "600.00",
                "date": ON.isoformat(),
                "payee": "EXAMPLE PAYROLL",
                "status": "expected",
            },
        ).json()["id"]
        from purser import matching

        with purser.db.get_sessionmaker()() as session:
            matching.run_matching(session)
            session.commit()
        with migrated_engine.connect() as conn:
            matches = dict(
                conn.execute(
                    text(
                        "SELECT expected_id::text, posted_id::text FROM matches "
                        "WHERE state IN ('proposed', 'confirmed')"
                    )
                ).all()
            )
        assert matches.get(expected) in (head, None) and head in matches.values()
        assert on_line not in matches


class TestGuardsUnchanged:
    def test_categorize_pass_and_transfer_pool_skip_user_splits(
        self, seeded: None, client: TestClient, migrated_engine: Engine, db_session: Session
    ) -> None:
        from purser import matching

        head = _ok(_split(client, _paycheck(client, migrated_engine), PAYCHECK_LINES))[
            "transaction_id"
        ]
        before = _postings(migrated_engine, head)
        assert client.post("/categorize/run").status_code == 200
        assert _postings(migrated_engine, head) == before
        pool = {str(t.id) for t in matching._one_sided_import_rows(db_session)}
        assert head not in pool
        # Reports: category lines count per category; the account line is
        # money moved to another account, never spending.
        from purser import ledger as ledger_service

        report = ledger_service.spending_summary(db_session, group_by="category", start=ON, end=ON)
        groups = {g["key"]: D(str(g["total"])) for g in report["groups"]}
        assert groups.get(TAXES) == D("900.00")
        assert RETIREMENT not in groups


# ---------------------------------------------------------------------------
# Properties (CLAUDE.md guardrail 3)
# ---------------------------------------------------------------------------

_cents = st.integers(min_value=-500_000, max_value=500_000).filter(lambda c: c != 0)


def _session() -> Session:
    return purser.db.get_sessionmaker()()


@st.composite
def _split_case(draw: st.DrawFn) -> tuple[Decimal, list[tuple[str, Decimal]], list[int]]:
    """A row amount and lines over categories and accounts summing to it;
    for each account line how many bank rows back it (0 = unbacked)."""
    targets = draw(
        st.lists(
            st.sampled_from([SALARY, TAXES, GROCERIES, f"[{SAVINGS}]", f"[{RETIREMENT}]"]),
            min_size=1,
            max_size=5,
        )
    )
    shares = [D(draw(_cents)) / 100 for _ in targets]
    closing = draw(st.sampled_from([HOUSEHOLD, f"[{LOAN}]"]))
    closing_share = D(draw(_cents)) / 100
    lines = [*zip(targets, shares, strict=True), (closing, closing_share)]
    amount = sum((s for _, s in lines), D(0))
    if amount == 0:
        lines[-1] = (closing, closing_share + D("1.00"))
        amount = sum((s for _, s in lines), D(0))
    backing = [
        draw(st.integers(min_value=0, max_value=3)) if t.startswith("[") else 0 for t, _ in lines
    ]
    return amount, lines, backing


def _bank_rows_for(engine: Engine, account: str, leg: Decimal, n: int, tag: str) -> list[str]:
    """``n`` posted bank rows on ``account`` summing to ``leg``."""
    if n == 0:
        return []
    parts = [(leg / n).quantize(D("0.01")) for _ in range(n - 1)]
    parts.append(leg - sum(parts, D(0)))
    out = []
    for i, part in enumerate(parts):
        if part == 0:
            return []
        out.append(
            _imported(
                None,  # type: ignore[arg-type]
                engine,
                account,
                str(part),
                ON,
                payee=f"BANK {tag} {i}",
                content_hash=f"{tag}-{i}",
            )
        )
    return out


class TestProperties:
    @settings(
        max_examples=25,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    )
    @given(case=_split_case())
    def test_zero_sum_never_a_transfer_and_unsplit_restores_exactly(
        self,
        seeded: None,
        client: TestClient,
        migrated_engine: Engine,
        case: tuple[Decimal, list[tuple[str, Decimal]], list[int]],
    ) -> None:
        from purser import splits, transfers
        from purser.models import Transaction

        amount, lines, backing = case
        tag = uuid.uuid4().hex[:10]
        tx_id = _imported(
            client, migrated_engine, CHECKING, str(amount), ON, payee=f"P {tag}", content_hash=tag
        )
        original = _postings(migrated_engine, tx_id)
        body_lines = []
        backers: list[str] = []
        for i, ((target, share), n) in enumerate(zip(lines, backing, strict=True)):
            line: dict[str, Any] = {"category": target, "amount": str(share)}
            if n:
                ids = _bank_rows_for(migrated_engine, target[1:-1], -share, n, f"{tag}-{i}")
                if ids:
                    line["backing_ids"] = ids
                    backers.extend(ids)
            body_lines.append(line)
        snapshot = {b: (_postings(migrated_engine, b), _row(migrated_engine, b)) for b in backers}
        head = _ok(_split(client, tx_id, body_lines))["transaction_id"]
        with _session() as session:
            tx = session.get(Transaction, uuid.UUID(head))
            assert tx is not None
            assert sum((D(p.amount) for p in tx.postings), D(0)) == 0
            assert transfers.transfer_shape(tx) is None and not transfers.is_transfer(tx)
            assert splits.own_leg(tx) is not None
        for b in backers:
            assert _row(migrated_engine, b)["status"] == "void"
        body = _ok(
            client.delete(
                f"/transactions/{head}/split", params={"category": "expenses.uncategorized"}
            )
        )
        assert _postings(migrated_engine, body["transaction_id"]) == original
        clones = body["restored_ids"]
        assert len(clones) == len(backers)
        restored = sorted(
            (_postings(migrated_engine, c), _row(migrated_engine, c)["content_hash"])
            for c in clones
        )
        expected = sorted((p, r["content_hash"]) for p, r in snapshot.values())
        assert restored == expected
        for c in clones:
            assert _row(migrated_engine, c)["status"] == "posted"


class TestAccountPurge:
    def test_deleting_a_line_account_keeps_the_split_rows_bank_line(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        from purser.admin import account_delete
        from purser.models import Account

        tx_id = _paycheck(client, migrated_engine, content_hash="h-pay")
        backer = _imported(client, migrated_engine, SAVINGS, "600.00", ON, payee="SWEEP")
        lines = [
            {"category": SALARY, "amount": "4000.00"},
            {"category": TAXES, "amount": "-900.00"},
            {"category": f"[{SAVINGS}]", "amount": "-600.00", "backing_ids": [backer]},
        ]
        _ok(_split(client, tx_id, lines))
        with purser.db.get_sessionmaker()() as session:
            savings = session.query(Account).filter(Account.path == SAVINGS).one()
            summary = account_delete.execute_delete(session, savings, actor_token_id=None)
            session.commit()
        assert summary["splits_removed"] == 1
        items = client.get(
            "/transactions", params={"account": CHECKING, "status": ["posted"]}
        ).json()["items"]
        # The bank line survives on checking as a flat row (the split's
        # reversal went with the deleted account, so the first correction's
        # reversal stands beside it): the account still sums to the bank's.
        assert _balances(client)[CHECKING] == D("2500.00")
        assert any(D(i["amount"]) == D("2500.00") and not i["split_lines"] for i in items)
        assert all(not i["split_lines"] for i in items)
