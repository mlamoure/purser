"""Split transactions and QIF split import (issue #274 part L, decision 87,
DESIGN §3.3/§7.2).

A split transaction is one real leg plus two or more category legs. A QIF
row's ``S``/``E``/``$`` lines become its category legs; each transfer line
``S[X]`` is carved out as an ordinary transfer leg that pairs with X's mirror
row (decomposition, model B), so the money moves once. Split rows are
read-only: edits, the categorize pass, a categorize rule's apply-history and
the transfer pool leave them alone. The fixture is synthetic: a paycheck split
with a transfer line to savings, a two-line grocery split and a split whose
lines leave part of the amount unexplained.
"""

import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from purser.core import ledger as core
from purser.core import qif as core_qif
from tests.conftest import reset_db

FIXTURE = (Path(__file__).parent / "fixtures" / "quicken" / "split_transfers.qif").read_text()
ACCOUNT_MAP = {"Checking": "assets.checking", "Harbor": "assets.harbor"}

# Quicken's own registers for the fixture. Checking: the net paycheck, the two
# purchases. Harbor: the savings sweep. Before #274 the sweep's mirror row
# formed its own Checking -> Harbor transfer while the paycheck stayed whole,
# so Checking came out 500.00 short (1,520.00).
CHECKING_REGISTER = Decimal("2200.00") - Decimal("120.00") - Decimal("60.00")
HARBOR_REGISTER = Decimal("500.00")

D = Decimal


@pytest.fixture
def seeded(admin_client: TestClient) -> None:
    for name, path in [("Checking", "assets.checking"), ("Harbor", "assets.harbor")]:
        resp = admin_client.post("/accounts", json={"name": name, "type": "checking", "path": path})
        assert resp.status_code == 201, resp.text


def _balances(client: TestClient) -> dict[str, Decimal]:
    accounts = client.get("/accounts", params={"include_balances": True}).json()
    return {a["path"]: Decimal(a["balance"]) for a in accounts}


def _items(client: TestClient, account: str) -> list[dict[str, Any]]:
    page = client.get("/transactions", params={"account": account, "limit": 100}).json()
    items: list[dict[str, Any]] = page["items"]
    return items


def _rows(client: TestClient, account: str) -> dict[str, dict[str, Any]]:
    """The account's non-transfer rows by payee."""
    return {row["payee"]: row for row in _items(client, account) if not row["is_transfer"]}


def _category_legs(client: TestClient, tx_id: str) -> dict[str, Decimal]:
    detail = client.get(f"/transactions/{tx_id}").json()
    legs: dict[str, Decimal] = {}
    for p in detail["postings"]:
        if not p["account_path"].startswith("assets."):
            legs[p["account_path"]] = legs.get(p["account_path"], D(0)) + D(p["amount"])
    return legs


def _counts(preview: dict[str, Any]) -> dict[str, dict[str, int]]:
    return {a["name"]: a["counts"] for a in preview["accounts"]}


class TestSplitPrimitive:
    def test_expand_split_is_one_real_leg_plus_category_legs(self) -> None:
        account, food, home = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        specs = core.expand_split(
            account_id=account,
            amount=D("-120.00"),
            lines=[core.SplitLine(food, D("-80.00")), core.SplitLine(home, D("-40.00"), "x")],
            currency="USD",
        )
        assert [(s.account_id, s.amount, s.memo) for s in specs] == [
            (account, D("-120.0000"), None),
            (food, D("80.0000"), None),
            (home, D("40.0000"), "x"),
        ]
        legs = [core.Leg(s.account_id, s.amount, s.account_id != account) for s in specs]
        assert core.transfer_shape(legs) is None  # not a transfer
        assert core.split_line_count(legs) == 2

    @pytest.mark.parametrize(
        ("amount", "shares", "message"),
        [
            ("-120.00", ["-120.00"], "at least two"),
            ("-120.00", ["-80.00", "-30.00"], "sum to"),
            ("-120.00", ["-120.00", "0"], "nonzero"),
            ("0", ["-10.00", "10.00"], "nonzero"),
        ],
    )
    def test_expand_split_refuses(self, amount: str, shares: list[str], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            core.expand_split(
                account_id=uuid.uuid4(),
                amount=D(amount),
                lines=[core.SplitLine(uuid.uuid4(), D(s)) for s in shares],
                currency="USD",
            )

    def test_split_line_count_is_zero_for_other_shapes(self) -> None:
        a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        flat = [core.Leg(a, D("-5"), False), core.Leg(b, D("5"), True)]
        plain = [core.Leg(a, D("-5"), False), core.Leg(b, D("5"), False)]
        loan = [core.Leg(a, D("-5"), False), core.Leg(b, D("4"), False), core.Leg(c, D("1"), True)]
        for legs in (flat, plain, loan):
            assert core.split_line_count(legs) == 0

    @settings(max_examples=200, deadline=None)
    @given(
        shares=st.lists(
            st.decimals(
                min_value=D("-99999.99"),
                max_value=D("99999.99"),
                places=2,
                allow_nan=False,
                allow_infinity=False,
            ).filter(lambda v: v != 0),
            min_size=2,
            max_size=8,
        )
    )
    def test_expand_split_is_zero_sum(self, shares: list[Decimal]) -> None:
        amount = sum(shares, D(0))
        if amount == 0:
            return
        specs = core.expand_split(
            account_id=uuid.uuid4(),
            amount=amount,
            lines=[core.SplitLine(uuid.uuid4(), s) for s in shares],
            currency="USD",
        )
        assert core.is_balanced(specs)
        assert len(specs) == len(shares) + 1


class TestParser:
    def test_split_lines_accumulate_in_order_and_l_is_ignored(self) -> None:
        parsed = core_qif.parse(FIXTURE)
        payroll, grocer, pharmacy = parsed.transactions["Checking"]
        assert payroll.category is None and payroll.transfer_account is None  # L ignored
        assert payroll.splits == (
            core_qif.QifSplit("Salary", None, D("3200.00"), "Semi-monthly gross"),
            core_qif.QifSplit("Taxes:Federal", None, D("-500.00"), None),
            core_qif.QifSplit(None, "Harbor", D("-500.00"), "Savings sweep"),
        )
        assert [s.category for s in grocer.splits] == ["Groceries", "Household"]
        assert [s.amount for s in pharmacy.splits] == [D("-45.00")]
        (mirror,) = parsed.transactions["Harbor"]
        assert mirror.splits == ()
        assert mirror.transfer_account == "Checking"

    def test_a_category_in_l_is_ignored_and_a_repeated_amount_opens_a_blank_line(self) -> None:
        content = (
            "!Type:Bank\nD1/5'26\nT-30.00\nPShop\nLGroceries\n"
            "SGroceries\n$-20.00\n$-10.00\nEno category\n^\n"
        )
        (row,) = core_qif.parse(content).transactions[core_qif.DEFAULT_ACCOUNT]
        assert row.category is None
        assert row.splits == (
            core_qif.QifSplit("Groceries", None, D("-20.00"), None),
            core_qif.QifSplit(None, None, D("-10.00"), "no category"),
        )


class TestSplitImport:
    def test_split_transfer_lines_no_longer_debit_checking_twice(
        self, seeded: None, client: TestClient
    ) -> None:
        body = {"content": FIXTURE, "account_map": ACCOUNT_MAP}
        preview = client.post("/import/qif/preview", json=body).json()
        counts = _counts(preview)
        assert counts["Checking"]["in_file"] == 3
        assert counts["Checking"]["would_import"] == 3
        assert counts["Checking"]["split_transfers"] == 1
        assert counts["Checking"]["split_remainders"] == 1
        assert counts["Checking"]["transfers"] == 0  # the carved line is a sub-count
        assert counts["Harbor"]["in_file"] == 1
        assert counts["Harbor"]["transfers"] == 1
        assert set(preview["categories"]) >= {"Salary", "Taxes:Federal", "Groceries", "Health"}

        resp = client.post("/import/qif", json=body)
        assert resp.status_code == 200, resp.text
        result = resp.json()
        assert result["per_account"] == counts
        assert result["transfers_created"] == 1  # the sweep pairs exactly once
        assert result["split_transfers"] == 1

        balances = _balances(client)
        assert balances["assets.checking"] == CHECKING_REGISTER
        assert balances["assets.harbor"] == HARBOR_REGISTER

        rows = _rows(client, "assets.checking")
        payroll = rows["Synthetic Employer Payroll"]
        assert D(payroll["amount"]) == D("2700.00")  # 2,200.00 net + the 500.00 swept
        assert payroll["category_path"] is None
        assert payroll["split_lines"] == 2
        assert _category_legs(client, payroll["id"]) == {
            "income.salary": D("-3200.0000"),
            "expenses.taxes.federal": D("500.0000"),
        }
        grocer = rows["Synthetic Grocer"]
        assert grocer["split_lines"] == 2
        assert _category_legs(client, grocer["id"]) == {
            "expenses.groceries": D("80.0000"),
            "expenses.household": D("40.0000"),
        }
        # The lines left 15.00 unexplained: the placeholder takes it.
        pharmacy = rows["Synthetic Pharmacy"]
        assert _category_legs(client, pharmacy["id"]) == {
            "expenses.health": D("45.0000"),
            "expenses.uncategorized": D("15.0000"),
        }
        (sweep,) = [r for r in _items(client, "assets.checking") if r["is_transfer"]]
        assert D(sweep["amount"]) == D("-500.00")
        assert sweep["transfer_account_path"] == "assets.harbor"
        assert sweep["split_lines"] == 0
        # Nothing is categorized --Split-- any more.
        assert all(not c["path"].endswith(".split") for c in client.get("/categories").json())

        again = client.post("/import/qif", json=body).json()
        assert again["imported"] == 0
        assert again["transfers_created"] == 0
        assert _balances(client) == balances

    def test_cutoff_split_line_degrades_to_one_leg(self, seeded: None, client: TestClient) -> None:
        """Harbor's feed owns the sweep's date: the carved line imports as a
        one-leg row on Checking, its mirror is left to the feed."""
        body = {
            "content": FIXTURE,
            "account_map": ACCOUNT_MAP,
            "account_options": {"Harbor": {"cutoff": "2026-01-10"}},
        }
        preview = client.post("/import/qif/preview", json=body).json()
        result = client.post("/import/qif", json=body).json()
        assert result["per_account"] == _counts(preview)
        assert result["transfers_created"] == 0
        assert result["per_account"]["Harbor"]["skipped_cutoff"] == 1
        balances = _balances(client)
        assert balances["assets.checking"] == CHECKING_REGISTER
        assert balances["assets.harbor"] == D("0")
        one_leg = [r for r in _items(client, "assets.checking") if D(r["amount"]) == D("-500.00")]
        assert len(one_leg) == 1 and not one_leg[0]["is_transfer"]

    def test_a_file_imported_with_split_category_dedups(
        self, seeded: None, client: TestClient
    ) -> None:
        """A file imported before split lines were read landed each split row
        whole on ``--Split--``. Its identity is the row's flat hash over T, so
        a re-import dedups instead of booking the row twice."""
        from purser import db, qif_import
        from purser import ledger as ledger_service

        grocer_only = (
            "!Account\nNChecking\nTBank\n^\n!Type:Bank\n"
            "D1/20'26\nT-120.00\nPSynthetic Grocer\nL--Split--\n"
            "SGroceries\n$-80.00\nSHousehold\n$-40.00\nC*\n^\n"
        )
        with db.get_sessionmaker()() as session:
            category = ledger_service.ensure_category_path(
                session, "expenses.split", actor_token_id=None
            )
            ledger_service.create_flat_transaction(
                session,
                account_ref="assets.checking",
                category_ref=str(category.id),
                amount=D("-120.00"),
                date=date(2026, 1, 20),
                payee_name="Synthetic Grocer",
                description="Synthetic Grocer",
                requested_status=core.Status.POSTED,
                origin=core.Origin.IMPORT,
                token=None,
                content_hash=qif_import._flat_hash(
                    "Checking", date(2026, 1, 20), D("-120.00"), "Synthetic Grocer"
                ),
            )
            session.commit()

        result = client.post(
            "/import/qif",
            json={"content": grocer_only, "account_map": {"Checking": "assets.checking"}},
        ).json()
        assert result["imported"] == 0
        assert result["skipped"] == 1
        assert _balances(client)["assets.checking"] == D("-120.00")


@settings(max_examples=12, deadline=None)
@given(
    shares=st.lists(
        st.decimals(
            min_value=D("-500.00"),
            max_value=D("500.00"),
            places=2,
            allow_nan=False,
            allow_infinity=False,
        ).filter(lambda v: v != 0),
        min_size=1,
        max_size=5,
    ),
    sweep=st.sampled_from([None, D("-25.00"), D("40.00")]),
)
def test_split_import_is_zero_sum_and_idempotent(
    migrated_engine: Engine, shares: list[Decimal], sweep: Decimal | None
) -> None:
    """Guardrail 3: any category lines (with or without a transfer line)
    summing to T import as zero-sum transactions, and the same file again adds
    no rows."""
    from purser import ledger as ledger_service
    from purser import qif_import
    from purser.db import get_sessionmaker

    total = sum(shares, D(0)) + (sweep or D(0))
    if total == 0:
        return
    lines = "".join(f"SCat{i}\n${share}\n" for i, share in enumerate(shares))
    if sweep is not None:
        lines += f"S[Harbor]\n${sweep}\n"
    content = (
        "!Account\nNChecking\nTBank\n^\n!Type:Bank\n"
        f"D1/20'26\nT{total}\nPSynthetic Split\nL--Split--\n{lines}C*\n^\n"
    )
    reset_db(migrated_engine)
    with get_sessionmaker()() as session:
        for name, path in (("Checking", "assets.checking"), ("Harbor", "assets.harbor")):
            ledger_service.create_account(
                session, name=name, type_="checking", path=path, actor_token_id=None
            )
        session.commit()
        for _ in range(2):
            qif_import.import_qif(session, content=content, account_map=ACCOUNT_MAP, token=None)
            session.commit()
            rows = session.execute(text("SELECT COUNT(*) FROM transactions")).scalar_one()
            if _ == 0:
                first = rows
        assert rows == first
        unbalanced = session.execute(
            text(
                "SELECT transaction_id FROM postings GROUP BY transaction_id "
                "HAVING SUM(amount) <> 0"
            )
        ).all()
        assert unbalanced == []
        checking = session.execute(
            text(
                "SELECT COALESCE(SUM(p.amount), 0) FROM postings p "
                "JOIN accounts a ON a.id = p.account_id WHERE a.path = 'assets.checking'"
            )
        ).scalar_one()
        assert Decimal(checking) == total


def _split_row(client: TestClient, *, cleared: bool) -> str:
    """Import one split row whose blank line lands on the placeholder."""
    content = (
        "!Account\nNChecking\nTBank\n^\n!Type:Bank\n"
        "D1/20'26\nT-30.00\nPSynthetic Corner Shop\nL--Split--\n"
        "SGroceries\n$-20.00\nS\n$-10.00\n" + ("C*\n" if cleared else "") + "^\n"
    )
    resp = client.post(
        "/import/qif", json={"content": content, "account_map": {"Checking": "assets.checking"}}
    )
    assert resp.status_code == 200, resp.text
    (row,) = [
        r
        for r in client.get("/transactions", params={"account": "assets.checking"}).json()["items"]
        if r["payee"] == "Synthetic Corner Shop"
    ]
    return str(row["id"])


class TestSplitRowsAreReadOnly:
    def test_edit_is_refused(self, seeded: None, client: TestClient) -> None:
        tx_id = _split_row(client, cleared=True)
        resp = client.patch(f"/transactions/{tx_id}", json={"category": "expenses.groceries"})
        assert resp.status_code == 422
        assert "split rows are read-only" in resp.json()["detail"]

    @pytest.mark.parametrize("cleared", [True, False])
    def test_categorize_and_apply_history_skip_split_rows(
        self, seeded: None, client: TestClient, cleared: bool
    ) -> None:
        tx_id = _split_row(client, cleared=cleared)
        before = _category_legs(client, tx_id)
        assert "expenses.uncategorized" in before

        dining = client.post(
            "/categories",
            json={"name": "Dining", "type": "category_expense", "path": "expenses.dining"},
        ).json()["id"]
        rule_id = client.post(
            "/rules",
            json={
                "type": "categorize",
                "match_field": "payee_or_description",
                "match_mode": "contains",
                "match_text": "corner shop",
                "category_id": dining,
                "activate": True,
            },
        ).json()["id"]
        # The nightly pass would apply the rule to an uncategorized flat row.
        stats = client.post("/categorize/run").json()
        assert stats["rule"] == 0
        assert _category_legs(client, tx_id) == before

        body = {"include_user_categorized": True, "posted": "correct"}
        dry = client.post(f"/rules/{rule_id}/apply-history", json={**body, "dry_run": True})
        wet = client.post(f"/rules/{rule_id}/apply-history", json={**body, "dry_run": False})
        assert dry.status_code == wet.status_code == 200, wet.text
        for run in (dry.json(), wet.json()):
            assert run["matched"] == 0
            assert run["skipped_splits"] == 1
        assert _category_legs(client, tx_id) == before

    def test_split_rows_stay_out_of_the_transfer_pool(
        self, seeded: None, client: TestClient, db_session: Session
    ) -> None:
        from purser import matching

        tx_id = _split_row(client, cleared=True)
        pool = {str(t.id) for t in matching._one_sided_import_rows(db_session)}
        assert tx_id not in pool

    def test_readmodel_reports_no_category_for_a_split_row(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        tx_id = _split_row(client, cleared=True)
        with migrated_engine.connect() as conn:
            category = conn.execute(
                text("SELECT category_path FROM readmodel.v2_transactions_flat WHERE id = :id"),
                {"id": tx_id},
            ).scalar_one()
        assert category is None
