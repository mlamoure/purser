"""Matching engine: soft-amount scoring, tiers, variance, transfers, aging."""

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import Engine, text

from purser import crypto
from purser.core import ledger as core
from purser.core import matching as core_matching

TODAY = date(2026, 7, 15)


class TestCoreScoring:
    def test_utility_bill_case_is_confident_despite_amount_gap(self) -> None:
        # Expected $180, posted $214 -> still a match (payee + date agree).
        expected = core_matching.MatchInput(
            id=uuid.uuid4(),
            date=date(2026, 7, 15),
            amount=Decimal("-180.00"),
            normalized_payee="city utilities",
        )
        posted = core_matching.MatchInput(
            id=uuid.uuid4(),
            date=date(2026, 7, 16),
            amount=Decimal("-214.00"),
            normalized_payee="city utilities",
        )
        scored = core_matching.score(expected, posted)
        assert scored.tier is core_matching.Tier.AUTO
        assert scored.variance == Decimal("-34.00")
        ratio = core_matching.variance_ratio(expected.amount, scored.variance)
        assert ratio is not None
        assert abs(round(float(ratio) * 100)) == 19  # the +19% variance case

    def test_outside_date_window_never_matches(self) -> None:
        expected = core_matching.MatchInput(
            id=uuid.uuid4(), date=date(2026, 7, 1), amount=Decimal("-50"), normalized_payee="x"
        )
        posted = core_matching.MatchInput(
            id=uuid.uuid4(), date=date(2026, 7, 20), amount=Decimal("-50"), normalized_payee="x"
        )
        assert core_matching.score(expected, posted).tier is core_matching.Tier.NONE

    def test_opposite_sign_amounts_score_zero(self) -> None:
        assert core_matching.amount_score(Decimal("-50"), Decimal("50")) == 0.0

    @given(
        amount=st.decimals(
            min_value=Decimal("-9999.99"),
            max_value=Decimal("-0.01"),
            places=2,
            allow_nan=False,
            allow_infinity=False,
        )
    )
    def test_identical_transactions_always_auto(self, amount: Decimal) -> None:
        a = core_matching.MatchInput(
            id=uuid.uuid4(), date=date(2026, 7, 1), amount=amount, normalized_payee="acme corp"
        )
        b = core_matching.MatchInput(
            id=uuid.uuid4(), date=date(2026, 7, 1), amount=amount, normalized_payee="acme corp"
        )
        scored = core_matching.score(a, b)
        assert scored.tier is core_matching.Tier.AUTO
        assert scored.variance == 0

    def test_mentions_counterparty_strict_and_loose(self) -> None:
        # Strict is the auto-link test: phrase or squashed-name token.
        assert core_matching.mentions_counterparty("CITICARD ONLINE PMT", "Citi Card")
        assert not core_matching.mentions_counterparty("CITI AUTOPAY", "Citi Card")
        assert not core_matching.mentions_counterparty("PURCHASE RETURN", "Chase")
        # Loose (the new-liability scan, #234) takes the identifying token
        # as a whole token, never a generic one and never a substring.
        assert core_matching.mentions_counterparty("CITI AUTOPAY", "Citi Card", loose=True)
        assert core_matching.mentions_counterparty(
            "CHASE CREDIT CRD AUTOPAY", "Chase Amazon", loose=True
        )
        assert not core_matching.mentions_counterparty("PURCHASE RETURN", "Chase", loose=True)
        assert not core_matching.mentions_counterparty(
            "GIFT CARD SHOP", "Card Services", loose=True
        )
        assert not core_matching.mentions_counterparty("MORTGAGE PAYMENT", "Citi Card", loose=True)

    def test_transfer_pair_detection(self) -> None:
        out = core_matching.MatchInput(
            id=uuid.uuid4(), date=date(2026, 7, 1), amount=Decimal("-500"), normalized_payee=""
        )
        inc = core_matching.MatchInput(
            id=uuid.uuid4(), date=date(2026, 7, 2), amount=Decimal("500"), normalized_payee=""
        )
        noise = core_matching.MatchInput(
            id=uuid.uuid4(), date=date(2026, 7, 2), amount=Decimal("123"), normalized_payee=""
        )
        pairs = core_matching.detect_transfer_pairs([out, inc, noise])
        assert len(pairs) == 1
        assert pairs[0].outgoing_id == out.id
        assert pairs[0].incoming_id == inc.id

    def test_same_account_row_does_not_consume_the_true_counterpart(self) -> None:
        """Issue #173: an earlier same-account +amount (reversal/return) must
        not greedily consume the out-row — the cross-account counterpart
        pairs instead of being silently lost."""
        account_a, account_b = uuid.uuid4(), uuid.uuid4()
        out = core_matching.MatchInput(
            id=uuid.uuid4(),
            date=date(2026, 7, 2),
            amount=Decimal("-750"),
            normalized_payee="",
            account_id=account_a,
        )
        same_account_refund = core_matching.MatchInput(
            id=uuid.uuid4(),
            date=date(2026, 7, 1),  # earlier: greedy order would pick it first
            amount=Decimal("750"),
            normalized_payee="",
            account_id=account_a,
        )
        true_counterpart = core_matching.MatchInput(
            id=uuid.uuid4(),
            date=date(2026, 7, 2),
            amount=Decimal("750"),
            normalized_payee="",
            account_id=account_b,
        )
        pairs = core_matching.detect_transfer_pairs([out, same_account_refund, true_counterpart])
        assert len(pairs) == 1
        assert pairs[0].outgoing_id == out.id
        assert pairs[0].incoming_id == true_counterpart.id

    def test_window_edge_gap_of_exactly_three_days_pairs(self) -> None:
        out = core_matching.MatchInput(
            id=uuid.uuid4(),
            date=date(2026, 7, 1),
            amount=Decimal("-100"),
            normalized_payee="",
            account_id=uuid.uuid4(),
        )
        inc = core_matching.MatchInput(
            id=uuid.uuid4(),
            date=date(2026, 7, 4),
            amount=Decimal("100"),
            normalized_payee="",
            account_id=uuid.uuid4(),
        )
        far = core_matching.MatchInput(
            id=uuid.uuid4(),
            date=date(2026, 7, 6),
            amount=Decimal("100"),
            normalized_payee="",
            account_id=uuid.uuid4(),
        )
        pairs = core_matching.detect_transfer_pairs([out, inc, far])
        assert len(pairs) == 1
        assert pairs[0].date_gap_days == 3

    def test_equal_candidates_pick_earliest_deterministically(self) -> None:
        out = core_matching.MatchInput(
            id=uuid.uuid4(),
            date=date(2026, 7, 2),
            amount=Decimal("-100"),
            normalized_payee="",
            account_id=uuid.uuid4(),
        )
        early = core_matching.MatchInput(
            id=uuid.uuid4(),
            date=date(2026, 7, 1),
            amount=Decimal("100"),
            normalized_payee="",
            account_id=uuid.uuid4(),
        )
        late = core_matching.MatchInput(
            id=uuid.uuid4(),
            date=date(2026, 7, 3),
            amount=Decimal("100"),
            normalized_payee="",
            account_id=uuid.uuid4(),
        )
        first = core_matching.detect_transfer_pairs([out, early, late])
        second = core_matching.detect_transfer_pairs([out, late, early])
        assert first == second
        assert first[0].incoming_id == early.id


@pytest.fixture
def seeded(admin_client: TestClient) -> dict[str, str]:
    ids: dict[str, str] = {}
    for name, type_, path in [
        ("Checking", "checking", "assets.checking"),
        ("Savings", "savings", "assets.savings"),
    ]:
        resp = admin_client.post("/accounts", json={"name": name, "type": type_, "path": path})
        ids[path] = resp.json()["id"]
    resp = admin_client.post(
        "/categories",
        json={"name": "Utilities", "type": "category_expense", "path": "expenses.utilities"},
    )
    ids["expenses.utilities"] = resp.json()["id"]
    return ids


def _expected(client: TestClient, amount: str, on: date, payee: str = "City Utilities") -> str:
    resp = client.post(
        "/transactions",
        json={
            "account": "assets.checking",
            "category": "expenses.utilities",
            "amount": amount,
            "date": on.isoformat(),
            "payee": payee,
            "status": "expected",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _imported(
    client: TestClient,
    migrated_engine: Engine,
    account_path: str,
    amount: str,
    on: date,
    payee: str = "CITY UTILITIES PMT",
    status: str = "posted",
) -> str:
    """Insert an import-origin row the way the importer would."""
    with migrated_engine.begin() as conn:
        account_id = conn.execute(
            text("SELECT id FROM accounts WHERE path = :p"), {"p": account_path}
        ).scalar_one()
        cat = conn.execute(
            text("SELECT id FROM accounts WHERE path = 'expenses.uncategorized'")
        ).scalar_one_or_none()
        if cat is None:
            cat = conn.execute(
                text(
                    "INSERT INTO accounts (name, type, is_virtual, path) "
                    "VALUES ('Uncategorized', 'category_expense', true, "
                    "'expenses.uncategorized') RETURNING id"
                )
            ).scalar_one()
        payee_id = conn.execute(
            text("INSERT INTO payees (name_enc, name_bidx) VALUES (:ne, :nb) RETURNING id"),
            {"ne": crypto.encrypt(payee), "nb": crypto.blind_index(core.normalize_payee(payee))},
        ).scalar_one()
        tx_id = conn.execute(
            text(
                "INSERT INTO transactions (date, payee_id, description_enc, status, origin) "
                "VALUES (:d, :p, :desc, :status, 'import') RETURNING id"
            ),
            {"d": on, "p": payee_id, "desc": crypto.encrypt(payee), "status": status},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO postings (transaction_id, account_id, amount) VALUES "
                "(:tx, :acct, :amt), (:tx, :cat, :neg)"
            ),
            {
                "tx": tx_id,
                "acct": account_id,
                "amt": amount,
                "cat": cat,
                "neg": str(-Decimal(amount)),
            },
        )
    return str(tx_id)


class TestMatchingFlow:
    def test_utility_bill_auto_match_with_variance(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        expected_id = _expected(client, "-180.00", TODAY)
        posted_id = _imported(
            client,
            migrated_engine,
            "assets.checking",
            "-214.00",
            TODAY + timedelta(days=1),
            payee="City Utilities",
        )
        stats = client.post("/matching/run").json()
        assert stats["auto_matched"] == 1

        detail = client.get(f"/transactions/{expected_id}").json()
        assert detail["status"] == "void"
        assert detail["superseded_by"] == posted_id

        with migrated_engine.connect() as conn:
            row = conn.execute(
                text("SELECT score, variance, state FROM matches WHERE expected_id = :e"),
                {"e": expected_id},
            ).one()
        assert row.state == "confirmed"
        assert Decimal(row.variance) == Decimal("-34.00")
        assert row.score >= 0.8

        # posted amount wins: the posted row is untouched truth
        posted = client.get(f"/transactions/{posted_id}").json()
        assert posted["status"] == "posted"
        assert Decimal(posted["amount"]) == Decimal("-214.00")

    def test_variance_threshold_event_fires(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        _expected(client, "-100.00", TODAY)
        _imported(
            client,
            migrated_engine,
            "assets.checking",
            "-140.00",
            TODAY,
            payee="City Utilities",
        )
        client.post("/matching/run")
        with migrated_engine.connect() as conn:
            count = conn.execute(
                text("SELECT COUNT(*) FROM events WHERE type = 'variance.threshold'")
            ).scalar_one()
        assert count == 1

    def test_mid_score_lands_in_review_queue(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        expected_id = _expected(client, "-180.00", TODAY, payee="City Utilities")
        _imported(
            client,
            migrated_engine,
            "assets.checking",
            "-95.00",  # far-off amount
            TODAY + timedelta(days=3),
            payee="City Utilities Pmt",  # close-but-not-identical payee
        )
        stats = client.post("/matching/run").json()
        assert stats["proposed"] == 1
        proposed = client.get("/matching/proposed").json()
        assert len(proposed) == 1

        # Proposal context rides along for review surfaces (registers, dashboard).
        assert proposed[0]["expected_payee"] == "City Utilities"
        assert Decimal(proposed[0]["expected_amount"]) == Decimal("-180.00")
        assert proposed[0]["expected_date"] == TODAY.isoformat()

        # Expected is NOT superseded until confirm.
        assert client.get(f"/transactions/{expected_id}").json()["status"] == "expected"
        match_id = proposed[0]["id"]
        client.post(f"/matching/{match_id}/confirm")
        assert client.get(f"/transactions/{expected_id}").json()["status"] == "void"

    def test_unmatch_recreates_expected(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        expected_id = _expected(client, "-180.00", TODAY)
        _imported(
            client, migrated_engine, "assets.checking", "-180.00", TODAY, payee="City Utilities"
        )
        client.post("/matching/run")
        with migrated_engine.connect() as conn:
            match_id = conn.execute(
                text("SELECT id FROM matches WHERE expected_id = :e"), {"e": expected_id}
            ).scalar_one()
        resp = client.post(f"/matching/{match_id}/unmatch")
        assert resp.status_code == 200
        clone_id = resp.json()["recreated_expected_id"]
        clone = client.get(f"/transactions/{clone_id}").json()
        assert clone["status"] == "expected"
        assert Decimal(clone["amount"]) == Decimal("-180.00")
        # Original stays void (void is terminal); lineage preserved.
        assert client.get(f"/transactions/{expected_id}").json()["status"] == "void"

    def test_confirm_stale_proposal_rejected(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        # A proposal decided against rows that have since moved on is stale.
        expected_id = _expected(client, "-180.00", TODAY, payee="City Utilities")
        _imported(
            client,
            migrated_engine,
            "assets.checking",
            "-95.00",
            TODAY + timedelta(days=3),
            payee="City Utilities Pmt",
        )
        client.post("/matching/run")
        (proposed,) = client.get("/matching/proposed").json()
        # The expected is voided out from under the open proposal.
        assert client.post(f"/transactions/{expected_id}/void").status_code == 200

        resp = client.post(f"/matching/{proposed['id']}/confirm")
        assert resp.status_code == 422
        assert "stale" in resp.json()["detail"]
        with migrated_engine.connect() as conn:
            state = conn.execute(
                text("SELECT state FROM matches WHERE id = :m"), {"m": proposed["id"]}
            ).scalar_one()
        assert state == "proposed"  # not silently confirmed

    def test_second_manual_match_on_same_posted_rejected(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        first_expected = _expected(client, "-180.00", TODAY)
        second_expected = _expected(client, "-180.00", TODAY + timedelta(days=1))
        posted_id = _imported(
            client, migrated_engine, "assets.checking", "-180.00", TODAY, payee="City Utilities"
        )
        ok = client.post(
            "/matching/match", json={"expected_id": first_expected, "posted_id": posted_id}
        )
        assert ok.status_code == 200, ok.text

        dup = client.post(
            "/matching/match", json={"expected_id": second_expected, "posted_id": posted_id}
        )
        assert dup.status_code == 422
        assert "already has a live match" in dup.json()["detail"]
        # Second expected is untouched; exactly one live match on the posted row.
        assert client.get(f"/transactions/{second_expected}").json()["status"] == "expected"
        with migrated_engine.connect() as conn:
            live = conn.execute(
                text(
                    "SELECT COUNT(*) FROM matches WHERE posted_id = :p "
                    "AND state IN ('proposed', 'confirmed')"
                ),
                {"p": posted_id},
            ).scalar_one()
        assert live == 1

    def test_manual_match_of_the_proposed_pair_confirms_the_proposal(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        """#352 (decision 76): the proposal itself holds the bank row, so
        "mark paid" on exactly that pair used to refuse — and agents have no
        other way to decide a proposal. It now confirms it: same match row,
        the proposal's score and variance, the expected settled."""
        expected_id = _expected(client, "-180.00", TODAY, payee="City Utilities")
        posted_id = _imported(
            client,
            migrated_engine,
            "assets.checking",
            "-95.00",
            TODAY + timedelta(days=3),
            payee="City Utilities Pmt",
        )
        client.post("/matching/run")
        (proposed,) = client.get("/matching/proposed").json()
        # The unmatched report names the proposal's bank row — what match takes.
        (late,) = client.get("/matching/unmatched", params={"direction": "expected"}).json()
        assert late["has_proposed_match"] is True
        assert late["proposed_posted_id"] == posted_id
        resp = client.post(
            "/matching/match", json={"expected_id": expected_id, "posted_id": posted_id}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["id"] == proposed["id"]
        assert resp.json()["state"] == "confirmed"
        assert Decimal(resp.json()["variance"]) == Decimal("85.00")
        detail = client.get(f"/transactions/{expected_id}").json()
        assert detail["status"] == "void" and detail["superseded_by"] == posted_id
        with migrated_engine.connect() as conn:
            states = conn.execute(
                text("SELECT state FROM matches WHERE posted_id = :p"), {"p": posted_id}
            ).scalars()
            assert list(states) == ["confirmed"]  # confirmed in place, nothing stacked
        (confirm,) = client.get("/audit", params={"action": "match.confirm"}).json()["items"]
        assert confirm["entity_id"] == proposed["id"]

    def test_manual_match_refuses_a_row_proposed_for_another_expected(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        first = _expected(client, "-180.00", TODAY, payee="City Utilities")
        posted_id = _imported(
            client,
            migrated_engine,
            "assets.checking",
            "-95.00",
            TODAY + timedelta(days=3),
            payee="City Utilities Pmt",
        )
        client.post("/matching/run")
        assert len(client.get("/matching/proposed").json()) == 1
        second = _expected(client, "-95.00", TODAY + timedelta(days=2), payee="Other Payee")
        resp = client.post("/matching/match", json={"expected_id": second, "posted_id": posted_id})
        assert resp.status_code == 422
        assert "already has a live match" in resp.json()["detail"]
        assert first in resp.json()["detail"]  # names the expected it is proposed for
        assert client.get(f"/transactions/{second}").json()["status"] == "expected"
        assert client.get(f"/transactions/{first}").json()["status"] == "expected"

    def test_a_read_only_token_cannot_confirm(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        _expected(client, "-180.00", TODAY, payee="City Utilities")
        _imported(
            client,
            migrated_engine,
            "assets.checking",
            "-95.00",
            TODAY + timedelta(days=3),
            payee="City Utilities Pmt",
        )
        client.post("/matching/run")
        (proposed,) = client.get("/matching/proposed").json()
        agent = client.post("/tokens", json={"name": "agent", "scopes": ["read"]})
        headers = {"Authorization": f"Bearer {agent.json()['token']}"}
        resp = client.post(f"/matching/{proposed['id']}/confirm", headers=headers)
        assert resp.status_code == 403


class TestUnmatchedReporting:
    def test_expected_but_missing_shows_aging(
        self, seeded: dict[str, str], client: TestClient
    ) -> None:
        past = date.today() - timedelta(days=4)
        _expected(client, "-60.00", past, payee="Trash Service")
        report = client.get("/matching/unmatched", params={"direction": "expected"}).json()
        (row,) = report
        assert row["aging_days"] == 4
        assert row["payee"] == "Trash Service"
        assert row["has_proposed_match"] is False
        assert row["proposed_posted_id"] is None

    def test_posted_but_unexpected_direction(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        _imported(
            client,
            migrated_engine,
            "assets.checking",
            "-42.00",
            date.today() - timedelta(days=2),
            payee="Mystery Shop",
        )
        report = client.get("/matching/unmatched", params={"direction": "posted"}).json()
        (row,) = report
        assert row["aging_days"] == 2
        assert Decimal(row["amount"]) == Decimal("-42.00")


class TestTransfers:
    def test_no_transfer_link_without_confirm(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id = _imported(
            client, migrated_engine, "assets.checking", "-500.00", TODAY, payee="Transfer out"
        )
        in_id = _imported(
            client,
            migrated_engine,
            "assets.savings",
            "500.00",
            TODAY + timedelta(days=1),
            payee="Transfer in",
        )
        client.post("/matching/run")
        candidates = client.get("/matching/transfer-candidates").json()
        assert len(candidates) == 1
        assert candidates[0]["outgoing_id"] == out_id
        assert candidates[0]["incoming_id"] == in_id

        # Nothing merged, nothing voided — candidates only.
        assert client.get(f"/transactions/{out_id}").json()["status"] == "posted"
        assert client.get(f"/transactions/{in_id}").json()["status"] == "posted"
        with migrated_engine.connect() as conn:
            event_count = conn.execute(
                text("SELECT COUNT(*) FROM events WHERE type = 'transfer.candidate'")
            ).scalar_one()
        assert event_count == 1

        # Candidate events are emitted once, not per listing.
        client.get("/matching/transfer-candidates")
        with migrated_engine.connect() as conn:
            event_count = conn.execute(
                text("SELECT COUNT(*) FROM events WHERE type = 'transfer.candidate'")
            ).scalar_one()
        assert event_count == 1

    def test_confirm_transfer_merges_and_voids_with_links(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id = _imported(
            client, migrated_engine, "assets.checking", "-500.00", TODAY, payee="Xfer"
        )
        in_id = _imported(client, migrated_engine, "assets.savings", "500.00", TODAY, payee="Xfer")
        resp = client.post(
            "/matching/confirm-transfer", json={"outgoing_id": out_id, "incoming_id": in_id}
        )
        assert resp.status_code == 200, resp.text
        merged_id = resp.json()["merged_transaction_id"]

        merged = client.get(f"/transactions/{merged_id}").json()
        assert merged["is_transfer"] is True
        assert len(merged["postings"]) == 2
        assert {p["account_path"] for p in merged["postings"]} == {
            "assets.checking",
            "assets.savings",
        }
        for original in (out_id, in_id):
            detail = client.get(f"/transactions/{original}").json()
            assert detail["status"] == "void"
            assert detail["superseded_by"] == merged_id

        # Ledger stays balanced end to end.
        accounts = client.get("/accounts", params={"include_balances": True}).json()
        checking = next(a for a in accounts if a["path"] == "assets.checking")
        savings = next(a for a in accounts if a["path"] == "assets.savings")
        assert Decimal(checking["balance"]) == Decimal("-500.00")
        assert Decimal(savings["balance"]) == Decimal("500.00")

    def test_double_confirm_transfer_rejected(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id = _imported(
            client, migrated_engine, "assets.checking", "-500.00", TODAY, payee="Xfer"
        )
        in_id = _imported(client, migrated_engine, "assets.savings", "500.00", TODAY, payee="Xfer")
        first = client.post(
            "/matching/confirm-transfer", json={"outgoing_id": out_id, "incoming_id": in_id}
        )
        assert first.status_code == 200, first.text

        # Replay: the originals are void now, so the merge must fail cleanly
        # instead of minting a second transfer against dead rows.
        second = client.post(
            "/matching/confirm-transfer", json={"outgoing_id": out_id, "incoming_id": in_id}
        )
        assert second.status_code == 422
        assert "not matchable" in second.json()["detail"]

        with migrated_engine.connect() as conn:
            # description is encrypted now (#137) — decrypt-and-count app-side.
            encs = (
                conn.execute(
                    text(
                        "SELECT description_enc FROM transactions WHERE description_enc IS NOT NULL"
                    )
                )
                .scalars()
                .all()
            )
        merged_count = sum(1 for e in encs if crypto.decrypt(bytes(e)).startswith("Transfer:"))
        assert merged_count == 1
        # Balances unchanged by the replay.
        accounts = client.get("/accounts", params={"include_balances": True}).json()
        checking = next(a for a in accounts if a["path"] == "assets.checking")
        savings = next(a for a in accounts if a["path"] == "assets.savings")
        assert Decimal(checking["balance"]) == Decimal("-500.00")
        assert Decimal(savings["balance"]) == Decimal("500.00")

    def test_mismatched_amounts_rejected(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        out_id = _imported(client, migrated_engine, "assets.checking", "-500.00", TODAY)
        in_id = _imported(client, migrated_engine, "assets.savings", "499.00", TODAY)
        resp = client.post(
            "/matching/confirm-transfer", json={"outgoing_id": out_id, "incoming_id": in_id}
        )
        assert resp.status_code == 422


class TestFeedVoidedMatch:
    """#273 (decision 69) at the service seam: the reconciler voids a pending
    that a live match names. End-to-end sync flows live in
    test_feed.TestMatchedPendingLifecycle; these pin the edge cases."""

    def _void(self, pending_id: str, successor_id: str | None = None) -> None:
        from purser import db
        from purser.feed import reconcile
        from purser.models import Transaction

        with db.get_sessionmaker()() as session:
            pending = session.get(Transaction, uuid.UUID(pending_id))
            assert pending is not None
            successor = session.get(Transaction, uuid.UUID(successor_id)) if successor_id else None
            reconcile.void_pending(session, pending, successor=successor)
            session.commit()

    def _matches(self, engine: Engine) -> dict[str, tuple[str, str]]:
        """expected_id -> (posted_id, state) for every match row."""
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT expected_id, posted_id, state FROM matches")).all()
        return {str(r.expected_id): (str(r.posted_id), r.state) for r in rows}

    def _audit_after(self, client: TestClient, action: str) -> list[dict[str, object]]:
        items = client.get("/audit", params={"action": action}).json()["items"]
        return [item["before_after"]["after"] for item in items]

    def test_successor_settling_another_bill_releases_instead(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        first = _expected(client, "-180.00", TODAY)
        second = _expected(client, "-180.00", TODAY + timedelta(days=1))
        pending = _imported(
            client, migrated_engine, "assets.checking", "-180.00", TODAY, status="pending"
        )
        posted = _imported(
            client, migrated_engine, "assets.checking", "-180.00", TODAY + timedelta(days=1)
        )
        for expected_id, bank_id in ((first, pending), (second, posted)):
            resp = client.post(
                "/matching/match", json={"expected_id": expected_id, "posted_id": bank_id}
            )
            assert resp.status_code == 200, resp.text

        self._void(pending, posted)

        matches = self._matches(migrated_engine)
        assert matches[first] == (pending, "rejected")
        assert matches[second] == (posted, "confirmed")  # untouched
        (released,) = self._audit_after(client, "match.unmatch")
        assert released["reason"] == "successor_matched"
        clone = client.get(f"/transactions/{released['recreated_expected_id']}").json()
        assert clone["status"] == "expected"
        assert Decimal(clone["amount"]) == Decimal("-180.00")
        assert clone["date"] == TODAY.isoformat()

    def test_confirmed_match_outranks_a_proposal_on_the_successor(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        first = _expected(client, "-180.00", TODAY)
        second = _expected(client, "-180.00", TODAY + timedelta(days=2))
        pending = _imported(
            client, migrated_engine, "assets.checking", "-180.00", TODAY, status="pending"
        )
        posted = _imported(client, migrated_engine, "assets.checking", "-180.00", TODAY)
        resp = client.post("/matching/match", json={"expected_id": first, "posted_id": pending})
        assert resp.status_code == 200, resp.text
        with migrated_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO matches (expected_id, posted_id, score, variance, state) "
                    "VALUES (:e, :p, 0.6, 0, 'proposed')"
                ),
                {"e": second, "p": posted},
            )

        self._void(pending, posted)

        matches = self._matches(migrated_engine)
        assert matches[first] == (posted, "confirmed")  # followed the successor
        assert matches[second] == (posted, "rejected")  # the proposal yielded
        assert client.get(f"/transactions/{second}").json()["status"] == "expected"
        (rescinded,) = self._audit_after(client, "match.rescind")
        assert rescinded["reason"] == "successor_claimed"
        (carried,) = self._audit_after(client, "match.carry")
        assert carried["posted_id"] == posted

    def test_release_keeps_the_instance_pin(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        schedule = client.post(
            "/schedules",
            json={
                "name": "City Utilities",
                "account": "assets.checking",
                "category": "expenses.utilities",
                "rrule": "FREQ=WEEKLY",
                "amount_type": "fixed",
                "amount": "-180.00",
                "payee": "City Utilities",
            },
        ).json()
        instance = client.get(f"/schedules/{schedule['id']}").json()["instances"][0]
        # The user's override pins the instance (#231).
        patched = client.patch(
            f"/transactions/{instance['transaction_id']}", json={"amount": "-175.00"}
        )
        assert patched.status_code == 200, patched.text
        due = date.fromisoformat(instance["date"])
        pending = _imported(
            client, migrated_engine, "assets.checking", "-175.00", due, status="pending"
        )
        resp = client.post(
            "/matching/match",
            json={"expected_id": instance["transaction_id"], "posted_id": pending},
        )
        assert resp.status_code == 200, resp.text

        self._void(pending)

        live = client.get(f"/schedules/{schedule['id']}").json()["instances"]
        (back,) = [i for i in live if i["occurrence_date"] == instance["occurrence_date"]]
        assert back["pinned"] is True
        assert back["amount"] == "-175.0000"
        # A pinned instance is exempt from estimate refresh and resync.
        client.post("/schedules/materialize")
        refreshed = client.get(f"/schedules/{schedule['id']}").json()["instances"]
        assert [
            i["amount"] for i in refreshed if i["transaction_id"] == back["transaction_id"]
        ] == ["-175.0000"]


class TestManualPostPriceChange:
    """#330 at the service seam: posting a row by hand moves the estimate of
    every schedule whose confirmed match leads to it, not only a match that
    names it — matched_actuals follows a bank row to its lineage head
    (decision 69), and a merge leaves a flat bill's match on the voided
    original (decision 65)."""

    def test_posting_a_pending_merge_emits_for_the_match_on_its_original(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        for route, body in (
            ("/accounts", {"name": "Card", "type": "credit_card", "path": "liabilities.card"}),
            (
                "/categories",
                {"name": "Card payments", "type": "category_expense", "path": "expenses.card"},
            ),
        ):
            assert client.post(route, json=body).status_code == 201
        schedule = client.post(
            "/schedules",
            json={
                "name": "Card Autopay",
                "account": "assets.checking",
                "category": "expenses.card",
                "rrule": "FREQ=MONTHLY",
                "amount_type": "estimated",
                "estimate_basis": "last",
                "amount": "-100.00",
                "payee": "Card Autopay",
            },
        ).json()
        instance = client.get(f"/schedules/{schedule['id']}").json()["instances"][0]
        due = date.fromisoformat(instance["date"])
        outflow = _imported(
            client, migrated_engine, "assets.checking", "-80.00", due, "CARD AUTOPAY", "pending"
        )
        resp = client.post(
            "/matching/match",
            json={"expected_id": instance["transaction_id"], "posted_id": outflow},
        )
        assert resp.status_code == 200, resp.text
        inflow = _imported(
            client,
            migrated_engine,
            "liabilities.card",
            "80.00",
            due,
            "PAYMENT THANK YOU",
            "pending",
        )
        resp = client.post(
            "/matching/confirm-transfer", json={"outgoing_id": outflow, "incoming_id": inflow}
        )
        assert resp.status_code == 200, resp.text
        merged = resp.json()["merged_transaction_id"]
        assert client.get(f"/transactions/{merged}").json()["status"] == "pending"
        with migrated_engine.connect() as conn:
            match_id, posted_id, state = conn.execute(
                text("SELECT id, posted_id, state FROM matches WHERE expected_id = :e"),
                {"e": instance["transaction_id"]},
            ).one()
        # A flat bill's match stays on the bank row it settled (decision 65).
        assert (str(posted_id), state) == (outflow, "confirmed")
        assert client.get("/events", params={"type": "recurring.price_changed"}).json() == []

        resp = client.post(f"/transactions/{merged}/post")
        assert resp.status_code == 200, resp.text

        (change,) = client.get("/events", params={"type": "recurring.price_changed"}).json()
        assert change["entity_id"] == schedule["id"]
        assert change["payload"]["match_id"] == str(match_id)
        assert Decimal(change["payload"]["new_estimate"]) == Decimal("-80.00")
        client.post("/schedules/materialize")
        live = client.get(f"/schedules/{schedule['id']}").json()["instances"]
        assert {i["amount"] for i in live} == {"-80.0000"}


def _estimated_bill(client: TestClient) -> tuple[dict[str, str], dict[str, str], date]:
    """An estimated monthly bill on checking (basis ``last``, -100.00) and
    its first instance: the schedule an absorb's estimate probe watches."""
    schedule = client.post(
        "/schedules",
        json={
            "name": "Savings Sweep",
            "account": "assets.checking",
            "category": "expenses.utilities",
            "rrule": "FREQ=MONTHLY",
            "amount_type": "estimated",
            "estimate_basis": "last",
            "amount": "-100.00",
            "payee": "Savings Sweep",
        },
    ).json()
    instance = client.get(f"/schedules/{schedule['id']}").json()["instances"][0]
    return schedule, instance, date.fromisoformat(instance["date"])


def _manual_transfer(client: TestClient, on: date, *, status: str) -> str:
    resp = client.post(
        "/transactions/transfer",
        json={
            "from_account": "assets.checking",
            "to_account": "assets.savings",
            "amount": "80.00",
            "date": on.isoformat(),
            "status": status,
        },
    )
    assert resp.status_code == 201, resp.text
    tx_id: str = resp.json()["id"]
    return tx_id


def _match_id(engine: Engine, expected_id: str) -> str:
    with engine.connect() as conn:
        return str(
            conn.execute(
                text("SELECT id FROM matches WHERE expected_id = :e AND state = 'confirmed'"),
                {"e": expected_id},
            ).scalar_one()
        )


def _auto_link() -> dict[str, int]:
    from purser import db, transfers

    with db.get_sessionmaker()() as session:
        stats = transfers.auto_link_pass(session)
        session.commit()
    return stats


class TestAbsorbPriceChange:
    """#343 item 1: an absorb that posts a matched pending transfer — or that
    moves a matched pending row under a posted transfer — changes what the
    estimate reads exactly as posting by hand does (#330), so it is
    bracketed by the same probe."""

    def test_auto_link_absorb_posting_a_matched_pending_transfer_emits(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        schedule, instance, due = _estimated_bill(client)
        transfer = _manual_transfer(client, due, status="pending")
        resp = client.post(
            "/matching/match",
            json={"expected_id": instance["transaction_id"], "posted_id": transfer},
        )
        assert resp.status_code == 200, resp.text
        match_id = _match_id(migrated_engine, instance["transaction_id"])
        _imported(client, migrated_engine, "assets.savings", "80.00", due, "TRANSFER FROM CHECKING")

        assert _auto_link()["transfers_absorbed"] == 1

        assert client.get(f"/transactions/{transfer}").json()["status"] == "posted"
        (change,) = client.get("/events", params={"type": "recurring.price_changed"}).json()
        assert change["entity_id"] == schedule["id"]
        assert change["payload"]["match_id"] == match_id
        assert Decimal(change["payload"]["old_estimate"]) == Decimal("-100.00")
        assert Decimal(change["payload"]["new_estimate"]) == Decimal("-80.00")

    def test_convert_absorb_posting_a_matched_pending_transfer_emits(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        schedule, instance, due = _estimated_bill(client)
        transfer = _manual_transfer(client, due, status="pending")
        resp = client.post(
            "/matching/match",
            json={"expected_id": instance["transaction_id"], "posted_id": transfer},
        )
        assert resp.status_code == 200, resp.text
        # No name signal: only the explicit conversion claims it.
        row = _imported(client, migrated_engine, "assets.savings", "80.00", due, "DEPOSIT 4411")

        resp = client.patch(f"/transactions/{row}", json={"category": "[assets.checking]"})
        assert resp.status_code == 200, resp.text

        assert client.get(f"/transactions/{transfer}").json()["status"] == "posted"
        (change,) = client.get("/events", params={"type": "recurring.price_changed"}).json()
        assert change["entity_id"] == schedule["id"]
        assert change["payload"]["match_id"] == _match_id(
            migrated_engine, instance["transaction_id"]
        )
        assert Decimal(change["payload"]["new_estimate"]) == Decimal("-80.00")

    def test_converting_a_matched_pending_into_a_posted_transfer_emits(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        schedule, instance, due = _estimated_bill(client)
        pending = _imported(
            client, migrated_engine, "assets.checking", "-80.00", due, "SAVINGS SWEEP", "pending"
        )
        resp = client.post(
            "/matching/match",
            json={"expected_id": instance["transaction_id"], "posted_id": pending},
        )
        assert resp.status_code == 200, resp.text
        transfer = _manual_transfer(client, due, status="posted")
        assert client.get("/events", params={"type": "recurring.price_changed"}).json() == []

        # The pending is absorbed into the posted transfer: the match's
        # lineage head moves from a pending row to a posted one.
        resp = client.patch(f"/transactions/{pending}", json={"category": "[assets.savings]"})
        assert resp.status_code == 200, resp.text

        detail = client.get(f"/transactions/{pending}").json()
        assert (detail["status"], detail["superseded_by"]) == ("void", transfer)
        (change,) = client.get("/events", params={"type": "recurring.price_changed"}).json()
        assert change["entity_id"] == schedule["id"]
        assert Decimal(change["payload"]["new_estimate"]) == Decimal("-80.00")

    def test_an_absorb_that_flips_nothing_emits_nothing(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        _schedule, instance, due = _estimated_bill(client)
        transfer = _manual_transfer(client, due, status="posted")
        resp = client.post(
            "/matching/match",
            json={"expected_id": instance["transaction_id"], "posted_id": transfer},
        )
        assert resp.status_code == 200, resp.text
        # Matching a posted head moved the estimate once, at the match.
        (at_match,) = client.get("/events", params={"type": "recurring.price_changed"}).json()
        _imported(client, migrated_engine, "assets.savings", "80.00", due, "TRANSFER FROM CHECKING")

        assert _auto_link()["transfers_absorbed"] == 1

        assert client.get(f"/transactions/{transfer}").json()["status"] == "posted"
        events = client.get("/events", params={"type": "recurring.price_changed"}).json()
        assert [e["id"] for e in events] == [at_match["id"]]


class TestManualVoidOfMatchedPending:
    """#343 item 2, the manual twin of #273: voiding a matched pending by hand
    (``POST /transactions/{id}/void``) settles its match as the feed voiding
    it does, with no successor — released with the pin kept, or rescinded —
    and the voiding token is the actor (decision 69)."""

    def _void_actor(self, engine: Engine, tx_id: str) -> str:
        with engine.connect() as conn:
            return str(
                conn.execute(
                    text(
                        "SELECT actor_token_id FROM audit_log"
                        " WHERE action = 'transaction.void' AND entity_id = :t"
                    ),
                    {"t": tx_id},
                ).scalar_one()
            )

    def test_a_confirmed_match_is_released_with_the_pin_kept(
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
        patched = client.patch(
            f"/transactions/{instance['transaction_id']}", json={"amount": "-175.00"}
        )
        assert patched.status_code == 200, patched.text
        due = date.fromisoformat(instance["date"])
        pending = _imported(
            client, migrated_engine, "assets.checking", "-175.00", due, status="pending"
        )
        resp = client.post(
            "/matching/match",
            json={"expected_id": instance["transaction_id"], "posted_id": pending},
        )
        assert resp.status_code == 200, resp.text
        assert client.get(f"/schedules/{schedule['id']}").json()["next_due"] != instance["date"]

        resp = client.post(f"/transactions/{pending}/void")
        assert resp.status_code == 200, resp.text

        actor = self._void_actor(migrated_engine, pending)
        with migrated_engine.connect() as conn:
            state, decided_by = conn.execute(
                text("SELECT state, decided_by FROM matches WHERE expected_id = :e"),
                {"e": instance["transaction_id"]},
            ).one()
            unmatch_actor = conn.execute(
                text("SELECT actor_token_id FROM audit_log WHERE action = 'match.unmatch'")
            ).scalar_one()
        assert (state, str(decided_by), str(unmatch_actor)) == ("rejected", actor, actor)
        released = client.get("/audit", params={"action": "match.unmatch"}).json()["items"]
        (after,) = [item["before_after"]["after"] for item in released]
        assert after["reason"] == "pending_voided"
        live = client.get(f"/schedules/{schedule['id']}").json()
        (back,) = [
            i for i in live["instances"] if i["occurrence_date"] == instance["occurrence_date"]
        ]
        assert back["transaction_id"] == after["recreated_expected_id"]
        assert back["pinned"] is True
        assert back["amount"] == "-175.0000"
        assert live["next_due"] == instance["date"]  # the bill is due again

    def test_a_proposal_is_rescinded_and_the_bill_can_be_proposed_again(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        expected_id = _expected(client, "-180.00", TODAY, payee="City Utilities")
        pending = _imported(
            client,
            migrated_engine,
            "assets.checking",
            "-95.00",
            TODAY + timedelta(days=3),
            payee="City Utilities Pmt",
            status="pending",
        )
        assert client.post("/matching/run").json()["proposed"] == 1

        resp = client.post(f"/transactions/{pending}/void")
        assert resp.status_code == 200, resp.text

        actor = self._void_actor(migrated_engine, pending)
        with migrated_engine.connect() as conn:
            state, decided_by = conn.execute(
                text("SELECT state, decided_by FROM matches WHERE expected_id = :e"),
                {"e": expected_id},
            ).one()
        assert (state, str(decided_by)) == ("rejected", actor)
        (rescind,) = client.get("/audit", params={"action": "match.rescind"}).json()["items"]
        assert rescind["before_after"]["after"]["reason"] == "pending_voided"
        assert client.get("/matching/proposed").json() == []
        assert client.get(f"/transactions/{expected_id}").json()["status"] == "expected"

        # The dead proposal no longer blocks the bill (matches_expected_live_uniq).
        _imported(
            client,
            migrated_engine,
            "assets.checking",
            "-96.00",
            TODAY + timedelta(days=2),
            payee="City Utilities Pmt",
        )
        assert client.post("/matching/run").json()["proposed"] == 1

    def test_voiding_an_unmatched_pending_is_unchanged(
        self, seeded: dict[str, str], client: TestClient, migrated_engine: Engine
    ) -> None:
        pending = _imported(
            client, migrated_engine, "assets.checking", "-42.00", TODAY, status="pending"
        )
        resp = client.post(f"/transactions/{pending}/void")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "void"
        with migrated_engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM matches")).scalar_one() == 0
            actions = set(
                conn.execute(
                    text("SELECT action FROM audit_log WHERE action LIKE 'match.%'")
                ).scalars()
            )
        assert actions == set()
