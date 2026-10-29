"""Guarded feed repair: purge-feed-rows + replay-archive (issue #172,
decision 45).

The round-trip test below IS the production repair rehearsal for the
Fidelity sign inversion (#170): import a sign-wrong payload, set
feed_invert, purge, replay from the archive — amounts flip, content hashes
are preserved (hash-on-raw, decision 43), the masked re-delivery stays
deduplicated (#171), and a second replay is a no-op.
"""

import hashlib
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from purser import crypto, qif_import, review
from purser.admin import feed_repair
from purser.admin.feed_repair import RepairRefused
from purser.db import get_sessionmaker
from purser.feed import importer
from purser.models import Account, RawFetch, Transaction, Valuation

from .test_feed import (
    FeedHarness,
    _account_id_by_path,
    _recent_epoch,
    _set_feed_invert,
    account_of,
    multi_payload_of,
    payload_of,
)

SINGLE_QIF = """!Type:Bank
D01/15/2026
T-50.00
PGrocery Store
^
"""


@pytest.fixture
def harness(clean_db: None, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> FeedHarness:
    return FeedHarness(client, monkeypatch)


def _tx_rows(client: TestClient, account: str) -> list[dict]:
    return client.get(
        "/transactions", params={"account": account, "status": ["pending", "posted"]}
    ).json()["items"]


def _valuations(account_id: uuid.UUID) -> list[tuple[date, Decimal, str]]:
    with get_sessionmaker()() as session:
        rows = session.execute(
            select(Valuation.as_of, Valuation.value, Valuation.source)
            .where(Valuation.account_id == account_id)
            .order_by(Valuation.as_of, Valuation.value)
        ).all()
    return [(as_of, Decimal(value), source) for as_of, value, source in rows]


def _content_hashes(account_path: str) -> set[str]:
    with get_sessionmaker()() as session:
        account = session.execute(select(Account).where(Account.path == account_path)).scalar_one()
        return set(
            session.execute(
                select(Transaction.content_hash)
                .join(Transaction.postings)
                .where(
                    Transaction.content_hash.is_not(None),
                )
                .where(Transaction.postings.any(account_id=account.id))
            ).scalars()
        )


class TestPurge:
    def test_purge_refuses_categories_and_closed(self, harness: FeedHarness) -> None:
        harness.use_payload(payload_of([]))
        cid = harness.connect()
        harness.map_new(cid, "Demo Checking", "assets.checking")
        resp = harness.client.post(
            "/categories",
            json={"name": "Stuff", "type": "category_expense", "path": "expenses.stuff"},
        )
        assert resp.status_code == 201, resp.text
        with get_sessionmaker()() as session, pytest.raises(RepairRefused, match="category"):
            feed_repair.resolve_purge_account(session, "expenses.stuff")
        acct_id = _account_id_by_path(harness.client, "assets.checking")
        assert harness.client.post(f"/accounts/{acct_id}/close", json={}).status_code == 200
        with get_sessionmaker()() as session, pytest.raises(RepairRefused, match="closed"):
            feed_repair.resolve_purge_account(session, "assets.checking")

    def test_purge_spares_qif_rows_and_respects_since(self, harness: FeedHarness) -> None:
        rows = [
            {"id": "t1", "posted": _recent_epoch(9), "amount": "-10.00", "description": "older"},
            {"id": "t2", "posted": _recent_epoch(2), "amount": "-20.00", "description": "newer"},
        ]
        harness.use_payload(payload_of(rows))
        cid = harness.connect()
        harness.map_new(cid, "Demo Checking", "assets.checking")
        assert harness.sync(cid)["imported"] == 2
        with get_sessionmaker()() as session:
            qif_import.import_qif(
                session,
                content=SINGLE_QIF,
                account_map={"__default__": "assets.checking"},
                token=None,
            )
            session.commit()
        assert len(_tx_rows(harness.client, "assets.checking")) == 3

        from datetime import UTC, datetime, timedelta

        since = (datetime.now(UTC) - timedelta(days=5)).date()
        with get_sessionmaker()() as session:
            account = feed_repair.resolve_purge_account(session, "assets.checking")
            plan = feed_repair.purge_preflight(session, account, since)
            assert plan["doomed_count"] == 1  # only the newer feed row
            summary = feed_repair.execute_purge(session, account, since=since, actor_token_id=None)
            session.commit()
        assert summary["transactions_deleted"] == 1
        remaining = {t["description"] for t in _tx_rows(harness.client, "assets.checking")}
        assert remaining == {"older", "Grocery Store"}  # QIF row untouched

        # Full purge takes the older feed row too, still sparing QIF.
        with get_sessionmaker()() as session:
            account = feed_repair.resolve_purge_account(session, "assets.checking")
            summary = feed_repair.execute_purge(session, account, since=None, actor_token_id=None)
            session.commit()
        assert summary["transactions_deleted"] == 1
        remaining = {t["description"] for t in _tx_rows(harness.client, "assets.checking")}
        assert remaining == {"Grocery Store"}

    def test_purge_takes_review_marks_with_the_rows(self, harness: FeedHarness) -> None:
        """A reviewed feed row purges: its mark goes with it (#284) — every
        row migration 0021 found carries one, so without this the foreign
        key refused the whole purge."""
        rows = [{"id": "t1", "posted": _recent_epoch(3), "amount": "-10.00", "description": "x"}]
        harness.use_payload(payload_of(rows))
        cid = harness.connect()
        harness.map_new(cid, "Demo Checking", "assets.checking")
        assert harness.sync(cid)["imported"] == 1
        with get_sessionmaker()() as session:
            account = feed_repair.resolve_purge_account(session, "assets.checking")
            (tx_id,) = feed_repair._doomed_feed_tx_ids(session, account.id, None)
            review.mark(session, tx_id=tx_id, token=None, via=review.VIA_BACKFILL)
            session.commit()
            summary = feed_repair.execute_purge(session, account, since=None, actor_token_id=None)
            session.commit()
        assert summary["transactions_deleted"] == 1
        assert summary["reviews_deleted"] == 1
        assert _tx_rows(harness.client, "assets.checking") == []

    def test_purging_a_carried_marks_source_keeps_the_copy(self, harness: FeedHarness) -> None:
        """Decision 82: a mark carried from a pending names it in
        ``carried_from``, ON DELETE SET NULL — purging the pending keeps
        the successor's mark and drops only the link."""
        hold = {
            "id": "hold",
            "posted": _recent_epoch(0),
            "amount": "-31.00",
            "description": "Harbor Market hold",
            "pending": True,
        }
        harness.use_payload(payload_of([hold]))
        cid = harness.connect()
        harness.map_new(cid, "Demo Checking", "assets.checking")
        harness.sync(cid)
        (pending,) = harness.client.get("/transactions", params={"status": "pending"}).json()[
            "items"
        ]
        resp = harness.client.post(
            "/transactions/mark-reviewed", json={"transaction_ids": [pending["id"]]}
        )
        assert resp.status_code == 200, resp.text
        # The bank posts it dated three days earlier, under a new description.
        posted = {"id": "p", "posted": _recent_epoch(3), "amount": "-31.00", "description": "HM"}
        harness.use_payload(payload_of([posted]))
        assert harness.sync(cid)["voided_pendings"] == 1
        successor_id = harness.client.get(f"/transactions/{pending['id']}").json()["superseded_by"]

        with get_sessionmaker()() as session:
            account = feed_repair.resolve_purge_account(session, "assets.checking")
            summary = feed_repair.execute_purge(
                session,
                account,
                since=date.fromisoformat(pending["date"]),
                actor_token_id=None,
            )
            session.commit()
        assert summary["transactions_deleted"] == 1

        with get_sessionmaker()() as session:
            from purser.models import TransactionReview

            kept = session.get(TransactionReview, uuid.UUID(successor_id))
            assert kept is not None
            assert (kept.via, kept.carried_from) == ("explicit", None)
            assert session.get(Transaction, uuid.UUID(pending["id"])) is None


class TestReplay:
    def test_replay_plan_refuses_unknown_connection(self, harness: FeedHarness) -> None:
        with get_sessionmaker()() as session:
            with pytest.raises(RepairRefused, match="not found"):
                feed_repair.replay_plan(session, str(uuid.uuid4()), None)
            with pytest.raises(RepairRefused, match="UUID"):
                feed_repair.replay_plan(session, "not-a-uuid", None)

    def test_purge_then_replay_roundtrip_with_invert_and_masked_dup(
        self, harness: FeedHarness
    ) -> None:
        """The #170/#171/#172 production rehearsal."""
        epoch_a, epoch_b = _recent_epoch(6), _recent_epoch(2)
        first = payload_of(
            [
                {
                    "id": "t-a",
                    "posted": epoch_a,
                    "amount": "-750.00",
                    "description": "EFT FUNDS RECEIVED ER13529574 (Cash)",
                },
            ],
            account_id="Fidelity College",
        )
        # Later fetch: same row re-delivered masked + a second real deposit.
        second = multi_payload_of(
            [
                account_of(
                    [
                        {
                            "id": "t-a2",
                            "posted": epoch_a,
                            "amount": "-750.00",
                            "description": "EFT FUNDS RECEIVED ERXXXX9574 (Cash)",
                        },
                        {
                            "id": "t-b",
                            "posted": epoch_b,
                            "amount": "-750.00",
                            "description": "EFT FUNDS RECEIVED ER99887766 (Cash)",
                        },
                    ],
                    account_id="Fidelity College",
                )
            ]
        )
        harness.use_payload(first)
        cid = harness.connect()
        harness.map_new(cid, "Fidelity College", "assets.fidelity_college")
        assert harness.sync(cid)["imported"] == 1

        harness.use_payload(second)
        sync2 = harness.sync(cid)
        # Masked re-delivery deduped at live-sync time (#171); real row lands.
        assert sync2["imported"] == 1
        assert sync2["skipped_masked_duplicate"] == 1

        # Wrong signs are in the ledger (the bug this repairs).
        amounts = sorted(t["amount"] for t in _tx_rows(harness.client, "assets.fidelity_college"))
        assert amounts == ["-750.0000", "-750.0000"]
        hashes_before = _content_hashes("assets.fidelity_college")
        assert len(hashes_before) == 2

        # Repair: set feed_invert -> purge -> replay from the archive.
        _set_feed_invert(
            harness.client,
            _account_id_by_path(harness.client, "assets.fidelity_college"),
            True,
        )
        with get_sessionmaker()() as session:
            account = feed_repair.resolve_purge_account(session, "assets.fidelity_college")
            summary = feed_repair.execute_purge(session, account, since=None, actor_token_id=None)
            session.commit()
        assert summary["transactions_deleted"] == 2
        assert _tx_rows(harness.client, "assets.fidelity_college") == []

        with get_sessionmaker()() as session:
            _connection, fetch_ids = feed_repair.replay_plan(session, cid, None)
            assert len(fetch_ids) >= 2
            totals = feed_repair.execute_replay(session, fetch_ids, actor_token_id=None)
        assert totals["imported"] == 2
        assert totals["skipped_masked_duplicate"] == 1  # the masked re-delivery again

        rows = _tx_rows(harness.client, "assets.fidelity_college")
        assert sorted(t["amount"] for t in rows) == ["750.0000", "750.0000"]
        # Identities preserved: hash-on-raw means the replayed rows carry the
        # SAME content hashes, so future live syncs dedup forever.
        assert _content_hashes("assets.fidelity_college") == hashes_before

        # Replay is idempotent: a second run re-lands nothing.
        with get_sessionmaker()() as session:
            _connection, fetch_ids = feed_repair.replay_plan(session, cid, None)
            totals2 = feed_repair.execute_replay(session, fetch_ids, actor_token_id=None)
        assert totals2["imported"] == 0
        assert len(_tx_rows(harness.client, "assets.fidelity_college")) == 2

    def test_replay_records_no_valuations_the_live_imports_already_did(
        self, harness: FeedHarness
    ) -> None:
        """#328: a loan's reported balance describes the moment its fetch was
        made, and the live import already recorded it. Replaying two archived
        fetches with different balances used to compare each OLD balance with
        the LATEST valuation and insert it again as a new valuation dated the
        old fetch's day; a replay now records none. The live path is unchanged:
        a later sync with a new balance still records one."""
        harness.use_payload(payload_of([], account_id="Loan X", balance="-50000.00"))
        cid = harness.connect()
        resp = harness.client.post(
            f"/connections/{cid}/map",
            json={
                "external_id": "Loan X",
                "create": {"name": "Loan X", "type": "loan", "path": "liabilities.loan_x"},
            },
        )
        assert resp.status_code == 200, resp.text
        loan_id = uuid.UUID(resp.json()["account_id"])

        # Two archived fetches on different days, each imported live as
        # sync_connection does (archive, then import with the fetch window).
        today = datetime.now(UTC)
        with get_sessionmaker()() as session:
            for days_ago, balance in ((20, "-50000.00"), (5, "-49500.00")):
                canonical = json.dumps(
                    payload_of([], account_id="Loan X", balance=balance), sort_keys=True
                )
                raw = RawFetch(
                    connection_id=uuid.UUID(cid),
                    fetched_at=today - timedelta(days=days_ago),
                    content_hash=hashlib.sha256(canonical.encode()).hexdigest(),
                    payload_enc=crypto.encrypt(canonical),
                )
                session.add(raw)
                session.flush()
                importer.import_fetch(
                    session, raw, window_start=(today - timedelta(days=days_ago + 7)).date()
                )
            session.commit()
        live = _valuations(loan_id)
        assert live == [
            ((today - timedelta(days=20)).date(), Decimal("-50000.0000"), "api"),
            ((today - timedelta(days=5)).date(), Decimal("-49500.0000"), "api"),
        ]

        with get_sessionmaker()() as session:
            _connection, fetch_ids = feed_repair.replay_plan(session, cid, None)
            assert len(fetch_ids) == 2
            totals = feed_repair.execute_replay(session, fetch_ids, actor_token_id=None)
        assert totals["fetches_replayed"] == 2
        assert totals["holdings_snapshots"] == 0
        assert _valuations(loan_id) == live  # no valuation rows inserted

        # The live sync path still values the loan by its reported balance.
        harness.use_payload(payload_of([], account_id="Loan X", balance="-49000.00"))
        harness.sync(cid)
        after_sync = _valuations(loan_id)
        assert len(after_sync) == 3
        assert after_sync[:2] == live
        assert after_sync[2][1:] == (Decimal("-49000.0000"), "api")
