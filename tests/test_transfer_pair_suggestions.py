"""Transfer candidates in the suggestions inbox (issue #386, #246 phase 2,
DESIGN decision 102).

A transfer candidate — two one-sided bank rows of exactly opposite amounts on
two accounts, inside the transfer window, that no signal links on its own —
gets a ``kind='transfer_pair'`` card from the matching pass after each import
and in the daily job, while its newer leg is at most 60 days old. The
candidate list stays the source of truth and is a pure read. Accept confirms
the transfer; reject means "not a transfer", which every automatic link path
honours; a card whose pair stops being a candidate, or that the auto-linker
links, is withdrawn.

Synthetic data only. Dates are offsets from the real date, read per call,
because the card window is measured from today.
"""

import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from time import monotonic, sleep
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

import purser.db
from tests.conftest import now, today
from tests.test_matching import _expected, _imported
from tests.test_mcp import LiveServer, _mcp_call
from tests.test_transfer_link_window import _activate, _merged_into, _rule

CHECKING, CARD, SAVINGS = "assets.checking", "liabilities.card", "assets.savings"
BROKERAGE = "assets.brokerage"
# Descriptors with no name signal: no transfer keyword, and no token naming
# the counterparty account ("Everyday", "Rewards", "Reserve").
OUT_TEXT = "EXAMPLE BANK WEB DEBIT"
IN_TEXT = "THANK YOU FOR YOUR REMITTANCE"


def _ago(days: int) -> date:
    # The card window and lookback_days count from the server's today, which
    # is the test's pinned day.
    return today() - timedelta(days=days)


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
        ("Everyday", "checking", CHECKING),
        ("Rewards", "credit_card", CARD),
        ("Reserve", "savings", SAVINGS),
        ("Invest", "brokerage", BROKERAGE),
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


def _pair(
    client: TestClient,
    engine: Engine,
    amount: str,
    out_ago: int,
    in_ago: int,
    *,
    to: str = CARD,
    out_text: str = OUT_TEXT,
    in_text: str = IN_TEXT,
    out_status: str = "posted",
    in_status: str = "posted",
) -> tuple[str, str]:
    """One-sided bank rows: ``amount`` leaves checking ``out_ago`` days ago
    and arrives on ``to`` ``in_ago`` days ago."""
    out_id = _imported(
        client, engine, CHECKING, f"-{amount}", _ago(out_ago), payee=out_text, status=out_status
    )
    in_id = _imported(client, engine, to, amount, _ago(in_ago), payee=in_text, status=in_status)
    return out_id, in_id


def _key(a: str, b: str) -> str:
    low, high = sorted((uuid.UUID(a), uuid.UUID(b)))
    return f"{low}:{high}"


def _pass() -> dict[str, int]:
    """What the importer and the daily job run, committed: the auto-link
    pass, then the inbox side of what it left unlinked."""
    from purser import transfers

    with purser.db.get_sessionmaker()() as session:
        stats = transfers.auto_link_pass(session)
        stats.update(transfers.sync_candidate_suggestions(session))
        session.commit()
    return stats


def _cards(client: TestClient, status: str = "all") -> list[dict[str, Any]]:
    resp = client.get(
        "/suggestions",
        params={"kind": "transfer_pair", "status": status, "include_snoozed": True},
    )
    assert resp.status_code == 200, resp.text
    return list(resp.json())


def _only_card(client: TestClient) -> dict[str, Any]:
    (card,) = _cards(client)
    return card


def _candidates(client: TestClient, **params: Any) -> list[dict[str, Any]]:
    resp = client.get("/matching/transfer-candidates", params=params)
    assert resp.status_code == 200, resp.text
    return list(resp.json())


def _decide(client: TestClient, card_id: str, **body: Any) -> Any:
    return client.post(f"/suggestions/{card_id}/decide", json=body)


def _not_a_transfer(client: TestClient, out_id: str, in_id: str, **headers: str) -> Any:
    return client.post(
        "/matching/transfer-candidates/reject",
        json={"outgoing_id": out_id, "incoming_id": in_id},
        headers=headers or None,
    )


def _event_keys(engine: Engine, type_: str) -> list[str]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text("SELECT dedup_key FROM events WHERE type = :t ORDER BY id"), {"t": type_}
            ).scalars()
        )


def _audit(client: TestClient, action: str) -> list[dict[str, Any]]:
    return list(client.get("/audit", params={"action": action}).json()["items"])


def _row_counts(engine: Engine) -> dict[str, int]:
    """Rows of everything a write would touch. Audit rows are counted by
    kind: ``change`` rows record what a write did, ``write`` rows a write
    call; every authenticated request, reads included, also leaves one call
    row of its own kind (#122), so ``read`` rows are not counted."""
    with engine.connect() as conn:
        counts = {
            table: int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())
            for table in ("events", "suggestions", "transactions", "postings")
        }
        for kind in ("change", "write"):
            counts[f"audit_{kind}"] = int(
                conn.execute(
                    text("SELECT count(*) FROM audit_log WHERE kind = :k"), {"k": kind}
                ).scalar_one()
            )
    return counts


def _without_calls(counts: dict[str, int]) -> dict[str, int]:
    return {key: n for key, n in counts.items() if key != "audit_write"}


def _tx(client: TestClient, tx_id: str) -> dict[str, Any]:
    resp = client.get(f"/transactions/{tx_id}")
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def _post(client: TestClient, engine: Engine, pending_id: str) -> str:
    """The bank posts a pending row: its posted successor arrives as a new
    import row, and the importer's reconcile step supersedes the pending with
    it (``reconcile.void_pending`` — the production path: audit, balances,
    any live match carried to the successor, the review mark)."""
    from purser.feed import reconcile

    row = _tx(client, pending_id)
    successor_id = _imported(
        client,
        engine,
        row["account_path"],
        row["amount"],
        date.fromisoformat(row["date"]),
        payee=row["description"],
    )
    with purser.db.get_sessionmaker()() as session:
        from purser import matching

        reconcile.void_pending(
            session,
            matching._load_tx(session, uuid.UUID(pending_id)),
            successor=matching._load_tx(session, uuid.UUID(successor_id)),
        )
        session.commit()
    return successor_id


class TestTheCard:
    def test_a_recent_candidate_becomes_a_card_with_one_event(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id, in_id = _pair(client, migrated_engine, "250.00", 3, 2)

        stats = _pass()

        assert stats["transfer_pair_cards_created"] == 1
        card = _only_card(client)
        assert (card["kind"], card["status"], card["source"]) == (
            "transfer_pair",
            "open",
            "detected",
        )
        assert card["confidence"] is None  # nothing scores a candidate
        draft = card["draft"]
        assert (draft["outgoing_id"], draft["incoming_id"]) == (out_id, in_id)
        assert (draft["from_account"], draft["to_account"]) == (CHECKING, CARD)
        assert Decimal(draft["amount"]) == Decimal("250.00")
        assert (draft["outgoing_date"], draft["incoming_date"]) == (
            _ago(3).isoformat(),
            _ago(2).isoformat(),
        )
        assert draft["date_gap_days"] == 1
        assert (draft["outgoing_description"], draft["incoming_description"]) == (
            OUT_TEXT,
            IN_TEXT,
        )
        # Accounts by name in the headline (#552), by path in the draft.
        assert card["headline"] == (
            f"Link 250.00 from Everyday ({_ago(3).isoformat()}) to Rewards"
            f" ({_ago(2).isoformat()}) as one transfer"
        )
        assert card["evidence_summary"]["count"] == 1
        assert card["evidence_summary"]["amount_range"] == {"low": "250.00", "high": "250.00"}
        # transfer.candidate keeps its old dedup key: the pair key.
        assert _event_keys(migrated_engine, "transfer.candidate") == [_key(out_id, in_id)]
        assert _event_keys(migrated_engine, "suggestion.created") == [
            f"transfer_pair:{_key(out_id, in_id)}"
        ]
        created = client.get("/events", params={"type": "suggestion.created"}).json()
        assert created[0]["payload"]["kind"] == "transfer_pair"
        assert created[0]["payload"]["account"] == CHECKING
        assert created[0]["payload"]["counterparty_account"] == CARD
        assert Decimal(created[0]["payload"]["amount"]) == Decimal("250.00")
        (listed,) = _candidates(client)
        assert listed["suggestion_id"] == card["id"]

        # Another pass changes nothing: one card, one event of each.
        assert _pass()["transfer_pair_cards_created"] == 0
        (again,) = _cards(client)
        assert (again["id"], again["seen_count"]) == (card["id"], 1)
        assert len(_event_keys(migrated_engine, "transfer.candidate")) == 1
        assert len(_event_keys(migrated_engine, "suggestion.created")) == 1

    def test_cards_reach_back_60_days(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """A pair whose newer leg is 60 days old gets a card; 61 days old,
        it stays in the register only — no card and no event."""
        recent = _pair(client, migrated_engine, "61.00", 61, 60)
        old = _pair(client, migrated_engine, "62.00", 62, 61)

        _pass()

        (card,) = _cards(client)
        assert (card["draft"]["outgoing_id"], card["draft"]["incoming_id"]) == recent
        assert _event_keys(migrated_engine, "transfer.candidate") == [_key(*recent)]
        listed = {(c["outgoing_id"], c["incoming_id"]): c for c in _candidates(client)}
        assert set(listed) == {recent, old}
        assert listed[recent]["suggestion_id"] == card["id"]
        assert listed[old]["suggestion_id"] is None

    def test_accept_confirms_the_transfer_and_chains_a_transfer_link(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id, in_id = _pair(client, migrated_engine, "250.00", 3, 2)
        _pass()
        card = _only_card(client)

        resp = _decide(client, card["id"], decision="accept")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        merged_id = body["transfer"]["merged_transaction_id"]
        assert body["suggestion"]["status"] == "accepted"
        assert (
            body["suggestion"]["result_entity_type"],
            body["suggestion"]["result_entity_id"],
        ) == (
            "transaction",
            merged_id,
        )
        merged = _tx(client, merged_id)
        assert merged["is_transfer"] is True
        assert sorted(Decimal(p["amount"]) for p in merged["postings"]) == [
            Decimal("-250.00"),
            Decimal("250.00"),
        ]
        for original in (out_id, in_id):
            detail = _tx(client, original)
            assert (detail["status"], detail["superseded_by"]) == ("void", merged_id)
        # The confirm proposed the payee-scoped rule, as a register confirm does.
        link = client.get(f"/suggestions/{body['transfer']['suggestion_id']}").json()
        assert (link["kind"], link["status"], link["source"]) == (
            "transfer_link",
            "open",
            "confirm",
        )
        assert len(_event_keys(migrated_engine, "transfer.confirmed")) == 1
        # One decision, one audit row: the merge's hook skipped the card being decided.
        assert len(_audit(client, "suggestion.decide")) == 1
        decided = client.get("/events", params={"type": "suggestion.decided"}).json()
        assert [(e["payload"]["kind"], e["payload"]["result_entity_type"]) for e in decided] == [
            ("transfer_pair", "transaction")
        ]
        _pass()
        assert _candidates(client) == []
        assert [c["status"] for c in _cards(client)] == ["accepted"]

    def test_reject_is_remembered_by_the_pass_the_list_and_the_inbox(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id, in_id = _pair(client, migrated_engine, "250.00", 3, 2)
        _pass()
        card = _only_card(client)

        resp = _decide(client, card["id"], decision="reject")

        assert resp.status_code == 200, resp.text
        assert (resp.json()["suggestion"]["status"], resp.json()["suggestion"]["reject_scope"]) == (
            "rejected",
            "exact",
        )
        assert resp.json()["transfer"] is None
        for _ in range(2):
            assert _pass()["transfer_pair_cards_created"] == 0
        assert [c["status"] for c in _cards(client)] == ["rejected"]
        assert _candidates(client) == []
        assert len(_event_keys(migrated_engine, "transfer.candidate")) == 1
        for row in (out_id, in_id):
            assert (_tx(client, row)["status"], _tx(client, row)["superseded_by"]) == (
                "posted",
                None,
            )
        # No undo but an explicit link, as for an unlink or a rejected match:
        # a confirm still merges the pair, and the rejection stays recorded.
        confirmed = client.post(
            "/matching/confirm-transfer", json={"outgoing_id": out_id, "incoming_id": in_id}
        )
        assert confirmed.status_code == 200, confirmed.text
        assert [c["status"] for c in _cards(client)] == ["rejected"]

    def test_a_card_rejects_exactly_and_has_nothing_to_edit(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        _pair(client, migrated_engine, "250.00", 3, 2)
        _pass()
        card = _only_card(client)

        scoped = _decide(client, card["id"], decision="reject", reject_scope="counterparty")
        edited = _decide(client, card["id"], decision="edit_then_accept", edits={"amount": "1.00"})

        assert scoped.status_code == 422 and "exact" in scoped.json()["detail"]
        assert edited.status_code == 422 and "nothing to edit" in edited.json()["detail"]
        assert _only_card(client)["status"] == "open"

    def test_snooze_hides_the_card_and_the_pass_keeps_it(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        _pair(client, migrated_engine, "250.00", 3, 2)
        _pass()
        card = _only_card(client)

        resp = _decide(client, card["id"], decision="snooze")

        assert resp.status_code == 200, resp.text
        assert client.get("/suggestions", params={"kind": "transfer_pair"}).json() == []
        _pass()
        (kept,) = _cards(client)
        assert (kept["status"], kept["snooze_until"]) == (
            "open",
            resp.json()["suggestion"]["snooze_until"],
        )
        # Snoozed is still the pair's card: the register can decide it.
        assert _candidates(client)[0]["suggestion_id"] == card["id"]

    def test_a_card_whose_pair_moved_on_is_withdrawn_on_decide(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id, _ = _pair(client, migrated_engine, "250.00", 3, 2)
        _pass()
        card = _only_card(client)
        # The outgoing row pays a bill instead; no pass has run since.
        bill_id = _expected(client, "-250.00", _ago(3), payee="City Utilities")
        matched = client.post("/matching/match", json={"expected_id": bill_id, "posted_id": out_id})
        assert matched.status_code == 200, matched.text

        resp = _decide(client, card["id"], decision="accept")

        assert resp.status_code == 422
        assert "no longer a transfer candidate" in resp.json()["detail"]
        assert _only_card(client)["status"] == "withdrawn"  # committed despite the 422
        (withdrawn,) = _audit(client, "suggestion.withdraw")
        assert withdrawn["before_after"]["after"]["reason"] == "no_longer_candidate"

    def test_the_pass_withdraws_a_card_and_reopens_it_when_the_pair_is_back(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id, _ = _pair(client, migrated_engine, "250.00", 3, 2)
        _pass()
        card = _only_card(client)
        # Marking a bill paid with the outgoing row takes it out of the pool.
        bill_id = _expected(client, "-250.00", _ago(3), payee="City Utilities")
        matched = client.post("/matching/match", json={"expected_id": bill_id, "posted_id": out_id})
        assert matched.status_code == 200, matched.text

        assert _pass()["transfer_pair_cards_withdrawn"] == 1
        assert _only_card(client)["status"] == "withdrawn"
        assert _candidates(client) == []
        withdrawn = _audit(client, "suggestion.withdraw")
        assert [w["before_after"]["after"]["reason"] for w in withdrawn] == ["no_longer_candidate"]

        # Unmatched, the row is free again: the same card opens once more.
        assert client.post(f"/matching/{matched.json()['id']}/unmatch").status_code == 200
        assert _pass()["transfer_pair_cards_created"] == 1
        reopened = _only_card(client)
        assert (reopened["id"], reopened["status"], reopened["seen_count"]) == (
            card["id"],
            "open",
            2,
        )
        assert len(_event_keys(migrated_engine, "transfer.candidate")) == 1

    def test_a_reopened_card_keeps_its_snooze(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """#386 review, item 7: "ask me later" outlives a withdrawal. The pair
        leaves the candidates for a while (a row marked a bill paid, then
        unmatched) and comes back: the card re-opens still snoozed."""
        out_id, _ = _pair(client, migrated_engine, "250.00", 3, 2)
        _pass()
        card = _only_card(client)
        snoozed = _decide(client, card["id"], decision="snooze")
        assert snoozed.status_code == 200, snoozed.text
        until = snoozed.json()["suggestion"]["snooze_until"]
        bill_id = _expected(client, "-250.00", _ago(3), payee="City Utilities")
        matched = client.post("/matching/match", json={"expected_id": bill_id, "posted_id": out_id})
        assert matched.status_code == 200, matched.text
        assert _pass()["transfer_pair_cards_withdrawn"] == 1

        assert client.post(f"/matching/{matched.json()['id']}/unmatch").status_code == 200
        assert _pass()["transfer_pair_cards_created"] == 1

        reopened = _only_card(client)
        assert (reopened["id"], reopened["status"], reopened["snooze_until"]) == (
            card["id"],
            "open",
            until,
        )
        assert client.get("/suggestions", params={"kind": "transfer_pair"}).json() == []

    def test_an_account_move_repoints_the_card(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """#386 review, item 10: the draft names its accounts by path, so a
        move re-points it at once, as it does a schedule draft's. The headline
        names them (#552), read when the card is served, so a rename shows at
        once too."""
        _pair(client, migrated_engine, "250.00", 3, 2)
        _pass()

        moved = client.patch(f"/accounts/{seeded[CHECKING]}", json={"path": "assets.everyday"})

        assert moved.status_code == 200, moved.text
        card = _only_card(client)
        assert (card["draft"]["from_account"], card["draft"]["to_account"]) == (
            "assets.everyday",
            CARD,
        )
        assert card["headline"].startswith("Link 250.00 from Everyday (")

        renamed = client.patch(f"/accounts/{seeded[CHECKING]}", json={"name": "Daily Spending"})
        assert renamed.status_code == 200, renamed.text
        assert _only_card(client)["headline"].startswith("Link 250.00 from Daily Spending (")

    def test_the_auto_linker_withdraws_the_card(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id, _ = _pair(client, migrated_engine, "250.00", 3, 2)
        _pass()
        _only_card(client)

        assert _activate(client, _rule(client, seeded)) == 1
        assert _pass()["transfers_auto_linked"] == 1

        assert _merged_into(migrated_engine, out_id) is not None
        assert _only_card(client)["status"] == "withdrawn"
        (withdrawn,) = _audit(client, "suggestion.withdraw")
        assert withdrawn["before_after"]["after"]["reason"] == "transfer.auto_link"
        assert withdrawn["before_after"]["after"]["merged_id"] == _merged_into(
            migrated_engine, out_id
        )

    def test_confirming_the_pair_in_the_register_accepts_the_card(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id, in_id = _pair(client, migrated_engine, "250.00", 3, 2)
        _pass()

        resp = client.post(
            "/matching/confirm-transfer", json={"outgoing_id": out_id, "incoming_id": in_id}
        )

        assert resp.status_code == 200, resp.text
        card = _only_card(client)
        assert (card["status"], card["result_entity_type"], card["result_entity_id"]) == (
            "accepted",
            "transaction",
            resp.json()["merged_transaction_id"],
        )
        decided = client.get("/events", params={"type": "suggestion.decided"}).json()
        assert [e["payload"]["decision"] for e in decided] == ["accept"]

    def test_only_a_confirm_accepts_the_card(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """#386 review, item 6: a card is accepted by its own decide or a
        person's confirm_transfer. A merge some other path makes — a machine
        conversion (the categorize pass, a rule's apply-history), even run
        under a token, or an '[account]' edit that picks the counterpart
        itself — withdraws it: nobody decided the pair the card showed."""
        from purser import matching, transfers
        from purser.models import ApiToken

        machine_out, _ = _pair(client, migrated_engine, "250.00", 3, 2)
        edited_out, _ = _pair(client, migrated_engine, "130.00", 3, 2)
        _pass()
        runner = client.post("/tokens", json={"name": "rule runner", "scopes": ["read", "write"]})
        assert runner.status_code == 201, runner.text

        with purser.db.get_sessionmaker()() as session:
            token = session.get(ApiToken, uuid.UUID(runner.json()["id"]))
            assert token is not None
            transfers.convert_to_transfer(
                session,
                tx=matching._load_tx(session, uuid.UUID(machine_out)),
                target_ref=CARD,
                token=token,
                auto_pairing=True,
            )
            session.commit()
        edited = client.patch(f"/transactions/{edited_out}", json={"category": f"[{CARD}]"})
        assert edited.status_code == 200, edited.text

        cards = {c["draft"]["outgoing_id"]: c for c in _cards(client)}
        assert _merged_into(migrated_engine, machine_out) is not None  # it did merge
        assert _merged_into(migrated_engine, edited_out) is not None
        assert cards[machine_out]["status"] == "withdrawn"
        assert cards[edited_out]["status"] == "withdrawn"
        assert client.get("/events", params={"type": "suggestion.decided"}).json() == []
        # Round 2, item 11: the withdrawal names the conversion that merged
        # the pair, not a confirm nobody made.
        withdrawals = _audit(client, "suggestion.withdraw")
        assert sorted(w["entity_id"] for w in withdrawals) == sorted(
            (cards[machine_out]["id"], cards[edited_out]["id"])
        )
        assert {w["before_after"]["after"]["reason"] for w in withdrawals} == {"transfer.convert"}

    def test_merging_one_of_its_rows_elsewhere_withdraws_the_card(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """The out-row pairs with the earlier arrival on the card, so that is
        the candidate; confirming it with the savings arrival instead leaves
        the card's pair nothing to link."""
        out_id = _imported(client, migrated_engine, CHECKING, "-250.00", _ago(3), payee=OUT_TEXT)
        card_in = _imported(client, migrated_engine, CARD, "250.00", _ago(2), payee=IN_TEXT)
        savings_in = _imported(client, migrated_engine, SAVINGS, "250.00", _ago(1), payee=IN_TEXT)
        _pass()
        card = _only_card(client)
        assert card["draft"]["incoming_id"] == card_in

        resp = client.post(
            "/matching/confirm-transfer", json={"outgoing_id": out_id, "incoming_id": savings_in}
        )

        assert resp.status_code == 200, resp.text
        assert _only_card(client)["status"] == "withdrawn"
        (withdrawn,) = _audit(client, "suggestion.withdraw")
        assert withdrawn["before_after"]["after"]["reason"] == "transfer.confirm"


class TestTheCandidateList:
    def test_it_is_a_pure_read(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """No events, no cards, no audit rows, no ledger change — before a
        pass has run and after one."""
        out_id, in_id = _pair(client, migrated_engine, "250.00", 3, 2)
        before = _row_counts(migrated_engine)

        whole = _candidates(client)
        bounded = _candidates(client, lookback_days=60)

        assert _row_counts(migrated_engine) == before
        assert [(c["outgoing_id"], c["incoming_id"]) for c in whole] == [(out_id, in_id)]
        assert bounded == whole
        assert whole[0]["suggestion_id"] is None  # no pass has made its card yet
        assert _event_keys(migrated_engine, "transfer.candidate") == []

        _pass()
        after_pass = _row_counts(migrated_engine)
        assert _candidates(client)[0]["suggestion_id"] == _only_card(client)["id"]
        assert _row_counts(migrated_engine) == after_pass

    def test_lookback_days_bounds_it_by_the_newer_leg(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        near = _pair(client, migrated_engine, "11.00", 2, 1)
        ten = _pair(client, migrated_engine, "12.00", 12, 10)  # the newer leg is 10 days old
        far = _pair(client, migrated_engine, "13.00", 42, 40)

        def pairs(**params: Any) -> set[tuple[str, str]]:
            return {(c["outgoing_id"], c["incoming_id"]) for c in _candidates(client, **params)}

        assert pairs() == {near, ten, far}  # omitted: the whole history
        assert pairs(lookback_days=10) == {near, ten}
        assert pairs(lookback_days=9) == {near}
        assert pairs(lookback_days=0) == set()
        refused = client.get("/matching/transfer-candidates", params={"lookback_days": -1})
        assert refused.status_code == 422

    def test_lookback_days_has_an_upper_bound(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """#386 review, item 11: a century back is the most it takes; a
        larger value is a 422, never a date overflow (a 500)."""
        pair = _pair(client, migrated_engine, "11.00", 2, 1)

        widest = _candidates(client, lookback_days=36500)
        refused = client.get("/matching/transfer-candidates", params={"lookback_days": 10**9})

        assert [(c["outgoing_id"], c["incoming_id"]) for c in widest] == [pair]
        assert refused.status_code == 422, refused.text

    def test_an_unlinked_pair_is_left_out(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """Unlink suppression, which the auto paths always honoured, now
        applies to the list too: the resurrected rows get no card and no
        "Link" flag."""
        out_id, in_id = _pair(client, migrated_engine, "250.00", 3, 2)
        merged = client.post(
            "/matching/confirm-transfer", json={"outgoing_id": out_id, "incoming_id": in_id}
        ).json()["merged_transaction_id"]

        resp = client.post(f"/transactions/{merged}/unlink-transfer")

        assert resp.status_code == 200, resp.text
        assert _candidates(client) == []
        _pass()
        assert _cards(client, "open") == []


class TestSuppression:
    def test_a_rejected_pair_is_never_auto_linked_even_by_a_rule(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """The rule declares the pair, so it would link both pairs without a
        name signal; the projected count leaves the rejected one out and the
        pass links exactly what was projected (decision 66)."""
        rejected = _pair(client, migrated_engine, "250.00", 6, 5)
        control = _pair(client, migrated_engine, "130.00", 2, 1)
        _pass()
        (card,) = [c for c in _cards(client) if c["draft"]["outgoing_id"] == rejected[0]]
        assert _decide(client, card["id"], decision="reject").status_code == 200

        rule_id = _rule(client, seeded)
        projected = _activate(client, rule_id)
        stats = _pass()

        assert projected == 1
        assert stats["transfers_auto_linked"] == projected
        assert _merged_into(migrated_engine, control[0]) is not None
        assert _merged_into(migrated_engine, rejected[0]) is None
        assert _merged_into(migrated_engine, rejected[1]) is None
        from purser import rules
        from purser.models import Rule

        with purser.db.get_sessionmaker()() as session:
            rule = session.get(Rule, uuid.UUID(rule_id))
            assert rule is not None
            assert rules.project_links(session, rule) == 0

    def test_an_automatic_conversion_never_links_a_rejected_pair(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """The AI classifier's and a rule's ``[account]`` conversion pairs
        the way the auto-linker does, so it leaves a rejected counterpart
        alone — and since round 2 (item 6) it does not convert the row in
        place either: that would make it a transfer to the rejected side's
        account after all. A person's explicit conversion still links the
        pair, as after an unlink."""
        from purser import matching, transfers

        auto_out, auto_in = _pair(client, migrated_engine, "250.00", 3, 2)
        by_hand_out, by_hand_in = _pair(client, migrated_engine, "130.00", 3, 2)
        for out_id, in_id in ((auto_out, auto_in), (by_hand_out, by_hand_in)):
            assert _not_a_transfer(client, out_id, in_id).status_code == 200

        # The machine path, as the categorize pass calls it.
        with purser.db.get_sessionmaker()() as session:
            with pytest.raises(transfers.MachineAnswerRefused):
                transfers.convert_to_transfer(
                    session,
                    tx=matching._load_tx(session, uuid.UUID(auto_out)),
                    target_ref=CARD,
                    token=None,
                    auto_pairing=True,
                )
            session.rollback()
        # A person's '[account]' edit.
        linked = client.patch(f"/transactions/{by_hand_out}", json={"category": f"[{CARD}]"})

        for row_id in (auto_out, auto_in):
            assert (_tx(client, row_id)["status"], _tx(client, row_id)["superseded_by"]) == (
                "posted",
                None,
            )
        assert _head(auto_out) == auto_out
        assert linked.status_code == 200, linked.text
        assert _tx(client, by_hand_in)["superseded_by"] == linked.json()["id"]

    def test_a_rejected_pending_pair_stays_rejected_once_it_posts(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """Pending rows are candidates (the auto paths wait for posted), so a
        pending pair can be rejected. When the bank posts each leg as a new
        row, the rejection carries to the posted successors: their transfer
        keyword would otherwise link them on the next pass. The control pair
        shows it would."""

        def pending_pair(amount: str) -> tuple[str, str]:
            return _pair(
                client,
                migrated_engine,
                amount,
                3,
                2,
                out_text="ONLINE TRANSFER TO REWARDS",
                in_text="TRANSFER FROM EVERYDAY",
                out_status="pending",
                in_status="pending",
            )

        rejected = pending_pair("250.00")
        control = pending_pair("130.00")
        assert _pass()["transfers_auto_linked"] == 0  # pending: confirm-first
        # The register flags a pending pair (no card waits for it yet).
        assert _not_a_transfer(client, *rejected).status_code == 200

        posted_rejected = tuple(_post(client, migrated_engine, row) for row in rejected)
        posted_control = tuple(_post(client, migrated_engine, row) for row in control)
        stats = _pass()

        assert stats["transfers_auto_linked"] == 1
        assert _merged_into(migrated_engine, posted_control[0]) is not None
        assert _merged_into(migrated_engine, posted_rejected[0]) is None
        assert _candidates(client) == []
        assert _cards(client, "open") == []


class TestExplicitMergesOfCorrectedRows:
    """#386 review round 2, item 2: a corrected original stays posted while
    its replacement carries the bank line on. No explicit path may merge or
    absorb it — that would leave the replacement live beside the transfer and
    count the money twice. A confirm that names it acts on its replacement:
    the same bank line."""

    def test_confirming_a_corrected_original_links_its_replacement(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id, in_id = _pair(client, migrated_engine, "250.00", 3, 2)
        _categorize_rule(client, "remittance")
        _categorize_pass()
        in_head = _head(in_id)

        confirmed = client.post(
            "/matching/confirm-transfer", json={"outgoing_id": out_id, "incoming_id": in_id}
        )

        assert confirmed.status_code == 200, confirmed.text
        merged = confirmed.json()["merged_transaction_id"]
        assert _merged_into(migrated_engine, in_head) == merged
        assert _merged_into(migrated_engine, in_id) is None  # corrected, never merged
        _rebuild()
        assert _stored_balance(client, seeded[CHECKING]) == Decimal("-250.00")
        assert _stored_balance(client, seeded[CARD]) == Decimal("250.00")

    def test_an_agent_confirming_a_corrected_original_links_its_replacement(
        self,
        seeded: dict[str, str],
        client: TestClient,
        migrated_engine: Engine,
        live_server: LiveServer,
    ) -> None:
        out_id, in_id = _pair(client, migrated_engine, "250.00", 3, 2)
        _categorize_rule(client, "web debit")
        _categorize_pass()
        out_head = _head(out_id)
        operator = client.post("/tokens", json={"name": "operator", "scopes": ["read", "write"]})
        assert operator.status_code == 201, operator.text

        (confirmed,) = _mcp_call(
            live_server.url,
            operator.json()["token"],
            [("confirm_transfer", {"outgoing_id": out_id, "incoming_id": in_id})],
        )

        merged = confirmed["merged_transaction_id"]
        assert _merged_into(migrated_engine, out_head) == merged
        assert _merged_into(migrated_engine, out_id) is None
        _rebuild()
        assert _stored_balance(client, seeded[CHECKING]) == Decimal("-250.00")
        assert _stored_balance(client, seeded[CARD]) == Decimal("250.00")

    def test_an_account_edit_of_a_corrected_original_is_refused(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """The register's '[account]' sugar would merge the corrected
        original with its counterpart; it is refused like any other edit of
        it, before the conversion looks for a counterpart."""
        out_id, in_id = _pair(client, migrated_engine, "250.00", 3, 2)
        _categorize_rule(client, "web debit")
        _categorize_pass()

        refused = client.patch(f"/transactions/{out_id}", json={"category": f"[{CARD}]"})

        assert refused.status_code == 422, refused.text
        assert "already corrected" in refused.text
        assert _merged_into(migrated_engine, in_id) is None
        assert _tx(client, _head(out_id))["category_path"] == "expenses.utilities"

    def test_a_conversion_refuses_a_corrected_original_before_it_pairs(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """``convert_to_transfer`` itself, for the callers that reach it
        without the register's check (the machine path): the guard runs
        before the absorb and the merge branches, not only before the
        in-place conversion."""
        from purser import matching, transfers
        from purser.ledger import LedgerError

        out_id, in_id = _pair(client, migrated_engine, "250.00", 3, 2)
        _categorize_rule(client, "web debit")
        _categorize_pass()

        with purser.db.get_sessionmaker()() as session:
            with pytest.raises(LedgerError, match="already corrected"):
                transfers.convert_to_transfer(
                    session,
                    tx=matching._load_tx(session, uuid.UUID(out_id)),
                    target_ref=CARD,
                    token=None,
                    auto_pairing=True,
                )
            session.rollback()

        assert _merged_into(migrated_engine, in_id) is None


class TestOneForwardWalk:
    """#386 review round 3, item 8: confirm, reject and a card's decision
    follow stale ids along one walk — through corrections and through a
    pending's posted successor, never into a merge."""

    def test_confirm_and_reject_follow_a_pending_to_its_posted_row(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        confirm_out, confirm_in = _pair(
            client, migrated_engine, "250.00", 3, 2, out_status="pending"
        )
        reject_out, reject_in = _pair(client, migrated_engine, "130.00", 3, 2, out_status="pending")
        posted_confirm = _post(client, migrated_engine, confirm_out)
        posted_reject = _post(client, migrated_engine, reject_out)

        confirmed = client.post(
            "/matching/confirm-transfer",
            json={"outgoing_id": confirm_out, "incoming_id": confirm_in},
        )
        rejected = _not_a_transfer(client, reject_out, reject_in)

        assert confirmed.status_code == 200, confirmed.text
        merged = confirmed.json()["merged_transaction_id"]
        assert _merged_into(migrated_engine, posted_confirm) == merged
        assert rejected.status_code == 200, rejected.text
        assert _not_a_transfer(client, posted_reject, reject_in).json()["already_rejected"]

    def test_a_merged_rows_id_is_not_followed_into_its_transfer(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id, in_id = _pair(client, migrated_engine, "250.00", 3, 2)
        first = client.post(
            "/matching/confirm-transfer", json={"outgoing_id": out_id, "incoming_id": in_id}
        )
        assert first.status_code == 200, first.text

        again = client.post(
            "/matching/confirm-transfer", json={"outgoing_id": out_id, "incoming_id": in_id}
        )

        assert again.status_code == 422, again.text
        assert "not matchable" in again.text


class TestUnlinkedBankLines:
    """#386 review round 3, items 2 and 3: unlinking resurrects each
    original as a clone, and the clone carries the original's bank line on
    — a clone of a replacement has no hash and the original's origin, yet
    it pairs, gets its card and backs a transfer like any bank line. The
    original no longer counts as merged into the transfer, so it backs none
    of its legs."""

    def _corrected_and_linked_then_unlinked(
        self, client: TestClient, engine: Engine
    ) -> tuple[str, str]:
        """A keyword pair whose out-row the categorize pass corrected, linked
        and then unlinked: the out-row's clone and the in-row's clone."""
        out_id = _imported(
            client, engine, CHECKING, "-500.00", _ago(4), payee="ONLINE TRANSFER TO RESERVE"
        )
        _categorize_rule(client, "online transfer")
        assert _categorize_pass()["rule"] == 1
        _imported(client, engine, SAVINGS, "500.00", _ago(3), payee="TRANSFER FROM EVERYDAY")
        assert _pass()["transfers_auto_linked"] == 1
        merged = _merged_into(engine, _head(out_id))
        unlinked = client.post(f"/transactions/{merged}/unlink-transfer")
        assert unlinked.status_code == 200, unlinked.text
        clones = {
            _tx(client, row_id)["account_path"]: row_id
            for row_id in unlinked.json()["resurrected_ids"]
        }
        return clones[CHECKING], clones[SAVINGS]

    def test_a_clone_of_a_replacement_links_and_backs_its_transfer(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        clone_out, clone_in = self._corrected_and_linked_then_unlinked(client, migrated_engine)
        assert _tx(client, clone_out)["origin"] == "rule"
        card_side = _imported(
            client, migrated_engine, CARD, "500.00", _ago(3), payee="TRANSFER FROM EVERYDAY"
        )

        assert _pass()["transfers_auto_linked"] == 1  # never the unlinked pair again
        merged = _merged_into(migrated_engine, clone_out)
        assert merged is not None and _merged_into(migrated_engine, card_side) == merged
        assert _merged_into(migrated_engine, clone_in) is None

        second = _imported(
            client, migrated_engine, CHECKING, "-500.00", _ago(2), payee="TRANSFER TO REWARDS"
        )
        # The merge's checking side is backed by the clone: the next real
        # -500.00 is never absorbed into it (it may pair elsewhere).
        assert _pass()["transfers_absorbed"] == 0
        assert _tx(client, second)["superseded_by"] != merged

    def test_a_clone_of_a_replacement_gets_its_card(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id, in_id = _pair(client, migrated_engine, "250.00", 4, 3, to=SAVINGS)
        _categorize_rule(client, "web debit")
        assert _categorize_pass()["rule"] == 1
        confirmed = client.post(
            "/matching/confirm-transfer", json={"outgoing_id": out_id, "incoming_id": in_id}
        )
        assert confirmed.status_code == 200, confirmed.text
        unlinked = client.post(
            f"/transactions/{confirmed.json()['merged_transaction_id']}/unlink-transfer"
        )
        assert unlinked.status_code == 200, unlinked.text
        (clone_out,) = [
            row_id
            for row_id in unlinked.json()["resurrected_ids"]
            if _tx(client, row_id)["account_path"] == CHECKING
        ]
        card_side = _imported(client, migrated_engine, CARD, "250.00", _ago(3), payee=IN_TEXT)

        _pass()

        (card,) = _cards(client, "open")
        assert (card["draft"]["outgoing_id"], card["draft"]["incoming_id"]) == (
            clone_out,
            card_side,
        )

    def test_an_unlinked_replacement_backs_nothing(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """A replacement absorbed into a converted transfer, then unlinked:
        the transfer's side is open again, so the real bank row that
        arrives next is absorbed into it — while the unlinked clone is
        never taken back."""
        arrival = _imported(
            client, migrated_engine, SAVINGS, "500.00", _ago(5), payee="DEPOSIT 9921"
        )
        converted = client.patch(f"/transactions/{arrival}", json={"category": f"[{CHECKING}]"})
        assert converted.status_code == 200, converted.text
        transfer = converted.json()["id"]
        out_id = _imported(
            client, migrated_engine, CHECKING, "-500.00", _ago(4), payee="TRANSFER TO RESERVE"
        )
        _categorize_rule(client, "transfer to reserve")
        assert _categorize_pass()["rule"] == 1
        assert _pass()["transfers_absorbed"] == 1
        assert _merged_into(migrated_engine, _head(out_id)) == transfer
        unlinked = client.post(f"/transactions/{transfer}/unlink-transfer")
        assert unlinked.status_code == 200, unlinked.text
        (clone,) = unlinked.json()["resurrected_ids"]

        real = _imported(
            client, migrated_engine, CHECKING, "-500.00", _ago(3), payee="TRANSFER TO RESERVE"
        )
        assert _pass()["transfers_absorbed"] == 1

        assert _merged_into(migrated_engine, real) == transfer
        assert _merged_into(migrated_engine, clone) is None


class TestRejectionsFollowTheBankLine:
    """#386 review round 2, item 3: "not a transfer" and an unlink are
    remembered by bank line — the lineage from the row's first sighting,
    through the corrections that carried it on (the categorize pass corrects
    most posted rows) and a pending's posted successor — so a correction
    since never makes a rejected pair new."""

    def test_the_machine_path_never_links_a_rejected_pair_through_a_correction(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id, in_id = _pair(client, migrated_engine, "500.00", 3, 2, to=SAVINGS)
        _pass()
        assert _decide(client, _only_card(client)["id"], decision="reject").status_code == 200
        _categorize_rule(client, "remittance")
        _categorize_pass()  # corrects the savings row: a reversal + a replacement
        in_head = _head(in_id)
        assert in_head != in_id

        _categorize_rule(client, "web debit", f"[{SAVINGS}]")
        _categorize_pass()  # the machine path answers [savings] for the checking row

        assert _merged_into(migrated_engine, in_head) is None
        assert _tx(client, in_head)["category_path"] == "expenses.utilities"
        assert _merged_into(migrated_engine, out_id) is None

    def test_one_pass_never_links_a_rejected_pair_through_its_own_correction(
        self,
        seeded: dict[str, str],
        client: TestClient,
        migrated_engine: Engine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The same, in ONE pass (#497 review): an unrelated '[account]'
        answer first makes the pass read the memory, then the pass corrects
        the savings row, then answers [savings] for the checking row. The
        memory read before the correction did not know the replacement row;
        the correction makes the pass read it again."""
        from purser import categorize

        out_id, in_id = _pair(client, migrated_engine, "500.00", 3, 2, to=SAVINGS)
        _pass()
        assert _decide(client, _only_card(client)["id"], decision="reject").status_code == 200
        other = _imported(
            client, migrated_engine, CHECKING, "-12.34", _ago(5), payee="CARD AUTOPAY XYZ"
        )
        _categorize_rule(client, "autopay xyz", f"[{CARD}]")
        _categorize_rule(client, "remittance")
        _categorize_rule(client, "web debit", f"[{SAVINGS}]")
        uncategorized = categorize._uncategorized_rows

        def in_order(session: Session) -> Any:
            # the unrelated row, then the savings row, then the checking row
            rows = uncategorized(session)
            return sorted(rows, key=lambda t: (str(t.id) != other, str(t.id) != in_id))

        monkeypatch.setattr(categorize, "_uncategorized_rows", in_order)
        _categorize_pass()

        in_head = _head(in_id)
        assert in_head != in_id  # the savings row was corrected in this pass
        assert _merged_into(migrated_engine, in_head) is None
        assert _merged_into(migrated_engine, out_id) is None

    def test_the_auto_linker_never_links_a_rejected_pair_through_a_correction(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id, in_id = _pair(client, migrated_engine, "250.00", 3, 2)
        assert _not_a_transfer(client, out_id, in_id).status_code == 200
        _categorize_rule(client, "web debit")
        _categorize_rule(client, "remittance")
        assert _categorize_pass()["rule"] == 2

        assert _activate(client, _rule(client, seeded)) == 0
        assert _pass()["transfers_auto_linked"] == 0

        assert _merged_into(migrated_engine, _head(out_id)) is None
        assert _merged_into(migrated_engine, _head(in_id)) is None
        assert _candidates(client) == []
        assert _cards(client, "open") == []

    def test_a_correction_keeps_the_pairs_card_and_its_one_event(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """The card is the pair's, not its rows': after the categorize pass
        corrects a row the same card stays open, keeps its snooze, and names
        the replacement; no second transfer.candidate fires."""
        out_id, in_id = _pair(client, migrated_engine, "250.00", 3, 2)
        _pass()
        card = _only_card(client)
        snoozed = _decide(client, card["id"], decision="snooze")
        assert snoozed.status_code == 200, snoozed.text
        _categorize_rule(client, "remittance")
        _categorize_pass()

        stats = _pass()

        assert (stats["transfer_pair_cards_created"], stats["transfer_pair_cards_withdrawn"]) == (
            0,
            0,
        )
        (kept,) = _cards(client)
        assert (kept["id"], kept["status"]) == (card["id"], "open")
        assert kept["snooze_until"] == snoozed.json()["suggestion"]["snooze_until"]
        assert (kept["draft"]["outgoing_id"], kept["draft"]["incoming_id"]) == (
            out_id,
            _head(in_id),
        )
        assert _event_keys(migrated_engine, "transfer.candidate") == [_key(out_id, in_id)]
        assert [c["suggestion_id"] for c in _candidates(client)] == [card["id"]]

    def test_not_a_transfer_still_works_after_a_correction(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """The pass corrects a row while the card waits: the card's reject
        acts on the replacement, and the register's "not a transfer" accepts
        the old ids and the new alike — the same pair."""
        out_id, in_id = _pair(client, migrated_engine, "300.00", 3, 2)
        _pass()
        card = _only_card(client)
        _categorize_rule(client, "remittance")
        _categorize_pass()

        rejected = _decide(client, card["id"], decision="reject")

        assert rejected.status_code == 200, rejected.text
        for ids in ((out_id, in_id), (out_id, _head(in_id))):
            again = _not_a_transfer(client, *ids)
            assert again.status_code == 200, again.text
            assert (again.json()["suggestion_id"], again.json()["already_rejected"]) == (
                card["id"],
                True,
            )

    def test_accepting_the_card_after_a_correction_links_the_replacement(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """The categorize pass corrects a row between two passes: the card
        still names the original, and accepting it links the row that
        carries the bank line now — not the original, which would leave the
        replacement live beside the transfer."""
        _, in_id = _pair(client, migrated_engine, "250.00", 3, 2)
        _pass()
        card = _only_card(client)
        _categorize_rule(client, "remittance")
        _categorize_pass()
        in_head = _head(in_id)

        accepted = _decide(client, card["id"], decision="accept")

        assert accepted.status_code == 200, accepted.text
        merged = accepted.json()["transfer"]["merged_transaction_id"]
        assert _merged_into(migrated_engine, in_head) == merged
        assert _merged_into(migrated_engine, in_id) is None
        _rebuild()
        assert _stored_balance(client, seeded[CARD]) == Decimal("250.00")

    def test_an_unlinked_pair_stays_unlinked_through_a_correction(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id, _ = _pair(
            client, migrated_engine, "250.00", 3, 2, out_text="ONLINE TRANSFER TO REWARDS"
        )
        assert _pass()["transfers_auto_linked"] == 1
        merged = _merged_into(migrated_engine, out_id)
        unlinked = client.post(f"/transactions/{merged}/unlink-transfer")
        assert unlinked.status_code == 200, unlinked.text
        clone_out, clone_in = unlinked.json()["resurrected_ids"]
        _categorize_rule(client, "online transfer")
        assert _categorize_pass()["rule"] == 1  # corrects the outgoing clone

        stats = _pass()

        assert stats["transfers_auto_linked"] == 0
        assert _merged_into(migrated_engine, _head(clone_out)) is None
        assert _merged_into(migrated_engine, clone_in) is None


class TestRejectionsSurviveUnlinksAndGapImports:
    """#407: "not a transfer" is remembered by bank line, and a bank line
    changes row in two more ways than a correction. An unlink resurrects
    each original as a clone with a new id; a gap-window import
    (``window_start=None`` — the outage repair, ``POST
    /connections/{id}/refetch``) brings a pending's posted row in without
    the reconcile step, so nothing has superseded the pending yet when the
    import's pass runs. Neither makes a rejected pair new: no card, no
    candidate, no automatic link — while a control pair the same signal
    links shows the signal was live."""

    @pytest.mark.parametrize("detour", ["outgoing", "incoming"])
    def test_a_rejected_pair_stays_rejected_after_an_unlink_of_either_leg(
        self,
        seeded: dict[str, str],
        client: TestClient,
        migrated_engine: Engine,
        detour: str,
    ) -> None:
        """A rejected leg is linked to a third row by hand and unlinked
        again: its clone is the same bank line, so a transfer_link rule over
        the rejected pair's accounts still links nothing."""
        out_id, in_id = _pair(client, migrated_engine, "250.00", 3, 2)
        assert _not_a_transfer(client, out_id, in_id).status_code == 200
        if detour == "outgoing":
            third = _imported(client, migrated_engine, SAVINGS, "250.00", _ago(2), payee=IN_TEXT)
            body = {"outgoing_id": out_id, "incoming_id": third}
        else:
            third = _imported(client, migrated_engine, SAVINGS, "-250.00", _ago(3), payee=OUT_TEXT)
            body = {"outgoing_id": third, "incoming_id": in_id}
        linked = client.post("/matching/confirm-transfer", json=body)
        assert linked.status_code == 200, linked.text
        merged = linked.json()["merged_transaction_id"]
        unlinked = client.post(f"/transactions/{merged}/unlink-transfer")
        assert unlinked.status_code == 200, unlinked.text
        clones = {
            _tx(client, row_id)["account_path"]: row_id
            for row_id in unlinked.json()["resurrected_ids"]
        }
        clone_out = clones[CHECKING] if detour == "outgoing" else out_id
        clone_in = clones[CARD] if detour == "incoming" else in_id
        assert {clone_out, clone_in} != {out_id, in_id}  # a new id on one side

        projected = _activate(client, _rule(client, seeded))
        stats = _pass()

        assert projected == 0
        assert (stats["transfers_auto_linked"], stats["transfer_pair_cards_created"]) == (0, 0)
        assert _merged_into(migrated_engine, clone_out) is None
        assert _merged_into(migrated_engine, clone_in) is None
        assert all(
            {c["outgoing_id"], c["incoming_id"]} != {clone_out, clone_in}
            for c in _candidates(client)
        )
        assert _cards(client, "open") == []

    @pytest.mark.parametrize("posts", ["outgoing", "incoming", "both"])
    def test_a_rejected_pair_stays_rejected_after_a_gap_window_import(
        self,
        seeded: dict[str, str],
        client: TestClient,
        migrated_engine: Engine,
        posts: str,
    ) -> None:
        """End to end through the importer. A pair is rejected while a leg
        is pending; the bank posts that leg a day later under a transfer
        keyword, and the posted row arrives in a gap-window import, so the
        pending is still open when the import's pass runs. The rejection
        holds there, and again once a later sync reconciles the pending."""
        connection_id = _connect_feed(CHECKING, CARD)
        ids: dict[str, str] = {}
        out_pending, in_pending = posts in ("outgoing", "both"), posts in ("incoming", "both")
        rejected = {
            CHECKING: [_sf_row("r-out", 3, "-250.00", OUT_KEYWORD, pending=out_pending)],
            CARD: [_sf_row("r-in", 2, "250.00", IN_KEYWORD, pending=in_pending)],
        }
        control = {
            CHECKING: [_sf_row("c-out", 3, "-130.00", OUT_KEYWORD, pending=out_pending)],
            CARD: [_sf_row("c-in", 2, "130.00", IN_KEYWORD, pending=in_pending)],
        }
        first = _feed_import(
            connection_id, _merged_rows(rejected, control), ids, window_start=_ago(10)
        )
        assert first["transfers_auto_linked"] == 0  # a pending leg: confirm-first
        assert _not_a_transfer(client, ids["r-out"], ids["r-in"]).status_code == 200

        def posted(feed: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
            # The posting moves the pending legs one day on: a new identity.
            return {
                path: [
                    {
                        **row,
                        "id": f"{row['id']}-posted",
                        "pending": False,
                        "posted": _ts(_age(row) - 1),
                    }
                    if row.get("pending")
                    else row
                    for row in sf_rows
                ]
                for path, sf_rows in feed.items()
            }

        gap = _feed_import(
            connection_id,
            _merged_rows(posted(rejected), posted(control)),
            ids,
            window_start=None,
            tail_before=_ago(30),
        )

        heads = _feed_heads(ids)
        assert gap["voided_pendings"] == 0  # no reconcile: the pendings are still open
        assert gap["transfers_auto_linked"] == 1  # the control pair: the signal is live
        assert _merged_into(migrated_engine, heads["c-out"]) is not None
        assert _merged_into(migrated_engine, heads["r-out"]) is None
        assert _merged_into(migrated_engine, heads["r-in"]) is None
        assert _rejected_rows_in_candidates(client, ids) == []
        assert _cards(client, "open") == []

        later = _feed_import(
            connection_id,
            _merged_rows(posted(rejected), posted(control)),
            ids,
            window_start=_ago(10),
        )

        assert later["transfers_auto_linked"] == 0
        for key in ("r-out", "r-in"):
            if f"{key}-posted" in ids:  # the reconcile step now supersedes the pending
                pending = _tx(client, ids[key])
                assert (pending["status"], pending["superseded_by"]) == (
                    "void",
                    ids[f"{key}-posted"],
                )
        assert _merged_into(migrated_engine, heads["r-out"]) is None
        assert _merged_into(migrated_engine, heads["r-in"]) is None
        assert _rejected_rows_in_candidates(client, ids) == []
        assert _cards(client, "open") == []

    def test_a_posted_row_that_carries_another_bank_line_is_not_carried(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """Only a posted row the reconcile step could still give the open
        pending counts as its bank line. One another pending already posted
        into (its ``superseded_by``) is a different bank line, so the
        rejection does not reach it and it pairs freely."""
        out_pending, in_id = _pair(client, migrated_engine, "250.00", 3, 2, out_status="pending")
        assert _not_a_transfer(client, out_pending, in_id).status_code == 200
        other_pending = _imported(
            client, migrated_engine, CHECKING, "-250.00", _ago(4), payee=OUT_TEXT, status="pending"
        )
        posted = _post(client, migrated_engine, other_pending)  # claims the posted row

        assert [(c["outgoing_id"], c["incoming_id"]) for c in _candidates(client)] == [
            (posted, in_id)
        ]


OUT_KEYWORD = "ONLINE TRANSFER TO REWARDS"
IN_KEYWORD = "TRANSFER FROM EVERYDAY"


def _connect_feed(*paths: str) -> uuid.UUID:
    """Map the seeded ``paths`` onto a fresh SimpleFIN connection (external
    id ``SF-<path>``), the way the account mapping does."""
    from purser.models import Account, Connection

    with purser.db.get_sessionmaker()() as session:
        connection = Connection(provider="simplefin")
        session.add(connection)
        session.flush()
        for path in paths:
            account = session.execute(select(Account).where(Account.path == path)).scalar_one()
            account.connection_id = connection.id
            account.external_id = f"SF-{path}"
        session.commit()
        return connection.id


def _ts(days_ago: int) -> int:
    return int(datetime.combine(_ago(days_ago), time(12), tzinfo=UTC).timestamp())


def _age(row: dict[str, Any]) -> int:
    return (today() - datetime.fromtimestamp(int(row["posted"]), tz=UTC).date()).days


def _sf_row(
    row_id: str, days_ago: int, amount: str, description: str, *, pending: bool = False
) -> dict[str, Any]:
    return {
        "id": row_id,
        "posted": _ts(days_ago),
        "amount": amount,
        "description": description,
        "pending": pending,
    }


def _merged_rows(
    *feeds: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for feed in feeds:
        for path, rows in feed.items():
            out.setdefault(path, []).extend(rows)
    return out


def _feed_import(
    connection_id: uuid.UUID,
    rows: dict[str, list[dict[str, Any]]],
    ids: dict[str, str],
    *,
    window_start: date | None,
    tail_before: date | None = None,
) -> dict[str, Any]:
    """Archive a SimpleFIN payload for the connection and run the importer
    on it, committed — ``window_start=None`` is a gap-window import. Records
    in ``ids`` which ledger row each payload id became."""
    import json

    from purser import crypto
    from purser.feed import importer
    from purser.models import RawFetch, Transaction

    payload = {
        "errors": [],
        "accounts": [
            {
                "id": f"SF-{path}",
                "name": path,
                "currency": "USD",
                "balance": "0.00",
                "balance-date": int(now().timestamp()),
                "transactions": sf_rows,
                "holdings": [],
            }
            for path, sf_rows in rows.items()
        ],
    }
    with purser.db.get_sessionmaker()() as session:
        before = set(session.execute(select(Transaction.id)).scalars())
        raw = RawFetch(
            connection_id=connection_id,
            content_hash=f"t407-{uuid.uuid4().hex}",
            payload_enc=crypto.encrypt(json.dumps(payload, sort_keys=True)),
        )
        session.add(raw)
        session.flush()
        stats = importer.import_fetch(
            session, raw, window_start=window_start, tail_before=tail_before
        )
        created = session.execute(
            select(Transaction).where(
                Transaction.source_fetch_id == raw.id, Transaction.id.not_in(before)
            )
        ).scalars()
        from purser.matching import _real_leg

        by_content = {
            (str(tx.description), tx.date, Decimal(leg.amount)): tx.id
            for tx in created
            if tx.origin == "import" and (leg := _real_leg(tx)) is not None
        }
        session.commit()
    for sf_rows in rows.values():
        for row in sf_rows:
            found = by_content.get(
                (
                    row["description"],
                    datetime.fromtimestamp(row["posted"], tz=UTC).date(),
                    Decimal(row["amount"]),
                )
            )
            if found is not None:
                ids[row["id"]] = str(found)
    return stats


def _rejected_rows_in_candidates(client: TestClient, ids: dict[str, str]) -> list[str]:
    """The rows of the rejected bank lines (``r-…``) any candidate names. The
    control pair's pendings, still open in a gap window, may pair with each
    other there; that is the gap's, not the rejection's."""
    rejected = {row_id for key, row_id in ids.items() if key.startswith("r-")}
    return [
        row_id
        for c in _candidates(client)
        for row_id in (c["outgoing_id"], c["incoming_id"])
        if row_id in rejected
    ]


def _feed_heads(ids: dict[str, str]) -> dict[str, str]:
    """Each bank line by its first payload id -> the row that carries it
    now: the posted successor (``<id>-posted``) when the bank posted it."""
    return {
        key: ids.get(f"{key}-posted", row_id)
        for key, row_id in ids.items()
        if not key.endswith("-posted")
    }


class TestNotATransferBindsTheMachinePath:
    """#386 review round 2, item 6: "not a transfer" means automation never
    makes either row a transfer to the other's account. An automatic
    '[account]' answer whose only counterpart there is the rejected pair's
    other side is no answer: converted in place, the row would carry that
    account a second copy of the money its rejected counterpart already
    holds there."""

    def test_a_rule_answering_the_other_sides_account_is_no_answer(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id, in_id = _pair(client, migrated_engine, "500.00", 3, 2, to=SAVINGS)
        assert _not_a_transfer(client, out_id, in_id).status_code == 200
        _categorize_rule(client, "web debit", f"[{SAVINGS}]")
        _categorize_rule(client, "remittance", f"[{CHECKING}]")

        stats = _categorize_pass()

        assert (stats["rule"], stats["unresolved"]) == (0, 2)
        for row_id in (out_id, in_id):
            assert _head(row_id) == row_id  # neither converted, merged nor corrected
        _rebuild()
        assert _stored_balance(client, seeded[CHECKING]) == Decimal("-500.00")
        assert _stored_balance(client, seeded[SAVINGS]) == Decimal("500.00")

    def test_apply_history_skips_the_row_and_says_so(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id, in_id = _pair(client, migrated_engine, "500.00", 3, 2, to=SAVINGS)
        assert _not_a_transfer(client, out_id, in_id).status_code == 200
        _categorize_rule(client, "web debit", f"[{SAVINGS}]")
        (rule,) = [r for r in client.get("/rules").json() if r["type"] == "categorize"]

        dry = client.post(
            f"/rules/{rule['id']}/apply-history", json={"dry_run": True, "posted": "correct"}
        )
        applied = client.post(
            f"/rules/{rule['id']}/apply-history", json={"dry_run": False, "posted": "correct"}
        )

        assert dry.status_code == 200, dry.text
        assert applied.status_code == 200, applied.text
        for result in (dry.json(), applied.json()):
            assert (result["skipped_not_a_transfer"], result["posted"]) == (1, 0)
        assert _head(out_id) == out_id
        assert _merged_into(migrated_engine, in_id) is None


class TestApplyHistoryAfterAnEarlierRow:
    """#386 review round 3, item 1: every row passes the "not a transfer"
    check before any is applied, so an earlier row of the same run can take
    the counterpart a later row passed it with. The later row's answer is
    refused as it is applied: it is skipped and counted, never an error that
    aborts the whole run."""

    def test_the_later_row_is_skipped_and_the_run_completes(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        # Bank rows carry their identity (the importer's content hash): a
        # merged one backs its side, so nothing is absorbed into the merge.
        def bank(account: str, amount: str, days: int, text_: str) -> str:
            return _imported(
                client,
                migrated_engine,
                account,
                amount,
                _ago(days),
                payee=text_,
                content_hash=f"hash-{uuid.uuid4()}",
            )

        older = bank(CHECKING, "-500.00", 5, OUT_TEXT)
        newer = bank(CHECKING, "-500.00", 3, OUT_TEXT)
        shared = bank(SAVINGS, "500.00", 4, IN_TEXT)
        rejected = bank(SAVINGS, "500.00", 6, IN_TEXT)
        assert _not_a_transfer(client, older, rejected).status_code == 200
        _categorize_rule(client, "web debit", f"[{SAVINGS}]")
        (rule,) = [r for r in client.get("/rules").json() if r["type"] == "categorize"]

        dry = client.post(
            f"/rules/{rule['id']}/apply-history", json={"dry_run": True, "posted": "correct"}
        )
        applied = client.post(
            f"/rules/{rule['id']}/apply-history", json={"dry_run": False, "posted": "correct"}
        )

        assert dry.status_code == 200, dry.text
        assert (dry.json()["posted"], dry.json()["skipped_not_a_transfer"]) == (2, 0)
        assert applied.status_code == 200, applied.text
        assert (applied.json()["posted"], applied.json()["skipped_not_a_transfer"]) == (1, 1)
        # Newest first: the newer row merged with the shared arrival.
        assert _merged_into(migrated_engine, newer) == _merged_into(migrated_engine, shared)
        assert _head(older) == older
        assert _merged_into(migrated_engine, rejected) is None


class TestTheMachinePathsCost:
    """#386 review round 3, item 7: the categorize pass checked and applied
    each '[account]' answer by re-reading the "not a transfer" memory and
    every open transfer leg in the ledger, several times per row. It reads
    the memory once per pass, and a conversion looks only at the transfers
    on its row's account."""

    def test_one_read_of_the_memory_per_pass(
        self,
        seeded: dict[str, str],
        client: TestClient,
        migrated_engine: Engine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from purser import transfers

        for n, amount in enumerate(("101.00", "202.00", "303.00")):
            _imported(client, migrated_engine, CHECKING, f"-{amount}", _ago(3 + n), payee=OUT_TEXT)
        _categorize_rule(client, "web debit", f"[{SAVINGS}]")
        reads = 0
        leg_scans: list[object] = []
        real_pairs, real_legs = transfers._suppressed_pairs, transfers._open_transfer_legs

        def counted_pairs(session: Session) -> object:
            nonlocal reads
            reads += 1
            return real_pairs(session)

        def scoped_legs(session: Session, **kwargs: Any) -> object:
            leg_scans.append(kwargs.get("account_id"))
            return real_legs(session, **kwargs)

        monkeypatch.setattr(transfers, "_suppressed_pairs", counted_pairs)
        monkeypatch.setattr(transfers, "_open_transfer_legs", scoped_legs)

        assert _categorize_pass()["rule"] == 3

        assert reads == 1
        assert leg_scans and all(account is not None for account in leg_scans)


class TestRejectedAbsorbs:
    """#386 review, item 4 (round 1): a rejection holds for absorbs too.
    Once one side of a rejected pair is a transfer of its own, the other side
    is never absorbed into that transfer's open leg — that would link the
    rejected pair after all."""

    def test_the_other_side_is_never_absorbed(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """The person converts the out-row to [savings] while the rejected
        arrival is matched to a bill (so it is no counterpart then); the
        bill is unmatched, and the pass's absorb phase would claim the
        arrival — its keyword is a name signal — for the converted transfer's
        open savings leg."""
        out_id = _imported(client, migrated_engine, CHECKING, "-500.00", _ago(3), payee=OUT_TEXT)
        in_id = _imported(
            client, migrated_engine, SAVINGS, "500.00", _ago(2), payee="TRANSFER FROM EVERYDAY"
        )
        assert _not_a_transfer(client, out_id, in_id).status_code == 200
        refund = client.post(
            "/transactions",
            json={
                "account": SAVINGS,
                "category": "expenses.utilities",
                "amount": "500.00",
                "date": _ago(2).isoformat(),
                "payee": "City Utilities",
                "status": "expected",
            },
        )
        assert refund.status_code == 201, refund.text
        matched = client.post(
            "/matching/match", json={"expected_id": refund.json()["id"], "posted_id": in_id}
        )
        assert matched.status_code == 200, matched.text
        converted = client.patch(f"/transactions/{out_id}", json={"category": f"[{SAVINGS}]"})
        assert converted.status_code == 200, converted.text
        unmatched = client.post(f"/matching/{matched.json()['id']}/unmatch")
        assert unmatched.status_code == 200, unmatched.text

        stats = _pass()

        assert stats["transfers_absorbed"] == 0
        assert (_tx(client, in_id)["status"], _tx(client, in_id)["superseded_by"]) == (
            "posted",
            None,
        )

    def test_a_machine_conversion_of_either_side_is_refused(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """Round 2, item 6: the machine path never makes the transfer the
        absorb would need — ``convert_to_transfer`` refuses the answer
        itself, for a caller that did not ask ``machine_answer_refused``."""
        from purser import matching, transfers

        out_id, in_id = _pair(client, migrated_engine, "500.00", 3, 2, to=SAVINGS)
        assert _not_a_transfer(client, out_id, in_id).status_code == 200

        for row_id, target in ((out_id, SAVINGS), (in_id, CHECKING)):
            with purser.db.get_sessionmaker()() as session:
                with pytest.raises(transfers.MachineAnswerRefused):
                    transfers.convert_to_transfer(
                        session,
                        tx=matching._load_tx(session, uuid.UUID(row_id)),
                        target_ref=target,
                        token=None,
                        auto_pairing=True,
                    )
                session.rollback()

        assert _pass()["transfers_absorbed"] == 0
        assert _head(out_id) == out_id
        assert _head(in_id) == in_id


class TestAConvertedRowBacksItsOwnSide:
    """#386 review round 2, item 4 (pre-existing data loss). A bank row
    converted to '[account]' is that account's bank line, so the transfer's
    leg there was never open; read as open, it absorbed — voided — the next
    real same-amount row on that account, which then dropped out of the
    ledger. In each case a second real -500.00 posts after the conversion
    and stays a row of its own."""

    SECOND = "TRANSFER TO RESERVE"  # a name signal: the pass would absorb it

    def _second(self, client: TestClient, engine: Engine) -> str:
        return _imported(client, engine, CHECKING, "-500.00", _ago(2), payee=self.SECOND)

    def test_after_an_account_edit(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id = _imported(client, migrated_engine, CHECKING, "-500.00", _ago(3), payee=OUT_TEXT)
        converted = client.patch(f"/transactions/{out_id}", json={"category": f"[{SAVINGS}]"})
        assert converted.status_code == 200, converted.text
        second = self._second(client, migrated_engine)

        assert _pass()["transfers_absorbed"] == 0
        assert (_tx(client, second)["status"], _tx(client, second)["superseded_by"]) == (
            "posted",
            None,
        )
        # Marked a transfer to savings too, it becomes one of its own.
        again = client.patch(f"/transactions/{second}", json={"category": f"[{SAVINGS}]"})
        assert again.status_code == 200, again.text
        assert again.json()["id"] != converted.json()["id"]
        _rebuild()
        assert _stored_balance(client, seeded[CHECKING]) == Decimal("-1000.00")
        assert _stored_balance(client, seeded[SAVINGS]) == Decimal("1000.00")

    def test_after_a_machine_conversion(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id = _imported(client, migrated_engine, CHECKING, "-500.00", _ago(3), payee=OUT_TEXT)
        _categorize_rule(client, "web debit", f"[{SAVINGS}]")
        assert _categorize_pass()["rule"] == 1
        converted = _head(out_id)
        assert _tx(client, converted)["is_transfer"] is True
        second = self._second(client, migrated_engine)

        assert _pass()["transfers_absorbed"] == 0
        assert _tx(client, second)["superseded_by"] is None
        _categorize_rule(client, "transfer to reserve", f"[{SAVINGS}]")
        assert _categorize_pass()["rule"] == 1
        assert _head(second) not in (second, converted)  # a transfer of its own
        _rebuild()
        assert _stored_balance(client, seeded[CHECKING]) == Decimal("-1000.00")
        assert _stored_balance(client, seeded[SAVINGS]) == Decimal("1000.00")

    def test_after_a_pending_conversion_past_the_audit_retention(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """Round 3, item 4: the side a pending row converted in place stands
        for is recorded durably (``transfer.converted``), not only in the
        audit row the retention prunes: long after, a second real row on
        that side still stays its own, and the other side is still open for
        the counterpart's bank row."""
        from datetime import UTC, datetime, timedelta

        from purser import audit

        pending = _imported(
            client, migrated_engine, CHECKING, "-500.00", _ago(3), payee=OUT_TEXT, status="pending"
        )
        converted = client.patch(f"/transactions/{pending}", json={"category": f"[{SAVINGS}]"})
        assert converted.status_code == 200, converted.text
        assert audit.prune_expired(now=datetime.now(UTC) + timedelta(days=3650)) > 0
        second = self._second(client, migrated_engine)
        counterpart = _imported(
            client, migrated_engine, SAVINGS, "500.00", _ago(2), payee="TRANSFER FROM EVERYDAY"
        )

        assert _pass()["transfers_absorbed"] == 1

        assert _tx(client, second)["superseded_by"] is None
        assert _merged_into(migrated_engine, counterpart) == pending
        _rebuild()
        assert _stored_balance(client, seeded[CHECKING]) == Decimal("-1000.00")
        assert _stored_balance(client, seeded[SAVINGS]) == Decimal("500.00")

    def test_after_a_pending_conversion_in_place(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        pending = _imported(
            client, migrated_engine, CHECKING, "-500.00", _ago(3), payee=OUT_TEXT, status="pending"
        )
        converted = client.patch(f"/transactions/{pending}", json={"category": f"[{SAVINGS}]"})
        assert converted.status_code == 200, converted.text
        assert converted.json()["id"] == pending  # in place
        second = self._second(client, migrated_engine)

        assert _pass()["transfers_absorbed"] == 0
        assert _tx(client, second)["superseded_by"] is None
        assert _tx(client, pending)["status"] == "pending"
        _rebuild()
        assert _stored_balance(client, seeded[CHECKING]) == Decimal("-1000.00")
        assert _stored_balance(client, seeded[SAVINGS]) == Decimal("500.00")


class TestPostedRowsOnly:
    """#386 review, item 2: a card waits until both rows are posted — the
    auto paths' own rule (§5). A bank-pending row comes back posted as a new
    row, so a card on a pending pair always churned: withdrawn and replaced,
    or, with a keyword, withdrawn when the posted pair auto-linked."""

    def test_a_pending_pair_gets_its_card_once_both_rows_post(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        pending = _pair(
            client, migrated_engine, "250.00", 3, 2, out_status="pending", in_status="pending"
        )

        assert _pass()["transfer_pair_cards_created"] == 0

        assert _cards(client) == []
        assert _event_keys(migrated_engine, "transfer.candidate") == []
        # The register still flags it, for a confirm or a "not a transfer".
        assert [(c["outgoing_id"], c["incoming_id"]) for c in _candidates(client)] == [pending]
        posted = tuple(_post(client, migrated_engine, row) for row in pending)
        assert _pass()["transfer_pair_cards_created"] == 1
        card = _only_card(client)
        assert (card["draft"]["outgoing_id"], card["draft"]["incoming_id"]) == posted
        assert _event_keys(migrated_engine, "transfer.candidate") == [_key(*posted)]

    def test_a_pending_row_never_takes_a_posted_pairs_counterpart(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """#386 review round 2, item 5: posted rows pair first, as the plan
        pairs them; pending rows only from what is left. An earlier-dated
        pending row arriving would otherwise take the posted pair's incoming
        row in the greedy pairing and withdraw its (snoozed) card."""
        out_id, in_id = _pair(client, migrated_engine, "250.00", 3, 2)
        _pass()
        card = _only_card(client)
        snoozed = _decide(client, card["id"], decision="snooze")
        assert snoozed.status_code == 200, snoozed.text

        _imported(
            client, migrated_engine, CHECKING, "-250.00", _ago(4), payee=OUT_TEXT, status="pending"
        )
        stats = _pass()

        assert stats["transfer_pair_cards_withdrawn"] == 0
        (kept,) = _cards(client)
        assert (kept["id"], kept["status"], kept["snooze_until"]) == (
            card["id"],
            "open",
            snoozed.json()["suggestion"]["snooze_until"],
        )
        assert [(c["outgoing_id"], c["incoming_id"]) for c in _candidates(client)] == [
            (out_id, in_id)
        ]

    def test_a_pending_pair_with_a_keyword_never_gets_a_card(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        pending = _pair(
            client,
            migrated_engine,
            "130.00",
            3,
            2,
            out_text="ONLINE TRANSFER TO REWARDS",
            in_text="TRANSFER FROM EVERYDAY",
            out_status="pending",
            in_status="pending",
        )
        _pass()
        assert _cards(client) == []

        posted = tuple(_post(client, migrated_engine, row) for row in pending)
        stats = _pass()

        assert stats["transfers_auto_linked"] == 1
        assert _merged_into(migrated_engine, posted[0]) is not None
        assert _cards(client) == []
        assert _event_keys(migrated_engine, "transfer.candidate") == []


class TestSuppressionInsideThePairing:
    """#386 review, item 3: a suppressed pair is never formed, so its rows
    stay free for their real counterparts. Applied after the greedy pairing,
    a rejected pair consumed both rows and the true transfer never paired."""

    def test_a_rejected_pair_leaves_its_rows_to_their_real_counterparts(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id = _imported(client, migrated_engine, CHECKING, "-500.00", _ago(3), payee=OUT_TEXT)
        savings_in = _imported(client, migrated_engine, SAVINGS, "500.00", _ago(2), payee=IN_TEXT)
        brokerage_in = _imported(
            client, migrated_engine, BROKERAGE, "500.00", _ago(1), payee="TRANSFER FROM EVERYDAY"
        )
        # The earlier arrival pairs first: checking -> savings is the candidate.
        assert [(c["outgoing_id"], c["incoming_id"]) for c in _candidates(client)] == [
            (out_id, savings_in)
        ]
        assert _not_a_transfer(client, out_id, savings_in).status_code == 200

        stats = _pass()

        assert stats["transfers_auto_linked"] == 1  # the keyword pair, now free to form
        merged = _merged_into(migrated_engine, out_id)
        assert merged is not None
        assert _merged_into(migrated_engine, brokerage_in) == merged
        assert _merged_into(migrated_engine, savings_in) is None

    def test_the_projected_count_is_still_the_linked_count(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """Decision 66 with a rejection in the pool: a rule declaring
        checking <-> savings projects the out-row's link to its next
        counterpart, and the pass links exactly that."""
        out_id, rejected_in = _pair(client, migrated_engine, "500.00", 3, 2, to=SAVINGS)
        other_in = _imported(client, migrated_engine, SAVINGS, "500.00", _ago(1), payee=IN_TEXT)
        assert _not_a_transfer(client, out_id, rejected_in).status_code == 200
        rule = client.post(
            "/rules",
            json={
                "type": "transfer_link",
                "pair_account_a": seeded[CHECKING],
                "pair_account_b": seeded[SAVINGS],
            },
        )
        assert rule.status_code == 201, rule.text

        projected = _activate(client, rule.json()["id"])
        stats = _pass()

        assert projected == 1
        assert stats["transfers_auto_linked"] == projected
        assert _merged_into(migrated_engine, out_id) == _merged_into(migrated_engine, other_in)
        assert _merged_into(migrated_engine, rejected_in) is None
        # The candidate list pairs the same way: nothing is left to flag.
        assert _candidates(client) == []


def _waiting_on_a_lock(engine: Engine) -> bool:
    """Another backend is blocked on a lock — the second session's insert
    waiting for the first one's uncommitted row."""
    with engine.connect() as conn:
        return bool(
            conn.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE state = 'active'"
                    " AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
                )
            ).scalar_one()
        )


def _race(engine: Engine, call: Callable[[Session], Any]) -> tuple[Any, Any]:
    """The same public call in two real sessions at once: the first makes
    its row and holds it uncommitted while the second, in a thread, reaches
    the same fingerprint and waits on it; the first commits, then the second
    finishes and commits. Returns both results."""
    maker = purser.db.get_sessionmaker()
    result: dict[str, Any] = {}
    with maker() as first:
        result["first"] = call(first)

        def second() -> None:
            with maker() as session:
                try:
                    result["second"] = call(session)
                    session.commit()
                except BaseException as exc:  # re-raised in the main thread
                    result["error"] = exc

        thread = threading.Thread(target=second, daemon=True)
        thread.start()
        deadline = monotonic() + 30
        while not _waiting_on_a_lock(engine):
            assert thread.is_alive(), f"the second session never waited: {result}"
            assert monotonic() < deadline, "the second session never waited"
            sleep(0.05)
        first.commit()
    thread.join(timeout=30)
    assert not thread.is_alive()
    if "error" in result:
        raise result["error"]
    return result["first"], result["second"]


def _while_held(engine: Engine, hold: Callable[[Session], Any], other: Callable[[], Any]) -> Any:
    """``hold`` writes in a real session and keeps it uncommitted while
    ``other`` runs in a thread; once ``other`` waits on a row lock the held
    session commits, and ``other``'s result (or exception) comes back."""
    result: dict[str, Any] = {}

    def run() -> None:
        try:
            result["value"] = other()
        except BaseException as exc:  # re-raised in the main thread
            result["error"] = exc

    with purser.db.get_sessionmaker()() as session:
        hold(session)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        deadline = monotonic() + 30
        while not _waiting_on_a_lock(engine):
            assert thread.is_alive(), f"the second writer never waited: {result}"
            assert monotonic() < deadline, "the second writer never waited"
            sleep(0.05)
        session.commit()
    thread.join(timeout=30)
    assert not thread.is_alive()
    if "error" in result:
        raise result["error"]
    return result.get("value")


class TestARowLockSerializesItsWriters:
    """#386 review round 3, item 5: a merge and a correction of one bank row
    check "still live, never corrected" under the row lock, so the second
    waits for the first and sees it — instead of both landing: the corrected
    original voided into a transfer beside its live replacement."""

    def _utilities(self, session: Session) -> uuid.UUID:
        from purser.models import Account

        return session.execute(
            select(Account.id).where(Account.path == "expenses.utilities")
        ).scalar_one()

    def test_a_confirm_waits_for_a_correction_and_refuses_the_original(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        from purser import ledger

        out_id, in_id = _pair(client, migrated_engine, "250.00", 3, 2)

        def correct(session: Session) -> None:
            ledger.recategorize_posted(
                session,
                tx=ledger.get_transaction(session, uuid.UUID(in_id)),
                category_id=self._utilities(session),
                token=None,
                provenance={"source": "test"},
            )

        refused = _while_held(
            migrated_engine,
            correct,
            lambda: client.post(
                "/matching/confirm-transfer", json={"outgoing_id": out_id, "incoming_id": in_id}
            ),
        )

        assert refused.status_code == 422, refused.text
        assert "already corrected" in refused.text
        assert _merged_into(migrated_engine, in_id) is None
        _rebuild()
        assert _stored_balance(client, seeded[CARD]) == Decimal("250.00")

    def test_the_categorize_pass_waits_for_a_merge_and_skips_the_row(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        from purser import categorize, matching

        out_id, in_id = _pair(client, migrated_engine, "250.00", 3, 2)
        _categorize_rule(client, "remittance")

        def merge(session: Session) -> None:
            matching._merge_transfer_pair(
                session,
                outgoing=matching._load_tx(session, uuid.UUID(out_id)),
                incoming=matching._load_tx(session, uuid.UUID(in_id)),
                token=None,
            )

        def categorize_pass() -> dict[str, int]:
            with purser.db.get_sessionmaker()() as session:
                stats = categorize.run(session, token=None)
                session.commit()
                return stats

        stats = _while_held(migrated_engine, merge, categorize_pass)

        # Both rows were loaded uncategorized; the out-row has no answer, and
        # the in-row's rule answer finds it merged under the lock.
        assert (stats["rule"], stats["unresolved"]) == (0, 2)
        merged = _merged_into(migrated_engine, in_id)
        assert merged is not None
        assert _head(in_id) == in_id  # never corrected after the merge
        _rebuild()
        assert _stored_balance(client, seeded[CARD]) == Decimal("250.00")


class TestConcurrentCreation:
    """#386 review, item 9 (round 1), and round 2, items 12 and 13: two
    passes — an import and the daily job — can find the same new pair at
    once, and two people can reject one pair at once. Each looks, finds no
    row, and inserts. The insert is ``INSERT … ON CONFLICT (fingerprint) DO
    NOTHING``: the second waits for the first, then reads its row back —
    never a unique violation that would roll back the import or the job."""

    def test_two_passes_finding_one_new_pair_make_one_card(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        from purser import matching, suggestions

        out_id, in_id = _pair(client, migrated_engine, "250.00", 3, 2)

        def upsert(session: Session) -> tuple[uuid.UUID, bool]:
            row, created = suggestions.upsert_transfer_pair_suggestion(
                session,
                outgoing=matching._load_tx(session, uuid.UUID(out_id)),
                incoming=matching._load_tx(session, uuid.UUID(in_id)),
                amount=Decimal("250.00"),
                gap_days=1,
            )
            assert row is not None
            return row.id, created

        (first_id, first_created), (second_id, second_created) = _race(migrated_engine, upsert)

        assert (first_created, second_created) == (True, False)
        assert first_id == second_id
        assert len(_cards(client)) == 1
        assert len(_event_keys(migrated_engine, "suggestion.created")) == 1
        assert len(_audit(client, "suggestion.create")) == 1

    def test_two_rejections_of_one_pair_make_one_row(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        from purser import suggestions
        from purser.models import ApiToken

        out_id, in_id = _pair(client, migrated_engine, "250.00", 3, 2)
        operator = client.post("/tokens", json={"name": "operator", "scopes": ["read", "write"]})
        assert operator.status_code == 201, operator.text
        token_id = uuid.UUID(operator.json()["id"])

        def reject(session: Session) -> tuple[uuid.UUID, bool]:
            token = session.get(ApiToken, token_id)
            assert token is not None
            row, changed = suggestions.reject_transfer_pair(
                session,
                outgoing_id=uuid.UUID(out_id),
                incoming_id=uuid.UUID(in_id),
                token=token,
            )
            return row.id, changed

        (first_id, first_changed), (second_id, second_changed) = _race(migrated_engine, reject)

        assert (first_changed, second_changed) == (True, False)
        assert first_id == second_id
        (row,) = _cards(client)
        assert (row["id"], row["status"]) == (str(first_id), "rejected")

    def test_two_confirms_racing_to_propose_one_rule_keep_both_pairs(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """Round 3, item 9: two confirmed pairs of one account pair and payee
        both propose the same transfer_link suggestion at once; the one that
        loses the insert adds its pair to the winner's evidence instead of
        dropping it."""
        from purser import matching, suggestions
        from purser.models import ApiToken

        operator = client.post("/tokens", json={"name": "operator", "scopes": ["read", "write"]})
        assert operator.status_code == 201, operator.text
        token_id = uuid.UUID(operator.json()["id"])
        merges: list[tuple[str, str, str]] = []

        def row(account: str, amount: str, days: int) -> str:
            # One payee for every row (resolved by name): one rule to propose.
            created = client.post(
                "/transactions",
                json={
                    "account": account,
                    "category": "expenses.utilities",
                    "amount": amount,
                    "date": _ago(days).isoformat(),
                    "payee": "Example Credit Union",
                    "status": "posted",
                },
            )
            assert created.status_code == 201, created.text
            return str(created.json()["id"])

        for amount, out_ago in (("250.00", 6), ("130.00", 3)):
            out_id = row(CHECKING, f"-{amount}", out_ago)
            in_id = row(CARD, amount, out_ago - 1)
            with purser.db.get_sessionmaker()() as session:
                merged = matching._merge_transfer_pair(
                    session,
                    outgoing=matching._load_tx(session, uuid.UUID(out_id)),
                    incoming=matching._load_tx(session, uuid.UUID(in_id)),
                    token=None,
                )
                session.commit()
                merges.append((out_id, in_id, str(merged.id)))
        turn = iter(merges)

        def propose(session: Session) -> uuid.UUID:
            out_id, in_id, merged_id = next(turn)
            token = session.get(ApiToken, token_id)
            assert token is not None
            row = suggestions.propose_transfer_link(
                session,
                outgoing=matching._load_tx(session, uuid.UUID(out_id)),
                incoming=matching._load_tx(session, uuid.UUID(in_id)),
                merged=matching._load_tx(session, uuid.UUID(merged_id)),
                token=token,
            )
            assert row is not None
            return row.id

        first, second = _race(migrated_engine, propose)

        assert first == second
        evidence = client.get(f"/suggestions/{first}").json()["evidence"]
        assert sorted(o["transaction_id"] for o in evidence["occurrences"]) == sorted(
            merged_id for _, _, merged_id in merges
        )

    def test_creating_cards_takes_no_savepoint(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """Round 2, item 13: a SAVEPOINT's release runs the session's commit
        hook — a balance recompute — at every new row, and more than 64 of
        them overflow the backend's subtransaction cache. Creating cards is
        one plain insert each."""
        from sqlalchemy import event

        for amount, out_ago in (("111.00", 3), ("222.00", 4), ("333.00", 5)):
            _pair(client, migrated_engine, amount, out_ago, out_ago - 1)
        statements: list[str] = []

        def seen(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
            statements.append(statement)

        engine = purser.db.get_engine()
        event.listen(engine, "before_cursor_execute", seen)
        try:
            stats = _pass()
        finally:
            event.remove(engine, "before_cursor_execute", seen)

        assert stats["transfer_pair_cards_created"] == 3
        assert not [st for st in statements if "SAVEPOINT" in st.upper()]
        inserts = [st for st in statements if st.startswith("INSERT INTO suggestions")]
        assert len(inserts) == 3
        assert all("ON CONFLICT (fingerprint) DO NOTHING" in st for st in inserts)


class TestInboxTotals:
    """#386 review round 2, item 9: the inbox lists a page; its totals per
    kind come beside it, and ``offset`` reaches every page, so no kind is
    ever capped out of sight."""

    def test_each_kind_has_its_total_and_every_page_is_reachable(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        for n in range(5):
            _pair(client, migrated_engine, f"{100 + n}.00", 3, 2)
        _pass()
        (snoozed, *_) = _cards(client)
        assert _decide(client, snoozed["id"], decision="snooze").status_code == 200

        counts = client.get("/suggestions/counts")
        pages = [
            client.get(
                "/suggestions", params={"kind": "transfer_pair", "limit": 2, "offset": offset}
            )
            for offset in (0, 2, 4)
        ]

        assert counts.status_code == 200, counts.text
        assert counts.json() == {
            "total": 4,
            "schedule": 0,
            "transfer_link": 0,
            "match": 0,
            "transfer_pair": 4,  # the snoozed card waits for its date
            "split": 0,
            "trade_rule": 0,
        }
        assert [page.status_code for page in pages] == [200, 200, 200]
        listed = [row["id"] for page in pages for row in page.json()]
        assert [len(page.json()) for page in pages] == [2, 2, 0]
        assert len(set(listed)) == 4 and snoozed["id"] not in listed
        refused = client.get("/suggestions", params={"offset": -1})
        assert refused.status_code == 422


class TestCurrencies:
    """#386 review, item 8: two rows pair only in one currency — a transfer
    between a dollar and a Canadian-dollar account is never a candidate,
    and the auto-linker never tries to merge one (create_transfer refuses
    mixed currencies, which failed the whole pass)."""

    def test_rows_in_two_currencies_never_pair(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        resp = client.post(
            "/accounts",
            json={"name": "Loonie", "type": "savings", "path": "assets.cad", "currency": "CAD"},
        )
        assert resp.status_code == 201, resp.text
        _imported(client, migrated_engine, CHECKING, "-500.00", _ago(3), payee=OUT_TEXT)
        _imported(
            client,
            migrated_engine,
            "assets.cad",
            "500.00",
            _ago(2),
            payee=IN_TEXT,
            currency="CAD",
        )
        assert _candidates(client) == []

        # A keyword pair across the currencies: the plan must not take it.
        _imported(client, migrated_engine, CHECKING, "-75.00", _ago(3), payee="TRANSFER TO LOONIE")
        _imported(
            client,
            migrated_engine,
            "assets.cad",
            "75.00",
            _ago(2),
            payee="TRANSFER FROM EVERYDAY",
            currency="CAD",
        )
        stats = _pass()

        assert stats["transfers_auto_linked"] == 0
        assert _candidates(client) == []
        assert _cards(client) == []


def _rebuild() -> None:
    """Recompute the stored running balances from the ledger: rows inserted
    straight into the tables (``_imported``) mark nothing dirty."""
    from purser import balances

    with purser.db.get_sessionmaker()() as session:
        balances.rebuild_running(session)
        session.commit()


def _stored_balance(client: TestClient, account_id: str) -> Decimal:
    """The account's stored running balance (chain heads only, decision 37):
    where a merge of a corrected original would count the row twice."""
    resp = client.get(f"/accounts/{account_id}/balance")
    assert resp.status_code == 200, resp.text
    return Decimal(resp.json()["balance"])


def _categorize_rule(
    client: TestClient, match_text: str, category: str = "expenses.utilities"
) -> None:
    """An active categorize rule — payee contains ``match_text`` ->
    ``category`` (a category, or ``[account]`` for a real account)."""
    target = client.get("/accounts", params={"include_hidden": True}).json()
    target += client.get("/categories").json()
    category_id = next(a["id"] for a in target if a["path"] == category.strip("[]"))
    resp = client.post(
        "/rules",
        json={
            "type": "categorize",
            "match_field": "payee",
            "match_mode": "contains",
            "match_text": match_text,
            "category_id": category_id,
        },
    )
    assert resp.status_code == 201, resp.text
    resp = client.post(f"/rules/{resp.json()['id']}/state", json={"state": "active"})
    assert resp.status_code == 200, resp.text


def _categorize_pass() -> dict[str, int]:
    """The post-sync categorize pass (sync_all_active runs it after the
    syncs): categorize.run as the system actor. A posted row it answers is
    corrected — a reversal plus an origin=rule replacement that carries the
    bank line on."""
    from purser import categorize

    with purser.db.get_sessionmaker()() as session:
        stats = categorize.run(session, token=None)
        session.commit()
    return stats


def _head(tx_id: str) -> str:
    """The row a bank line lives on now (``lineage.current_rows``: through
    corrections and later sightings, never into a merge)."""
    from purser import lineage

    with purser.db.get_sessionmaker()() as session:
        return str(lineage.current_rows(session, [uuid.UUID(tx_id)])[uuid.UUID(tx_id)])


class TestCorrectedBankLines:
    """#386 review round 2, item 1: a corrected bank line lives on in its
    chain head. The categorize pass corrects most uncategorized posted rows
    (a reversal + an origin=rule replacement), so the transfer pool holds the
    live HEAD of each import lineage — never the corrected original, which
    merging would leave beside its live replacement (counting twice), and
    never a lineage cut off from pairing for good."""

    def test_a_corrected_leg_still_auto_links(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id = _imported(
            client,
            migrated_engine,
            CHECKING,
            "-500.00",
            _ago(3),
            payee="ONLINE TRANSFER TO RESERVE",
        )
        _pass()
        _categorize_rule(client, "online transfer")
        assert _categorize_pass()["rule"] == 1
        out_head = _head(out_id)
        assert out_head != out_id
        in_id = _imported(
            client, migrated_engine, SAVINGS, "500.00", _ago(2), payee="TRANSFER FROM EVERYDAY"
        )

        stats = _pass()

        assert stats["transfers_auto_linked"] == 1
        merged = _merged_into(migrated_engine, out_head)
        assert merged is not None and _merged_into(migrated_engine, in_id) == merged
        assert _merged_into(migrated_engine, out_id) is None  # the corrected original stays put
        assert _stored_balance(client, seeded[CHECKING]) == Decimal("-500.00")
        assert _stored_balance(client, seeded[SAVINGS]) == Decimal("500.00")

    def test_a_corrected_pair_still_gets_its_card(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id, in_id = _pair(client, migrated_engine, "250.00", 3, 2)
        _pass()
        _categorize_rule(client, "web debit")
        _categorize_rule(client, "remittance")
        assert _categorize_pass()["rule"] == 2

        _pass()

        heads = (_head(out_id), _head(in_id))
        assert heads != (out_id, in_id)
        (card,) = _cards(client, "open")
        assert (card["draft"]["outgoing_id"], card["draft"]["incoming_id"]) == heads
        assert [(c["outgoing_id"], c["incoming_id"]) for c in _candidates(client)] == [heads]
        accepted = _decide(client, card["id"], decision="accept")
        assert accepted.status_code == 200, accepted.text
        merged = accepted.json()["transfer"]["merged_transaction_id"]
        assert _merged_into(migrated_engine, heads[0]) == merged
        assert _stored_balance(client, seeded[CHECKING]) == Decimal("-250.00")
        assert _stored_balance(client, seeded[CARD]) == Decimal("250.00")

    def test_a_rule_links_a_corrected_pair_once(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """A rule declaring the pair, activated after the corrections, links
        the heads — projected 1, linked 1 — and each balance counts once."""
        out_id, _ = _pair(client, migrated_engine, "250.00", 3, 2)
        _categorize_rule(client, "web debit")
        _categorize_rule(client, "remittance")
        _categorize_pass()
        out_head = _head(out_id)

        assert _activate(client, _rule(client, seeded)) == 1
        assert _pass()["transfers_auto_linked"] == 1

        assert _merged_into(migrated_engine, out_head) is not None
        assert _merged_into(migrated_engine, out_id) is None
        assert _stored_balance(client, seeded[CHECKING]) == Decimal("-250.00")
        assert _stored_balance(client, seeded[CARD]) == Decimal("250.00")

    def test_a_merged_replacement_backs_its_leg(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """A replacement holds no content hash (the hash stays on the
        corrected original), yet it IS the bank line: once merged, the
        transfer's leg on its account is backed, so a second real
        same-amount row there stays a row of its own instead of being
        absorbed (voided) into the transfer."""
        out_id, in_id = _pair(client, migrated_engine, "500.00", 4, 3, to=SAVINGS)
        _categorize_rule(client, "web debit")
        _categorize_pass()
        confirmed = client.post(
            "/matching/confirm-transfer",
            json={"outgoing_id": _head(out_id), "incoming_id": in_id},
        )
        assert confirmed.status_code == 200, confirmed.text
        merged = confirmed.json()["merged_transaction_id"]

        second = _imported(
            client, migrated_engine, CHECKING, "-500.00", _ago(2), payee="TRANSFER TO RESERVE"
        )
        stats = _pass()

        assert stats["transfers_absorbed"] == 0
        assert _tx(client, second)["status"] == "posted"
        assert _tx(client, second)["superseded_by"] is None
        assert _merged_into(migrated_engine, in_id) == merged
        _rebuild()
        assert _stored_balance(client, seeded[CHECKING]) == Decimal("-1000.00")
        assert _stored_balance(client, seeded[SAVINGS]) == Decimal("500.00")


class TestAnExplicitCounterpartReadsTheLineage:
    """#386 review round 3, item 6: an '[account]' edit's counterpart is
    matched or held through its correction chain, as the pool reads it. The
    categorize pass corrects a row a bill match settles; the replacement is
    that bill's bank row, never a transfer leg — merging it would count the
    money as the bill's payment and as a transfer."""

    def test_a_replacement_whose_original_settles_a_bill_is_no_counterpart(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id = _imported(client, migrated_engine, CHECKING, "-500.00", _ago(3), payee=OUT_TEXT)
        in_id = _imported(client, migrated_engine, SAVINGS, "500.00", _ago(2), payee=IN_TEXT)
        refund = client.post(
            "/transactions",
            json={
                "account": SAVINGS,
                "category": "expenses.utilities",
                "amount": "500.00",
                "date": _ago(2).isoformat(),
                "payee": "City Utilities",
                "status": "expected",
            },
        )
        assert refund.status_code == 201, refund.text
        matched = client.post(
            "/matching/match", json={"expected_id": refund.json()["id"], "posted_id": in_id}
        )
        assert matched.status_code == 200, matched.text
        _categorize_rule(client, "remittance")
        assert _categorize_pass()["rule"] == 1
        in_head = _head(in_id)
        assert in_head != in_id

        converted = client.patch(f"/transactions/{out_id}", json={"category": f"[{SAVINGS}]"})

        assert converted.status_code == 200, converted.text
        assert _merged_into(migrated_engine, in_head) is None
        assert converted.json()["is_transfer"] is True  # converted on its own


class TestThePersonsCategory:
    """#386 review round 2, item 8: the machine path mirrors the auto-link
    pool. A replacement a person wrote carries their category; an automatic
    ``[account]`` conversion merging it would discard that decision without
    a trace (balance-neutral, so nothing shows it)."""

    def test_a_machine_conversion_never_merges_a_persons_replacement(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id, in_id = _pair(client, migrated_engine, "500.00", 3, 2, to=SAVINGS)
        edited = client.patch(f"/transactions/{out_id}", json={"category": "expenses.utilities"})
        assert edited.status_code == 200, edited.text
        persons = edited.json()["id"]
        assert _tx(client, persons)["origin"] == "human"

        _categorize_rule(client, "remittance", f"[{CHECKING}]")
        assert _categorize_pass()["rule"] == 1

        kept = _tx(client, persons)
        assert (kept["status"], kept["superseded_by"]) == ("posted", None)
        assert kept["category_path"] == "expenses.utilities"
        # The savings row became a transfer of its own instead (in place:
        # a reversal + a transfer-shaped replacement).
        converted = _tx(client, _head(in_id))
        assert converted["is_transfer"] is True
        assert {p["account_path"] for p in converted["postings"]} == {CHECKING, SAVINGS}

    def test_a_persons_description_edit_leaves_the_bank_line_pairable(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """Only a category is a person's decision to keep: a description or
        payee edit of a posted row writes a replacement that is still
        uncategorized, and that bank line still auto-links."""
        out_id = _imported(
            client,
            migrated_engine,
            CHECKING,
            "-500.00",
            _ago(3),
            payee="ONLINE TRANSFER TO RESERVE",
        )
        edited = client.patch(f"/transactions/{out_id}", json={"description": "rent share"})
        assert edited.status_code == 200, edited.text
        persons = edited.json()["id"]
        assert _tx(client, persons)["origin"] == "human"
        in_id = _imported(
            client, migrated_engine, SAVINGS, "500.00", _ago(2), payee="TRANSFER FROM EVERYDAY"
        )

        assert _pass()["transfers_auto_linked"] == 1

        merged = _merged_into(migrated_engine, persons)
        assert merged is not None and _merged_into(migrated_engine, in_id) == merged


class TestNotATransfer:
    def test_an_old_pair_without_a_card_is_rejected_and_suppressed(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id, in_id = _pair(client, migrated_engine, "250.00", 201, 200)
        _pass()
        assert _cards(client) == []  # older than the card window

        resp = _not_a_transfer(client, out_id, in_id)

        assert resp.status_code == 200, resp.text
        assert (resp.json()["status"], resp.json()["already_rejected"]) == ("rejected", False)
        (row,) = _cards(client)
        assert (row["id"], row["status"], row["reject_scope"]) == (
            resp.json()["suggestion_id"],
            "rejected",
            "exact",
        )
        assert (row["draft"]["outgoing_id"], row["draft"]["incoming_id"]) == (out_id, in_id)
        assert _candidates(client) == []
        assert len(_event_keys(migrated_engine, "suggestion.created")) == 1
        assert len(_event_keys(migrated_engine, "suggestion.decided")) == 1
        assert _event_keys(migrated_engine, "transfer.candidate") == []
        assert len(_audit(client, "suggestion.create")) == 1
        assert len(_audit(client, "suggestion.decide")) == 1

        # Idempotent: the same answer, and nothing changed (only the call
        # itself is audited).
        before = _row_counts(migrated_engine)
        again = _not_a_transfer(client, out_id, in_id)
        assert again.status_code == 200, again.text
        assert again.json() == {
            "suggestion_id": row["id"],
            "status": "rejected",
            "already_rejected": True,
        }
        assert _without_calls(_row_counts(migrated_engine)) == _without_calls(before)

        # Suppressed everywhere, even from a rule that declares the pair.
        assert _activate(client, _rule(client, seeded)) == 0
        assert _pass()["transfers_auto_linked"] == 0
        assert _merged_into(migrated_engine, out_id) is None

    def test_it_rejects_the_open_card_when_there_is_one(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id, in_id = _pair(client, migrated_engine, "250.00", 3, 2)
        _pass()
        card = _only_card(client)

        # Either order of the two ids names the same pair.
        resp = _not_a_transfer(client, in_id, out_id)

        assert resp.status_code == 200, resp.text
        assert resp.json()["suggestion_id"] == card["id"]
        assert _only_card(client)["status"] == "rejected"
        assert len(_event_keys(migrated_engine, "suggestion.created")) == 1
        decided = client.get("/events", params={"type": "suggestion.decided"}).json()
        assert [e["payload"]["decision"] for e in decided] == ["reject"]

    def test_it_refuses_rows_that_do_not_pair_and_needs_write(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id, in_id = _pair(client, migrated_engine, "250.00", 3, 2)
        other = _imported(client, migrated_engine, CARD, "99.00", _ago(2), payee=IN_TEXT)
        agent = client.post("/tokens", json={"name": "agent", "scopes": ["read"]})
        assert agent.status_code == 201, agent.text

        uneven = _not_a_transfer(client, out_id, other)
        itself = _not_a_transfer(client, out_id, out_id)
        unknown = _not_a_transfer(client, out_id, str(uuid.uuid4()))
        read_only = _not_a_transfer(
            client, out_id, in_id, Authorization=f"Bearer {agent.json()['token']}"
        )

        for refused in (uneven, itself, unknown):
            assert refused.status_code == 422, refused.text
            assert "not a transfer candidate" in refused.json()["detail"]
        assert read_only.status_code == 403
        assert _cards(client) == []


class TestThePassRunsWhereItShould:
    def test_the_daily_job_makes_the_cards(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        from purser.matching import run_matching_job

        out_id, in_id = _pair(client, migrated_engine, "250.00", 3, 2)

        run_matching_job()

        card = _only_card(client)
        assert (card["draft"]["outgoing_id"], card["draft"]["incoming_id"]) == (out_id, in_id)
        assert _event_keys(migrated_engine, "transfer.candidate") == [_key(out_id, in_id)]

    def test_an_import_makes_the_cards(
        self, clean_db: None, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.test_transfers import Harness, two_account_payload

        def epoch(days_ago: int) -> int:
            return int(datetime.combine(_ago(days_ago), time(12), tzinfo=UTC).timestamp())

        harness = Harness(client, monkeypatch)
        harness.use_payload(
            two_account_payload(
                [{"id": "o-1", "posted": epoch(3), "amount": "-250.00", "description": OUT_TEXT}],
                [{"id": "i-1", "posted": epoch(2), "amount": "250.00", "description": IN_TEXT}],
            )
        )

        harness.connect_and_map_both()

        card = _only_card(client)
        assert (card["draft"]["from_account"], card["draft"]["to_account"]) == (
            "assets.checking",
            "assets.savings",
        )
        assert len(client.get("/events", params={"type": "transfer.candidate"}).json()) == 1


class TestOverMcp:
    def test_an_agent_lists_and_decides_transfer_pair_cards(
        self,
        seeded: dict[str, str],
        client: TestClient,
        migrated_engine: Engine,
        live_server: LiveServer,
    ) -> None:
        accept_out, accept_in = _pair(client, migrated_engine, "250.00", 3, 2)
        reject_out, reject_in = _pair(client, migrated_engine, "130.00", 3, 2, to=SAVINGS)
        _pass()
        operator = client.post("/tokens", json={"name": "operator", "scopes": ["read", "write"]})
        assert operator.status_code == 201, operator.text
        token = operator.json()["token"]

        (listed,) = _mcp_call(
            live_server.url, token, [("list_suggestions", {"kind": "transfer_pair"})]
        )
        cards = {item["draft"]["outgoing_id"]: item for item in listed["items"]}
        assert set(cards) == {accept_out, reject_out}
        assert listed["open_by_kind"]["transfer_pair"] == 2
        # Round 3, item 9: an agent pages past `limit` with `offset`.
        pages = _mcp_call(
            live_server.url,
            token,
            [
                ("list_suggestions", {"kind": "transfer_pair", "limit": 1, "offset": 0}),
                ("list_suggestions", {"kind": "transfer_pair", "limit": 1, "offset": 1}),
            ],
        )
        assert sorted(item["id"] for page in pages for item in page["items"]) == sorted(
            c["id"] for c in cards.values()
        )
        assert cards[accept_out]["headline"].startswith("Link 250.00 from Everyday (")
        accepted, rejected = _mcp_call(
            live_server.url,
            token,
            [
                (
                    "decide_suggestion",
                    {"suggestion_id": cards[accept_out]["id"], "decision": "accept"},
                ),
                (
                    "decide_suggestion",
                    {"suggestion_id": cards[reject_out]["id"], "decision": "reject"},
                ),
            ],
        )

        assert accepted["status"] == "accepted", accepted
        merged_id = accepted["transfer"]["merged_transaction_id"]
        assert _tx(client, accept_in)["superseded_by"] == merged_id
        assert accepted["transfer"]["suggestion_id"]  # the chained transfer_link
        assert rejected["status"] == "rejected", rejected
        assert "transfer" not in rejected
        assert _tx(client, reject_in)["status"] == "posted"
        assert _candidates(client) == []

    def test_minimal_disclosure_withholds_the_bank_texts_and_the_amount(
        self,
        seeded: dict[str, str],
        client: TestClient,
        migrated_engine: Engine,
        live_server: LiveServer,
    ) -> None:
        """#437 meets #386: a card's draft quotes both bank texts and the
        amount moved, and its headline the amount — all withheld by default
        under the privacy settings, and back with `include` (the bank texts
        are payee names here too, so `payee` comes along)."""
        _pair(
            client,
            migrated_engine,
            "263.47",
            3,
            2,
            out_text="CANARY OUTBOUND LINE",
            in_text="CANARY INBOUND LINE",
        )
        _pass()
        operator = client.post("/tokens", json={"name": "operator", "scopes": ["read", "write"]})
        token = operator.json()["token"]
        withheld, included = _mcp_call(
            live_server.url,
            token,
            [
                (
                    "list_suggestions",
                    {"kind": "transfer_pair", "withhold": ["payee", "description", "amount"]},
                ),
                ("list_suggestions", {"kind": "transfer_pair"}),
            ],
        )

        dumped = str(withheld)
        for secret in ("CANARY OUTBOUND LINE", "CANARY INBOUND LINE", "263.47"):
            assert secret not in dumped, (secret, withheld)
        assert {"description", "amount"} <= set(withheld["withheld"])
        (card,) = included["items"]
        assert card["draft"]["outgoing_description"] == "CANARY OUTBOUND LINE"
        assert card["draft"]["incoming_description"] == "CANARY INBOUND LINE"
        assert Decimal(card["draft"]["amount"]) == Decimal("263.47")


# --------------------------------------------------------------------------
# Property (guardrail 3): a confirm through a card keeps the ledger zero-sum
# --------------------------------------------------------------------------


class TestCardConfirmProperty:
    @settings(
        max_examples=15, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(
        cents=st.integers(min_value=1, max_value=99_999_999),
        out_ago=st.integers(min_value=0, max_value=59),
        gap=st.integers(min_value=0, max_value=3),
        to=st.sampled_from([CARD, SAVINGS]),
    )
    def test_accepting_a_card_merges_one_zero_sum_transfer(
        self,
        seeded: dict[str, str],
        client: TestClient,
        migrated_engine: Engine,
        cents: int,
        out_ago: int,
        gap: int,
        to: str,
    ) -> None:
        amount = Decimal(cents).scaleb(-2)
        in_ago = max(out_ago - gap, 0)
        out_id, in_id = _pair(client, migrated_engine, str(amount), out_ago, in_ago, to=to)
        _pass()
        (card,) = [c for c in _cards(client, "open") if c["draft"]["outgoing_id"] == out_id]

        resp = _decide(client, card["id"], decision="accept")

        assert resp.status_code == 200, resp.text
        merged_id = resp.json()["transfer"]["merged_transaction_id"]
        with migrated_engine.connect() as conn:
            legs = {
                path: Decimal(str(total))
                for path, total in conn.execute(
                    text(
                        "SELECT a.path::text, sum(p.amount) FROM postings p"
                        " JOIN accounts a ON a.id = p.account_id"
                        " WHERE p.transaction_id = :t GROUP BY a.path"
                    ),
                    {"t": merged_id},
                ).all()
            }
            unbalanced = conn.execute(
                text(
                    "SELECT count(*) FROM (SELECT p.transaction_id FROM postings p"
                    " JOIN transactions t ON t.id = p.transaction_id"
                    " WHERE t.status IN ('pending', 'posted')"
                    " GROUP BY p.transaction_id, p.currency HAVING sum(p.amount) <> 0) s"
                )
            ).scalar_one()
        assert legs == {CHECKING: -amount, to: amount}
        assert sum(legs.values(), Decimal(0)) == 0
        assert unbalanced == 0
        for original in (out_id, in_id):
            assert _tx(client, original)["superseded_by"] == merged_id


# --------------------------------------------------------------------------
# Migration 0042 up/down (scratch database, never the suite's)
# --------------------------------------------------------------------------


def test_migration_0042_round_trips(database_url: str) -> None:
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
        conn.execute(sa.text("DROP DATABASE IF EXISTS purser_transfer_pair_mig WITH (FORCE)"))
        conn.execute(sa.text("CREATE DATABASE purser_transfer_pair_mig"))
    admin.dispose()
    scratch_url = database_url.rsplit("/", 1)[0] + "/purser_transfer_pair_mig"
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
        command.upgrade(cfg, "0042")
        with engine.begin() as conn:
            conn.execute(insert, {"k": "transfer_pair", "s": "rejected", "f": "transfer_pair:a:b"})
            conn.execute(insert, {"k": "match", "s": "withdrawn", "f": "match:tx:a:b"})

        command.downgrade(cfg, "-1")  # back to 0041

        with engine.connect() as conn:
            left = set(conn.execute(sa.text("SELECT fingerprint FROM suggestions")).scalars())
        assert left == {"match:tx:a:b"}
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
            conn.execute(insert, {"k": "transfer_pair", "s": "open", "f": "transfer_pair:again"})
    finally:
        engine.dispose()


def test_a_sync_never_overwrites_a_rejection_made_after_it_read_the_card(
    seeded: dict[str, str], client: TestClient, migrated_engine: Engine
) -> None:
    """#497 review: the sync picks the cards it withdraws from a plain read.
    A person who rejects one between that read and the withdrawal keeps the
    rejection: the withdrawal re-reads the card under its lock and leaves a
    decided card alone."""
    from purser import suggestions
    from purser.models import Suggestion

    _pair(client, migrated_engine, "500.00", 3, 2, to=SAVINGS)
    _pass()
    card = _only_card(client)
    with purser.db.get_sessionmaker()() as sync_session:
        row = sync_session.get(Suggestion, uuid.UUID(card["id"]))
        assert row is not None and row.status == "open"  # the sync's read
        assert _decide(client, card["id"], decision="reject").status_code == 200
        assert not suggestions.withdraw_transfer_pair(
            sync_session, row, actor_token_id=None, reason="no_longer_candidate"
        )
        sync_session.commit()
    assert [c["id"] for c in _cards(client, "rejected")] == [card["id"]]


@pytest.mark.parametrize(
    "credit", ["CASH BACK CREDIT", "MERCHANT REFUND", "DIVIDEND", "INTEREST EARNED"]
)
def test_income_credit_does_not_steal_a_transfer_candidate(
    credit: str,
    seeded: dict[str, str],
    client: TestClient,
    migrated_engine: Engine,
) -> None:
    from purser import transfers

    _pair(client, migrated_engine, "19.00", 2, 1, in_text=credit)
    with purser.db.get_sessionmaker()() as session:
        assert transfers.candidate_pairs(session) == []
    # Explicit confirmation remains available if a bank mislabeled a transfer.
