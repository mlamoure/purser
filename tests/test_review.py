"""Transactions that need review (issue #694, DESIGN §4, decision 141).

A row needs review only for a reason: a bank-sync row with no category
(``uncategorized``, behind the ``review_uncategorized`` setting) or a
reconciliation adjustment nobody has explained or accepted
(``reconciliation_adjustment``). Both are derived from the row, so these tests
check what a row says, never a mark: every surface evaluates
``review.flagged_rows`` and must agree with it (#248).

Synthetic data only; dates come from the pinned clock (#533).
"""

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from purser import crypto, review
from tests.conftest import today

UNCATEGORIZED = review.REASON_UNCATEGORIZED
ADJUSTMENT = review.REASON_ADJUSTMENT


@pytest.fixture
def seeded(admin_client: TestClient) -> dict[str, str]:
    ids: dict[str, str] = {}
    for key, name, type_, path in [
        ("checking", "Checking", "checking", "assets.checking"),
        ("savings", "Savings", "savings", "assets.savings"),
        ("house", "House", "property", "assets.house"),
    ]:
        resp = admin_client.post("/accounts", json={"name": name, "type": type_, "path": path})
        assert resp.status_code == 201, resp.text
        ids[key] = resp.json()["id"]
    for name, type_, path in [
        ("Uncategorized", "category_expense", "expenses.uncategorized"),
        ("Dining", "category_expense", "expenses.dining"),
        ("Groceries", "category_expense", "expenses.groceries"),
        ("Uncategorized Income", "category_income", "income.uncategorized"),
    ]:
        resp = admin_client.post("/categories", json={"name": name, "type": type_, "path": path})
        assert resp.status_code == 201, resp.text
        ids[path] = resp.json()["id"]
    return ids


def _feed_row(
    session: Session,
    *,
    status: str = "posted",
    category: str = "expenses.uncategorized",
    amount: str = "-25.00",
    account: str = "assets.checking",
    payee: str = "Synthetic Feed Payee",
    days_ago: int = 0,
) -> uuid.UUID:
    """A row that came from the bank feed: origin='import' WITH a
    source_fetch_id (decision 45's feed-row definition)."""
    from purser import ledger
    from purser.core import ledger as core
    from purser.models import Connection, RawFetch

    conn = Connection(provider="simplefin")
    session.add(conn)
    session.flush()
    fetch = RawFetch(
        connection_id=conn.id, content_hash=uuid.uuid4().hex, payload_enc=crypto.encrypt("{}")
    )
    session.add(fetch)
    session.flush()
    tx = ledger.create_flat_transaction(
        session,
        account_ref=account,
        category_ref=category,
        amount=Decimal(amount),
        date=today() - timedelta(days=days_ago),
        payee_name=payee,
        description="SYNTHETIC FEED ROW",
        requested_status=core.Status(status),
        origin=core.Origin.IMPORT,
        token=None,
        source_fetch_id=fetch.id,
        content_hash=uuid.uuid4().hex,
    )
    session.flush()
    return tx.id


def _manual(
    client: TestClient,
    *,
    category: str = "expenses.uncategorized",
    amount: str = "-10.00",
    status: str = "posted",
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    resp = client.post(
        "/transactions",
        headers=headers or {},
        json={
            "account": "assets.checking",
            "category": category,
            "amount": amount,
            "date": today().isoformat(),
            "payee": "Synthetic Manual Payee",
            "status": status,
        },
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def _detail(client: TestClient, tx_id: object) -> dict[str, Any]:
    resp = client.get(f"/transactions/{tx_id}")
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def _reason(client: TestClient, tx_id: object) -> str | None:
    detail = _detail(client, tx_id)
    assert detail["needs_review"] is (detail["review_reason"] is not None)
    return detail["review_reason"]


ALL_STATUSES = ["expected", "pending", "posted", "void"]


def _all_rows(client: TestClient, **params: object) -> list[dict[str, Any]]:
    """Every row the API will show — all statuses, correction chains
    uncollapsed — paged through so nothing hides past `limit`."""
    items: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = client.get(
            "/transactions",
            params={
                "status": ALL_STATUSES,
                "include_collapsed": True,
                "limit": 200,
                "offset": offset,
                **params,
            },
        ).json()
        items += page["items"]
        offset += 200
        if offset >= page["total"]:
            return items


def _assert_one_predicate(client: TestClient, session: Session) -> dict[str, str]:
    """The per-row flag and reason, ?needs_review=true (with false its exact
    complement), the search term, the counts, the summary and the batch all
    describe ONE set (#248). Returns it as {id: reason}."""
    rows = _all_rows(client)
    flagged = {str(t["id"]): t["review_reason"] for t in rows if t["needs_review"] is True}
    assert all(t["needs_review"] is (t["review_reason"] is not None) for t in rows)
    needs = {str(t["id"]) for t in _all_rows(client, needs_review=True)}
    complement = {str(t["id"]) for t in _all_rows(client, needs_review=False)}
    searched = {str(t["id"]) for t in _all_rows(client, q="is:needs-review")}
    assert needs == set(flagged) == searched
    assert needs.isdisjoint(complement)
    assert needs | complement == {str(t["id"]) for t in rows}
    session.expire_all()
    counts = review.counts(session)
    assert counts["total"] == len(flagged)
    assert counts[UNCATEGORIZED] == sum(r == UNCATEGORIZED for r in flagged.values())
    assert counts[ADJUSTMENT] == sum(r == ADJUSTMENT for r in flagged.values())
    summary = client.get("/transactions/review-summary").json()
    assert summary["total"] == len(flagged)
    assert summary["uncategorized"] == counts[UNCATEGORIZED]
    assert summary["reconciliation_adjustments"] == counts[ADJUSTMENT]
    batch = client.get("/transactions/review-batch", params={"limit": 25}).json()
    assert batch["total"] == len(flagged)
    assert {r["id"]: r["reason"] for r in batch["rows"]}.items() <= flagged.items()
    return flagged


# --------------------------------------------------------------------------
# Reason 1: a bank-sync row with no category
# --------------------------------------------------------------------------


class TestUncategorized:
    def test_a_bank_sync_row_with_no_category_needs_review(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        expense = _feed_row(db_session)
        income = _feed_row(db_session, category="income.uncategorized", amount="40.00")
        filed = _feed_row(db_session, category="expenses.dining")
        db_session.commit()
        assert _reason(client, expense) == UNCATEGORIZED
        assert _reason(client, income) == UNCATEGORIZED
        assert _reason(client, filed) is None
        assert _assert_one_predicate(client, db_session) == {
            str(expense): UNCATEGORIZED,
            str(income): UNCATEGORIZED,
        }

    def test_a_rule_or_payee_memory_categorizing_it_clears_it(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        """Automation that files a row is exactly as good as a person doing
        it: the flag reads the category, not who set it."""
        by_rule = _feed_row(db_session, payee="Synthetic Rule Payee")
        by_memory = _feed_row(db_session, status="pending", payee="Synthetic Memory Payee")
        db_session.commit()
        resp = client.post(
            "/rules",
            json={
                "type": "categorize",
                "match_field": "payee",
                "match_mode": "equals",
                "match_text": "Synthetic Rule Payee",
                "category_id": seeded["expenses.groceries"],
                "activate": True,
            },
        )
        assert resp.status_code == 201, resp.text
        for _ in range(2):  # accepted history: payee memory
            client.post(
                "/transactions",
                json={
                    "account": "assets.checking",
                    "category": "expenses.dining",
                    "amount": "-9.00",
                    "date": today().isoformat(),
                    "payee": "Synthetic Memory Payee",
                    "status": "posted",
                },
            )
        assert _reason(client, by_rule) == UNCATEGORIZED
        assert _reason(client, by_memory) == UNCATEGORIZED

        stats = client.post("/categorize/run").json()
        assert stats["rule"] == 1 and stats["deterministic"] == 1
        assert _assert_one_predicate(client, db_session) == {}
        assert _detail(client, by_memory)["category_path"] == "expenses.dining"

    def test_rows_a_person_an_agent_or_a_file_import_wrote_never_need_review(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        manual = _manual(client)
        agent_token = client.post(
            "/tokens", json={"name": "agent", "scopes": ["read", "write"]}
        ).json()["token"]
        agent = _manual(client, headers={"Authorization": f"Bearer {agent_token}"})
        assert agent["origin"] == "agent"
        before = {str(t["id"]) for t in _all_rows(client)}
        qif = client.post(
            "/import/qif",
            json={
                "content": (
                    "!Account\nNChecking\nTBank\n^\n!Type:Bank\n"
                    "D1/15/2024\nT-42.00\nPSynthetic Store\n^\n"
                ),
                "account_map": {"Checking": "assets.checking"},
            },
        )
        assert qif.status_code == 200, qif.text
        csv = client.post(
            "/import/csv",
            json={
                "content": "Date,Payee,Amount\n2024-07-02,Synthetic Cafe,-12.00\n",
                "account_map": {"__default__": "assets.checking"},
            },
        )
        assert csv.status_code == 200, csv.text
        imported = [t for t in _all_rows(client) if str(t["id"]) not in before]
        assert len(imported) == 2
        assert {t["origin"] for t in imported} == {"import"}
        # Every one of them is uncategorized, and none needs review.
        for row in [manual, agent, *imported]:
            assert _detail(client, row["id"])["category_path"] == "expenses.uncategorized"
            assert _reason(client, row["id"]) is None
        assert _assert_one_predicate(client, db_session) == {}

    def test_the_setting_off_clears_the_reason_everywhere(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        feed = _feed_row(db_session)
        db_session.commit()
        adjustment = _adjustment(client, seeded)
        assert _assert_one_predicate(client, db_session) == {
            str(feed): UNCATEGORIZED,
            adjustment: ADJUSTMENT,
        }

        resp = client.put("/config", json={"review_uncategorized": False})
        assert resp.status_code == 200, resp.text
        assert resp.json()["values"]["review_uncategorized"] is False
        assert _reason(client, feed) is None
        # The adjustment is always on.
        assert _assert_one_predicate(client, db_session) == {adjustment: ADJUSTMENT}
        assert review.counts(db_session)[UNCATEGORIZED] == 0

        assert client.delete("/config/review_uncategorized").status_code == 200
        assert _reason(client, feed) == UNCATEGORIZED

    def test_a_pending_row_needs_review_and_its_categorized_posted_row_does_not(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        from purser.feed import reconcile
        from purser.models import Transaction

        pending = _feed_row(db_session, status="pending")
        db_session.commit()
        assert _reason(client, pending) == UNCATEGORIZED

        posted = _feed_row(db_session, category="expenses.dining")
        row = db_session.get(Transaction, pending)
        successor = db_session.get(Transaction, posted)
        assert row is not None and successor is not None
        reconcile.void_pending(db_session, row, successor=successor)
        db_session.commit()
        assert _detail(client, pending)["status"] == "void"
        assert _assert_one_predicate(client, db_session) == {}

    def test_a_corrected_row_that_is_still_uncategorized_still_needs_review(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        """A person editing a posted bank row's description writes a
        correction whose replacement is origin='human' with no fetch: it is
        still that bank line, and still has no category."""
        original = _feed_row(db_session)
        db_session.commit()
        resp = client.patch(f"/transactions/{original}", json={"description": "Synthetic note"})
        assert resp.status_code == 200, resp.text
        head = resp.json()
        assert head["id"] != str(original)
        assert head["origin"] == "human" and head["source_fetch_id"] is None
        assert head["review_reason"] == UNCATEGORIZED
        # The original and its reversal are never asked about.
        assert _assert_one_predicate(client, db_session) == {head["id"]: UNCATEGORIZED}

        categorized = client.patch(
            f"/transactions/{head['id']}", json={"category": "expenses.dining"}
        )
        assert categorized.status_code == 200, categorized.text
        assert categorized.json()["needs_review"] is False
        assert _assert_one_predicate(client, db_session) == {}

    def test_an_unlinked_clone_of_a_corrected_row_is_still_that_bank_line(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        """The clone of a replacement has no fetch and the replacement's
        origin; only the walk through the void row it carries on (and that
        row's correction) reaches the feed row."""
        original = _feed_row(db_session, amount="-30.00")
        db_session.commit()
        head = client.patch(f"/transactions/{original}", json={"description": "Synthetic"}).json()
        incoming = client.post(
            "/transactions",
            json={
                "account": "assets.savings",
                "category": "expenses.dining",
                "amount": "30.00",
                "date": today().isoformat(),
                "status": "posted",
            },
        ).json()
        resp = client.post(
            "/matching/confirm-transfer",
            json={"outgoing_id": head["id"], "incoming_id": incoming["id"]},
        )
        assert resp.status_code == 200, resp.text
        merged = resp.json()["merged_transaction_id"]
        assert _reason(client, merged) is None  # a merged transfer never needs review
        unlink = client.post(f"/transactions/{merged}/unlink-transfer")
        assert unlink.status_code == 200, unlink.text
        resurrected = unlink.json()["resurrected_ids"]
        clone = next(
            i for i in resurrected if _detail(client, i)["account_path"] == "assets.checking"
        )
        assert _detail(client, clone)["origin"] == "human"
        assert _detail(client, clone)["source_fetch_id"] is None
        assert _reason(client, clone) == UNCATEGORIZED
        assert _assert_one_predicate(client, db_session) == {clone: UNCATEGORIZED}

    def test_a_split_with_an_uncategorized_line_needs_review(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        tx_id = _feed_row(db_session, category="expenses.dining", amount="-50.00")
        db_session.commit()
        assert _reason(client, tx_id) is None
        resp = client.put(
            f"/transactions/{tx_id}/split",
            json={
                "lines": [
                    {"category": "expenses.dining", "amount": "-30.00"},
                    {"category": "expenses.uncategorized", "amount": "-20.00"},
                ]
            },
        )
        assert resp.status_code == 200, resp.text
        split = str(resp.json()["transaction_id"])
        assert _assert_one_predicate(client, db_session) == {split: UNCATEGORIZED}

        resp = client.put(
            f"/transactions/{split}/split",
            json={
                "lines": [
                    {"category": "expenses.dining", "amount": "-30.00"},
                    {"category": "expenses.groceries", "amount": "-20.00"},
                ]
            },
        )
        assert resp.status_code == 200, resp.text
        assert _assert_one_predicate(client, db_session) == {}

    def test_never_flagged_whatever_the_origin_when_no_feed_row_is_in_its_lineage(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        """Purser's own entries (origin system), rule replacements and
        schedule-instance shaped rows carry no feed row in their lineage."""
        for origin, status in [("system", "posted"), ("rule", "posted"), ("rule", "expected")]:
            _shaped_row(db_session, origin=origin, status=status, from_feed=False)
        db_session.commit()
        assert _assert_one_predicate(client, db_session) == {}


# --------------------------------------------------------------------------
# Reason 2: a reconciliation adjustment
# --------------------------------------------------------------------------


def _adjustment(client: TestClient, ids: dict[str, str], *, balance: str = "75.00") -> str:
    """Reconcile checking (holding nothing) to ``balance``: the adjustment
    row's id."""
    resp = client.post(
        f"/accounts/{ids['checking']}/reconcile",
        json={"as_of": (today() - timedelta(days=1)).isoformat(), "statement_balance": balance},
    )
    assert resp.status_code == 200, resp.text
    adjustment = resp.json()["adjustment_tx_id"]
    assert adjustment is not None
    return str(adjustment)


def _audits(engine: Engine, action: str) -> list[dict[str, Any]]:
    import json

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT before_after_enc FROM audit_log WHERE action = :a AND kind = 'change'"
                " ORDER BY at"
            ),
            {"a": action},
        ).scalars()
        return [json.loads(crypto.decrypt(bytes(r))) for r in rows]


class TestReconciliationAdjustment:
    def test_an_adjustment_needs_review_until_it_is_recategorized(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        adjustment = _adjustment(client, seeded)
        assert _detail(client, adjustment)["origin"] == "system"
        assert _assert_one_predicate(client, db_session) == {adjustment: ADJUSTMENT}

        resp = client.patch(f"/transactions/{adjustment}", json={"category": "expenses.dining"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["needs_review"] is False
        assert _assert_one_predicate(client, db_session) == {}

    def test_a_description_edit_keeps_it_flagged_on_its_new_row(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        adjustment = _adjustment(client, seeded)
        head = client.patch(f"/transactions/{adjustment}", json={"description": "Synthetic"})
        assert head.status_code == 200, head.text
        assert head.json()["review_reason"] == ADJUSTMENT
        assert _assert_one_predicate(client, db_session) == {head.json()["id"]: ADJUSTMENT}

    def test_splitting_it_away_clears_it_and_a_line_left_on_it_does_not(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        adjustment = _adjustment(client, seeded, balance="50.00")
        kept = client.put(
            f"/transactions/{adjustment}/split",
            json={
                "lines": [
                    {"category": "expenses.reconciliation_adjustments", "amount": "20.00"},
                    {"category": "income.uncategorized", "amount": "30.00"},
                ]
            },
        )
        assert kept.status_code == 200, kept.text
        split = str(kept.json()["transaction_id"])
        # A line still on the adjustments category: still the adjustment (an
        # uncategorized line of a non-feed row is no reason of its own).
        assert _assert_one_predicate(client, db_session) == {split: ADJUSTMENT}

        away = client.put(
            f"/transactions/{split}/split",
            json={
                "lines": [
                    {"category": "expenses.dining", "amount": "20.00"},
                    {"category": "expenses.groceries", "amount": "30.00"},
                ]
            },
        )
        assert away.status_code == 200, away.text
        assert _assert_one_predicate(client, db_session) == {}

    def test_accepting_it_clears_it_is_audited_and_idempotent(
        self,
        seeded: dict[str, str],
        client: TestClient,
        db_session: Session,
        migrated_engine: Engine,
    ) -> None:
        adjustment = _adjustment(client, seeded)
        first = client.post(
            f"/transactions/{adjustment}/accept-adjustment",
            json={"note": "Synthetic: bank fee"},
        )
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["created"] is True
        assert body["adjustment_tx_id"] == adjustment
        assert body["accepted_by"] is not None
        assert _assert_one_predicate(client, db_session) == {}

        again = client.post(f"/transactions/{adjustment}/accept-adjustment")
        assert again.status_code == 200, again.text
        assert again.json()["created"] is False
        assert again.json()["accepted_at"] == body["accepted_at"]

        audits = _audits(migrated_engine, "reconciliation.adjustment_accepted")
        assert len(audits) == 1
        assert audits[0]["after"]["adjustment_tx_id"] == adjustment
        assert audits[0]["after"]["note"] == "Synthetic: bank fee"

        history = client.get(f"/accounts/{seeded['checking']}/reconciliations").json()
        assert history[0]["adjustment_accepted_at"] is not None
        assert history[0]["adjustment_accepted_by"] == body["accepted_by"]

    def test_accepting_by_the_row_a_correction_left(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        adjustment = _adjustment(client, seeded)
        head = client.patch(
            f"/transactions/{adjustment}", json={"description": "Synthetic"}
        ).json()["id"]
        resp = client.post(f"/transactions/{head}/accept-adjustment")
        assert resp.status_code == 200, resp.text
        assert resp.json()["adjustment_tx_id"] == adjustment
        assert _assert_one_predicate(client, db_session) == {}

    def test_refusals(self, seeded: dict[str, str], client: TestClient) -> None:
        assert client.post(f"/transactions/{uuid.uuid4()}/accept-adjustment").status_code == 404
        manual = _manual(client, category="expenses.dining")
        refused = client.post(f"/transactions/{manual['id']}/accept-adjustment")
        assert refused.status_code == 422
        assert "not a reconciliation adjustment" in refused.json()["detail"]
        too_long = client.post(
            f"/transactions/{_adjustment(client, seeded)}/accept-adjustment",
            json={"note": "x" * 201},
        )
        assert too_long.status_code == 422

    def test_accepting_requires_write_scope(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        adjustment = _adjustment(client, seeded)
        reader = client.post("/tokens", json={"name": "reader", "scopes": ["read"]}).json()
        resp = client.post(
            f"/transactions/{adjustment}/accept-adjustment",
            headers={"Authorization": f"Bearer {reader['token']}"},
        )
        assert resp.status_code == 403

    def test_an_asset_value_change_never_needs_review(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        resp = client.post(
            f"/accounts/{seeded['house']}/reconcile",
            json={"as_of": today().isoformat(), "statement_balance": "250000.00"},
        )
        assert resp.status_code == 200, resp.text
        change = resp.json()["adjustment_tx_id"]
        assert resp.json()["category"] == "equity.revaluation"
        assert _reason(client, change) is None
        assert _assert_one_predicate(client, db_session) == {}
        refused = client.post(f"/transactions/{change}/accept-adjustment")
        assert refused.status_code == 422
        assert "needs no review" in refused.json()["detail"]

    def test_the_mcp_tool_accepts(
        self, seeded: dict[str, str], admin_client: TestClient, live_server: Any
    ) -> None:
        from tests.test_mcp import _mcp_call

        adjustment = _adjustment(admin_client, seeded)
        token = admin_client.post(
            "/tokens", json={"name": "agent", "scopes": ["read", "write"]}
        ).json()["token"]
        batch, accepted, again, after = _mcp_call(
            live_server.url,
            token,
            [
                ("review_batch", {}),
                ("accept_reconciliation_adjustment", {"transaction_id": adjustment}),
                ("accept_reconciliation_adjustment", {"transaction_id": adjustment}),
                ("review_batch", {}),
            ],
        )
        assert [(r["id"], r["reason"]) for r in batch["rows"]] == [(adjustment, ADJUSTMENT)]
        assert set(batch["rows"][0]) == {
            "id",
            "date",
            "account",
            "payee",
            "description",
            "amount",
            "status",
            "reason",
            "category",
            "memories",
        }
        assert accepted["created"] is True and again["created"] is False
        assert after["total"] == 0 and after["rows"] == []


@pytest.fixture
def live_server(migrated_engine: Engine) -> Any:
    from tests.test_mcp import LiveServer

    server = LiveServer()
    server.start()
    yield server
    server.stop()


# --------------------------------------------------------------------------
# One predicate, every surface (#248)
# --------------------------------------------------------------------------


# Row shapes by (origin, status, from_feed), built directly so a property run
# stays fast.
_SHAPES = st.sampled_from(
    [
        ("import", "posted", True),  # feed row
        ("import", "pending", True),  # feed pending
        ("import", "posted", False),  # QIF/CSV-shaped: no source_fetch_id
        ("system", "posted", False),  # Purser's own entry
        ("rule", "posted", False),
        ("rule", "expected", False),  # schedule-instance shaped
        ("human", "posted", False),
        ("agent", "pending", False),
    ]
)


def _shaped_row(session: Session, *, origin: str, status: str, from_feed: bool) -> uuid.UUID:
    if from_feed:
        return _feed_row(session, status=status)
    from purser import ledger
    from purser.core import ledger as core

    tx = ledger.create_flat_transaction(
        session,
        account_ref="assets.checking",
        category_ref="expenses.uncategorized",
        amount=Decimal("-5.00"),
        date=today(),
        payee_name="Synthetic Payee",
        requested_status=core.Status(status),
        origin=core.Origin(origin),
        token=None,
    )
    session.flush()
    return tx.id


def _correct(session: Session, tx_id: uuid.UUID, category: str) -> uuid.UUID:
    """A system-actor correction (reversal + replacement, origin rule)."""
    from purser import ledger
    from purser.models import Transaction

    tx = session.get(Transaction, tx_id)
    assert tx is not None
    replacement = ledger.recategorize_posted(
        session,
        tx=tx,
        category_id=ledger.resolve_account(session, category).id,
        token=None,
        provenance={"source": "rule"},
    )
    session.flush()
    return replacement.id


class TestOnePredicate:
    def test_flag_and_filter_and_counts_for_each_kind_of_row(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        feed = _feed_row(db_session)
        pending = _feed_row(db_session, status="pending")
        recategorized = _correct(db_session, _feed_row(db_session), "expenses.dining")
        still_uncategorized = _correct(db_session, _feed_row(db_session), "expenses.uncategorized")
        db_session.commit()
        adjustment = _adjustment(client, seeded)
        _manual(client)
        flagged = _assert_one_predicate(client, db_session)
        assert flagged == {
            str(feed): UNCATEGORIZED,
            str(pending): UNCATEGORIZED,
            str(still_uncategorized): UNCATEGORIZED,
            adjustment: ADJUSTMENT,
        }
        assert str(recategorized) not in flagged

    @settings(
        max_examples=10, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(
        plan=st.lists(
            st.tuples(
                _SHAPES, st.sampled_from([None, "expenses.dining", "expenses.uncategorized"])
            ),
            min_size=1,
            max_size=6,
        )
    )
    def test_every_surface_agrees_for_any_ledger(
        self,
        seeded: dict[str, str],
        client: TestClient,
        db_session: Session,
        plan: list[tuple[tuple[str, str, bool], str | None]],
    ) -> None:
        """Whatever mix of origins, statuses and corrections the ledger holds,
        every surface agrees, and a row is flagged exactly when it is a live
        head with no category whose lineage holds a feed row (rows
        accumulate across examples, which only widens the mix)."""
        want: dict[str, str] = {}
        heads: list[str] = []
        for (origin, status, from_feed), correction in plan:
            tx_id = _shaped_row(db_session, origin=origin, status=status, from_feed=from_feed)
            head = tx_id
            if correction is not None and status == "posted":
                head = _correct(db_session, tx_id, correction)
            heads.append(str(head))
            category = correction if head != tx_id else "expenses.uncategorized"
            assert category is not None
            if from_feed and category.endswith("uncategorized"):
                want[str(head)] = UNCATEGORIZED
        db_session.commit()
        flagged = _assert_one_predicate(client, db_session)
        assert {h: flagged.get(h) for h in heads} == {h: want.get(h) for h in heads}


class TestSurfaces:
    def test_review_batch_route_is_not_shadowed_and_is_lean(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        tx_id = _feed_row(db_session, days_ago=1)
        newer = _feed_row(db_session)
        db_session.commit()
        resp = client.get("/transactions/review-batch", params={"limit": 1})
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert set(payload) == {"total", "rows", "remaining"}
        assert payload["total"] == 2 and payload["remaining"] == 1
        (row,) = payload["rows"]
        assert row["id"] == str(newer)  # newest first
        assert row["reason"] == UNCATEGORIZED
        assert row["category"] == "expenses.uncategorized"
        assert row["account"] == "assets.checking" and row["amount"] == "-25.0000"
        by_account = client.get(
            "/transactions/review-batch", params={"account": "assets.savings"}
        ).json()
        assert by_account["total"] == 0
        assert client.get(f"/transactions/{tx_id}").status_code == 200

    def test_removed_surfaces_are_gone(self, seeded: dict[str, str], client: TestClient) -> None:
        assert client.post(
            "/transactions/mark-reviewed", json={"transaction_ids": []}
        ).status_code in (
            404,
            405,
            422,
        )
        refused = client.get("/transactions", params={"q": "is:unreviewed"})
        assert refused.status_code == 422
        assert "needs-review" in refused.json()["detail"]
        row = _manual(client)
        assert "reviewed_at" not in row and "escalated_at" not in row

    def test_summary_counts_by_reason_and_account(
        self, seeded: dict[str, str], client: TestClient, db_session: Session
    ) -> None:
        _feed_row(db_session, days_ago=3)
        _feed_row(db_session, status="pending", days_ago=2)
        _feed_row(db_session, account="assets.savings", category="income.uncategorized", amount="5")
        db_session.commit()
        _adjustment(client, seeded)
        data = client.get("/transactions/review-summary").json()
        assert data["total"] == 4
        assert data["uncategorized"] == 3 and data["reconciliation_adjustments"] == 1
        assert data["pending"] == 1
        assert data["oldest_date"] == (today() - timedelta(days=3)).isoformat()
        by_path = {a["path"]: a for a in data["accounts"]}
        assert by_path["assets.checking"]["total"] == 3
        assert by_path["assets.checking"]["uncategorized"] == 2
        assert by_path["assets.checking"]["reconciliation_adjustments"] == 1
        assert by_path["assets.checking"]["pending"] == 1
        assert by_path["assets.savings"]["total"] == 1

    def test_the_headline_names_both_reasons(self) -> None:
        assert review.headline({"total": 0}) is None
        assert (
            review.headline({"total": 3, UNCATEGORIZED: 2, ADJUSTMENT: 1})
            == "3 transactions need review (2 with no category, 1 reconciliation adjustment)"
        )
        assert (
            review.headline({"total": 1, UNCATEGORIZED: 1, ADJUSTMENT: 0})
            == "1 transaction needs review (1 with no category)"
        )
