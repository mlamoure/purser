"""CSV import (issue #138): parser layouts + the shared mapping-first flow —
per-account cutoffs, sign inversion, per-account skip (skipped_unmapped), the
preview==commit contract, and column-mapping fallback. CSV rides the QIF
import-v2 planner unchanged, so these focus on what CSV adds."""

from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from purser.core import csv_import as cc

FIXTURES = Path(__file__).parent / "fixtures" / "csv"
AMEX = (FIXTURES / "amex_activity.csv").read_text()
QUICKEN_SINGLE = (FIXTURES / "quicken_single.csv").read_text()
QUICKEN_MULTI = (FIXTURES / "quicken_multi_account.csv").read_text()


# ---------------------------------------------------------------------------
# Parser (pure, no DB)
# ---------------------------------------------------------------------------


class TestParser:
    def test_amex_layout_is_flat_with_mixed_dates_and_amounts(self) -> None:
        parsed = cc.parse_csv(AMEX)
        assert list(parsed.transactions) == [cc.DEFAULT_ACCOUNT]
        rows = parsed.transactions[cc.DEFAULT_ACCOUNT]
        assert len(rows) == 4
        # Amex "Description" is the payee; charges are positive; no categories.
        assert rows[0].payee == "COSTCO WHSE #12"
        assert rows[0].amount == Decimal("85.43")
        assert isinstance(rows[0].amount, Decimal)
        assert rows[0].category is None and rows[0].transfer_account is None
        assert rows[1].amount == Decimal("-500.00")  # payment
        # Quoted field with an embedded comma stays one payee.
        assert rows[3].payee == "DELTA AIR LINES, ATLANTA"

    def test_quicken_multi_account_grouping_transfers_and_negatives(self) -> None:
        parsed = cc.parse_csv(QUICKEN_MULTI)
        assert sorted(parsed.transactions) == ["Checking", "Savings"]
        checking = parsed.transactions["Checking"]
        assert len(checking) == 3
        # Accountant-style "(85.43)" parses to a negative Decimal.
        grocery = checking[1]
        assert grocery.amount == Decimal("-85.43")
        assert grocery.category == "Groceries" and grocery.memo == "weekly shop"
        # [Account] bracket notation becomes a transfer (reused core.qif logic).
        transfer = checking[2]
        assert transfer.transfer_account == "Savings" and transfer.category is None
        assert parsed.transactions["Savings"][0].transfer_account == "Checking"

    def test_iso_and_slashed_dates(self) -> None:
        iso = cc.parse_csv("Date,Payee,Amount\n2024-07-02,X,1.00\n")
        assert iso.transactions[cc.DEFAULT_ACCOUNT][0].date.isoformat() == "2024-07-02"
        mdy = cc.parse_csv("Date,Payee,Amount\n07/02/2024,X,1.00\n")
        assert mdy.transactions[cc.DEFAULT_ACCOUNT][0].date.isoformat() == "2024-07-02"
        dmy = cc.parse_csv("Date,Payee,Amount\n02/07/2024,X,1.00\n", date_format="dmy")
        assert dmy.transactions[cc.DEFAULT_ACCOUNT][0].date.isoformat() == "2024-07-02"

    def test_amount_cleaning(self) -> None:
        parsed = cc.parse_csv('Date,Payee,Amount\n2024-01-01,A,"$1,200.00"\n2024-01-02,B,(50.00)\n')
        rows = parsed.transactions[cc.DEFAULT_ACCOUNT]
        assert rows[0].amount == Decimal("1200.00")
        assert rows[1].amount == Decimal("-50.00")

    def test_column_map_fallback(self) -> None:
        parsed = cc.parse_csv(
            "When,Who,HowMuch\n2024-03-01,Bob,10.00\n",
            column_map={"date": "When", "payee": "Who", "amount": "HowMuch"},
        )
        row = parsed.transactions[cc.DEFAULT_ACCOUNT][0]
        assert row.payee == "Bob" and row.amount == Decimal("10.00")

    def test_unrecognized_layout_raises_with_columns(self) -> None:
        with pytest.raises(cc.CsvLayoutError) as exc:
            cc.parse_csv("When,Who,HowMuch\n2024-03-01,Bob,10.00\n")
        assert exc.value.columns == ["When", "Who", "HowMuch"]

    def test_garbage_amount_is_a_clear_error_never_a_float(self) -> None:
        with pytest.raises(cc.CsvParseError):
            cc.parse_csv("Date,Payee,Amount\n2024-01-01,X,not-money\n")

    def test_bad_column_map_reports_missing_column(self) -> None:
        with pytest.raises(cc.CsvParseError):
            cc.parse_csv("A,B,C\n1,2,3\n", column_map={"date": "A", "amount": "B", "payee": "NOPE"})


# ---------------------------------------------------------------------------
# Service + API (DB-backed, via the shared planner)
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded(admin_client: TestClient) -> None:
    for name, path, type_ in [
        ("Checking", "assets.checking", "checking"),
        ("Savings", "assets.savings", "checking"),
        ("Amex", "liabilities.amex", "credit_card"),
        ("Amex B", "liabilities.amex_b", "credit_card"),
    ]:
        admin_client.post("/accounts", json={"name": name, "type": type_, "path": path})


MULTI_MAP = {"Checking": "assets.checking", "Savings": "assets.savings"}


class TestImport:
    def test_multi_account_transfers_link_not_double(
        self, seeded: None, client: TestClient
    ) -> None:
        resp = client.post("/import/csv", json={"content": QUICKEN_MULTI, "account_map": MULTI_MAP})
        assert resp.status_code == 200, resp.text
        stats = resp.json()
        assert stats["transfers_created"] == 1
        assert stats["imported"] == 3  # payroll, grocery, interest
        assert stats["per_account"]["Checking"]["transfers"] == 1
        assert stats["per_account"]["Savings"]["would_import"] == 1

    def test_audit_action_is_csv_import(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        client.post("/import/csv", json={"content": QUICKEN_MULTI, "account_map": MULTI_MAP})
        with migrated_engine.connect() as conn:
            n = conn.execute(
                text("SELECT COUNT(*) FROM audit_log WHERE action = 'csv.import'")
            ).scalar_one()
        assert n == 1

    def test_reimport_is_a_noop(self, seeded: None, client: TestClient) -> None:
        first = client.post(
            "/import/csv", json={"content": QUICKEN_MULTI, "account_map": MULTI_MAP}
        ).json()
        assert first["imported"] == 3
        second = client.post(
            "/import/csv", json={"content": QUICKEN_MULTI, "account_map": MULTI_MAP}
        ).json()
        assert second["imported"] == 0 and second["transfers_created"] == 0


class TestPreviewEqualsCommit:
    def test_preview_counts_equal_commit_per_account(
        self, seeded: None, client: TestClient
    ) -> None:
        body = {"content": QUICKEN_MULTI, "account_map": MULTI_MAP}
        preview = client.post("/import/csv/preview", json=body).json()
        prev_counts = {a["name"]: a["counts"] for a in preview["accounts"]}
        commit = client.post("/import/csv", json=body).json()
        assert commit["per_account"] == prev_counts

    def test_preview_needs_only_read_scope(self, seeded: None, client: TestClient) -> None:
        # Preview is parse-only (#213): read suffices; the import stays commit-gated.
        raw = client.post("/tokens", json={"name": "reader", "scopes": ["read"]}).json()["token"]
        headers = {"Authorization": f"Bearer {raw}"}
        body = {"content": QUICKEN_MULTI, "account_map": MULTI_MAP}
        ok = client.post("/import/csv/preview", json=body, headers=headers)
        assert ok.status_code == 200, ok.text
        denied = client.post("/import/csv", json=body, headers=headers)
        assert denied.status_code == 403


class TestCutoff:
    def test_cutoff_over_csv_single_account(self, seeded: None, client: TestClient) -> None:
        resp = client.post(
            "/import/csv",
            json={
                "content": QUICKEN_SINGLE,
                "account_map": {"__default__": "assets.checking"},
                "account_options": {"__default__": {"cutoff": "2023-02-01"}},
            },
        )
        stats = resp.json()
        # Jan 15 + Jan 20 strictly older -> import; Mar 1 -> skipped_cutoff.
        assert stats["imported"] == 2 and stats["skipped_cutoff"] == 1


class TestInvert:
    def test_amex_sign_is_suspect_for_a_card(self, seeded: None, client: TestClient) -> None:
        preview = client.post(
            "/import/csv/preview",
            json={"content": AMEX, "account_map": {"__default__": "liabilities.amex"}},
        ).json()
        assert preview["accounts"][0]["sign"]["suspect"] is True

    def test_invert_flips_every_amount(self, seeded: None, client: TestClient) -> None:
        plain = client.post(
            "/import/csv",
            json={"content": AMEX, "account_map": {"__default__": "liabilities.amex"}},
        )
        assert plain.json()["imported"] == 4
        inverted = client.post(
            "/import/csv",
            json={
                "content": AMEX,
                "account_map": {"__default__": "liabilities.amex_b"},
                "account_options": {"__default__": {"invert": True}},
            },
        )
        assert inverted.json()["imported"] == 4
        accounts = {
            a["path"]: Decimal(a["balance"])
            for a in client.get("/accounts", params={"include_balances": True}).json()
        }
        # No opening balance in CSV, so the inverted account is the exact negation.
        assert accounts["liabilities.amex_b"] == -accounts["liabilities.amex"]
        assert accounts["liabilities.amex"] != Decimal("0")


class TestPerAccountSkip:
    def test_skip_counts_unmapped_and_imports_only_the_mapped_account(
        self, seeded: None, client: TestClient
    ) -> None:
        # Map Checking; mark Savings "don't import" (null). The Checking->Savings
        # transfer degrades to a one-leg flat on the imported side.
        resp = client.post(
            "/import/csv",
            json={
                "content": QUICKEN_MULTI,
                "account_map": {"Checking": "assets.checking", "Savings": None},
            },
        )
        assert resp.status_code == 200, resp.text
        stats = resp.json()
        assert stats["skipped_unmapped"] == 2  # both Savings rows
        assert stats["one_leg_transfers"] == 1  # the transfer's imported leg
        assert stats["transfers_created"] == 0
        assert stats["imported"] == 2  # payroll + grocery
        assert stats["per_account"]["Savings"]["skipped_unmapped"] == 2
        assert stats["per_account"]["Savings"]["would_import"] == 0
        assert stats["per_account"]["Checking"]["one_leg_transfers"] == 1

    def test_skip_preview_equals_commit(self, seeded: None, client: TestClient) -> None:
        body = {
            "content": QUICKEN_MULTI,
            "account_map": {"Checking": "assets.checking", "Savings": None},
        }
        preview = client.post("/import/csv/preview", json=body).json()
        prev = {a["name"]: a["counts"] for a in preview["accounts"]}
        commit = client.post("/import/csv", json=body).json()
        assert commit["per_account"] == prev


ZERO_CSV = (
    "Date,Payee,Amount,Category,Memo,Account\n"
    "2020-01-05,Acme Payroll,2500.00,Salary,,Checking\n"
    "2020-01-08,Live Opening Balance,0.00,,,Checking\n"
    "2020-02-01,Zero Marker,0,,,Savings\n"
)


class TestZeroAmount:
    """Issue #144: Quicken exports carry $0 marker rows; the preview counted
    them importable while the commit 422ed at the ledger's nonzero rule."""

    def test_zero_rows_bucketed_and_commit_succeeds(self, seeded: None, client: TestClient) -> None:
        body = {"content": ZERO_CSV, "account_map": MULTI_MAP}
        preview = client.post("/import/csv/preview", json=body).json()
        prev = {a["name"]: a["counts"] for a in preview["accounts"]}
        assert prev["Checking"]["skipped_zero_amount"] == 1
        assert prev["Checking"]["would_import"] == 1
        assert prev["Savings"]["skipped_zero_amount"] == 1
        assert prev["Savings"]["would_import"] == 0

        resp = client.post("/import/csv", json=body)
        assert resp.status_code == 200, resp.text
        commit = resp.json()
        assert commit["per_account"] == prev
        assert commit["skipped_zero_amount"] == 2
        assert commit["imported"] == 1


class TestColumnMappingApi:
    def test_unrecognized_layout_signals_mapping_ui_then_succeeds(
        self, seeded: None, client: TestClient
    ) -> None:
        body_no_map = {
            "content": "When,Who,HowMuch\n2024-03-01,Bob,10.00\n",
            "account_map": {},
        }
        resp = client.post("/import/csv/preview", json=body_no_map)
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["code"] == "csv_layout_unrecognized"
        assert detail["columns"] == ["When", "Who", "HowMuch"]

        # Supplying the column map resolves it.
        ok = client.post(
            "/import/csv/preview",
            json={
                "content": "When,Who,HowMuch\n2024-03-01,Bob,10.00\n",
                "column_map": {"date": "When", "payee": "Who", "amount": "HowMuch"},
            },
        )
        assert ok.status_code == 200
        assert ok.json()["total_transactions"] == 1

    def test_garbage_amount_is_422(self, seeded: None, client: TestClient) -> None:
        resp = client.post(
            "/import/csv",
            json={
                "content": "Date,Payee,Amount\n2024-01-01,X,not-money\n",
                "account_map": {"__default__": "assets.checking"},
            },
        )
        assert resp.status_code == 422
