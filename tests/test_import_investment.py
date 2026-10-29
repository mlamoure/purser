"""QIF investment sections (issue #274 part S, decision 84, DESIGN §7.2).

Records from a ``!Type:Invst`` section are investment actions, not bank rows:
the parser flags them, the planner skips and counts them (``skipped_investment``)
in preview and commit alike, and an investment account the request leaves out
of ``account_map`` is skipped like a mapped-to-null account, so bank legs into
it degrade to one-leg rows instead of failing the commit. The fixture is
synthetic: a Checking (Bank) register and a Brokerage (Invst) register with
Buy, Sell, Div, ShrsIn without an amount, XIn/XOut, a whole-row
``[Brokerage]`` transfer and a split row with an ``S[Brokerage]`` line.
"""

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from purser.core import qif as core_qif

FIXTURE = (Path(__file__).parent / "fixtures" / "quicken" / "investment_sections.qif").read_text()

# Quicken's own Checking register: payroll, the whole-row transfer to the
# brokerage, the split payment (rent plus a contribution) and the transfer
# back. Before #274 the Invst XIn mirror of the split's S[Brokerage] line
# formed a second Checking -> Brokerage transfer and debited Checking twice.
CHECKING_REGISTER = Decimal("2500.00") - Decimal("500.00") - Decimal("1000.00") + Decimal("50.00")
INVESTMENT_RECORDS = 7


@pytest.fixture
def seeded(admin_client: TestClient) -> None:
    for name, type_, path in [
        ("Checking", "checking", "assets.checking"),
        ("Brokerage", "brokerage", "assets.brokerage"),
    ]:
        resp = admin_client.post("/accounts", json={"name": name, "type": type_, "path": path})
        assert resp.status_code == 201, resp.text


def _counts(preview: dict[str, Any]) -> dict[str, dict[str, int]]:
    return {a["name"]: a["counts"] for a in preview["accounts"]}


def _checking_balance(client: TestClient) -> Decimal:
    accounts = client.get("/accounts", params={"include_balances": True}).json()
    (checking,) = [a for a in accounts if a["path"] == "assets.checking"]
    return Decimal(checking["balance"])


def _rows(client: TestClient, path: str) -> list[dict[str, Any]]:
    page = client.get("/transactions", params={"account": path, "limit": 100}).json()
    items: list[dict[str, Any]] = page["items"]
    return items


class TestParser:
    def test_investment_records_are_flagged_and_counted(self) -> None:
        parsed = core_qif.parse(FIXTURE)
        assert dict(parsed.accounts) == {"Checking": "checking", "Brokerage": "brokerage"}
        assert parsed.investment_accounts == {"Brokerage"}

        brokerage = parsed.transactions["Brokerage"]
        assert len(brokerage) == INVESTMENT_RECORDS
        assert all(t.investment for t in brokerage)
        assert all(t.category is None and t.transfer_account is None for t in brokerage)
        amounts = [t.amount for t in brokerage]
        # Lenient amounts: T as written; ShrsIn carries none and reads 0.
        assert amounts == [
            Decimal("500.00"),
            Decimal("400.00"),
            Decimal("12.34"),
            Decimal("101.00"),
            Decimal("200.00"),
            Decimal("0"),
            Decimal("50.00"),
        ]

        checking = parsed.transactions["Checking"]
        assert len(checking) == 4
        assert not any(t.investment for t in checking)
        assert checking[1].transfer_account == "Brokerage"

    def test_single_account_invst_file_never_raises(self) -> None:
        """No !Account block, a mixed-case header, a garbage amount and a
        garbage date: every dated record is still kept and flagged."""
        content = (
            "!Type:INVST\n"
            "D1/6'26\nNBuy\nYSYNTHETIC INDEX FUND\nT400.00\n^\n"
            "D1/7'26\nNReinvDiv\nTnot-a-number\n^\n"
            "Dsometime\nNMiscExp\nT5.00\n^\n"
            "NNo date at all\nT1.00\n^\n"
        )
        parsed = core_qif.parse(content)
        assert parsed.accounts == [(core_qif.DEFAULT_ACCOUNT, "brokerage")]
        assert parsed.investment_accounts == {core_qif.DEFAULT_ACCOUNT}
        rows = parsed.transactions[core_qif.DEFAULT_ACCOUNT]
        assert [t.amount for t in rows] == [Decimal("400.00"), Decimal("0"), Decimal("5.00")]
        assert all(t.investment for t in rows)

    def test_declared_investment_type_without_a_section(self) -> None:
        """An account list declaring a 401(k) counts it as an investment
        account even when the file carries none of its records."""
        content = (
            "!Account\nNRetirement\nT401(k)/403(b)\n^\n"
            "!Account\nNChecking\nTBank\n^\n!Type:Bank\n"
            "D1/5'26\nT-100.00\nPContribution\nL[Retirement]\n^\n"
        )
        parsed = core_qif.parse(content)
        assert parsed.investment_accounts == {"Retirement"}
        assert dict(parsed.accounts)["Retirement"] == "brokerage"
        assert "Retirement" not in parsed.transactions

    def test_bank_sections_after_an_invst_section_are_bank_rows(self) -> None:
        content = (
            "!Account\nNBrokerage\nTInvst\n^\n!Type:Invst\nD1/6'26\nNBuy\nT400.00\n^\n"
            "!Account\nNChecking\nTBank\n^\n!Type:Bank\nD1/7'26\nT-20.00\nPGrocer\n^\n"
        )
        parsed = core_qif.parse(content)
        (grocer,) = parsed.transactions["Checking"]
        assert grocer.investment is False
        assert grocer.amount == Decimal("-20.00")


class TestImportInvestmentSections:
    def test_unmapped_investment_account_is_left_out(
        self, seeded: None, client: TestClient
    ) -> None:
        body = {"content": FIXTURE, "account_map": {"Checking": "assets.checking"}}
        preview = client.post("/import/qif/preview", json=body).json()
        counts = _counts(preview)
        assert counts["Brokerage"]["in_file"] == INVESTMENT_RECORDS
        assert counts["Brokerage"]["skipped_investment"] == INVESTMENT_RECORDS
        assert counts["Brokerage"]["skipped_unmapped"] == 0
        # Bank legs into the left-out account degrade to one-leg rows.
        assert counts["Checking"]["one_leg_transfers"] == 2
        assert counts["Checking"]["would_import"] == 2  # payroll + the split row
        assert counts["Checking"]["transfers"] == 0
        assert counts["Checking"]["in_file"] == 4
        brokerage_out = next(a for a in preview["accounts"] if a["name"] == "Brokerage")
        assert brokerage_out["type"] == "brokerage"
        assert brokerage_out["sign"] == {
            "positive": "0",
            "negative": "0",
            "net": "0",
            "suspect": False,
        }

        resp = client.post("/import/qif", json=body)
        assert resp.status_code == 200, resp.text  # no "no mapping" refusal
        result = resp.json()
        assert result["per_account"] == counts
        assert result["skipped_investment"] == INVESTMENT_RECORDS
        assert result["transfers_created"] == 0
        assert _checking_balance(client) == CHECKING_REGISTER
        assert _rows(client, "assets.brokerage") == []

        again = client.post("/import/qif", json=body).json()
        assert again["imported"] == 0
        assert again["one_leg_transfers"] == 0
        assert again["skipped_investment"] == INVESTMENT_RECORDS
        assert _checking_balance(client) == CHECKING_REGISTER

    def test_mapped_investment_account_gets_one_transfer_per_pair(
        self, seeded: None, client: TestClient
    ) -> None:
        body = {
            "content": FIXTURE,
            "account_map": {"Checking": "assets.checking", "Brokerage": "assets.brokerage"},
        }
        preview = client.post("/import/qif/preview", json=body).json()
        counts = _counts(preview)
        assert counts["Brokerage"]["skipped_investment"] == INVESTMENT_RECORDS
        assert counts["Brokerage"]["transfers"] == 0  # its XIn/XOut never pair
        assert counts["Checking"]["transfers"] == 2
        assert counts["Checking"]["one_leg_transfers"] == 0
        # The split's S[Brokerage] line is carved out as a transfer of its own
        # (decision 87); its Invst XIn mirror never pairs.
        assert counts["Checking"]["split_transfers"] == 1

        resp = client.post("/import/qif", json=body)
        assert resp.status_code == 200, resp.text
        result = resp.json()
        assert result["per_account"] == counts
        assert result["transfers_created"] == 3
        # Checking matches Quicken: the split's S[Brokerage] mirror no longer
        # books a second transfer.
        assert _checking_balance(client) == CHECKING_REGISTER
        brokerage_rows = _rows(client, "assets.brokerage")
        assert sorted(Decimal(r["amount"]) for r in brokerage_rows) == [
            Decimal("-50.00"),
            Decimal("200.00"),
            Decimal("500.00"),
        ]

        again = client.post("/import/qif", json=body).json()
        assert again["imported"] == 0
        assert again["transfers_created"] == 0
        assert len(_rows(client, "assets.brokerage")) == 3

    def test_preview_writes_nothing(self, seeded: None, client: TestClient) -> None:
        client.post(
            "/import/qif/preview",
            json={"content": FIXTURE, "account_map": {"Checking": "assets.checking"}},
        )
        page = client.get("/transactions", params={"include_collapsed": True}).json()
        assert page["total"] == 0
