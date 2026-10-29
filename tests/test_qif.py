"""QIF import: parsing, transfer linking, idempotency, opening balances, and
the v2 mapping-first flow — per-account cutoffs, sign inversion, split-transfer
degradation, and DB-aware preview counts (issue #125)."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, text

from purser.core import ledger as core
from purser.core import qif as core_qif
from tests.conftest import reset_db

FIXTURE = (Path(__file__).parent / "fixtures" / "quicken" / "multi_account.qif").read_text()
# Same file-account names (Checking/Savings) so ACCOUNT_MAP covers both formats.
CSV_FIXTURE = (Path(__file__).parent / "fixtures" / "csv" / "quicken_multi_account.csv").read_text()


class TestParser:
    def test_dates_in_every_quicken_style(self) -> None:
        assert core_qif.parse_date("1/15'04") == date(2004, 1, 15)
        assert core_qif.parse_date("01/15/2004") == date(2004, 1, 15)
        assert core_qif.parse_date("1/15/98") == date(1998, 1, 15)
        assert core_qif.parse_date("15/1/2004", date_format="dmy") == date(2004, 1, 15)
        # File disagreeing with the stated format still resolves when unambiguous.
        assert core_qif.parse_date("25/12/2004", date_format="mdy") == date(2004, 12, 25)
        with pytest.raises(core_qif.QifParseError):
            core_qif.parse_date("not-a-date")

    def test_a_file_that_is_not_qif_is_refused_by_name(self) -> None:
        """#427: the wrong file must not preview as "nothing found"."""
        with pytest.raises(core_qif.QifParseError, match="not a QIF file"):
            core_qif.parse("Date,Description,Amount\n2026-07-01,Example,-4.20\n")
        # A real export with a header and no rows is an empty file, not an error.
        assert core_qif.parse("!Type:Bank\n").transactions == {}

    def test_multi_account_file(self) -> None:
        parsed = core_qif.parse(FIXTURE)
        assert dict(parsed.accounts) == {"Checking": "checking", "Savings": "checking"}
        assert len(parsed.transactions["Checking"]) == 7
        assert len(parsed.transactions["Savings"]) == 3

        rent = parsed.transactions["Checking"][1]
        assert rent.amount == Decimal("-1200.00")  # comma stripped
        assert rent.category == "Housing:Rent"
        assert rent.transfer_account is None

        transfer = parsed.transactions["Checking"][3]
        assert transfer.transfer_account == "Savings"
        assert transfer.category is None

        opening = parsed.transactions["Checking"][0]
        assert opening.transfer_account == "Checking"  # self-transfer convention


@pytest.fixture
def seeded(admin_client: TestClient) -> None:
    for name, path in [("Checking", "assets.checking"), ("Savings", "assets.savings")]:
        admin_client.post("/accounts", json={"name": name, "type": "checking", "path": path})


ACCOUNT_MAP = {"Checking": "assets.checking", "Savings": "assets.savings"}


class TestImport:
    def test_multi_year_import_without_double_counted_transfers(
        self, seeded: None, client: TestClient
    ) -> None:
        resp = client.post("/import/qif", json={"content": FIXTURE, "account_map": ACCOUNT_MAP})
        assert resp.status_code == 200, resp.text
        stats = resp.json()
        # 3 transfer legs pairs -> 2 distinct transfers (500 in Feb, 2000 in Jun)
        assert stats["transfers_created"] == 2
        assert stats["opening_balances"] == 2
        assert stats["imported"] == 4  # rent x2, groceries, salary

        # Each transfer is ONE transaction with two real postings.
        page = client.get(
            "/transactions", params={"q": "Transfer", "include_collapsed": True}
        ).json()
        transfers = [t for t in page["items"] if t["is_transfer"]]
        assert len(transfers) == 2

    def test_opening_balances_reconcile_at_cutover(self, seeded: None, client: TestClient) -> None:
        client.post("/import/qif", json={"content": FIXTURE, "account_map": ACCOUNT_MAP})
        accounts = client.get("/accounts", params={"include_balances": True}).json()
        by_path = {a["path"]: Decimal(a["balance"]) for a in accounts}
        # Checking: 2500 - 1200 - 85.43 - 500 - 1200 + 3000 + 2000 = 4514.57
        assert by_path["assets.checking"] == Decimal("4514.57")
        # Savings: 1000 + 500 - 2000 = -500
        assert by_path["assets.savings"] == Decimal("-500.00")

    def test_rerun_is_a_noop(self, seeded: None, client: TestClient) -> None:
        first = client.post(
            "/import/qif", json={"content": FIXTURE, "account_map": ACCOUNT_MAP}
        ).json()
        assert first["imported"] > 0
        second = client.post(
            "/import/qif", json={"content": FIXTURE, "account_map": ACCOUNT_MAP}
        ).json()
        assert second["imported"] == 0
        assert second["transfers_created"] == 0
        assert second["opening_balances"] == 0

        # Balances unchanged after the re-run.
        accounts = client.get("/accounts", params={"include_balances": True}).json()
        by_path = {a["path"]: Decimal(a["balance"]) for a in accounts}
        assert by_path["assets.checking"] == Decimal("4514.57")

    def test_categories_map_and_autocreate(self, seeded: None, client: TestClient) -> None:
        resp = client.post(
            "/import/qif",
            json={
                "content": FIXTURE,
                "account_map": ACCOUNT_MAP,
                "category_map": {"Groceries": "expenses.food.groceries"},
            },
        )
        created = set(resp.json()["categories_created"])
        # Mapped category created at the mapped path; unmapped ones auto-slot.
        assert "expenses.food.groceries" in created
        assert "expenses.housing.rent" in created
        assert "income.salary" in created

        rollup = client.get("/categories/expenses.housing/rollup").json()
        assert Decimal(rollup["total"]) == Decimal("2400.00")

    def test_preview_is_read_only(self, seeded: None, client: TestClient) -> None:
        resp = client.post("/import/qif/preview", json={"content": FIXTURE})
        preview = resp.json()
        assert {a["name"] for a in preview["accounts"]} == {"Checking", "Savings"}
        assert "Housing:Rent" in preview["categories"]
        assert preview["date_range"]["start"] == "2023-01-02"
        assert preview["date_range"]["end"] == "2024-06-15"
        # Nothing written.
        page = client.get("/transactions", params={"include_collapsed": True}).json()
        assert page["total"] == 0

    def test_import_archives_verbatim_raw_first(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        """#127 / decision 53: the upload lands in raw_fetches (source='qif',
        no connection), decrypts back to the verbatim file, dedups on
        re-import, and the ledger rows keep source_fetch_id NULL."""
        import hashlib

        from purser import crypto

        # raw_fetches is append-only and NEVER truncated between tests, so
        # every assertion filters by this file's content hash.
        fixture_hash = hashlib.sha256(FIXTURE.encode()).hexdigest()
        client.post("/import/qif", json={"content": FIXTURE, "account_map": ACCOUNT_MAP})
        with migrated_engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT connection_id, payload_enc FROM raw_fetches"
                    " WHERE source = 'qif' AND content_hash = :h"
                ),
                {"h": fixture_hash},
            ).all()
        assert len(rows) == 1
        connection_id, payload_enc = rows[0]
        assert connection_id is None
        assert crypto.decrypt(bytes(payload_enc)) == FIXTURE

        # Idempotent end to end: the re-run archives nothing new either.
        client.post("/import/qif", json={"content": FIXTURE, "account_map": ACCOUNT_MAP})
        with migrated_engine.connect() as conn:
            archived = conn.execute(
                text("SELECT COUNT(*) FROM raw_fetches WHERE source = 'qif' AND content_hash = :h"),
                {"h": fixture_hash},
            ).scalar_one()
            feed_linked = conn.execute(
                text("SELECT COUNT(*) FROM transactions WHERE source_fetch_id IS NOT NULL")
            ).scalar_one()
        assert archived == 1
        assert feed_linked == 0  # the doubly load-bearing NULL (PR-7 + #175)

    def test_preview_needs_only_read_scope(self, seeded: None, client: TestClient) -> None:
        # Preview is parse-only (#213): read suffices; the import stays commit-gated.
        raw = client.post("/tokens", json={"name": "reader", "scopes": ["read"]}).json()["token"]
        headers = {"Authorization": f"Bearer {raw}"}
        ok = client.post("/import/qif/preview", json={"content": FIXTURE}, headers=headers)
        assert ok.status_code == 200, ok.text
        denied = client.post(
            "/import/qif",
            json={"content": FIXTURE, "account_map": ACCOUNT_MAP},
            headers=headers,
        )
        assert denied.status_code == 403

    def test_unmapped_account_is_clear_error(self, seeded: None, client: TestClient) -> None:
        resp = client.post(
            "/import/qif",
            json={"content": FIXTURE, "account_map": {"Checking": "assets.checking"}},
        )
        assert resp.status_code == 422
        assert "Savings" in resp.text


# ---------------------------------------------------------------------------
# v2: cutoffs, invert, split-transfer degradation, DB-aware preview (issue #125)
# ---------------------------------------------------------------------------

# Single account, three dated rows — used to exercise the strictly-older rule.
CUTOFF_QIF = """!Account
NChecking
TBank
^
!Type:Bank
D1/10/2024
T-100.00
PStore A
LGroceries
^
D2/10/2024
T-200.00
PStore B
LGroceries
^
D3/10/2024
T-300.00
PStore C
LGroceries
^
"""

# One 500.00 transfer between two accounts on 2024-06-15.
TRANSFER_QIF = """!Account
NChecking
TBank
^
!Type:Bank
D6/15/2024
T-500.00
PTransfer to savings
L[Savings]
^
!Account
NSavings
TBank
^
!Type:Bank
D6/15/2024
T500.00
PTransfer from checking
L[Checking]
^
"""

# A credit-card export: opening balance, an expense, and a payment credit.
CARD_QIF = """!Account
NCard
TCCard
^
!Type:CCard
D1/2/2024
T1000.00
POpening Balance
L[Card]
^
D1/10/2024
T-100.00
PStore
LShopping
^
D2/10/2024
T50.00
PRefund
^
"""

# A positive-heavy card file (bank exported spending as positive) — the sign
# diagnostic should flag it suspect when mapped to a credit_card/loan account.
SUSPECT_QIF = """!Account
NCard
TCCard
^
!Type:CCard
D1/10/2024
T100.00
PA
^
D2/10/2024
T200.00
PB
^
D3/10/2024
T-50.00
PC
^
"""


def _make_account(client: TestClient, *, name: str, type_: str, path: str) -> None:
    resp = client.post("/accounts", json={"name": name, "type": type_, "path": path})
    assert resp.status_code == 201, resp.text


def _seed_feed_flat(
    *, account_path: str, when: date, tag: str, amount: str, description: str = "feed seed"
) -> None:
    """A genuine feed-sourced (source_fetch_id set) flat transaction so the
    preview's default_cutoff query and the auto-link pass have something real
    to find. Balanced double-entry — no floats anywhere."""
    from purser import ledger as ls
    from purser.db import get_sessionmaker
    from purser.models import Connection, RawFetch

    with get_sessionmaker()() as s:
        conn = Connection(status="active")
        s.add(conn)
        s.flush()
        # Real ciphertext, not junk bytes: connections are never truncated
        # between tests, and GET /connections decrypts every connection's
        # latest payload — an undecryptable seed poisons later test files.
        from purser import crypto

        rf = RawFetch(
            connection_id=conn.id, content_hash=f"rf-{tag}", payload_enc=crypto.encrypt("{}")
        )
        s.add(rf)
        s.flush()
        income = Decimal(amount) > 0
        cat_path = "income.feed_seed" if income else "expenses.feed_seed"
        try:
            cat = ls.account_by_path(s, cat_path)
        except ls.NotFoundError:
            cat = ls.create_account(
                s,
                name="Feed Seed",
                type_="category_income" if income else "category_expense",
                path=cat_path,
                actor_token_id=None,
            )
        acct = ls.resolve_account(s, account_path)
        ls.create_flat_transaction(
            s,
            account_ref=str(acct.id),
            category_ref=str(cat.id),
            amount=Decimal(amount),
            date=when,
            payee_name=description,
            description=description,
            requested_status=core.Status.POSTED,
            origin=core.Origin.IMPORT,
            token=None,
            source_fetch_id=rf.id,
            content_hash=f"feedhash-{tag}",
        )
        s.commit()


def _run_auto_link() -> None:
    from purser import transfers
    from purser.db import get_sessionmaker

    with get_sessionmaker()() as s:
        transfers.auto_link_pass(s)
        s.commit()


def _hash_present(engine: Engine, content_hash: str) -> bool:
    with engine.connect() as conn:
        return (
            conn.execute(
                text("SELECT 1 FROM transactions WHERE content_hash = :h"), {"h": content_hash}
            ).scalar_one_or_none()
            is not None
        )


class TestCutoffs:
    def test_strictly_older_imported_boundary_and_newer_skipped(
        self, seeded: None, client: TestClient
    ) -> None:
        resp = client.post(
            "/import/qif",
            json={
                "content": CUTOFF_QIF,
                "account_map": {"Checking": "assets.checking"},
                "account_options": {"Checking": {"cutoff": "2024-02-10"}},
            },
        )
        assert resp.status_code == 200, resp.text
        stats = resp.json()
        # 1/10 is strictly older -> imported; 2/10 (boundary) and 3/10 -> skipped.
        assert stats["imported"] == 1
        assert stats["skipped_cutoff"] == 2
        assert stats["per_account"]["Checking"]["would_import"] == 1
        assert stats["per_account"]["Checking"]["skipped_cutoff"] == 2

    def test_cleared_cutoff_imports_everything(self, seeded: None, client: TestClient) -> None:
        resp = client.post(
            "/import/qif",
            json={
                "content": CUTOFF_QIF,
                "account_map": {"Checking": "assets.checking"},
                # no account_options -> the server imposes no cutoff
            },
        )
        assert resp.json()["imported"] == 3

    def test_default_cutoff_is_earliest_feed_minus_three_days(
        self, seeded: None, client: TestClient
    ) -> None:
        _seed_feed_flat(
            account_path="assets.checking", when=date(2024, 3, 10), tag="chk", amount="-50.00"
        )
        resp = client.post(
            "/import/qif/preview",
            json={"content": FIXTURE, "account_map": ACCOUNT_MAP},
        )
        assert resp.status_code == 200, resp.text
        accounts = {a["name"]: a for a in resp.json()["accounts"]}
        # Feed's earliest posted date on checking is 2024-03-10 -> suggest 3 days before.
        assert accounts["Checking"]["default_cutoff"] == "2024-03-07"
        # Savings has no feed rows at all -> no suggestion.
        assert accounts["Savings"]["default_cutoff"] is None


class TestTransferCutoffs:
    def test_both_older_creates_two_leg(self, seeded: None, client: TestClient) -> None:
        resp = client.post(
            "/import/qif", json={"content": TRANSFER_QIF, "account_map": ACCOUNT_MAP}
        )
        stats = resp.json()
        assert stats["transfers_created"] == 1
        assert stats["one_leg_transfers"] == 0
        assert stats["per_account"]["Checking"]["transfers"] == 1
        assert stats["per_account"]["Savings"]["transfers"] == 1
        page = client.get(
            "/transactions", params={"q": "Transfer", "include_collapsed": True}
        ).json()
        assert sum(1 for t in page["items"] if t["is_transfer"]) == 1

    def test_both_inside_skipped_by_cutoff(self, seeded: None, client: TestClient) -> None:
        resp = client.post(
            "/import/qif",
            json={
                "content": TRANSFER_QIF,
                "account_map": ACCOUNT_MAP,
                "account_options": {
                    "Checking": {"cutoff": "2024-01-01"},
                    "Savings": {"cutoff": "2024-01-01"},
                },
            },
        )
        stats = resp.json()
        assert stats["transfers_created"] == 0
        assert stats["skipped_cutoff"] == 2
        assert stats["per_account"]["Checking"]["skipped_cutoff"] == 1
        assert stats["per_account"]["Savings"]["skipped_cutoff"] == 1

    def test_split_degrades_to_one_leg_then_auto_links(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        # Checking older (no cutoff), Savings inside (feed owns from 2024-06-01):
        # the transfer degrades to a one-leg flat on Checking, never a 2-leg.
        resp = client.post(
            "/import/qif",
            json={
                "content": TRANSFER_QIF,
                "account_map": ACCOUNT_MAP,
                "account_options": {"Savings": {"cutoff": "2024-06-01"}},
            },
        )
        stats = resp.json()
        assert stats["transfers_created"] == 0
        assert stats["one_leg_transfers"] == 1
        assert stats["per_account"]["Checking"]["one_leg_transfers"] == 1
        assert stats["per_account"]["Savings"]["skipped_cutoff"] == 1

        # The flat row exists carrying the Checking leg's qif: content hash
        # (keyed HMAC form since #137 — the stored value is keyed_hash(sha256)).
        from purser import crypto

        leg_hash = crypto.keyed_hash(
            core.content_hash(
                external_account_id="qif:Checking",
                date=date(2024, 6, 15),
                amount=Decimal("-500.00"),
                description="Transfer to savings",
            )
        )
        assert _hash_present(migrated_engine, leg_hash)
        # Only the one leg landed — no phantom Savings row yet.
        checking = client.get("/transactions", params={"account": "assets.checking"}).json()
        assert len(checking["items"]) == 1
        assert checking["items"][0]["is_transfer"] is False

        # The feed later reports the Savings counterpart; auto-link claims the
        # one-leg flat into a real transfer instead of duplicating it.
        _seed_feed_flat(
            account_path="assets.savings",
            when=date(2024, 6, 15),
            tag="sav-counter",
            amount="500.00",
            description="TRANSFER FROM CHECKING",
        )
        _run_auto_link()

        checking = client.get("/transactions", params={"account": "assets.checking"}).json()
        (row,) = checking["items"]
        assert row["is_transfer"] is True
        assert row["transfer_account_path"] == "assets.savings"
        balances = {
            a["path"]: Decimal(a["balance"])
            for a in client.get("/accounts", params={"include_balances": True}).json()
        }
        assert balances["assets.checking"] == Decimal("-500.00")
        assert balances["assets.savings"] == Decimal("500.00")

    def test_both_older_flat_hash_guard_skips_after_prior_split(
        self, seeded: None, client: TestClient
    ) -> None:
        # First import as a split -> a one-leg flat on Checking.
        first = client.post(
            "/import/qif",
            json={
                "content": TRANSFER_QIF,
                "account_map": ACCOUNT_MAP,
                "account_options": {"Savings": {"cutoff": "2024-06-01"}},
            },
        ).json()
        assert first["one_leg_transfers"] == 1

        # Re-import with both older (Savings cutoff cleared): the guard sees the
        # existing flat leg and refuses to build a 2-leg that would duplicate it.
        second = client.post(
            "/import/qif", json={"content": TRANSFER_QIF, "account_map": ACCOUNT_MAP}
        ).json()
        assert second["transfers_created"] == 0
        assert second["per_account"]["Checking"]["skipped_dedup"] == 1
        assert second["per_account"]["Savings"]["skipped_dedup"] == 1

        # Checking balance is still just the single -500 leg (no duplication).
        balances = {
            a["path"]: Decimal(a["balance"])
            for a in client.get("/accounts", params={"include_balances": True}).json()
        }
        assert balances["assets.checking"] == Decimal("-500.00")


class TestInvert:
    def test_invert_flips_amounts_category_root_and_opening(
        self, seeded: None, client: TestClient
    ) -> None:
        _make_account(client, name="Card", type_="credit_card", path="liabilities.card")
        resp = client.post(
            "/import/qif",
            json={
                "content": CARD_QIF,
                "account_map": {"Card": "liabilities.card"},
                "account_options": {"Card": {"invert": True}},
            },
        )
        assert resp.status_code == 200, resp.text
        created = set(resp.json()["categories_created"])
        # -100 Shopping raw -> +100 after invert -> income root (not expenses).
        assert "income.shopping" in created
        assert "expenses.shopping" not in created
        # Opening +1000 raw -> -1000; expense +100; refund -50 -> balance -950.
        balances = {
            a["path"]: Decimal(a["balance"])
            for a in client.get(
                "/accounts", params={"include_balances": True, "include_virtual": False}
            ).json()
        }
        assert balances["liabilities.card"] == Decimal("-950.00")

    def test_suspect_flag_set_for_positive_heavy_card(
        self, seeded: None, client: TestClient
    ) -> None:
        _make_account(client, name="Card", type_="credit_card", path="liabilities.card")
        resp = client.post(
            "/import/qif/preview",
            json={"content": SUSPECT_QIF, "account_map": {"Card": "liabilities.card"}},
        )
        card = next(a for a in resp.json()["accounts"] if a["name"] == "Card")
        assert card["sign"]["suspect"] is True
        assert Decimal(card["sign"]["net"]) == Decimal("250.00")

    def test_not_suspect_when_mapped_to_checking(self, seeded: None, client: TestClient) -> None:
        resp = client.post(
            "/import/qif/preview",
            json={"content": SUSPECT_QIF, "account_map": {"Card": "assets.checking"}},
        )
        card = next(a for a in resp.json()["accounts"] if a["name"] == "Card")
        assert card["sign"]["suspect"] is False

    def test_reimport_with_invert_is_a_disjoint_hash_set(
        self, seeded: None, client: TestClient
    ) -> None:
        _make_account(client, name="Card", type_="credit_card", path="liabilities.card")
        base = {"content": CARD_QIF, "account_map": {"Card": "liabilities.card"}}
        assert client.post("/import/qif", json=base).status_code == 200

        # Re-preview the SAME file with invert flipped: inverted amounts hash to
        # a disjoint set, so NOTHING dedups — the footgun the preview surfaces.
        preview = client.post(
            "/import/qif/preview",
            json={**base, "account_options": {"Card": {"invert": True}}},
        ).json()
        card = next(a for a in preview["accounts"] if a["name"] == "Card")
        assert card["counts"]["in_file"] == 3
        assert card["counts"]["skipped_dedup"] == 0
        assert card["counts"]["would_import"] == 2  # the two flats
        assert card["counts"]["opening_balances"] == 1


class TestPreviewV2:
    def test_preview_counts_equal_commit_results(self, seeded: None, client: TestClient) -> None:
        body = {
            "content": TRANSFER_QIF,
            "account_map": ACCOUNT_MAP,
            "account_options": {"Savings": {"cutoff": "2024-06-01"}},
        }
        preview = client.post("/import/qif/preview", json=body).json()
        preview_counts = {a["name"]: a["counts"] for a in preview["accounts"]}

        result = client.post("/import/qif", json=body).json()
        assert result["per_account"] == preview_counts

    def test_zero_amount_rows_bucketed_in_every_shape(
        self, seeded: None, client: TestClient
    ) -> None:
        # Issue #144, QIF side: a zero opening balance (self-transfer), a zero
        # transfer pair, and one real row. Zeros land in skipped_zero_amount on
        # both paths; the commit no longer 422s.
        zero_qif = (
            "!Account\nNChecking\nTBank\n^\n!Type:Bank\n"
            "D1/2'23\nT0.00\nPOpening Balance\nL[Checking]\n^\n"
            "D1/15'23\nT-50.00\nPSafeway\nLGroceries\n^\n"
            "D2/1'23\nT0.00\nPZero transfer\nL[Savings]\n^\n"
            "!Account\nNSavings\nTBank\n^\n!Type:Bank\n"
            "D2/1'23\nT0.00\nPZero transfer\nL[Checking]\n^\n"
        )
        body = {"content": zero_qif, "account_map": ACCOUNT_MAP}
        preview = client.post("/import/qif/preview", json=body).json()
        prev = {a["name"]: a["counts"] for a in preview["accounts"]}
        assert prev["Checking"]["skipped_zero_amount"] == 2  # opening + transfer leg
        assert prev["Checking"]["would_import"] == 1
        assert prev["Checking"]["opening_balances"] == 0
        assert prev["Savings"]["skipped_zero_amount"] == 1

        resp = client.post("/import/qif", json=body)
        assert resp.status_code == 200, resp.text
        commit = resp.json()
        assert commit["per_account"] == prev
        assert commit["skipped_zero_amount"] == 3
        assert commit["imported"] == 1
        assert commit["transfers_created"] == 0

    def test_preview_unknown_map_target_is_422(self, seeded: None, client: TestClient) -> None:
        resp = client.post(
            "/import/qif/preview",
            json={"content": CUTOFF_QIF, "account_map": {"Checking": "assets.nope"}},
        )
        assert resp.status_code == 422

    def test_preview_writes_nothing_with_options(self, seeded: None, client: TestClient) -> None:
        before = client.get("/transactions", params={"include_collapsed": True}).json()["total"]
        client.post(
            "/import/qif/preview",
            json={
                "content": TRANSFER_QIF,
                "account_map": ACCOUNT_MAP,
                "account_options": {
                    "Checking": {"invert": True},
                    "Savings": {"cutoff": "2024-06-01"},
                },
            },
        )
        after = client.get("/transactions", params={"include_collapsed": True}).json()["total"]
        assert before == after == 0
        # No categories were auto-created by the preview either.
        cats = client.get("/categories").json()
        assert all(not c["path"].endswith(".feed_seed") for c in cats)


ZERO_SUFFIX_QIF = (
    "!Account\nNChecking\nTBank\n^\n!Type:Bank\nD4/1'24\nT0.00\nPZero Marker\nLGroceries\n^\n"
)
ZERO_SUFFIX_CSV = "2024-04-01,Zero Marker,0.00,,,Checking\n"


class TestIdempotencyProperty:
    """Guardrail 3: import is idempotent — extended to arbitrary cutoff/invert
    option combinations AND both formats (QIF + CSV). Running the SAME request
    twice adds zero rows. ``zero_rows`` appends a $0 marker row (issue #144):
    the import must succeed, bucket it, and stay idempotent."""

    @settings(max_examples=16, deadline=None)
    @given(
        cutoff_on=st.booleans(),
        invert_on=st.booleans(),
        fmt=st.sampled_from(["qif", "csv"]),
        zero_rows=st.booleans(),
    )
    def test_reimport_adds_zero_rows(
        self,
        migrated_engine: Engine,
        cutoff_on: bool,
        invert_on: bool,
        fmt: str,
        zero_rows: bool,
    ) -> None:
        from purser import ledger as ls
        from purser import qif_import
        from purser.db import get_sessionmaker

        content = FIXTURE if fmt == "qif" else CSV_FIXTURE
        if zero_rows:
            content += ZERO_SUFFIX_QIF if fmt == "qif" else ZERO_SUFFIX_CSV
        reset_db(migrated_engine)
        with get_sessionmaker()() as s:
            for name, path in (("Checking", "assets.checking"), ("Savings", "assets.savings")):
                ls.create_account(s, name=name, type_="checking", path=path, actor_token_id=None)
            s.commit()

            options: dict[str, qif_import.QifAccountOptions] = {}
            if cutoff_on or invert_on:
                options = {
                    name: qif_import.QifAccountOptions(
                        cutoff=date(2024, 1, 1) if cutoff_on else None, invert=invert_on
                    )
                    for name in ("Checking", "Savings")
                }

            first = qif_import.import_qif(
                s,
                content=content,
                account_map=ACCOUNT_MAP,
                account_options=options,
                fmt=fmt,
                token=None,
            )
            s.commit()
            before = s.execute(text("SELECT COUNT(*) FROM transactions")).scalar_one()

            second = qif_import.import_qif(
                s,
                content=content,
                account_map=ACCOUNT_MAP,
                account_options=options,
                fmt=fmt,
                token=None,
            )
            s.commit()
            after = s.execute(text("SELECT COUNT(*) FROM transactions")).scalar_one()

        assert after == before
        expected_zero = 1 if zero_rows else 0
        assert first["skipped_zero_amount"] == expected_zero
        assert second["skipped_zero_amount"] == expected_zero


# ---------------------------------------------------------------------------
# Identical rows in one file (issue #275, decision 68)
# ---------------------------------------------------------------------------


def _qif_of(rows: list[tuple[int, str, str]], *, account: str = "Checking") -> str:
    """A one-account QIF: (day of March 2024, amount, payee) per row."""
    body = "".join(
        f"D3/{day}/2024\nT{amount}\nP{payee}\nLDining\n^\n" for day, amount, payee in rows
    )
    return f"!Account\nN{account}\nTBank\n^\n!Type:Bank\n{body}"


def _csv_of(rows: list[tuple[int, str, str]], *, account: str = "Checking") -> str:
    """The same rows as a Quicken CSV export (shares the qif: hash namespace)."""
    lines = [f"2024-03-{day:02d},{payee},{amount},Dining,{account}" for day, amount, payee in rows]
    return "Date,Payee,Amount,Category,Account\n" + "".join(f"{line}\n" for line in lines)


def _file_hash(when: date, amount: str, description: str, n: int = 1) -> str:
    """Stored identity of a file row on Checking; ``n`` = 1 is the recipe as
    it stood before the occurrence ordinal."""
    from purser import crypto

    return crypto.keyed_hash(
        core.content_hash(
            external_account_id="qif:Checking",
            date=when,
            amount=Decimal(amount),
            description=description,
            occurrence=n,
        )
    )


def _counts_of(preview: dict) -> dict[str, dict[str, int]]:
    return {a["name"]: a["counts"] for a in preview["accounts"]}


def _balances_of(client: TestClient) -> dict[str, Decimal]:
    accounts = client.get("/accounts", params={"include_balances": True}).json()
    return {a["path"]: Decimal(a["balance"]) for a in accounts}


def _coffees(client: TestClient, payee: str = "Corner Cafe") -> int:
    page = client.get("/transactions", params={"account": "assets.checking"}).json()
    return sum(row["description"] == payee for row in page["items"])


PAIR = [(8, "-4.50", "Corner Cafe"), (8, "-4.50", "Corner Cafe"), (9, "-62.10", "Grocer")]


def _two_transfers_qif(*, back: bool = False) -> str:
    """Two same-day 500.00 transfers between Checking and Savings, both legs in
    both registers. ``back``: the second one runs Savings -> Checking (a round
    trip); otherwise both run Checking -> Savings."""
    second_out, second_in = ("500.00", "-500.00") if back else ("-500.00", "500.00")
    return (
        "!Account\nNChecking\nTBank\n^\n!Type:Bank\n"
        "D3/5/2024\nT-500.00\nPMove to savings\nL[Savings]\n^\n"
        f"D3/5/2024\nT{second_out}\nPMove to savings\nL[Savings]\n^\n"
        "!Account\nNSavings\nTBank\n^\n!Type:Bank\n"
        "D3/5/2024\nT500.00\nPMove from checking\nL[Checking]\n^\n"
        f"D3/5/2024\nT{second_in}\nPMove from checking\nL[Checking]\n^\n"
    )


class TestIdenticalRows:
    """Two identical rows in one file are occurrences 1 and 2 — both import,
    in QIF and CSV alike; occurrence 1 keeps the hash every row had before."""

    @pytest.mark.parametrize("fmt", ["qif", "csv"])
    def test_identical_rows_import_as_two(
        self, seeded: None, client: TestClient, migrated_engine: Engine, fmt: str
    ) -> None:
        content = _qif_of(PAIR) if fmt == "qif" else _csv_of(PAIR)
        body = {"content": content, "account_map": {"Checking": "assets.checking"}}
        preview = client.post(f"/import/{fmt}/preview", json=body).json()
        counts = _counts_of(preview)["Checking"]
        assert (counts["would_import"], counts["skipped_dedup"]) == (3, 0)

        resp = client.post(f"/import/{fmt}", json=body)
        assert resp.status_code == 200, resp.text
        assert resp.json()["per_account"] == _counts_of(preview)  # preview == commit (#144)
        assert _coffees(client) == 2
        assert _balances_of(client)["assets.checking"] == Decimal("-71.10")
        when = date(2024, 3, 8)
        assert _hash_present(migrated_engine, _file_hash(when, "-4.50", "Corner Cafe"))
        assert _hash_present(migrated_engine, _file_hash(when, "-4.50", "Corner Cafe", 2))

    def test_reimport_and_an_overlapping_file_add_nothing(
        self, seeded: None, client: TestClient
    ) -> None:
        account_map = {"Checking": "assets.checking"}
        first = client.post(
            "/import/qif", json={"content": _qif_of(PAIR), "account_map": account_map}
        )
        assert first.json()["imported"] == 3

        again_body = {"content": _qif_of(PAIR), "account_map": account_map}
        preview = client.post("/import/qif/preview", json=again_body).json()
        again = client.post("/import/qif", json=again_body).json()
        assert again["per_account"] == _counts_of(preview)
        assert (again["imported"], again["skipped"]) == (0, 3)

        # A later export overlapping the first: the same pair (in another order
        # and format) plus one new row. Only the new row lands.
        overlap = [(10, "-7.25", "Bakery"), *reversed(PAIR)]
        overlap_body = {"content": _csv_of(overlap), "account_map": account_map}
        preview = client.post("/import/csv/preview", json=overlap_body).json()
        later = client.post("/import/csv", json=overlap_body).json()
        assert later["per_account"] == _counts_of(preview)
        assert (later["imported"], later["skipped"]) == (1, 3)
        assert _coffees(client) == 2

    def test_the_twin_an_older_import_dropped_lands_once(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        """Before #275 the pair stored ONE row under the unqualified hash —
        the state a file with a single coffee leaves. Re-importing the pair
        lands the dropped twin once; every later re-import adds nothing."""
        account_map = {"Checking": "assets.checking"}
        old_state = [(8, "-4.50", "Corner Cafe"), (9, "-62.10", "Grocer")]
        client.post("/import/qif", json={"content": _qif_of(old_state), "account_map": account_map})
        when = date(2024, 3, 8)
        assert _hash_present(migrated_engine, _file_hash(when, "-4.50", "Corner Cafe"))
        assert _coffees(client) == 1

        body = {"content": _qif_of(PAIR), "account_map": account_map}
        fixup = client.post("/import/qif", json=body).json()
        assert (fixup["imported"], fixup["skipped"]) == (1, 2)
        assert _hash_present(migrated_engine, _file_hash(when, "-4.50", "Corner Cafe", 2))
        assert client.post("/import/qif", json=body).json()["imported"] == 0
        assert _coffees(client) == 2

    def test_identical_same_day_transfers_are_two_transfers(
        self, seeded: None, client: TestClient
    ) -> None:
        body = {"content": _two_transfers_qif(), "account_map": ACCOUNT_MAP}
        preview = client.post("/import/qif/preview", json=body).json()
        commit = client.post("/import/qif", json=body).json()
        assert commit["per_account"] == _counts_of(preview)
        assert commit["transfers_created"] == 2
        assert _counts_of(preview)["Checking"]["transfers"] == 2
        balances = _balances_of(client)
        assert balances["assets.checking"] == Decimal("-1000.00")
        assert balances["assets.savings"] == Decimal("1000.00")

        again = client.post("/import/qif", json=body).json()
        assert again["transfers_created"] == 0
        assert again["per_account"]["Checking"]["skipped_dedup"] == 2
        assert _balances_of(client)["assets.checking"] == Decimal("-1000.00")

    def test_a_same_day_round_trip_is_two_transfers(self, seeded: None, client: TestClient) -> None:
        # The transfer hash ignores direction (inversion-neutral), so before
        # #275 the way back merged into the way there and one side's balance
        # was off by the amount. Now each register's n-th leg is transfer n.
        body = {"content": _two_transfers_qif(back=True), "account_map": ACCOUNT_MAP}
        commit = client.post("/import/qif", json=body).json()
        assert commit["transfers_created"] == 2
        balances = _balances_of(client)
        assert balances["assets.checking"] == Decimal("0.00")
        assert balances["assets.savings"] == Decimal("0.00")
        assert client.post("/import/qif", json=body).json()["transfers_created"] == 0

    def test_identical_split_legs_degrade_to_two_one_leg_flats(
        self, seeded: None, client: TestClient
    ) -> None:
        # Savings' feed owns March: both transfers split, and Checking's two
        # identical legs become two one-leg flats rather than one.
        body = {
            "content": _two_transfers_qif(),
            "account_map": ACCOUNT_MAP,
            "account_options": {"Savings": {"cutoff": "2024-03-01"}},
        }
        preview = client.post("/import/qif/preview", json=body).json()
        commit = client.post("/import/qif", json=body).json()
        assert commit["per_account"] == _counts_of(preview)
        assert _counts_of(preview)["Checking"]["one_leg_transfers"] == 2
        assert _counts_of(preview)["Savings"]["skipped_cutoff"] == 2
        assert _balances_of(client)["assets.checking"] == Decimal("-1000.00")
        assert client.post("/import/qif", json=body).json()["one_leg_transfers"] == 0
        assert _balances_of(client)["assets.checking"] == Decimal("-1000.00")


file_rows = st.lists(
    st.sampled_from(
        [
            (8, "-4.50", "Corner Cafe"),
            (8, "-4.50", "CORNER CAFE"),  # the same identity once normalized
            (8, "-12.00", "Corner Cafe"),
            (9, "-4.50", "Corner Cafe"),
            (9, "250.00", "Payroll"),
        ]
    ),
    min_size=1,
    max_size=7,
)


class TestOccurrenceIdempotencyProperty:
    """Guardrail 3 with duplicates (decision 68), both formats: a file whose
    rows repeat imports every row; re-running it adds zero; a second,
    overlapping file adds only what it carries beyond the first (per identity,
    the larger count wins); and every preview equals its commit (#144)."""

    @settings(max_examples=20, deadline=None)
    @given(first=file_rows, second=file_rows, fmt=st.sampled_from(["qif", "csv"]))
    def test_counts_follow_the_largest_file_and_reruns_add_none(
        self,
        migrated_engine: Engine,
        first: list[tuple[int, str, str]],
        second: list[tuple[int, str, str]],
        fmt: str,
    ) -> None:
        from collections import Counter

        from purser import ledger as ls
        from purser import qif_import
        from purser.db import get_sessionmaker

        build = _qif_of if fmt == "qif" else _csv_of
        account_map = {"Checking": "assets.checking"}

        def identities(rows: list[tuple[int, str, str]]) -> Counter[tuple[int, str, str]]:
            return Counter(
                (day, amount, core.normalize_payee(payee)) for day, amount, payee in rows
            )

        reset_db(migrated_engine)
        with get_sessionmaker()() as s:
            ls.create_account(
                s, name="Checking", type_="checking", path="assets.checking", actor_token_id=None
            )
            s.commit()

            def rows_in_ledger() -> int:
                return int(s.execute(text("SELECT COUNT(*) FROM transactions")).scalar_one())

            def run(rows: list[tuple[int, str, str]]) -> dict:
                content = build(rows)
                preview = qif_import.preview(s, content, account_map=account_map, fmt=fmt)
                result = qif_import.import_qif(
                    s, content=content, account_map=account_map, fmt=fmt, token=None
                )
                s.commit()
                assert result["per_account"] == {
                    a["name"]: a["counts"] for a in preview["accounts"]
                }
                return result

            assert run(first)["imported"] == len(first)
            assert rows_in_ledger() == len(first)
            assert run(first)["imported"] == 0

            # Per identity the ledger holds the larger of the two files' counts.
            expected = identities(first) | identities(second)
            run(second)
            assert rows_in_ledger() == sum(expected.values())
            assert run(second)["imported"] == 0
            assert run(first)["imported"] == 0
            assert rows_in_ledger() == sum(expected.values())
