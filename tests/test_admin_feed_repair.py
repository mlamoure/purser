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

from purser import crypto, qif_import
from purser.admin import feed_repair
from purser.admin.feed_repair import RepairRefused
from purser.db import get_sessionmaker
from purser.feed import importer
from purser.models import Account, RawFetch, Transaction, Valuation
from tests.conftest import now, today

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

        from datetime import timedelta

        since = today() - timedelta(days=5)
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

    @staticmethod
    def _linked_sale(harness: FeedHarness, *, quantity: str = "10") -> dict[str, str]:
        """A brokerage account tracked by hand whose feed sent a SOLD row of
        500.00, and a sale that linked it as its cash."""
        sold_on = today() - timedelta(days=6)
        while sold_on.weekday() >= 4:
            sold_on -= timedelta(days=1)
        sold_epoch = int(
            datetime(sold_on.year, sold_on.month, sold_on.day, 15, tzinfo=UTC).timestamp()
        )
        harness.use_payload(
            payload_of(
                [{"id": "s1", "posted": sold_epoch, "amount": "500.00", "description": "SOLD"}]
            )
        )
        cid = harness.connect()
        resp = harness.client.post(
            f"/connections/{cid}/map",
            json={
                "external_id": "Demo Checking",
                "create": {"name": "Plan", "type": "brokerage", "path": "assets.plan"},
            },
        )
        assert resp.status_code == 200, resp.text
        plan = _account_id_by_path(harness.client, "assets.plan")
        assert (
            harness.client.patch(
                f"/accounts/{plan}", json={"holdings_source": "manual"}
            ).status_code
            == 200
        )
        assert harness.sync(cid)["imported"] == 1
        (sold,) = _tx_rows(harness.client, "assets.plan")
        position = harness.client.post(
            f"/accounts/{plan}/positions",
            json={
                "symbol": "ACME",
                "quantity": quantity,
                "as_of": (sold_on - timedelta(days=30)).isoformat(),
            },
        ).json()["id"]
        price = str(Decimal("500") / Decimal(quantity))
        sale = harness.client.post(
            f"/positions/{position}/events",
            json={
                "kind": "sell",
                "occurred_on": sold_on.isoformat(),
                "quantity": quantity,
                "price": price,
                "transaction_id": sold["id"],
            },
        )
        assert sale.status_code == 201, sale.text
        return {
            "cid": cid,
            "plan": plan,
            "position": position,
            "event": sale.json()["event"]["id"],
            "sold": sold["id"],
            "sold_on": sold_on.isoformat(),
            "quantity": quantity,
            "price": price,
        }

    def test_purge_refuses_a_feed_row_linked_to_a_trade(self, harness: FeedHarness) -> None:
        """A sale that linked a feed row as its cash (decision 103) is undone
        through that row's correction chain. Deleting the row and NULLing the
        chain's lineage would leave the trade impossible to reverse, so the
        purge refuses, naming the trade and the steps that work."""
        sale = self._linked_sale(harness)
        with get_sessionmaker()() as session:
            account = session.get(Account, uuid.UUID(sale["plan"]))
            assert account is not None
            assert feed_repair.purge_preflight(session, account, None)["trade_links"] == 1
            for include in (False, True):
                with pytest.raises(RepairRefused, match="reverse the trade") as refused:
                    feed_repair.execute_purge(
                        session,
                        account,
                        since=None,
                        actor_token_id=None,
                        include_corrections=include,
                    )
                message = str(refused.value)
                assert sale["event"] in message and "ACME" in message
                for step in ("reopen", "manual", "--include-corrections", "replay", "again"):
                    assert step in message, step
                session.rollback()
            # Nothing was deleted: the bank row is still the trade's cash.
            assert session.get(Transaction, uuid.UUID(sale["sold"])) is not None

    def test_the_trade_link_recipe_counts_the_cash_once(
        self, harness: FeedHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refusal's steps, end to end, on a closed position: reopen,
        reverse, purge with --include-corrections, replay, record the trade
        again linking the re-imported row. The cash counts once, the
        register's heads equal the sums, and the nightly parity check finds
        nothing to rebuild."""
        from purser import balances
        from purser.models import Posting

        sale = self._linked_sale(harness, quantity="40")  # every share: then closed
        position, plan = sale["position"], uuid.UUID(sale["plan"])
        assert (
            harness.client.patch(f"/positions/{position}", json={"closed": True}).status_code == 200
        )
        # 1. reopen (2. the account is already tracked by hand)
        assert (
            harness.client.patch(f"/positions/{position}", json={"closed": False}).status_code
            == 200
        )
        # 3. reverse the trade: the row gets its category back
        assert harness.client.post(f"/position-events/{sale['event']}/reverse").status_code == 200
        with get_sessionmaker()() as session:
            account = session.get(Account, plan)
            assert account is not None
            plan_report = feed_repair.purge_preflight(session, account, None)
            assert (plan_report["trade_links"], plan_report["corrected"]) == (0, 1)
            assert plan_report["correction_rows"] == 4  # link pair + undo pair
            # Without the flag the corrected row is refused, listing it.
            with pytest.raises(RepairRefused, match="--include-corrections") as refused:
                feed_repair.execute_purge(session, account, since=None, actor_token_id=None)
            assert sale["sold"] in str(refused.value)
            session.rollback()
            # 4. purge with --include-corrections
            summary = feed_repair.execute_purge(
                session, account, since=None, actor_token_id=None, include_corrections=True
            )
            session.commit()
        assert summary["transactions_deleted"] == 5
        assert summary["corrected_purged"] == 1 and summary["correction_rows_purged"] == 4
        assert summary["position_events_unlinked"] == 2  # the sell and its reversal
        assert _tx_rows(harness.client, "assets.plan") == []
        # 5. replay
        with get_sessionmaker()() as session:
            _c, fetch_ids = feed_repair.replay_plan(session, sale["cid"], None)
            assert (
                feed_repair.execute_replay(session, fetch_ids, actor_token_id=None)["imported"] == 1
            )
        (reimported,) = _tx_rows(harness.client, "assets.plan")
        # 6. record the trade again, linking the re-imported row
        again = harness.client.post(
            f"/positions/{position}/events",
            json={
                "kind": "sell",
                "occurred_on": sale["sold_on"],
                "quantity": sale["quantity"],
                "price": sale["price"],
                "transaction_id": reimported["id"],
            },
        )
        assert again.status_code == 201, again.text

        with get_sessionmaker()() as session:
            later = select(Transaction.reverses_id).where(Transaction.reverses_id.is_not(None))
            heads = list(
                session.execute(
                    select(Posting.amount)
                    .join(Transaction, Transaction.id == Posting.transaction_id)
                    .where(
                        Posting.account_id == plan,
                        Transaction.status.in_(["pending", "posted"]),
                        Transaction.id.not_in(later),
                    )
                ).scalars()
            )
            assert [Decimal(a) for a in heads] == [Decimal("500")]  # counted once
            from purser import ledger

            assert ledger.account_balances(session, [plan])[plan] == Decimal("500")
            rebuilt: list[bool] = []
            real_rebuild = balances.rebuild_running

            def spy(session_: object) -> None:
                rebuilt.append(True)
                real_rebuild(session_)  # type: ignore[arg-type]

            monkeypatch.setattr(balances, "rebuild_running", spy)
            balances.heal_and_probe(session)
            session.commit()
            assert rebuilt == []
        listed = _tx_rows(harness.client, "assets.plan")
        assert [Decimal(r["amount"]) for r in listed] == [Decimal("500")]
        assert listed[0]["securities_trade"] is True

    def test_a_person_corrected_feed_row_is_refused_then_purged_whole(
        self, harness: FeedHarness
    ) -> None:
        """A posted feed row a person recategorized (original, reversal,
        replacement). Purging the original alone used to leave the
        replacement beside the replayed row, counted twice: it is refused,
        the preflight names it as a person's correction, and
        --include-corrections purges the chain so the replay counts it once."""
        from purser import ledger

        harness.use_payload(
            payload_of(
                [
                    {
                        "id": "p1",
                        "posted": _recent_epoch(3),
                        "amount": "500.00",
                        "description": "PAYROLL",
                    }
                ]
            )
        )
        cid = harness.connect()
        harness.map_new(cid, "Demo Checking", "assets.checking")
        assert harness.sync(cid)["imported"] == 1
        (row,) = _tx_rows(harness.client, "assets.checking")
        with get_sessionmaker()() as session:
            ledger.ensure_category_path(session, "income.bonus", actor_token_id=None)
            session.commit()
        resp = harness.client.patch(f"/transactions/{row['id']}", json={"category": "income.bonus"})
        assert resp.status_code == 200, resp.text
        account_id = uuid.UUID(_account_id_by_path(harness.client, "assets.checking"))
        with get_sessionmaker()() as session:
            account = session.get(Account, account_id)
            assert account is not None
            report = feed_repair.purge_preflight(session, account, None)
            assert report["corrected"] == 1 and report["person_corrected"] == [row["id"]]
            with pytest.raises(RepairRefused, match="corrected after import"):
                feed_repair.execute_purge(session, account, since=None, actor_token_id=None)
            session.rollback()
            summary = feed_repair.execute_purge(
                session, account, since=None, actor_token_id=None, include_corrections=True
            )
            session.commit()
        assert summary["transactions_deleted"] == 3 and summary["lineage_nulled"] == 0
        with get_sessionmaker()() as session:
            _c, fetch_ids = feed_repair.replay_plan(session, cid, None)
            feed_repair.execute_replay(session, fetch_ids, actor_token_id=None)
        listed = _tx_rows(harness.client, "assets.checking")
        assert [Decimal(r["amount"]) for r in listed] == [Decimal("500")]
        with get_sessionmaker()() as session:
            assert ledger.account_balances(session, [account_id])[account_id] == Decimal("500")


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
        with get_sessionmaker()() as session:
            for days_ago, balance in ((20, "-50000.00"), (5, "-49500.00")):
                canonical = json.dumps(
                    payload_of([], account_id="Loan X", balance=balance), sort_keys=True
                )
                raw = RawFetch(
                    connection_id=uuid.UUID(cid),
                    fetched_at=now() - timedelta(days=days_ago),
                    content_hash=hashlib.sha256(canonical.encode()).hexdigest(),
                    payload_enc=crypto.encrypt(canonical),
                )
                session.add(raw)
                session.flush()
                importer.import_fetch(
                    session, raw, window_start=(now() - timedelta(days=days_ago + 7)).date()
                )
            session.commit()
        live = _valuations(loan_id)
        assert live == [
            ((now() - timedelta(days=20)).date(), Decimal("-50000.0000"), "api"),
            ((now() - timedelta(days=5)).date(), Decimal("-49500.0000"), "api"),
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
