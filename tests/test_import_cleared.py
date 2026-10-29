"""Quicken reminders and the cleared flag on manual import (issue #249,
decision 60).

A reminder row (a mark in a Quicken CSV ``Scheduled`` column) is not history:
it is skipped and counted ``skipped_scheduled``. A row its file marks uncleared
lands ``expected`` (counted ``uncleared``) — never ``posted``, where it inflated
the balance the drift check compares against the bank, and never ``pending``,
which the feed's vanished-pending sweep would void. A file account with no
cleared marks at all carries no clearing information and imports exactly as
before. Preview == commit (#144) and import idempotency (guardrail 3) hold for
every new shape. All data is synthetic.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, text

from purser.core import csv_import as cc
from purser.core import ledger as core
from purser.core import qif as core_qif
from tests.conftest import reset_db
from tests.test_qif import _seed_feed_flat

FIXTURES = Path(__file__).parent / "fixtures"
QUICKEN_CSV = (FIXTURES / "csv" / "quicken_cleared_scheduled.csv").read_text()
MARKED_QIF = (FIXTURES / "quicken" / "cleared_marks.qif").read_text()

ACCOUNTS = (
    ("Checking", "assets.checking", "checking"),
    ("Savings", "assets.savings", "savings"),
    ("Cash", "assets.cash", "cash"),
)
CSV_MAP = {"Checking": "assets.checking", "Savings": "assets.savings"}
QIF_MAP = {"Checking": "assets.checking", "Cash": "assets.cash"}
TRANSFER_MAP = {"Checking": "assets.checking", "Savings": "assets.savings"}
ALL_STATUSES = ["expected", "pending", "posted", "void"]

CHECK_1001 = "Check 1001 Landlord Properties / March rent"


# ---------------------------------------------------------------------------
# Parsers (pure, no DB)
# ---------------------------------------------------------------------------


class TestCsvParser:
    def test_quicken_layout_detects_clr_and_scheduled_columns(self) -> None:
        parsed = cc.parse_csv(QUICKEN_CSV)
        checking = parsed.transactions["Checking"]
        # R, c, blank, c, blank(reminder)
        assert [t.cleared for t in checking] == [True, True, False, True, False]
        assert [t.scheduled for t in checking] == [False, False, False, False, True]
        assert checking[2].amount == Decimal("-1200.00")
        savings = parsed.transactions["Savings"]
        assert [t.cleared for t in savings] == [True, False]
        assert not any(t.scheduled for t in savings)

    @pytest.mark.parametrize(
        "value", ["c", "C", "R", "r", "*", "X", "x", "Cleared", "reconciled", "yes", "TRUE"]
    )
    def test_cleared_values(self, value: str) -> None:
        parsed = cc.parse_csv(f"Date,Payee,Amount,Clr\n2024-01-01,Store,-1.00,{value}\n")
        assert parsed.transactions[cc.DEFAULT_ACCOUNT][0].cleared is True

    @pytest.mark.parametrize("value", ["", "  ", "u", "Uncleared", "no", "N", "false"])
    def test_uncleared_values(self, value: str) -> None:
        parsed = cc.parse_csv(f"Date,Payee,Amount,Cleared\n2024-01-01,Store,-1.00,{value}\n")
        assert parsed.transactions[cc.DEFAULT_ACCOUNT][0].cleared is False

    def test_unknown_cleared_value_is_a_clear_error_never_a_guess(self) -> None:
        with pytest.raises(cc.CsvParseError, match="cleared flag"):
            cc.parse_csv("Date,Payee,Amount,Clr\n2024-01-01,Store,-1.00,maybe\n")

    def test_no_cleared_column_marks_no_row(self) -> None:
        # No column, no mark: the planner reads the account as carrying no
        # clearing information (all rows import posted), and no row is
        # evidence that an earlier expected row has cleared (#310).
        parsed = cc.parse_csv("Date,Payee,Amount\n2024-01-01,Store,-1.00\n")
        row = parsed.transactions[cc.DEFAULT_ACCOUNT][0]
        assert row.cleared is False
        assert row.scheduled is False

    @pytest.mark.parametrize(
        ("value", "scheduled"),
        [("S", True), ("Yes", True), ("Scheduled", True), ("", False), ("No", False), ("0", False)],
    )
    def test_scheduled_values(self, value: str, scheduled: bool) -> None:
        parsed = cc.parse_csv(f"Date,Payee,Amount,Scheduled\n2024-01-01,Store,-1.00,{value}\n")
        assert parsed.transactions[cc.DEFAULT_ACCOUNT][0].scheduled is scheduled

    def test_column_map_can_name_cleared_and_scheduled(self) -> None:
        parsed = cc.parse_csv(
            "When,Who,HowMuch,Cl,Reminder\n2024-03-01,Bob,10.00,,yes\n2024-03-02,Ann,5.00,c,\n",
            column_map={
                "date": "When",
                "payee": "Who",
                "amount": "HowMuch",
                "cleared": "Cl",
                "scheduled": "Reminder",
            },
        )
        bob, ann = parsed.transactions[cc.DEFAULT_ACCOUNT]
        assert bob.scheduled is True and bob.cleared is False
        assert ann.scheduled is False and ann.cleared is True


class TestQifParser:
    @pytest.mark.parametrize(
        ("mark", "cleared"),
        [
            ("*", True),
            ("c", True),
            ("X", True),
            ("R", True),
            ("x", True),
            ("r", True),
            ("", False),
            (" ", False),
            ("?", False),
        ],
    )
    def test_cleared_marks(self, mark: str, cleared: bool) -> None:
        assert core_qif.is_cleared_mark(mark) is cleared

    def test_c_field_variants_in_a_file(self) -> None:
        parsed = core_qif.parse(MARKED_QIF)
        # opening (no C), CX, C*, Cc, CR, no C
        assert [t.cleared for t in parsed.transactions["Checking"]] == [
            False,
            True,
            True,
            True,
            True,
            False,
        ]
        assert not any(t.cleared for t in parsed.transactions["Cash"])
        assert not any(t.scheduled for txns in parsed.transactions.values() for t in txns)


# ---------------------------------------------------------------------------
# Service + API (DB-backed)
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded(admin_client: TestClient) -> None:
    for name, path, type_ in ACCOUNTS:
        resp = admin_client.post("/accounts", json={"name": name, "type": type_, "path": path})
        assert resp.status_code == 201, resp.text


def _register(client: TestClient, account: str) -> dict[str, str]:
    """description -> status for every row touching the account, void included."""
    page = client.get(
        "/transactions",
        params={"account": account, "status": ALL_STATUSES, "include_collapsed": True},
    ).json()
    return {row["description"]: row["status"] for row in page["items"]}


def _balances(client: TestClient) -> dict[str, Decimal]:
    accounts = client.get("/accounts", params={"include_balances": True}).json()
    return {a["path"]: Decimal(a["balance"]) for a in accounts}


def _counts(preview: dict[str, Any]) -> dict[str, dict[str, int]]:
    return {a["name"]: a["counts"] for a in preview["accounts"]}


def _tx_count(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text("SELECT COUNT(*) FROM transactions")).scalar_one())


def _flat_hash(qif_name: str, when: date, amount: str, description: str) -> str:
    from purser import crypto

    return crypto.keyed_hash(
        core.content_hash(
            external_account_id=f"qif:{qif_name}",
            date=when,
            amount=Decimal(amount),
            description=description,
        )
    )


class TestQuickenCsv:
    def test_reminder_skipped_uncleared_expected_and_preview_equals_commit(
        self, seeded: None, client: TestClient
    ) -> None:
        body = {"content": QUICKEN_CSV, "account_map": CSV_MAP}
        resp = client.post("/import/csv/preview", json=body)
        assert resp.status_code == 200, resp.text
        preview = resp.json()
        prev = _counts(preview)
        assert prev["Checking"] == {
            "in_file": 5,
            "would_import": 3,  # payroll, groceries, the uncleared check
            "skipped_dedup": 0,
            "skipped_cutoff": 0,
            "skipped_unmapped": 0,
            "skipped_zero_amount": 0,
            "skipped_scheduled": 1,  # the April reminder
            "skipped_investment": 0,
            "transfers": 1,
            "opening_balances": 0,
            "one_leg_transfers": 0,
            "promoted": 0,
            "uncleared": 1,  # check 1001
            "split_transfers": 0,
            "split_remainders": 0,
        }
        assert prev["Savings"]["in_file"] == 2
        assert prev["Savings"]["transfers"] == 1
        assert prev["Savings"]["would_import"] == 1
        assert prev["Savings"]["uncleared"] == 1  # the unmarked interest row
        # The file still has seven rows; the reminder is counted, not hidden.
        assert preview["total_transactions"] == 7

        resp = client.post("/import/csv", json=body)
        assert resp.status_code == 200, resp.text
        commit = resp.json()
        assert commit["per_account"] == prev  # the #144 invariant
        assert commit["skipped_scheduled"] == 1
        assert commit["uncleared"] == 2
        assert commit["imported"] == 4
        assert commit["transfers_created"] == 1

        assert _register(client, "assets.checking") == {
            "Acme Payroll": "posted",
            "Grocery Mart / weekly shop": "posted",
            CHECK_1001: "expected",
            "Move to savings": "posted",
        }
        assert _register(client, "assets.savings") == {
            "Move to savings": "posted",
            "Interest": "expected",
        }
        # Expected rows stay out of the balance — what the drift check compares
        # against the bank: 2500.00 - 85.43 - 500.00, and the transfer leg only.
        balances = _balances(client)
        assert balances["assets.checking"] == Decimal("1914.57")
        assert balances["assets.savings"] == Decimal("500.00")

    def test_reminder_never_lands_and_reimport_is_a_noop(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        body = {"content": QUICKEN_CSV, "account_map": CSV_MAP}
        first = client.post("/import/csv", json=body).json()
        assert first["imported"] == 4
        reminder = _flat_hash(
            "Checking",
            date(2024, 4, 1),
            "-1200.00",
            "Check 1002 Landlord Properties / April rent",
        )
        with migrated_engine.connect() as conn:
            landed = conn.execute(
                text("SELECT COUNT(*) FROM transactions WHERE content_hash = :h"),
                {"h": reminder},
            ).scalar_one()
        assert landed == 0
        before = _tx_count(migrated_engine)

        preview = client.post("/import/csv/preview", json=body).json()
        second = client.post("/import/csv", json=body).json()
        assert _tx_count(migrated_engine) == before  # zero new rows
        assert second["per_account"] == _counts(preview)
        assert second["imported"] == 0
        assert second["transfers_created"] == 0
        assert second["uncleared"] == 0
        assert second["skipped_scheduled"] == 1  # a reminder stays a reminder
        # Three flats (the expected check included) + the transfer leg dedup.
        assert second["per_account"]["Checking"]["skipped_dedup"] == 4
        assert second["per_account"]["Savings"]["skipped_dedup"] == 2
        assert _register(client, "assets.checking")[CHECK_1001] == "expected"

    def test_expected_rows_are_forecast_late_and_outside_review(
        self, seeded: None, client: TestClient
    ) -> None:
        client.post("/import/csv", json={"content": QUICKEN_CSV, "account_map": CSV_MAP})

        # Forecast: the outstanding check is still owed, so it lands on day 0
        # as an overdue event (it is dated in 2024 — overdue on any run date).
        forecast = client.get("/forecast", params={"account": "assets.checking", "days": 7}).json()
        assert Decimal(forecast["start_balance"]) == Decimal("1914.57")
        day0 = forecast["days"][0]
        (event,) = day0["events"]
        assert Decimal(event["amount"]) == Decimal("-1200.00")
        assert event["overdue"] is True
        assert Decimal(day0["balance"]) == Decimal("714.57")

        from purser import review, webhooks
        from purser.db import get_sessionmaker

        with get_sessionmaker()() as s:
            # A manual import never enters the unreviewed set (#175) — expected
            # rows included.
            assert review.unreviewed_count(s) == 0
            # Past-dated uncleared rows are late items: one bill.late each, once.
            assert webhooks.emit_late_bills(s) == 2
            assert webhooks.emit_late_bills(s) == 0
            s.commit()

        # ...and the register never badges them "needs review" (#248).
        page = client.get(
            "/transactions", params={"account": "assets.checking", "status": ["expected"]}
        ).json()
        (expected_row,) = page["items"]
        assert expected_row["description"] == CHECK_1001
        assert expected_row["needs_review"] is False


class TestQifClearedMarks:
    def test_c_variants_and_the_unmarked_account_rule(
        self, seeded: None, client: TestClient
    ) -> None:
        body = {"content": MARKED_QIF, "account_map": QIF_MAP}
        prev = _counts(client.post("/import/qif/preview", json=body).json())
        assert prev["Checking"]["in_file"] == 6
        assert prev["Checking"]["opening_balances"] == 1
        assert prev["Checking"]["would_import"] == 5
        assert prev["Checking"]["uncleared"] == 1
        assert prev["Cash"]["would_import"] == 2
        assert prev["Cash"]["uncleared"] == 0

        resp = client.post("/import/qif", json=body)
        assert resp.status_code == 200, resp.text
        commit = resp.json()
        assert commit["per_account"] == prev
        assert commit["uncleared"] == 1

        assert _register(client, "assets.checking") == {
            "Opening balance": "posted",  # bookkeeping: posted whatever its mark
            "Acme Payroll": "posted",  # CX
            "Grocery Mart": "posted",  # C*
            "Corner Pharmacy": "posted",  # Cc
            "City Utilities": "posted",  # CR
            "Check 1001 Landlord Properties": "expected",  # no C, marked account
        }
        # Cash carries no marks at all: no clearing information, all posted.
        assert set(_register(client, "assets.cash").values()) == {"posted"}
        balances = _balances(client)
        assert balances["assets.checking"] == Decimal("3312.47")  # check excluded
        assert balances["assets.cash"] == Decimal("-21.25")

    def test_a_file_without_marks_imports_exactly_as_before(
        self, seeded: None, client: TestClient
    ) -> None:
        bank_qif = (
            "!Type:Bank\n"
            "D2/1/2024\nT-40.00\nPHardware Store\nLHome\n^\n"
            "D2/3/2024\nT900.00\nPAcme Payroll\nLSalary\n^\n"
        )
        resp = client.post(
            "/import/qif",
            json={"content": bank_qif, "account_map": {"__default__": "assets.checking"}},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["uncleared"] == 0
        assert set(_register(client, "assets.checking").values()) == {"posted"}
        assert _balances(client)["assets.checking"] == Decimal("860.00")


def _transfer_qif(*, checking_mark: str, savings_mark: str) -> str:
    """A 500.00 Checking -> Savings transfer on 2024-03-05. Each account also
    carries one cleared row, so an unmarked leg reads as uncleared."""

    def mark(value: str) -> str:
        return f"C{value}\n" if value else ""

    return (
        "!Account\nNChecking\nTBank\n^\n!Type:Bank\n"
        "D3/1/2024\nT100.00\nC*\nPDeposit\nLIncome\n^\n"
        f"D3/5/2024\nT-500.00\n{mark(checking_mark)}PTransfer to savings\nL[Savings]\n^\n"
        "!Account\nNSavings\nTBank\n^\n!Type:Bank\n"
        "D3/2/2024\nT5.00\nCR\nPInterest\nLInterest Inc\n^\n"
        f"D3/5/2024\nT500.00\n{mark(savings_mark)}PTransfer from checking\nL[Checking]\n^\n"
    )


class TestTransferStatus:
    def test_no_leg_cleared_lands_an_expected_two_leg_transfer(
        self, seeded: None, client: TestClient
    ) -> None:
        body = {
            "content": _transfer_qif(checking_mark="", savings_mark=""),
            "account_map": TRANSFER_MAP,
        }
        prev = _counts(client.post("/import/qif/preview", json=body).json())
        commit = client.post("/import/qif", json=body).json()
        assert commit["per_account"] == prev
        assert commit["transfers_created"] == 1
        assert prev["Checking"]["uncleared"] == 1
        assert prev["Savings"]["uncleared"] == 1
        assert _register(client, "assets.checking")["Transfer to savings"] == "expected"
        balances = _balances(client)
        assert balances["assets.checking"] == Decimal("100.00")
        assert balances["assets.savings"] == Decimal("5.00")

    def test_one_cleared_leg_posts_the_transfer(self, seeded: None, client: TestClient) -> None:
        body = {
            "content": _transfer_qif(checking_mark="*", savings_mark=""),
            "account_map": TRANSFER_MAP,
        }
        commit = client.post("/import/qif", json=body).json()
        assert commit["uncleared"] == 0
        assert _register(client, "assets.checking")["Transfer to savings"] == "posted"
        balances = _balances(client)
        assert balances["assets.checking"] == Decimal("-400.00")
        assert balances["assets.savings"] == Decimal("505.00")

    def test_split_degraded_uncleared_leg_lands_expected(
        self, seeded: None, client: TestClient
    ) -> None:
        # Savings' feed owns 2024-03-03 onward: the transfer splits, and the
        # Checking leg — unmarked — degrades to an expected one-leg flat.
        body = {
            "content": _transfer_qif(checking_mark="", savings_mark="R"),
            "account_map": TRANSFER_MAP,
            "account_options": {"Savings": {"cutoff": "2024-03-03"}},
        }
        prev = _counts(client.post("/import/qif/preview", json=body).json())
        commit = client.post("/import/qif", json=body).json()
        assert commit["per_account"] == prev
        assert prev["Checking"]["one_leg_transfers"] == 1
        assert prev["Checking"]["uncleared"] == 1
        assert prev["Savings"]["skipped_cutoff"] == 1
        assert _register(client, "assets.checking")["Transfer to savings"] == "expected"


class TestFeedLifecycle:
    def test_matcher_settles_the_expected_against_the_posted_bank_row(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        body = {"content": QUICKEN_CSV, "account_map": CSV_MAP}
        client.post("/import/csv", json=body)
        # The check clears two days later and the feed reports it.
        _seed_feed_flat(
            account_path="assets.checking",
            when=date(2024, 3, 10),
            tag="i249-check-1001",
            amount="-1200.00",
            description="LANDLORD PROPERTIES CHECK 1001",
        )
        from purser import matching
        from purser.db import get_sessionmaker

        with get_sessionmaker()() as s:
            stats = matching.run_matching(s)
            s.commit()
        assert stats["auto_matched"] == 1  # the interest row has nothing to match yet

        checking = _register(client, "assets.checking")
        assert checking[CHECK_1001] == "void"
        assert checking["LANDLORD PROPERTIES CHECK 1001"] == "posted"
        with migrated_engine.connect() as conn:
            linked = conn.execute(
                text(
                    "SELECT e.superseded_by = f.id FROM transactions e, transactions f"
                    " WHERE e.content_hash = :e AND f.content_hash = :f"
                ),
                {
                    "e": _flat_hash("Checking", date(2024, 3, 8), "-1200.00", CHECK_1001),
                    "f": "feedhash-i249-check-1001",
                },
            ).scalar_one()
        assert linked is True
        # The bank's row counts exactly once: 1914.57 - 1200.00.
        assert _balances(client)["assets.checking"] == Decimal("714.57")

        # The superseded expected keeps its content hash: re-import is a no-op.
        before = _tx_count(migrated_engine)
        again = client.post("/import/csv", json=body).json()
        assert again["imported"] == 0 and again["uncleared"] == 0
        assert _tx_count(migrated_engine) == before

    def test_vanished_pending_sweep_leaves_expected_import_rows_alone(
        self, seeded: None, client: TestClient
    ) -> None:
        client.post("/import/csv", json={"content": QUICKEN_CSV, "account_map": CSV_MAP})

        from purser import ledger as ls
        from purser.db import get_sessionmaker
        from purser.feed import reconcile

        with get_sessionmaker()() as s:
            checking = ls.resolve_account(s, "assets.checking")
            groceries = ls.account_by_path(s, "expenses.groceries")
            # Contrast: a manual-import row landed `pending` — what #249 first
            # proposed — is voided by the very same sweep, because the sweep
            # never asks which source a row came from.
            ls.create_flat_transaction(
                s,
                account_ref=str(checking.id),
                category_ref=str(groceries.id),
                amount=Decimal("-20.00"),
                date=date(2024, 3, 9),
                payee_name="Corner Store",
                description="Corner Store",
                requested_status=core.Status.PENDING,
                origin=core.Origin.IMPORT,
                token=None,
                content_hash="i249-pending-probe",
            )
            s.flush()
            voided = reconcile.reconcile_vanished_pendings(
                s, account=checking, still_pending_hashes=set(), window_start=date(2024, 3, 1)
            )
            s.commit()
        assert voided == 1  # only the pending probe

        checking_rows = _register(client, "assets.checking")
        assert checking_rows["Corner Store"] == "void"
        assert checking_rows[CHECK_1001] == "expected"  # untouched


# ---------------------------------------------------------------------------
# Guardrail 3: idempotency, property-based, over the new shapes
# ---------------------------------------------------------------------------


class TestIdempotencyProperty:
    """Re-running the SAME request adds zero rows — reminders, uncleared
    (expected) rows and cleared rows alike — under any cutoff/invert options,
    in both formats; and the re-run's preview still equals its commit."""

    @settings(max_examples=12, deadline=None)
    @given(
        fmt=st.sampled_from(["qif", "csv"]),
        cutoff_on=st.booleans(),
        invert_on=st.booleans(),
    )
    def test_reimport_adds_zero_rows(
        self, migrated_engine: Engine, fmt: str, cutoff_on: bool, invert_on: bool
    ) -> None:
        from purser import ledger as ls
        from purser import qif_import
        from purser.db import get_sessionmaker

        if fmt == "qif":
            content, account_map, cutoff = MARKED_QIF, QIF_MAP, date(2024, 1, 15)
        else:
            content, account_map, cutoff = QUICKEN_CSV, CSV_MAP, date(2024, 3, 9)
        options: dict[str, qif_import.QifAccountOptions] = {}
        if cutoff_on or invert_on:
            options = {
                name: qif_import.QifAccountOptions(
                    cutoff=cutoff if cutoff_on else None, invert=invert_on
                )
                for name in account_map
            }

        reset_db(migrated_engine)
        with get_sessionmaker()() as s:
            for name, path, type_ in ACCOUNTS:
                ls.create_account(s, name=name, type_=type_, path=path, actor_token_id=None)
            s.commit()

            first = qif_import.import_qif(
                s,
                content=content,
                account_map=account_map,
                account_options=options,
                fmt=fmt,
                token=None,
            )
            s.commit()
            before = s.execute(text("SELECT COUNT(*) FROM transactions")).scalar_one()
            statuses_before = s.execute(
                text("SELECT status, COUNT(*) FROM transactions GROUP BY status ORDER BY status")
            ).all()

            preview = qif_import.preview(
                s, content, account_map=account_map, account_options=options, fmt=fmt
            )
            second = qif_import.import_qif(
                s,
                content=content,
                account_map=account_map,
                account_options=options,
                fmt=fmt,
                token=None,
            )
            s.commit()
            after = s.execute(text("SELECT COUNT(*) FROM transactions")).scalar_one()
            statuses_after = s.execute(
                text("SELECT status, COUNT(*) FROM transactions GROUP BY status ORDER BY status")
            ).all()

        assert after == before
        assert statuses_after == statuses_before
        assert {a["name"]: a["counts"] for a in preview["accounts"]} == second["per_account"]
        for key in ("imported", "transfers_created", "opening_balances", "one_leg_transfers"):
            assert second[key] == 0, key
        assert second["uncleared"] == 0
        assert second["skipped_scheduled"] == first["skipped_scheduled"]
        assert first["skipped_scheduled"] == (1 if fmt == "csv" else 0)


# ---------------------------------------------------------------------------
# A later file that marks the row cleared promotes it (issue #310)
# ---------------------------------------------------------------------------

CHECK_DESCRIPTION = "Check 1001 Landlord Properties"


def _check_qif(mark: str) -> str:
    """Checking: a reconciled deposit (so the account carries marks) and
    check 1001 carrying ``mark`` ('' = uncleared)."""
    check_mark = f"C{mark}\n" if mark else ""
    return (
        "!Account\nNChecking\nTBank\n^\n!Type:Bank\n"
        "D3/1/2024\nT2500.00\nCR\nPAcme Payroll\nLSalary\n^\n"
        f"D3/8/2024\nT-1200.00\n{check_mark}P{CHECK_DESCRIPTION}\nLRent\n^\n"
    )


# The #249 export a month later: check 1001 and the interest row have cleared.
CLEARED_CSV = QUICKEN_CSV.replace(",March rent,,Checking", ",March rent,c,Checking").replace(
    ",Interest Inc,1.25,,,Savings", ",Interest Inc,1.25,,c,Savings"
)

CHECKING_MAP = {"Checking": "assets.checking"}


def _tx_id(engine: Engine, content_hash: str) -> str:
    with engine.connect() as conn:
        return str(
            conn.execute(
                text("SELECT id FROM transactions WHERE content_hash = :h"), {"h": content_hash}
            ).scalar_one()
        )


def _audit_afters(engine: Engine, action: str, entity_id: str) -> list[dict[str, Any]]:
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from purser.models import AuditLog

    with Session(engine) as s:
        rows = s.execute(
            select(AuditLog).where(AuditLog.action == action, AuditLog.entity_id == entity_id)
        ).scalars()
        return [row.before_after["after"] for row in rows if row.before_after]


def _event_count(engine: Engine, type_: str, entity_id: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT COUNT(*) FROM events WHERE type = :t AND entity_id = :e"),
                {"t": type_, "e": entity_id},
            ).scalar_one()
        )


class TestPromoteOnReimport:
    def test_a_row_marked_cleared_later_posts_the_expected_row(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        """The #310 acceptance: import uncleared, then the same row cleared —
        one posted row, no duplicate, preview == commit, audited."""
        first = client.post(
            "/import/qif", json={"content": _check_qif(""), "account_map": CHECKING_MAP}
        ).json()
        assert first["uncleared"] == 1
        assert _register(client, "assets.checking")[CHECK_DESCRIPTION] == "expected"
        assert _balances(client)["assets.checking"] == Decimal("2500.00")
        before = _tx_count(migrated_engine)

        body = {"content": _check_qif("c"), "account_map": CHECKING_MAP}
        preview = client.post("/import/qif/preview", json=body).json()
        counts = _counts(preview)["Checking"]
        assert (counts["promoted"], counts["skipped_dedup"], counts["would_import"]) == (1, 1, 0)
        assert counts["in_file"] == 2

        resp = client.post("/import/qif", json=body)
        assert resp.status_code == 200, resp.text
        commit = resp.json()
        assert commit["per_account"] == _counts(preview)  # preview == commit (#144)
        assert (commit["promoted"], commit["imported"], commit["skipped"]) == (1, 0, 1)

        assert _tx_count(migrated_engine) == before  # promoted in place, no duplicate
        assert _register(client, "assets.checking")[CHECK_DESCRIPTION] == "posted"
        assert _balances(client)["assets.checking"] == Decimal("1300.00")

        check = _tx_id(
            migrated_engine, _flat_hash("Checking", date(2024, 3, 8), "-1200.00", CHECK_DESCRIPTION)
        )
        (after,) = _audit_afters(migrated_engine, "transaction.posted", check)
        assert after == {"status": "posted", "reason": "cleared_in_file", "source": "qif.import"}
        assert _event_count(migrated_engine, "transaction.posted", check) == 1

        # Posted now: the next re-import dedups it like any posted row.
        again = client.post("/import/qif", json=body).json()
        assert (again["promoted"], again["skipped"]) == (0, 2)
        assert _tx_count(migrated_engine) == before

    def test_csv_promotes_per_file_account(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        client.post("/import/csv", json={"content": QUICKEN_CSV, "account_map": CSV_MAP})
        before = _tx_count(migrated_engine)

        body = {"content": CLEARED_CSV, "account_map": CSV_MAP}
        preview = client.post("/import/csv/preview", json=body).json()
        commit = client.post("/import/csv", json=body).json()
        assert commit["per_account"] == _counts(preview)
        assert _counts(preview)["Checking"]["promoted"] == 1  # check 1001
        assert _counts(preview)["Savings"]["promoted"] == 1  # the interest row
        assert commit["promoted"] == 2
        assert commit["skipped_scheduled"] == 1  # the reminder is still not history
        assert _tx_count(migrated_engine) == before
        assert _register(client, "assets.checking")[CHECK_1001] == "posted"
        assert _register(client, "assets.savings")["Interest"] == "posted"
        balances = _balances(client)
        assert balances["assets.checking"] == Decimal("714.57")
        assert balances["assets.savings"] == Decimal("501.25")

    def test_expected_transfer_promotes_when_a_leg_is_marked(
        self, seeded: None, client: TestClient
    ) -> None:
        unmarked = {
            "content": _transfer_qif(checking_mark="", savings_mark=""),
            "account_map": TRANSFER_MAP,
        }
        client.post("/import/qif", json=unmarked)
        assert _register(client, "assets.checking")["Transfer to savings"] == "expected"

        marked = {
            "content": _transfer_qif(checking_mark="*", savings_mark=""),
            "account_map": TRANSFER_MAP,
        }
        preview = client.post("/import/qif/preview", json=marked).json()
        commit = client.post("/import/qif", json=marked).json()
        assert commit["per_account"] == _counts(preview)
        # One transfer row, counted per leg like `transfers`.
        assert _counts(preview)["Checking"]["promoted"] == 1
        assert _counts(preview)["Savings"]["promoted"] == 1
        assert commit["transfers_created"] == 0
        assert _register(client, "assets.checking")["Transfer to savings"] == "posted"
        balances = _balances(client)
        assert balances["assets.checking"] == Decimal("-400.00")
        assert balances["assets.savings"] == Decimal("505.00")

    def test_split_one_leg_flat_promotes_with_its_own_leg(
        self, seeded: None, client: TestClient
    ) -> None:
        options = {"Savings": {"cutoff": "2024-03-03"}}
        client.post(
            "/import/qif",
            json={
                "content": _transfer_qif(checking_mark="", savings_mark="R"),
                "account_map": TRANSFER_MAP,
                "account_options": options,
            },
        )
        assert _register(client, "assets.checking")["Transfer to savings"] == "expected"

        body = {
            "content": _transfer_qif(checking_mark="*", savings_mark="R"),
            "account_map": TRANSFER_MAP,
            "account_options": options,
        }
        preview = client.post("/import/qif/preview", json=body).json()
        commit = client.post("/import/qif", json=body).json()
        assert commit["per_account"] == _counts(preview)
        assert _counts(preview)["Checking"]["promoted"] == 1
        assert _counts(preview)["Savings"]["skipped_cutoff"] == 1
        assert _register(client, "assets.checking")["Transfer to savings"] == "posted"
        assert _balances(client)["assets.checking"] == Decimal("-400.00")

    @pytest.mark.parametrize(
        "content",
        [
            # No mark anywhere in the account: cleared by convention only.
            "!Account\nNChecking\nTBank\n^\n!Type:Bank\n"
            "D3/8/2024\nT-1200.00\nPCheck 1001 Landlord Properties\nLRent\n^\n",
            # A CSV without a cleared column carries no marks either.
            "Date,Payee,Amount,Account\n"
            "2024-03-08,Check 1001 Landlord Properties,-1200.00,Checking\n",
        ],
        ids=["unmarked-qif", "csv-without-cleared-column"],
    )
    def test_no_mark_is_no_evidence(self, seeded: None, client: TestClient, content: str) -> None:
        client.post("/import/qif", json={"content": _check_qif(""), "account_map": CHECKING_MAP})
        fmt = "csv" if content.startswith("Date,") else "qif"
        body = {"content": content, "account_map": CHECKING_MAP}
        preview = client.post(f"/import/{fmt}/preview", json=body).json()
        commit = client.post(f"/import/{fmt}", json=body).json()
        assert commit["per_account"] == _counts(preview)
        assert (commit["promoted"], commit["skipped"]) == (0, 1)
        assert _register(client, "assets.checking")[CHECK_DESCRIPTION] == "expected"

    def test_a_csv_without_a_cleared_column_still_imports_posted(
        self, seeded: None, client: TestClient
    ) -> None:
        content = "Date,Payee,Amount\n2024-02-01,Hardware Store,-40.00\n"
        resp = client.post(
            "/import/csv",
            json={"content": content, "account_map": {"__default__": "assets.checking"}},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["uncleared"] == 0
        assert _register(client, "assets.checking") == {"Hardware Store": "posted"}

    def test_feed_connected_account_is_left_to_the_matcher(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        client.post("/import/qif", json={"content": _check_qif(""), "account_map": CHECKING_MAP})
        # The account gains a feed: the bank's own row for the check will
        # arrive there, and the matcher settles the expected row against it.
        from purser.db import get_sessionmaker
        from purser.models import Account, Connection

        with get_sessionmaker()() as s:
            connection = Connection()
            s.add(connection)
            s.flush()
            account = s.execute(
                text("SELECT id FROM accounts WHERE path = 'assets.checking'")
            ).scalar_one()
            checking = s.get(Account, account)
            assert checking is not None
            checking.connection_id = connection.id
            checking.external_id = "ext-checking-310"
            s.commit()

        body = {"content": _check_qif("c"), "account_map": CHECKING_MAP}
        preview = client.post("/import/qif/preview", json=body).json()
        commit = client.post("/import/qif", json=body).json()
        assert commit["per_account"] == _counts(preview)
        assert (commit["promoted"], commit["skipped"]) == (0, 2)
        assert _register(client, "assets.checking")[CHECK_DESCRIPTION] == "expected"


class TestPromotablePredicate:
    """Exactly which existing rows a cleared re-import may post: an expected
    row a file import created, on open accounts without a feed — never a
    schedule instance, a feed row, a hand-made row, or anything not expected."""

    def test_only_expected_file_rows_on_manual_open_accounts(
        self, seeded: None, migrated_engine: Engine
    ) -> None:
        from purser import crypto, qif_import, schedules
        from purser import ledger as ls
        from purser.db import get_sessionmaker
        from purser.models import Connection, RawFetch

        def row(
            s: Any,
            tag: str,
            *,
            account: str = "assets.checking",
            status: core.Status = core.Status.EXPECTED,
            origin: core.Origin = core.Origin.IMPORT,
            **extra: Any,
        ) -> str:
            content_hash = f"i310-{tag}"
            ls.create_flat_transaction(
                s,
                account_ref=account,
                category_ref="expenses.i310",
                amount=Decimal("-10.00"),
                date=date(2024, 3, 8),
                description=tag,
                requested_status=status,
                origin=origin,
                token=None,
                content_hash=content_hash,
                **extra,
            )
            return content_hash

        with get_sessionmaker()() as s:
            ls.create_account(
                s, name="I310", type_="category_expense", path="expenses.i310", actor_token_id=None
            )
            for name, path in (("Fed", "assets.fed"), ("Shut", "assets.shut")):
                ls.create_account(s, name=name, type_="checking", path=path, actor_token_id=None)
            connection = Connection()
            s.add(connection)
            s.flush()
            fetch = RawFetch(
                connection_id=connection.id,
                content_hash=f"i310-fetch-{connection.id}",
                payload_enc=crypto.encrypt("{}"),
            )
            s.add(fetch)
            s.flush()
            schedule = schedules.create_schedule(
                s,
                name="Rent",
                account_ref="assets.checking",
                rrule="FREQ=MONTHLY;BYMONTHDAY=8",
                amount_type="fixed",
                amount=Decimal("-10.00"),
                token=None,
                category_path="expenses.i310",
                dtstart=date(2024, 3, 8),
            )

            eligible = row(s, "eligible")
            hashes = {
                eligible,
                row(s, "posted", status=core.Status.POSTED),
                row(s, "feed-row", source_fetch_id=fetch.id),
                row(s, "instance", schedule_id=schedule.id, occurrence_date=date(2024, 3, 8)),
                row(s, "by-hand", origin=core.Origin.HUMAN),
                row(s, "fed", account="assets.fed"),
                row(s, "shut", account="assets.shut"),
            }
            fed = ls.account_by_path(s, "assets.fed")
            fed.connection_id = connection.id
            fed.external_id = "ext-fed-310"
            s.execute(text("UPDATE accounts SET closed_at = now() WHERE path = 'assets.shut'"))
            s.flush()

            assert qif_import._promotable_hashes(s, hashes) == {eligible}
            # A void row (the matcher superseded it) is not expected any more.
            s.execute(
                text("UPDATE transactions SET status = 'void' WHERE content_hash = :h"),
                {"h": eligible},
            )
            assert qif_import._promotable_hashes(s, hashes) == set()
            s.rollback()


class TestPromotionProperty:
    """Guardrail 3 with promotion: whatever marks the first file carried, a
    second file that marks more rows cleared promotes exactly the rows that
    were expected and are now marked — on a manual account — creates nothing,
    keeps preview == commit, and a re-run of it promotes nothing."""

    @settings(max_examples=15, deadline=None)
    @given(
        first=st.lists(st.booleans(), min_size=3, max_size=3),
        later=st.lists(st.booleans(), min_size=3, max_size=3),
    )
    def test_promotes_exactly_the_newly_marked_expected_rows(
        self, migrated_engine: Engine, first: list[bool], later: list[bool]
    ) -> None:
        from purser import ledger as ls
        from purser import qif_import
        from purser.db import get_sessionmaker

        def qif(marks: list[bool]) -> str:
            rows = "".join(
                f"D3/{day}/2024\nT-{day}.00\n{'Cc\n' if mark else ''}PShop {day}\nLStuff\n^\n"
                for day, mark in zip((2, 3, 4), marks, strict=True)
            )
            # A reconciled deposit keeps the account marked, so an unmarked
            # row reads as uncleared in every example.
            return (
                "!Account\nNChecking\nTBank\n^\n!Type:Bank\n"
                f"D3/1/2024\nT500.00\nCR\nPDeposit\nLIncome\n^\n{rows}"
            )

        def statuses(s: Any) -> list[str]:
            return [
                str(v)
                for v in s.execute(
                    text("SELECT status FROM transactions ORDER BY date, status")
                ).scalars()
            ]

        reset_db(migrated_engine)
        with get_sessionmaker()() as s:
            ls.create_account(
                s, name="Checking", type_="checking", path="assets.checking", actor_token_id=None
            )
            s.commit()

            def run(marks: list[bool]) -> dict[str, Any]:
                content = qif(marks)
                preview = qif_import.preview(s, content, account_map=CHECKING_MAP)
                result = qif_import.import_qif(
                    s, content=content, account_map=CHECKING_MAP, token=None
                )
                s.commit()
                assert result["per_account"] == {
                    a["name"]: a["counts"] for a in preview["accounts"]
                }
                return result

            run(first)
            rows_before = int(s.execute(text("SELECT COUNT(*) FROM transactions")).scalar_one())
            second = run(later)
            newly_marked = sum(1 for was, now in zip(first, later, strict=True) if not was and now)
            assert second["promoted"] == newly_marked
            assert second["imported"] == 0
            assert int(s.execute(text("SELECT COUNT(*) FROM transactions")).scalar_one()) == (
                rows_before
            )
            # Each shop row is posted iff either file marked it.
            assert statuses(s) == ["posted"] + [
                "posted" if (was or now) else "expected"
                for was, now in zip(first, later, strict=True)
            ]
            assert run(later)["promoted"] == 0
