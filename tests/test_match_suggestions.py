"""Proposed matches in the suggestions inbox (issue #246 phase 1, DESIGN
decision 86).

A match proposal (``matches.state = 'proposed'``) gets a ``kind='match'``
card. The ``matches`` row stays the source of truth; the card renders it,
carries snooze, and remembers a rejection, so the nightly pass stops
proposing a pair a person said no to. Every path that moves a proposal off
``proposed`` closes its card: a token's confirm accepts it, a token's reject
rejects it, and any system action withdraws it.

Synthetic data only. Most dates are test_matching's fixed TODAY; the held
transfer leg and the schedule-slot tests read the real date, because the
transfer window and materialization do.
"""

import uuid
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, text

import purser.db
from tests.test_matching import TODAY, _expected, _imported
from tests.test_mcp import LiveServer, _mcp_call


@pytest.fixture(scope="module")
def live_server(migrated_engine: Engine) -> Iterator[LiveServer]:
    server = LiveServer()
    server.start()
    yield server
    server.stop()


@pytest.fixture
def seeded(admin_client: TestClient) -> dict[str, str]:
    ids: dict[str, str] = {}
    for name, type_, path in (
        ("Checking", "checking", "assets.checking"),
        ("Savings", "savings", "assets.savings"),
    ):
        resp = admin_client.post("/accounts", json={"name": name, "type": type_, "path": path})
        assert resp.status_code == 201, resp.text
        ids[path] = resp.json()["id"]
    resp = admin_client.post(
        "/categories",
        json={"name": "Utilities", "type": "category_expense", "path": "expenses.utilities"},
    )
    assert resp.status_code == 201, resp.text
    return ids


def _proposed_bill(client: TestClient, engine: Engine, *, status: str = "posted") -> dict[str, str]:
    """A bill and a bank row the matcher proposes (not auto-matches): close
    payee and date, far-off amount — test_matching's mid-score recipe."""
    expected_id = _expected(client, "-180.00", TODAY, payee="City Utilities")
    posted_id = _imported(
        client,
        engine,
        "assets.checking",
        "-95.00",
        TODAY + timedelta(days=3),
        payee="City Utilities Pmt",
        status=status,
    )
    stats = client.post("/matching/run").json()
    assert stats["proposed"] == 1, stats
    return {"expected_id": expected_id, "posted_id": posted_id}


def _cards(client: TestClient, status: str = "all") -> list[dict[str, Any]]:
    resp = client.get(
        "/suggestions", params={"kind": "match", "status": status, "include_snoozed": True}
    )
    assert resp.status_code == 200, resp.text
    return list(resp.json())


def _only_card(client: TestClient) -> dict[str, Any]:
    (card,) = _cards(client)
    return card


def _match(engine: Engine, expected_id: str) -> Any:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT id::text AS id, posted_id::text AS posted_id, state,"
                " decided_by::text AS decided_by FROM matches WHERE expected_id = :e"
                " ORDER BY created_at DESC LIMIT 1"
            ),
            {"e": expected_id},
        ).one()


def _event_count(engine: Engine, type_: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM events WHERE type = :t"), {"t": type_}
            ).scalar_one()
        )


def _audit(client: TestClient, action: str) -> list[dict[str, Any]]:
    return list(client.get("/audit", params={"action": action}).json()["items"])


def _decide(client: TestClient, card_id: str, **body: Any) -> Any:
    return client.post(f"/suggestions/{card_id}/decide", json=body)


class TestTheCard:
    def test_a_proposal_lands_in_the_inbox_as_a_match_card(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        pair = _proposed_bill(client, migrated_engine)

        card = _only_card(client)
        assert (card["kind"], card["status"], card["source"]) == ("match", "open", "detected")
        draft = card["draft"]
        assert (draft["expected_id"], draft["posted_id"]) == (
            pair["expected_id"],
            pair["posted_id"],
        )
        assert draft["account"] == "assets.checking"
        assert draft["label"] == "City Utilities"
        assert Decimal(draft["expected_amount"]) == Decimal("-180.00")
        assert Decimal(draft["posted_amount"]) == Decimal("-95.00")
        assert Decimal(draft["variance"]) == Decimal("85.00")
        assert draft["expected_date"] == TODAY.isoformat()
        assert "City Utilities" in card["headline"] and "variance 85.00" in card["headline"]
        assert card["evidence_summary"]["count"] == 1
        with migrated_engine.connect() as conn:
            score = conn.execute(
                text("SELECT score FROM matches WHERE expected_id = :e"),
                {"e": pair["expected_id"]},
            ).scalar_one()
        assert Decimal(card["confidence"]) == Decimal(str(score)).quantize(Decimal("0.001"))
        # match.proposed is kept beside suggestion.created.
        assert _event_count(migrated_engine, "match.proposed") == 1
        created = client.get("/events", params={"type": "suggestion.created"}).json()
        assert [e["payload"]["kind"] for e in created] == ["match"]

    def test_the_unmatched_report_names_the_proposal(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        pair = _proposed_bill(client, migrated_engine)
        rows = client.get("/matching/unmatched", params={"direction": "expected"}).json()
        (row,) = [r for r in rows if r["transaction_id"] == pair["expected_id"]]
        assert row["proposal_id"] == _match(migrated_engine, pair["expected_id"]).id
        assert row["proposed_posted_id"] == pair["posted_id"]


class TestDecidingTheCard:
    def test_accept_confirms_the_proposal(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        pair = _proposed_bill(client, migrated_engine)
        card = _only_card(client)

        resp = _decide(client, card["id"], decision="accept")

        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert out["match"]["state"] == "confirmed"
        assert out["suggestion"]["status"] == "accepted"
        assert out["suggestion"]["result_entity_type"] == "match"
        match = _match(migrated_engine, pair["expected_id"])
        assert (match.state, out["match"]["id"]) == ("confirmed", match.id)
        assert out["suggestion"]["result_entity_id"] == match.id
        assert match.decided_by is not None
        assert client.get(f"/transactions/{pair['expected_id']}").json()["status"] == "void"
        # One decision, one audit row: the hook skipped the card being decided.
        assert len(_audit(client, "suggestion.decide")) == 1

    def test_reject_rejects_the_proposal_and_is_remembered(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        pair = _proposed_bill(client, migrated_engine)
        card = _only_card(client)

        resp = _decide(client, card["id"], decision="reject")

        assert resp.status_code == 200, resp.text
        assert resp.json()["suggestion"]["status"] == "rejected"
        assert resp.json()["match"]["state"] == "rejected"
        assert _match(migrated_engine, pair["expected_id"]).state == "rejected"
        for _ in range(2):
            assert client.post("/matching/run").json()["proposed"] == 0
        assert _event_count(migrated_engine, "match.proposed") == 1
        assert client.get("/matching/proposed").json() == []
        assert _only_card(client)["status"] == "rejected"

    def test_a_register_reject_is_remembered_too(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """The nightly re-proposal #246 found: REST reject_match set the
        match rejected, and the next pass proposed the same pair again."""
        pair = _proposed_bill(client, migrated_engine)
        match = _match(migrated_engine, pair["expected_id"])

        assert client.post(f"/matching/{match.id}/reject").status_code == 200

        card = _only_card(client)
        assert (card["status"], card["reject_scope"]) == ("rejected", "exact")
        for _ in range(2):
            assert client.post("/matching/run").json()["proposed"] == 0
        assert _event_count(migrated_engine, "match.proposed") == 1
        # An explicit match can still pair them (the unlink precedent).
        resp = client.post(
            "/matching/match",
            json={"expected_id": pair["expected_id"], "posted_id": pair["posted_id"]},
        )
        assert resp.status_code == 200, resp.text

    def test_a_card_rejects_exactly_and_has_nothing_to_edit(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        _proposed_bill(client, migrated_engine)
        card = _only_card(client)

        scoped = _decide(client, card["id"], decision="reject", reject_scope="counterparty")
        edited = _decide(
            client, card["id"], decision="edit_then_accept", edits={"posted_id": str(uuid.uuid4())}
        )

        assert scoped.status_code == 422 and "exact" in scoped.json()["detail"]
        assert edited.status_code == 422 and "nothing to edit" in edited.json()["detail"]
        assert _only_card(client)["status"] == "open"

    def test_snooze_keeps_the_proposal_live(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        pair = _proposed_bill(client, migrated_engine)
        card = _only_card(client)

        resp = _decide(client, card["id"], decision="snooze")

        assert resp.status_code == 200, resp.text
        assert client.get("/suggestions", params={"kind": "match"}).json() == []  # hidden
        assert _match(migrated_engine, pair["expected_id"]).state == "proposed"
        assert [m["id"] for m in client.get("/matching/proposed").json()] == [
            _match(migrated_engine, pair["expected_id"]).id
        ]
        client.post("/matching/run")  # the sweep leaves a snoozed card alone
        (kept,) = _cards(client)
        assert (kept["status"], kept["snooze_until"]) == (
            "open",
            resp.json()["suggestion"]["snooze_until"],
        )

    def test_a_card_whose_proposal_is_gone_is_withdrawn_on_decide(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        pair = _proposed_bill(client, migrated_engine)
        card = _only_card(client)
        # A path with no hook took the proposal (a guarded delete does this).
        with migrated_engine.begin() as conn:
            conn.execute(
                text("UPDATE matches SET state = 'rejected' WHERE expected_id = :e"),
                {"e": pair["expected_id"]},
            )

        resp = _decide(client, card["id"], decision="accept")

        assert resp.status_code == 422
        assert "no longer proposed" in resp.json()["detail"]
        assert _only_card(client)["status"] == "withdrawn"  # committed despite the 422
        (withdrawn,) = _audit(client, "suggestion.withdraw")
        assert withdrawn["before_after"]["after"]["reason"] == "no_longer_proposed"

    def test_a_read_only_token_cannot_decide(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        _proposed_bill(client, migrated_engine)
        card = _only_card(client)
        agent = client.post("/tokens", json={"name": "agent", "scopes": ["read"]})
        assert agent.status_code == 201, agent.text

        resp = client.post(
            f"/suggestions/{card['id']}/decide",
            json={"decision": "accept"},
            headers={"Authorization": f"Bearer {agent.json()['token']}"},
        )

        assert resp.status_code == 403
        assert _only_card(client)["status"] == "open"


class TestTheCardFollowsTheProposal:
    """Each path that moves a proposal off ``proposed`` closes its card."""

    def test_confirming_in_the_register_accepts_the_card(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        pair = _proposed_bill(client, migrated_engine)
        match = _match(migrated_engine, pair["expected_id"])

        assert client.post(f"/matching/{match.id}/confirm").status_code == 200

        card = _only_card(client)
        assert (card["status"], card["result_entity_type"], card["result_entity_id"]) == (
            "accepted",
            "match",
            match.id,
        )
        decided = client.get("/events", params={"type": "suggestion.decided"}).json()
        assert [e["payload"]["decision"] for e in decided] == ["accept"]

    def test_matching_the_proposed_pair_by_hand_accepts_the_card(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        pair = _proposed_bill(client, migrated_engine)

        resp = client.post("/matching/match", json=pair)

        assert resp.status_code == 200, resp.text
        assert _only_card(client)["status"] == "accepted"

    def test_an_auto_match_that_rescinds_the_proposal_withdraws_the_card(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        pair = _proposed_bill(client, migrated_engine)
        # The real charge arrives: identical, so the AUTO tier settles the
        # bill and rescinds the competing proposal.
        _imported(
            client, migrated_engine, "assets.checking", "-180.00", TODAY, payee="City Utilities"
        )

        assert client.post("/matching/run").json()["auto_matched"] == 1

        card = _only_card(client)
        assert card["status"] == "withdrawn"
        assert card["draft"]["posted_id"] == pair["posted_id"]
        (withdrawn,) = _audit(client, "suggestion.withdraw")
        assert withdrawn["before_after"]["after"]["reason"] == "sibling_confirmed"

    def test_voiding_the_pending_by_hand_withdraws_the_card(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """#343's manual void rescinds the proposal; its card goes with it."""
        pair = _proposed_bill(client, migrated_engine, status="pending")

        assert client.post(f"/transactions/{pair['posted_id']}/void").status_code == 200

        assert _only_card(client)["status"] == "withdrawn"
        (withdrawn,) = _audit(client, "suggestion.withdraw")
        assert withdrawn["before_after"]["after"]["reason"] == "pending_voided"
        assert withdrawn["actor_token_id"] is not None  # the person who voided it

    def test_the_purge_withdraws_rather_than_rejects(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        from purser import matching

        pair = _proposed_bill(client, migrated_engine)
        match_id = uuid.UUID(_match(migrated_engine, pair["expected_id"]).id)
        with purser.db.get_sessionmaker()() as session:
            matching.reject_match(session, match_id=match_id, token=None)
            session.commit()

        assert _only_card(client)["status"] == "withdrawn"
        # A withdrawal is not a rejection: the pair may be proposed again.
        assert client.post("/matching/run").json()["proposed"] == 1
        assert _only_card(client)["status"] == "open"  # the same card, re-opened

    def test_a_merge_of_the_bank_row_withdraws_the_card(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        pair = _proposed_bill(client, migrated_engine)
        incoming = _imported(
            client,
            migrated_engine,
            "assets.savings",
            "95.00",
            TODAY + timedelta(days=3),
            payee="TRANSFER FROM CHECKING",
        )

        resp = client.post(
            "/matching/confirm-transfer",
            json={"outgoing_id": pair["posted_id"], "incoming_id": incoming},
        )
        assert resp.status_code == 200, resp.text

        old = [c for c in _cards(client) if c["draft"]["posted_id"] == pair["posted_id"]]
        assert [c["status"] for c in old] == ["withdrawn"]
        reasons = [
            a["before_after"]["after"]["reason"] for a in _audit(client, "suggestion.withdraw")
        ]
        assert "transfer.merge" in reasons

    def test_unlinking_the_transfer_a_proposal_names_withdraws_the_card(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        from purser import db, suggestions

        out_id = _imported(client, migrated_engine, "assets.checking", "-275.00", TODAY, "XFER")
        in_id = _imported(client, migrated_engine, "assets.savings", "275.00", TODAY, "XFER")
        resp = client.post(
            "/matching/confirm-transfer", json={"outgoing_id": out_id, "incoming_id": in_id}
        )
        assert resp.status_code == 200, resp.text
        merged = resp.json()["merged_transaction_id"]
        expected_id = _expected(client, "-300.00", TODAY, payee="Savings Sweep")
        with migrated_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO matches (expected_id, posted_id, score, variance, state)"
                    " VALUES (:e, :p, 0.6, 25, 'proposed')"
                ),
                {"e": expected_id, "p": merged},
            )
        with db.get_sessionmaker()() as session:
            suggestions.sync_match_suggestions(session)
            session.commit()
        assert _only_card(client)["status"] == "open"

        assert client.post(f"/transactions/{merged}/unlink-transfer").status_code == 200

        assert _only_card(client)["status"] == "withdrawn"
        reasons = [
            a["before_after"]["after"]["reason"] for a in _audit(client, "suggestion.withdraw")
        ]
        assert reasons == ["transfer.unlink"]


class TestRejectionMemory:
    def test_a_rejection_of_a_pending_holds_for_its_posted_successor(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        from purser.feed import reconcile
        from purser.models import Transaction

        pair = _proposed_bill(client, migrated_engine, status="pending")
        assert _decide(client, _only_card(client)["id"], decision="reject").status_code == 200
        successor = _imported(
            client,
            migrated_engine,
            "assets.checking",
            "-95.00",
            TODAY + timedelta(days=3),
            payee="CITY UTILITIES PMT 0042",
        )
        with purser.db.get_sessionmaker()() as session:
            pending = session.get(Transaction, uuid.UUID(pair["posted_id"]))
            posted = session.get(Transaction, uuid.UUID(successor))
            assert pending is not None and posted is not None
            reconcile.void_pending(session, pending, successor=posted)
            session.commit()

        assert client.post("/matching/run").json()["proposed"] == 0
        assert [c["status"] for c in _cards(client)] == ["rejected"]

    def test_a_slot_rejection_survives_an_unmatch_clone(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        schedule = client.post(
            "/schedules",
            json={
                "name": "City Utilities",
                "account": "assets.checking",
                "category": "expenses.utilities",
                "rrule": "FREQ=MONTHLY",
                "amount_type": "fixed",
                "amount": "-180.00",
                "payee": "City Utilities",
            },
        ).json()
        instance = client.get(f"/schedules/{schedule['id']}").json()["instances"][0]
        due = date.fromisoformat(instance["date"])
        rejected_row = _imported(
            client,
            migrated_engine,
            "assets.checking",
            "-95.00",
            due + timedelta(days=3),
            payee="City Utilities Pmt",
        )
        assert client.post("/matching/run").json()["proposed"] == 1
        assert _decide(client, _only_card(client)["id"], decision="reject").status_code == 200
        # The bill is marked paid by a row outside the window, then unmatched:
        # a fresh clone of the instance re-fills the same slot.
        far = _imported(
            client, migrated_engine, "assets.checking", "-180.00", due + timedelta(days=20)
        )
        paid = client.post(
            "/matching/match", json={"expected_id": instance["transaction_id"], "posted_id": far}
        )
        assert paid.status_code == 200, paid.text
        clone = client.post(f"/matching/{paid.json()['id']}/unmatch").json()
        assert clone["recreated_expected_id"] != instance["transaction_id"]

        assert client.post("/matching/run").json()["proposed"] == 0
        with migrated_engine.connect() as conn:
            proposals = conn.execute(
                text("SELECT count(*) FROM matches WHERE posted_id = :p AND state = 'proposed'"),
                {"p": rejected_row},
            ).scalar_one()
        assert proposals == 0


class TestTheSweep:
    def test_the_sweep_backfills_and_withdraws(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """Proposals made before cards existed get one on the next pass; an
        open card whose proposal is gone is withdrawn."""
        expected_id = _expected(client, "-180.00", TODAY)
        posted_id = _imported(client, migrated_engine, "assets.checking", "-150.00", TODAY)
        with migrated_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO matches (expected_id, posted_id, score, variance, state)"
                    " VALUES (:e, :p, 0.6, 30, 'proposed')"
                ),
                {"e": expected_id, "p": posted_id},
            )
        assert _cards(client) == []

        client.post("/matching/run")
        assert _only_card(client)["status"] == "open"

        with migrated_engine.begin() as conn:
            conn.execute(text("UPDATE matches SET state = 'rejected'"))
        client.post("/matching/run")
        assert _only_card(client)["status"] == "withdrawn"

    def test_a_skipped_bills_card_is_withdrawn(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """Voiding (skipping) the expected leaves its proposal live — confirming
        it fails "stale" — so the sweep takes the card out of the inbox."""
        pair = _proposed_bill(client, migrated_engine)
        assert client.post(f"/transactions/{pair['expected_id']}/void").status_code == 200

        client.post("/matching/run")

        assert _only_card(client)["status"] == "withdrawn"

    def test_a_held_transfer_leg_gets_a_card_only_after_its_window(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        from purser import db, ledger, suggestions
        from purser.core import ledger as core

        today = date.today()
        with db.get_sessionmaker()() as session:
            expected = ledger.create_transfer(
                session,
                from_account_ref="assets.checking",
                to_account_ref="assets.savings",
                amount=Decimal("275.00"),
                date=today,
                description="Monthly savings",
                requested_status=core.Status.EXPECTED,
                origin=core.Origin.RULE,
                token=None,
            )
            session.commit()
            expected_id = str(expected.id)
        # Pending, so the test can age it below (a posted row is immutable).
        leg = _imported(
            client,
            migrated_engine,
            "assets.checking",
            "-275.00",
            today,
            "TRANSFER TO SAVINGS",
            "pending",
        )
        with migrated_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO matches (expected_id, posted_id, score, variance, state)"
                    " VALUES (:e, :p, 0.6, 0, 'proposed')"
                ),
                {"e": expected_id, "p": leg},
            )

        def sweep() -> None:
            with db.get_sessionmaker()() as session:
                suggestions.sync_match_suggestions(session)
                session.commit()

        sweep()
        assert _cards(client) == []  # the merge usually settles it within days
        with migrated_engine.begin() as conn:
            conn.execute(
                text("UPDATE transactions SET date = :d WHERE id = :t"),
                {"d": today - timedelta(days=10), "t": leg},
            )
        sweep()
        assert _only_card(client)["status"] == "open"


class TestOverMcp:
    def test_an_agent_lists_and_accepts_a_match_card(
        self,
        seeded: dict[str, str],
        client: TestClient,
        migrated_engine: Engine,
        live_server: LiveServer,
    ) -> None:
        pair = _proposed_bill(client, migrated_engine)
        operator = client.post("/tokens", json={"name": "operator", "scopes": ["read", "write"]})
        assert operator.status_code == 201, operator.text
        token = operator.json()["token"]

        (listed,) = _mcp_call(live_server.url, token, [("list_suggestions", {"kind": "match"})])
        (item,) = listed["items"]
        assert item["kind"] == "match"
        (decided,) = _mcp_call(
            live_server.url,
            token,
            [("decide_suggestion", {"suggestion_id": item["id"], "decision": "accept"})],
        )

        assert decided["status"] == "accepted", decided
        match = _match(migrated_engine, pair["expected_id"])
        assert (decided["match"]["id"], decided["match"]["state"]) == (match.id, "confirmed")
        assert Decimal(decided["match"]["variance"]) == Decimal("85.00")
        assert match.decided_by == operator.json()["id"]


# --------------------------------------------------------------------------
# Property (guardrail 3)
# --------------------------------------------------------------------------

_OPS = st.lists(
    st.sampled_from(
        [
            "arrive",
            "arrive_pending",
            "card_accept",
            "card_reject",
            "snooze",
            "register_reject",
            "void_pending",
            "run",
        ]
    ),
    min_size=1,
    max_size=8,
)


class TestMirrorProperty:
    """Whatever sequence of proposals, decisions, voids and passes runs, at
    most one live match holds each side, and after a pass the open match
    cards are exactly the live, actionable proposals. Examples share the
    test's database, which only widens what the invariant covers."""

    @settings(
        max_examples=12, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(ops=_OPS)
    def test_the_inbox_mirrors_the_proposals(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine, ops: list[str]
    ) -> None:
        from sqlalchemy import func, select

        from purser import db, suggestions
        from purser.models import Match, Suggestion

        _expected(client, "-180.00", TODAY, payee="City Utilities")
        offset = 0
        for op in ops:
            open_cards = _cards(client, "open")
            if op in ("arrive", "arrive_pending"):
                offset = (offset + 1) % 5
                _imported(
                    client,
                    migrated_engine,
                    "assets.checking",
                    f"-{90 + offset}.00",
                    TODAY + timedelta(days=offset),
                    payee="City Utilities Pmt",
                    status="pending" if op == "arrive_pending" else "posted",
                )
            elif op == "run":
                client.post("/matching/run")
            elif op in ("card_accept", "card_reject", "snooze") and open_cards:
                decision = {"card_accept": "accept", "card_reject": "reject"}.get(op, "snooze")
                _decide(client, open_cards[0]["id"], decision=decision)
            elif op == "register_reject":
                proposed = client.get("/matching/proposed").json()
                if proposed:
                    client.post(f"/matching/{proposed[0]['id']}/reject")
            elif op == "void_pending":
                for m in client.get("/matching/proposed").json():
                    tx = client.get(f"/transactions/{m['posted_id']}").json()
                    if tx["status"] == "pending":
                        client.post(f"/transactions/{m['posted_id']}/void")
                        break
        client.post("/matching/run")

        with db.get_sessionmaker()() as session:
            for column in (Match.expected_id, Match.posted_id):
                worst = session.execute(
                    select(func.count())
                    .where(Match.state.in_(["proposed", "confirmed"]))
                    .group_by(column)
                    .order_by(func.count().desc())
                    .limit(1)
                ).scalar_one_or_none()
                assert (worst or 0) <= 1
            live = set()
            for match in session.execute(select(Match).where(Match.state == "proposed")).scalars():
                loaded = suggestions._match_rows(session, match)
                if loaded is not None and suggestions._card_worthy(match, *loaded):
                    live.add(suggestions.match_fingerprint(loaded[0], match.posted_id))
            open_cards_now = set(
                session.execute(
                    select(Suggestion.fingerprint).where(
                        Suggestion.kind == "match", Suggestion.status == "open"
                    )
                ).scalars()
            )
        assert open_cards_now == live


# --------------------------------------------------------------------------
# Migration 0034 up/down (scratch database, never the suite's)
# --------------------------------------------------------------------------


def test_migration_0034_round_trips(database_url: str) -> None:
    import os
    from pathlib import Path

    import sqlalchemy as sa
    from alembic import command
    from alembic.config import Config

    import purser.config
    from tests.conftest import TEST_ENCRYPTION_KEY

    os.environ["PURSER_ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
    purser.config.get_settings.cache_clear()
    admin = sa.create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text("DROP DATABASE IF EXISTS purser_match_sugg_mig WITH (FORCE)"))
        conn.execute(sa.text("CREATE DATABASE purser_match_sugg_mig"))
    admin.dispose()
    scratch_url = database_url.rsplit("/", 1)[0] + "/purser_match_sugg_mig"
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", scratch_url)
    engine = sa.create_engine(scratch_url)
    insert = sa.text(
        "INSERT INTO suggestions (kind, status, fingerprint, source, draft_enc)"
        " VALUES (:k, :s, :f, 'detected', '\\x00')"
    )
    try:
        command.upgrade(cfg, "0034")
        with engine.begin() as conn:
            conn.execute(insert, {"k": "match", "s": "open", "f": "match:tx:a:b"})
            conn.execute(insert, {"k": "schedule", "s": "withdrawn", "f": "schedule:w"})
            conn.execute(insert, {"k": "schedule", "s": "open", "f": "schedule:kept"})
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
            conn.execute(insert, {"k": "transfer_pair", "s": "open", "f": "pair"})

        command.downgrade(cfg, "-1")  # whatever 0034 is re-chained onto

        with engine.connect() as conn:
            left = set(conn.execute(sa.text("SELECT fingerprint FROM suggestions")).scalars())
        assert left == {"schedule:kept"}
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
            conn.execute(insert, {"k": "match", "s": "open", "f": "match:again"})
    finally:
        engine.dispose()
