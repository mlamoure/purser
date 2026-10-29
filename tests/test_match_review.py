"""Confirming a match is a review of its bank row (issue #286, DESIGN §4,
decision 47).

A row leaves the unreviewed set when anything token-bearing writes to it, and
confirming a match is such a write: whoever confirms that a bank row settles
an expected bill has looked at that row. Both confirm paths — confirming a
proposal, and matching a pair by hand (REST, and the MCP ``match`` tool) —
mark the bank row ``via='match'`` through the review service, so the mark is
audited like every other and names the token that decided the match.

Automation that merely touches a row never marks it (decision 47). The
matcher's own auto-confirm is automation, and so is decision 65's settlement
carry into an automatic merge: it moves the decision, not the review — the
merged transfer stays as reviewed as it was. A pending's posted successor is
different: it is the same bank line, so the pending's mark — the match's
review among them — follows it whole (decision 82, #342). Marks are
monotonic, so unmatching never clears one.

Synthetic data only. Dates are fixed (test_matching's TODAY), so nothing here
depends on the day the suite runs (#262).
"""

import uuid
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

import purser.db
from tests.test_matching import TODAY, _expected
from tests.test_mcp import LiveServer, _mcp_call
from tests.test_review import _all_rows


@pytest.fixture(scope="module")
def live_server(migrated_engine: Engine) -> Iterator[LiveServer]:
    server = LiveServer()
    server.start()
    yield server
    server.stop()


@pytest.fixture
def seeded(admin_client: TestClient) -> None:
    for name, type_, path in (
        ("Checking", "checking", "assets.checking"),
        ("Savings", "savings", "assets.savings"),
    ):
        resp = admin_client.post("/accounts", json={"name": name, "type": type_, "path": path})
        assert resp.status_code == 201, resp.text
    for name, path in (
        ("Uncategorized", "expenses.uncategorized"),
        ("Utilities", "expenses.utilities"),
    ):
        resp = admin_client.post(
            "/categories", json={"name": name, "type": "category_expense", "path": path}
        )
        assert resp.status_code == 201, resp.text


def _feed_row(
    *,
    amount: str,
    on: date,
    payee: str = "City Utilities",
    status: str = "posted",
    account: str = "assets.checking",
) -> str:
    """A bank row as the feed lands it — origin='import' WITH a
    source_fetch_id, which is what puts it in review scope (decision 45's
    feed-row definition). test_matching's ``_imported`` rows carry no fetch,
    so they are outside the set this file is about."""
    from purser import crypto, ledger
    from purser.core import ledger as core
    from purser.models import Connection, RawFetch

    with purser.db.get_sessionmaker()() as session:
        connection = Connection(provider="simplefin")
        session.add(connection)
        session.flush()
        fetch = RawFetch(
            connection_id=connection.id,
            content_hash=uuid.uuid4().hex,
            payload_enc=crypto.encrypt("{}"),
        )
        session.add(fetch)
        session.flush()
        tx = ledger.create_flat_transaction(
            session,
            account_ref=account,
            category_ref="expenses.uncategorized",
            amount=Decimal(amount),
            date=on,
            payee_name=payee,
            description=payee,
            requested_status=core.Status.POSTED if status == "posted" else core.Status.PENDING,
            origin=core.Origin.IMPORT,
            token=None,
            source_fetch_id=fetch.id,
            content_hash=uuid.uuid4().hex,
        )
        session.commit()
        return str(tx.id)


def _unreviewed(client: TestClient) -> set[str]:
    return {str(t["id"]) for t in _all_rows(client, reviewed=False)}


def _needs_review(client: TestClient, tx_id: str) -> bool:
    return bool(client.get(f"/transactions/{tx_id}").json()["needs_review"])


def _mark(engine: Engine, tx_id: str) -> Any:
    """The row's review mark (reviewed_by, via), or None."""
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT reviewed_by::text AS reviewed_by, via FROM transaction_reviews"
                " WHERE transaction_id = :t"
            ),
            {"t": tx_id},
        ).one_or_none()


def _match_on(engine: Engine, expected_id: str) -> Any:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT id::text AS id, posted_id::text AS posted_id, state,"
                " decided_by::text AS decided_by FROM matches"
                " WHERE expected_id = :e AND state IN ('proposed', 'confirmed')"
            ),
            {"e": expected_id},
        ).one()


def _review_audit(client: TestClient) -> dict[str, dict[str, Any]]:
    """transaction.reviewed audit rows by the row they mark."""
    items = client.get("/audit", params={"action": "transaction.reviewed"}).json()["items"]
    return {str(item["entity_id"]): item for item in items}


class TestConfirmingMarksTheBankRow:
    def test_confirming_a_proposal_marks_the_bank_row_reviewed(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        expected_id = _expected(client, "-180.00", TODAY)
        bank_id = _feed_row(
            amount="-95.00", on=TODAY + timedelta(days=3), payee="City Utilities Pmt"
        )
        assert client.post("/matching/run").json()["proposed"] == 1
        # A proposal is the matcher's guess, not anyone's decision.
        assert bank_id in _unreviewed(client)

        match = _match_on(migrated_engine, expected_id)
        assert client.post(f"/matching/{match.id}/confirm").status_code == 200

        assert bank_id not in _unreviewed(client)
        assert _needs_review(client, bank_id) is False
        confirmed = _match_on(migrated_engine, expected_id)
        mark = _mark(migrated_engine, bank_id)
        assert mark.via == "match"
        assert confirmed.decided_by is not None
        assert mark.reviewed_by == confirmed.decided_by
        audit = _review_audit(client)[bank_id]
        assert audit["before_after"]["after"]["via"] == "match"
        assert str(audit["actor_token_id"]) == confirmed.decided_by

    def test_a_manual_match_marks_the_bank_row_reviewed(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        expected_id = _expected(client, "-180.00", TODAY)
        bank_id = _feed_row(amount="-180.00", on=TODAY)
        assert bank_id in _unreviewed(client)

        resp = client.post(
            "/matching/match", json={"expected_id": expected_id, "posted_id": bank_id}
        )
        assert resp.status_code == 200, resp.text

        assert bank_id not in _unreviewed(client)
        mark = _mark(migrated_engine, bank_id)
        assert mark.via == "match"
        assert mark.reviewed_by == _match_on(migrated_engine, expected_id).decided_by

    def test_the_mcp_match_tool_marks_the_bank_row_reviewed(
        self,
        seeded: None,
        client: TestClient,
        migrated_engine: Engine,
        live_server: LiveServer,
    ) -> None:
        operator = client.post("/tokens", json={"name": "operator", "scopes": ["read", "write"]})
        assert operator.status_code == 201, operator.text
        expected_id = _expected(client, "-180.00", TODAY)
        bank_id = _feed_row(amount="-180.00", on=TODAY)

        (result,) = _mcp_call(
            live_server.url,
            operator.json()["token"],
            [("match", {"expected_id": expected_id, "posted_id": bank_id})],
        )
        assert result["state"] == "confirmed", result

        assert bank_id not in _unreviewed(client)
        mark = _mark(migrated_engine, bank_id)
        assert mark.via == "match"
        assert mark.reviewed_by == _match_on(migrated_engine, expected_id).decided_by
        assert mark.reviewed_by == operator.json()["id"]


class TestAutomationLeavesTheRowFlagged:
    def test_an_auto_match_leaves_the_bank_row_unreviewed(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        """The matcher confirming a confident match on its own is automation
        touching the row, not someone checking it."""
        expected_id = _expected(client, "-180.00", TODAY)
        bank_id = _feed_row(amount="-214.00", on=TODAY + timedelta(days=1))
        assert client.post("/matching/run").json()["auto_matched"] == 1

        match = _match_on(migrated_engine, expected_id)
        assert (match.posted_id, match.state, match.decided_by) == (bank_id, "confirmed", None)
        assert bank_id in _unreviewed(client)
        assert _mark(migrated_engine, bank_id) is None
        assert bank_id not in _review_audit(client)

    def test_a_match_carried_into_an_automatic_merge_moves_no_review(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        """Decision 65: a human matched the checking leg to an expected
        transfer; when the savings leg arrives the sweep merges the two and
        the confirmed match follows into the merge. An automatic merge is in
        the unreviewed set on its own account — two bank rows became one, and
        a merge is balance-neutral, so §7.3 cannot catch a wrong one — and the
        carried match does not take it out."""
        from purser import ledger, transfers
        from purser.core import ledger as core

        with purser.db.get_sessionmaker()() as session:
            expected = ledger.create_transfer(
                session,
                from_account_ref="assets.checking",
                to_account_ref="assets.savings",
                amount=Decimal("275.00"),
                date=TODAY,
                description="Monthly savings",
                requested_status=core.Status.EXPECTED,
                origin=core.Origin.RULE,
                token=None,
            )
            session.commit()
            expected_id = str(expected.id)
        out_id = _feed_row(amount="-275.00", on=TODAY, payee="TRANSFER TO SAVINGS")
        resp = client.post(
            "/matching/match", json={"expected_id": expected_id, "posted_id": out_id}
        )
        assert resp.status_code == 200, resp.text
        decided = _match_on(migrated_engine, expected_id)
        assert _mark(migrated_engine, out_id).via == "match"
        in_id = _feed_row(
            amount="275.00",
            on=TODAY + timedelta(days=1),
            payee="TRANSFER FROM CHECKING",
            account="assets.savings",
        )

        with purser.db.get_sessionmaker()() as session:
            stats = transfers.auto_link_pass(session)
            session.commit()
        assert stats["transfers_auto_linked"] == 1

        carried = _match_on(migrated_engine, expected_id)
        merged_id = carried.posted_id
        assert merged_id not in (out_id, in_id)
        assert (carried.id, carried.state, carried.decided_by) == (
            decided.id,
            "confirmed",
            decided.decided_by,
        )
        assert merged_id in _unreviewed(client)
        assert _mark(migrated_engine, merged_id) is None
        assert set(_review_audit(client)) == {out_id}


class TestUnmatchKeepsMarks:
    def test_unmatching_clears_no_mark(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        """Marks are monotonic: an unmatch neither clears the mark the
        confirm made nor one a person set by hand before it."""
        by_match_expected = _expected(client, "-180.00", TODAY)
        by_match = _feed_row(amount="-180.00", on=TODAY)
        by_hand_expected = _expected(client, "-60.00", TODAY, payee="Water Works")
        by_hand = _feed_row(amount="-60.00", on=TODAY, payee="Water Works")
        resp = client.post("/transactions/mark-reviewed", json={"transaction_ids": [by_hand]})
        assert resp.status_code == 200, resp.text

        for expected_id, bank_id in ((by_match_expected, by_match), (by_hand_expected, by_hand)):
            resp = client.post(
                "/matching/match", json={"expected_id": expected_id, "posted_id": bank_id}
            )
            assert resp.status_code == 200, resp.text
        # The hand mark stands; matching adds no second one.
        assert _mark(migrated_engine, by_hand).via == "explicit"
        assert _mark(migrated_engine, by_match).via == "match"

        for expected_id in (by_match_expected, by_hand_expected):
            match = _match_on(migrated_engine, expected_id)
            assert client.post(f"/matching/{match.id}/unmatch").status_code == 200

        assert {by_match, by_hand}.isdisjoint(_unreviewed(client))
        assert _mark(migrated_engine, by_hand).via == "explicit"
        assert _mark(migrated_engine, by_match).via == "match"


class TestTheReviewFollowsTheBankLine:
    """Decision 82 (#342): a pending's posted successor is the same bank
    line, so the mark a match made on the pending follows it."""

    def test_a_match_carried_onto_the_posted_successor_carries_its_review(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        """Decisions 69 and 82: the bank posts the pending a human matched;
        the confirmed match follows it to the posted row, and so does the
        review the match made — whole: who decided it, when, and how. The
        copy is automation (NULL audit actor) and emits nothing new."""
        from purser.feed import reconcile
        from purser.models import Transaction

        expected_id = _expected(client, "-180.00", TODAY)
        pending_id = _feed_row(amount="-180.00", on=TODAY, status="pending")
        successor_id = _feed_row(amount="-180.00", on=TODAY, payee="City Utilities Autopay")
        resp = client.post(
            "/matching/match", json={"expected_id": expected_id, "posted_id": pending_id}
        )
        assert resp.status_code == 200, resp.text
        decided = _match_on(migrated_engine, expected_id)
        assert _mark(migrated_engine, pending_id).via == "match"
        assert successor_id in _unreviewed(client)
        events_before = len(client.get("/events").json())

        with purser.db.get_sessionmaker()() as session:
            pending = session.get(Transaction, uuid.UUID(pending_id))
            successor = session.get(Transaction, uuid.UUID(successor_id))
            assert pending is not None and successor is not None
            reconcile.void_pending(session, pending, successor=successor)
            session.commit()

        carried = _match_on(migrated_engine, expected_id)
        assert (carried.id, carried.posted_id, carried.state) == (
            decided.id,
            successor_id,
            "confirmed",
        )
        assert successor_id not in _unreviewed(client)
        assert _needs_review(client, successor_id) is False
        with migrated_engine.connect() as conn:
            source, copy = (
                conn.execute(
                    text(
                        "SELECT reviewed_by::text AS reviewed_by, via, reviewed_at,"
                        " escalated_at, carried_from::text AS carried_from"
                        " FROM transaction_reviews WHERE transaction_id = :t"
                    ),
                    {"t": tx_id},
                ).one()
                for tx_id in (pending_id, successor_id)
            )
        assert (copy.via, copy.reviewed_by) == ("match", decided.decided_by)
        assert (copy.reviewed_at, copy.escalated_at) == (source.reviewed_at, source.escalated_at)
        assert copy.carried_from == pending_id
        audit = _review_audit(client)
        assert set(audit) == {pending_id, successor_id}
        assert audit[successor_id]["actor_token_id"] is None
        after = audit[successor_id]["before_after"]["after"]
        assert (after["via"], after["carried_from"], after["reason"]) == (
            "match",
            pending_id,
            "pending_posted",
        )
        assert after["reviewed_by"] == decided.decided_by
        # Decision 69 emits nothing for a flat bill's carry; the copy adds nothing.
        assert len(client.get("/events").json()) == events_before
