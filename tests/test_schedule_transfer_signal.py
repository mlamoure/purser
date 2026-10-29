"""A schedule's counterparty is a transfer signal, including legs that post on
different days (DESIGN §5/§6, decision 65, issue #270).

The bank legs of a scheduled transfer often post one to three days apart, and
the nightly job matches BEFORE it auto-links: the first leg used to be matched
to the two-leg expected instance and leave the transfer pool, so its
counterpart never linked. A leg held for an expected transfer now stays in the
pool for that transfer's other side, and the settlement follows it into the
merge — the instance is settled exactly once, by the merged transfer.

Every date is anchored in June 2026 (the instances are seeded on their slot
directly, the way materialization writes them), so nothing here depends on the
day the suite runs.
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import Engine, text

from purser import crypto
from purser.core import matching as core_matching
from tests.test_transfers import Harness, _epoch, _event_count, two_account_payload

# Opaque feed descriptors: no transfer keyword, no account name — only the
# schedule can say these two rows are one transfer (decision 46's Fidelity case).
OUT_ROW = {
    "id": "sched-out",
    "posted": _epoch(20),
    "amount": "-275.00",
    "description": "FID BKG SVC LLC MONEYLINE",
}
IN_ROW = {
    "id": "sched-in",
    "posted": _epoch(21),
    "amount": "275.00",
    "description": "MONEY LINE RECEIVED EFT",
}


@pytest.fixture
def harness(clean_db: None, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(client, monkeypatch)


def _transfer_schedule(client: TestClient, *, name: str, day: int = 20, **extra: Any) -> str:
    """An active checking -> savings schedule (decision 56). Creating it
    materializes its slots from today on — always well after the June 2026
    rows below, so they never interact with them."""
    body: dict[str, Any] = {
        "name": name,
        "account": "assets.checking",
        "counterparty_account": "assets.savings",
        "rrule": f"FREQ=MONTHLY;BYMONTHDAY={day}",
        "dtstart": f"2026-06-{day:02d}",
        "amount_type": "fixed",
        "amount": "-275.00",
    }
    body.update(extra)
    resp = client.post("/schedules", json=body)
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _june_instance(schedule_id: str, *, day: int = 20) -> str:
    """The schedule's June slot, written exactly as materialization writes an
    instance: a two-leg expected transfer through ``ledger.create_transfer``."""
    from purser import ledger as ledger_service
    from purser import schedules as schedules_service
    from purser.core import ledger as core
    from purser.db import get_sessionmaker
    from purser.models import Schedule

    with get_sessionmaker()() as session:
        schedule = session.get(Schedule, uuid.UUID(schedule_id))
        assert schedule is not None and schedule.counterparty_account_id is not None
        slot = date(2026, 6, day)
        amount = schedules_service.instance_amount(session, schedule)
        tx = ledger_service.create_transfer(
            session,
            from_account_ref=str(schedule.account_id),
            to_account_ref=str(schedule.counterparty_account_id),
            amount=-amount,
            date=slot,
            description=schedule.name,
            requested_status=core.Status.EXPECTED,
            origin=core.Origin.RULE,
            token=None,
            schedule_id=schedule.id,
            occurrence_date=slot,
        )
        schedules_service.refresh_next_due(session, schedule)
        session.commit()
        return str(tx.id)


def _nightly() -> None:
    """The scheduled 06:45 job: matching first, then the auto-link sweep."""
    from purser.matching import run_matching_job

    run_matching_job()


def _matches(engine: Engine, expected_id: str) -> list[tuple[str, str, str]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id::text, state, posted_id::text FROM matches"
                " WHERE expected_id = :e ORDER BY created_at, id"
            ),
            {"e": expected_id},
        ).all()
    return [(r[0], r[1], r[2]) for r in rows]


def _row(engine: Engine, tx_id: str) -> Any:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT status, superseded_by::text AS superseded_by"
                " FROM transactions WHERE id = :t"
            ),
            {"t": tx_id},
        ).one()


def _auto_links(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT payload_enc FROM events WHERE type = 'transfer.auto_linked' ORDER BY at")
        ).scalars()
        return [crypto.decrypt_json(bytes(r)) for r in rows]


def _confirmed_events_for(engine: Engine, expected_id: str) -> int:
    """match.confirmed events whose match settles ``expected_id``."""
    with engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM events e JOIN matches m ON m.id = e.entity_id"
                    " WHERE e.type = 'match.confirmed' AND m.expected_id = :e"
                ),
                {"e": expected_id},
            ).scalar_one()
        )


def _balances(client: TestClient) -> dict[str, Decimal]:
    accounts = client.get("/accounts", params={"include_balances": True}).json()
    return {
        a["path"]: Decimal(a["balance"])
        for a in accounts
        if a["path"] in {"assets.checking", "assets.savings"}
    }


def _transfer_row(client: TestClient) -> dict[str, Any]:
    (row,) = client.get("/transactions", params={"account": "assets.checking"}).json()["items"]
    assert row["is_transfer"] is True
    assert row["transfer_account_path"] == "assets.savings"
    return dict(row)


class TestHeldPairing:
    """The pure pairing constraint (core.matching, mypy strict)."""

    def _input(
        self, account: uuid.UUID, amount: str, day: int, held_for: uuid.UUID | None = None
    ) -> core_matching.MatchInput:
        return core_matching.MatchInput(
            id=uuid.uuid4(),
            date=date(2026, 6, day),
            amount=Decimal(amount),
            normalized_payee="",
            account_id=account,
            pair_account_id=held_for,
        )

    def test_held_leg_pairs_only_with_its_expected_other_side(self) -> None:
        checking, savings, brokerage = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        held = self._input(checking, "-275", 20, held_for=savings)
        # Earlier, so greedy date order would take it if the hold were a post-filter.
        elsewhere = self._input(brokerage, "275", 19)
        counterpart = self._input(savings, "275", 21)
        pairs = core_matching.detect_transfer_pairs([held, elsewhere, counterpart])
        assert [(p.outgoing_id, p.incoming_id) for p in pairs] == [(held.id, counterpart.id)]

    def test_two_held_legs_never_pair(self) -> None:
        checking, savings = uuid.uuid4(), uuid.uuid4()
        out = self._input(checking, "-275", 20, held_for=savings)
        inc = self._input(savings, "275", 21, held_for=checking)
        assert core_matching.detect_transfer_pairs([out, inc]) == []

    def test_unheld_rows_still_pair_with_a_held_counterpart(self) -> None:
        checking, savings = uuid.uuid4(), uuid.uuid4()
        out = self._input(checking, "-275", 20)
        inc = self._input(savings, "275", 21, held_for=checking)
        (pair,) = core_matching.detect_transfer_pairs([out, inc])
        assert (pair.outgoing_id, pair.incoming_id) == (out.id, inc.id)

    @given(
        st.lists(
            st.tuples(
                st.integers(0, 2),  # account
                st.sampled_from(["-100", "100", "-250", "250"]),
                st.integers(1, 12),  # day of June
                st.one_of(st.none(), st.integers(0, 2)),  # held for
            ),
            max_size=12,
        )
    )
    def test_pairs_respect_every_constraint(
        self, specs: list[tuple[int, str, int, int | None]]
    ) -> None:
        accounts = [uuid.UUID(int=i + 1) for i in range(3)]
        inputs = [
            self._input(accounts[a], amount, day, accounts[held] if held is not None else None)
            for a, amount, day, held in specs
        ]
        by_id = {i.id: i for i in inputs}
        pairs = core_matching.detect_transfer_pairs(inputs)
        used = [p.outgoing_id for p in pairs] + [p.incoming_id for p in pairs]
        assert len(used) == len(set(used)), "a row paired twice"
        for pair in pairs:
            out, inc = by_id[pair.outgoing_id], by_id[pair.incoming_id]
            assert out.amount == -inc.amount and out.amount < 0
            assert abs((inc.date - out.date).days) <= core_matching.TRANSFER_WINDOW_DAYS
            assert out.account_id != inc.account_id
            assert out.pair_account_id is None or inc.pair_account_id is None
            assert out.pair_account_id in (None, inc.account_id)
            assert inc.pair_account_id in (None, out.account_id)


class TestScheduleSignal:
    def test_same_sync_legs_link_and_settle_the_instance(
        self, harness: Harness, client: TestClient, migrated_engine: Engine
    ) -> None:
        harness.use_payload(two_account_payload([], []))
        connection_id = harness.connect_and_map_both()
        schedule_id = _transfer_schedule(client, name="College fund")
        instance_id = _june_instance(schedule_id)

        harness.use_payload(two_account_payload([OUT_ROW], [IN_ROW]))
        harness.sync(connection_id)

        (link,) = _auto_links(migrated_engine)
        assert link["signal"] == f"schedule:{schedule_id}"
        merged = _transfer_row(client)
        # Settled at link time, by the merge, once.
        assert _row(migrated_engine, instance_id).superseded_by == merged["id"]
        assert [(s, p) for _, s, p in _matches(migrated_engine, instance_id)] == [
            ("confirmed", merged["id"])
        ]
        assert _balances(client) == {
            "assets.checking": Decimal("-275.00"),
            "assets.savings": Decimal("275.00"),
        }

        _nightly()
        _nightly()
        assert [(s, p) for _, s, p in _matches(migrated_engine, instance_id)] == [
            ("confirmed", merged["id"])
        ]
        assert _confirmed_events_for(migrated_engine, instance_id) == 1
        assert len(_auto_links(migrated_engine)) == 1
        assert client.get("/matching/transfer-candidates").json() == []

    @pytest.mark.parametrize(
        ("schedule_name", "first_leg", "day1_state"),
        [
            # Exact amount and date alone score 0.55: the matcher PROPOSES.
            ("College fund", "out", "proposed"),
            # The schedule named like the descriptor: the matcher AUTO-matches.
            ("FID BKG SVC LLC MONEYLINE", "out", "confirmed"),
            # The receiving bank is the quicker one: the savings leg is held.
            ("College fund", "in", "proposed"),
        ],
    )
    def test_cross_day_legs_link_with_the_nightly_matcher_between(
        self,
        harness: Harness,
        client: TestClient,
        migrated_engine: Engine,
        schedule_name: str,
        first_leg: str,
        day1_state: str,
    ) -> None:
        harness.use_payload(two_account_payload([], []))
        connection_id = harness.connect_and_map_both()
        schedule_id = _transfer_schedule(client, name=schedule_name)
        instance_id = _june_instance(schedule_id)
        # A second transfer schedule between the same accounts, due four days
        # later: close enough to the merged transfer to be PROPOSED against it
        # (0.77) if the merge were left unmatched — the double settlement.
        other_id = _june_instance(_transfer_schedule(client, name="Extra savings", day=24), day=24)
        if first_leg == "out":
            out_row, in_row = OUT_ROW, IN_ROW  # 6/20, then 6/21
            day1 = two_account_payload([out_row], [])
        else:
            out_row = {**OUT_ROW, "posted": _epoch(21)}
            in_row = {**IN_ROW, "posted": _epoch(20)}
            day1 = two_account_payload([], [in_row])

        # Day 1: only the first leg; then the nightly job, matching first.
        harness.use_payload(day1)
        harness.sync(connection_id)
        _nightly()
        ((first_match, state, held_row),) = _matches(migrated_engine, instance_id)
        assert state == day1_state  # the leg is held for the instance
        assert client.get("/matching/transfer-candidates").json() == []

        # Day 2: the other leg posts a day later.
        harness.use_payload(two_account_payload([out_row], [in_row]))
        harness.sync(connection_id)

        (link,) = _auto_links(migrated_engine)
        assert link["signal"] == f"schedule:{schedule_id}"
        merged = _transfer_row(client)
        assert _row(migrated_engine, held_row).status == "void"
        assert _row(migrated_engine, instance_id).superseded_by == merged["id"]
        settled = [(m, p) for m, s, p in _matches(migrated_engine, instance_id) if s == "confirmed"]
        assert [p for _, p in settled] == [merged["id"]]
        if day1_state == "confirmed":
            # The settlement moved with its bank line: same match row.
            assert settled[0][0] == first_match
        else:
            # The proposal named a row that no longer exists: rescinded, and
            # the instance matched again at once — on the merge.
            assert ("rejected", held_row) in [
                (s, p) for _, s, p in _matches(migrated_engine, instance_id)
            ]

        # Nightly again: nothing moves, and the merge settles nothing else.
        _nightly()
        assert [p for _, s, p in _matches(migrated_engine, instance_id) if s == "confirmed"] == [
            merged["id"]
        ]
        assert _confirmed_events_for(migrated_engine, instance_id) == 1
        assert _matches(migrated_engine, other_id) == []
        assert _row(migrated_engine, other_id).status == "expected"
        assert len(_auto_links(migrated_engine)) == 1
        assert _balances(client) == {
            "assets.checking": Decimal("-275.00"),
            "assets.savings": Decimal("275.00"),
        }

    def test_leg_settling_a_bill_is_unaffected(
        self, harness: Harness, client: TestClient, migrated_engine: Engine
    ) -> None:
        harness.use_payload(two_account_payload([], []))
        connection_id = harness.connect_and_map_both()
        # A bill the checking row really is.
        assert (
            client.post(
                "/categories",
                json={
                    "name": "Utilities",
                    "type": "category_expense",
                    "path": "expenses.utilities",
                },
            ).status_code
            == 201
        )
        bill = client.post(
            "/transactions",
            json={
                "account": "assets.checking",
                "category": "expenses.utilities",
                "amount": "-275.00",
                "date": "2026-06-20",
                "payee": "City Power",
                "status": "expected",
            },
        )
        assert bill.status_code == 201, bill.text
        bill_id = bill.json()["id"]
        power = {**OUT_ROW, "id": "power-out", "description": "CITY POWER"}

        harness.use_payload(two_account_payload([power], []))
        harness.sync(connection_id)
        _nightly()
        ((match_id, state, paid_by),) = _matches(migrated_engine, bill_id)
        assert state == "confirmed"

        # A transfer schedule whose open instance WOULD cover the pair, and a
        # savings row carrying a NAME signal: neither claims the paid bill.
        _june_instance(_transfer_schedule(client, name="College fund"))
        signalled = {**IN_ROW, "description": "TRANSFER FROM CHECKING"}
        harness.use_payload(two_account_payload([power], [signalled]))
        harness.sync(connection_id)
        _nightly()

        assert _event_count(migrated_engine, "transfer.auto_linked") == 0
        assert _matches(migrated_engine, bill_id) == [(match_id, "confirmed", paid_by)]
        assert _row(migrated_engine, paid_by).status == "posted"
        (row,) = client.get("/transactions", params={"account": "assets.checking"}).json()["items"]
        assert row["id"] == paid_by
        assert row["is_transfer"] is False
        # A matched bill payment carries no linked bank rows: nothing to unlink.
        assert row["has_linked_originals"] is False
        assert client.get(f"/transactions/{paid_by}").json()["has_linked_originals"] is False

    @pytest.mark.parametrize(
        ("named", "cross_day"),
        [
            (False, False),  # linked by the schedule, same sync
            (False, True),  # linked by the schedule, matcher between the legs
            # Linked by a NAME signal and settled by the matcher: the shape
            # that already existed before the schedule signal (decision 56).
            (True, False),
        ],
    )
    def test_unlink_restores_and_releases_the_settlement(
        self,
        harness: Harness,
        client: TestClient,
        migrated_engine: Engine,
        named: bool,
        cross_day: bool,
    ) -> None:
        harness.use_payload(two_account_payload([], []))
        connection_id = harness.connect_and_map_both()
        schedule_id = _transfer_schedule(client, name="College fund")
        instance_id = _june_instance(schedule_id)
        out_row, in_row = OUT_ROW, IN_ROW
        if named:
            out_row = {**OUT_ROW, "description": "TRANSFER TO SAVINGS"}
            in_row = {**IN_ROW, "description": "TRANSFER FROM CHECKING"}
        if cross_day:
            harness.use_payload(two_account_payload([out_row], []))
            harness.sync(connection_id)
            _nightly()
        harness.use_payload(two_account_payload([out_row], [in_row]))
        harness.sync(connection_id)
        _nightly()
        merged = _transfer_row(client)
        assert _row(migrated_engine, instance_id).superseded_by == merged["id"]

        resp = client.post(f"/transactions/{merged['id']}/unlink-transfer")
        assert resp.status_code == 200, resp.text
        assert resp.json()["transfer_voided"] is True

        # Both bank rows come back exactly — and only them: the settled
        # instance is never resurrected as a posted copy of itself.
        resurrected = resp.json()["resurrected_ids"]
        assert len(resurrected) == 2
        with migrated_engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT t.status, t.origin, t.content_hash IS NOT NULL,"
                    " (SELECT COUNT(*) FROM postings p JOIN accounts a ON a.id = p.account_id"
                    "  WHERE p.transaction_id = t.id AND NOT a.is_virtual)"
                    " FROM transactions t WHERE t.id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": resurrected},
            ).all()
            # No live row with two real legs is left: the transfer is void, and
            # nothing re-created the instance's legs as a posted transfer.
            live_transfers = conn.execute(
                text(
                    "SELECT COUNT(*) FROM transactions t WHERE t.status IN ('pending', 'posted')"
                    " AND (SELECT COUNT(*) FROM postings p JOIN accounts a ON a.id = p.account_id"
                    "      WHERE p.transaction_id = t.id AND NOT a.is_virtual) = 2"
                )
            ).scalar_one()
        assert sorted(rows) == [("posted", "import", True, 1)] * 2
        assert live_transfers == 0
        assert _balances(client) == {
            "assets.checking": Decimal("-275.00"),
            "assets.savings": Decimal("275.00"),
        }

        # The settlement went with the transfer: the slot is due again, once.
        assert {s for _, s, _ in _matches(migrated_engine, instance_id)} == {"rejected"}
        instances = client.get(f"/schedules/{schedule_id}").json()["instances"]
        june = [i for i in instances if i["occurrence_date"] == "2026-06-20"]
        assert len(june) == 1 and june[0]["transaction_id"] != instance_id
        reopened = client.get(f"/transactions/{june[0]['transaction_id']}").json()
        assert reopened["status"] == "expected"
        assert {p["account_path"] for p in reopened["postings"]} == {
            "assets.checking",
            "assets.savings",
        }

        # A human unlinked the pair: the nightly job never relinks it, and the
        # re-opened instance holds one row at most.
        _nightly()
        assert len(_auto_links(migrated_engine)) == 1
        live = [
            p for _, s, p in _matches(migrated_engine, june[0]["transaction_id"]) if s != "rejected"
        ]
        assert len(live) <= 1 and set(live) <= set(resurrected)
        for path in ("assets.checking", "assets.savings"):
            (row,) = client.get("/transactions", params={"account": path}).json()["items"]
            assert row["is_transfer"] is False

    def test_converting_the_late_leg_merges_with_the_held_one(
        self, harness: Harness, client: TestClient, migrated_engine: Engine
    ) -> None:
        """The explicit path shares the pool rule: converting the savings row
        to [assets.checking] merges with the checking leg the matcher holds,
        instead of writing a second checking leg beside it."""
        harness.use_payload(two_account_payload([], []))
        connection_id = harness.connect_and_map_both()
        instance_id = _june_instance(_transfer_schedule(client, name="College fund"))
        assert client.put("/config", json={"transfer_auto_link": False}).status_code == 200

        harness.use_payload(two_account_payload([OUT_ROW], []))
        harness.sync(connection_id)
        _nightly()
        ((_, state, held_row),) = _matches(migrated_engine, instance_id)
        assert state == "proposed"
        harness.use_payload(two_account_payload([OUT_ROW], [IN_ROW]))
        harness.sync(connection_id)
        (late,) = client.get("/transactions", params={"account": "assets.savings"}).json()["items"]

        resp = client.patch(f"/transactions/{late['id']}", json={"category": "[assets.checking]"})
        assert resp.status_code == 200, resp.text

        merged = _transfer_row(client)
        assert _row(migrated_engine, held_row).superseded_by == merged["id"]
        assert _row(migrated_engine, instance_id).superseded_by == merged["id"]
        assert _balances(client) == {
            "assets.checking": Decimal("-275.00"),
            "assets.savings": Decimal("275.00"),
        }

    def test_estimates_read_one_amount_per_settling_transfer(
        self, harness: Harness, client: TestClient, migrated_engine: Engine
    ) -> None:
        """A merged transfer has two real legs; the schedule's history reads
        the one on its account, so an estimate keeps its sign and size."""
        from purser import schedules as schedules_service
        from purser.db import get_sessionmaker
        from purser.models import Schedule

        harness.use_payload(two_account_payload([], []))
        connection_id = harness.connect_and_map_both()
        schedule_id = _transfer_schedule(
            client, name="College fund", amount_type="estimated", estimate_basis="last"
        )
        _june_instance(schedule_id)
        harness.use_payload(two_account_payload([OUT_ROW], [IN_ROW]))
        harness.sync(connection_id)
        assert len(_auto_links(migrated_engine)) == 1

        with get_sessionmaker()() as session:
            schedule = session.get(Schedule, uuid.UUID(schedule_id))
            assert schedule is not None
            assert schedules_service.matched_actuals(session, schedule) == [
                (Decimal("-275.0000"), date(2026, 6, 20))
            ]
            assert schedules_service.instance_amount(session, schedule) == Decimal("-275.0000")
        # Every live instance keeps the outgoing amount after a refresh.
        client.post("/schedules/materialize")
        instances = client.get(f"/schedules/{schedule_id}").json()["instances"]
        assert instances and {i["amount"] for i in instances} == {"-275.0000"}


def test_instance_outside_the_window_is_no_signal(
    harness: Harness, client: TestClient, migrated_engine: Engine
) -> None:
    """The ±3-day transfer window bounds the schedule signal (decision 66
    widens declared pairs only): an instance four days from a leg vouches
    for nothing, and the pair stays a confirm-first candidate."""
    harness.use_payload(two_account_payload([], []))
    connection_id = harness.connect_and_map_both()
    _june_instance(_transfer_schedule(client, name="College fund", day=16), day=16)
    late_out = {**OUT_ROW, "posted": _epoch(19)}
    late_in = {**IN_ROW, "posted": _epoch(20)}
    harness.use_payload(two_account_payload([late_out], [late_in]))
    harness.sync(connection_id)
    assert _auto_links(migrated_engine) == []
    assert len(client.get("/matching/transfer-candidates").json()) == 1


def test_widened_declared_pair_links_a_held_leg_and_counts_it(
    harness: Harness, client: TestClient, migrated_engine: Engine
) -> None:
    """Decision 66 and 65 together: a transfer_link rule's widened window
    pairs a held leg too (second stage), its activation count includes that
    pair because it is the pass's own plan, and the merge carries the
    instance's settlement."""
    harness.use_payload(two_account_payload([], []))
    connection_id = harness.connect_and_map_both()
    instance_id = _june_instance(_transfer_schedule(client, name="College fund"))

    # Day 1: the checking leg; the nightly matcher holds it for the instance.
    harness.use_payload(two_account_payload([OUT_ROW], []))
    harness.sync(connection_id)
    _nightly()
    ((_, state, held_row),) = _matches(migrated_engine, instance_id)
    assert state == "proposed"

    # The savings leg posts five days later: past the ±3-day window, so
    # neither the schedule nor the standard stage links it.
    late_in = {**IN_ROW, "posted": _epoch(25)}
    harness.use_payload(two_account_payload([OUT_ROW], [late_in]))
    harness.sync(connection_id)
    _nightly()
    assert _auto_links(migrated_engine) == []

    accounts = {a["path"]: a["id"] for a in client.get("/accounts").json()}
    rule = client.post(
        "/rules",
        json={
            "type": "transfer_link",
            "pair_account_a": accounts["assets.checking"],
            "pair_account_b": accounts["assets.savings"],
            "window_days": 7,
        },
    )
    assert rule.status_code == 201, rule.text
    rule_id = rule.json()["id"]
    refused = client.post(f"/rules/{rule_id}/state", json={"state": "active"})
    assert refused.status_code == 422, refused.text
    assert "would link 1 historical pair" in refused.json()["detail"]
    activated = client.post(f"/rules/{rule_id}/state", json={"state": "active", "confirm_links": 1})
    assert activated.status_code == 200, activated.text

    _nightly()
    (link,) = _auto_links(migrated_engine)
    assert link["signal"] == f"rule:{rule_id}"
    merged = _transfer_row(client)
    assert _row(migrated_engine, held_row).superseded_by == merged["id"]
    assert _row(migrated_engine, instance_id).superseded_by == merged["id"]
    assert [p for _, s, p in _matches(migrated_engine, instance_id) if s == "confirmed"] == [
        merged["id"]
    ]
    assert _confirmed_events_for(migrated_engine, instance_id) == 1


@pytest.mark.parametrize("override", [False, True])
def test_an_override_to_the_bank_amount_makes_the_instance_vouch(
    harness: Harness, client: TestClient, migrated_engine: Engine, override: bool
) -> None:
    """#350 + decision 65: an instance vouches only for exactly its amount, so
    an estimate that is off links nothing — and overriding the instance to
    the amount the statement says (the leg of the schedule account, both legs
    moving) is what lets the bank pair link with ``schedule:<id>`` and
    settle it."""
    harness.use_payload(two_account_payload([], []))
    connection_id = harness.connect_and_map_both()
    schedule_id = _transfer_schedule(client, name="College fund", amount="-250.00")
    instance_id = _june_instance(schedule_id)
    if override:
        resp = client.patch(f"/transactions/{instance_id}", json={"amount": "-275.00"})
        assert resp.status_code == 200, resp.text

    harness.use_payload(two_account_payload([OUT_ROW], [IN_ROW]))
    harness.sync(connection_id)

    if not override:
        assert _auto_links(migrated_engine) == []
        assert _row(migrated_engine, instance_id).superseded_by is None
        return
    (link,) = _auto_links(migrated_engine)
    assert link["signal"] == f"schedule:{schedule_id}"
    merged = _transfer_row(client)
    assert _row(migrated_engine, instance_id).superseded_by == merged["id"]
    assert [(s, p) for _, s, p in _matches(migrated_engine, instance_id)] == [
        ("confirmed", merged["id"])
    ]
