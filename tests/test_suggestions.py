"""Suggestions inbox (issue #233, DESIGN decision 57): fingerprint upsert,
durable scoped rejection, snooze, deterministic accept, scope gates, the
propose-scope create_schedule path, digest section, and the legacy-row
migration helper."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from purser import crypto, digest
from purser import suggestions as service
from purser.models import ApiToken, Suggestion


@pytest.fixture
def seeded(admin_client: TestClient) -> None:
    admin_client.post(
        "/accounts", json={"name": "Checking", "type": "checking", "path": "assets.checking"}
    )
    admin_client.post(
        "/accounts",
        json={"name": "Citi Card", "type": "credit_card", "path": "liabilities.citicard"},
    )
    admin_client.post(
        "/categories",
        json={"name": "Housing", "type": "category_expense", "path": "expenses.housing"},
    )


def _month_day(months_ago: int, day: int) -> date:
    """`day` of the month, `months_ago` whole months before its most recent
    occurrence on or before today. Anchoring on that occurrence keeps the
    newest seeded row under a month old whatever today's day of month is, so
    a monthly series never reads as lapsed (1.5 periods, #262)."""
    today = date.today()
    y, m = today.year, today.month
    if day > today.day:
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    m -= months_ago
    while m <= 0:
        y, m = y - 1, m + 12
    return date(y, m, day)


def _seed_recurring(engine: Engine, payee: str, months: tuple[int, ...] = (2, 1, 0)) -> None:
    """Monthly import rows for a payee on checking (the detector's food)."""
    with engine.begin() as conn:
        account_id = conn.execute(
            text("SELECT id FROM accounts WHERE path = 'assets.checking'")
        ).scalar_one()
        cat_id = (
            conn.execute(
                text("SELECT id FROM accounts WHERE path = 'expenses.uncategorized'")
            ).scalar_one_or_none()
            or conn.execute(
                text(
                    "INSERT INTO accounts (name, type, is_virtual, path) VALUES "
                    "('Uncat', 'category_expense', true, 'expenses.uncategorized') RETURNING id"
                )
            ).scalar_one()
        )
        payee_id = conn.execute(
            text("INSERT INTO payees (name_enc, name_bidx) VALUES (:ne, :nb) RETURNING id"),
            {"ne": crypto.encrypt(payee), "nb": crypto.blind_index(payee.lower())},
        ).scalar_one()
        for months_ago in months:
            d = _month_day(months_ago, 15)
            tx_id = conn.execute(
                text(
                    "INSERT INTO transactions"
                    " (date, payee_id, description_enc, status, origin) "
                    "VALUES (:d, :p, :desc, 'posted', 'import') RETURNING id"
                ),
                {"d": d, "p": payee_id, "desc": crypto.encrypt(payee.upper())},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO postings (transaction_id, account_id, amount) VALUES "
                    "(:tx, :acct, -15.99), (:tx, :cat, 15.99)"
                ),
                {"tx": tx_id, "acct": account_id, "cat": cat_id},
            )


def _detect(client: TestClient) -> dict[str, object]:
    resp = client.post("/schedules/detect")
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


class TestInbox:
    def test_detection_lands_a_suggestion_with_evidence_and_upserts(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        _seed_recurring(migrated_engine, "Netflix")
        first = _detect(client)
        assert first["created"] == 1
        sid = first["suggestions"][0]["suggestion_id"]  # type: ignore[index]
        row = client.get(f"/suggestions/{sid}").json()
        assert row["kind"] == "schedule" and row["status"] == "open"
        assert row["source"] == "detected"
        assert row["draft"]["account"] == "assets.checking"
        assert row["draft"]["payee"] == "Netflix"
        assert row["draft"]["amount_type"] == "fixed"  # steady amount (#234)
        assert row["draft"]["category"] == "expenses.uncategorized"
        assert Decimal(row["draft"]["amount"]) == Decimal("-15.99")
        assert row["evidence"]["cadence"] == "monthly"
        assert len(row["evidence"]["occurrences"]) == 3
        assert row["evidence"]["gaps"]["misses"] == 0
        assert row["evidence_summary"]["amount_range"] == {"low": "-15.99", "high": "-15.99"}
        assert Decimal(row["confidence"]) == Decimal("0.750")
        assert "Netflix" in row["headline"]

        again = _detect(client)
        assert again["created"] == 0 and again["refreshed"] == 1
        assert client.get("/suggestions").json()[0]["seen_count"] == 2
        assert len(client.get("/suggestions").json()) == 1

    def test_reject_is_durable_and_counterparty_scope_covers_the_pair(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        _seed_recurring(migrated_engine, "Spotify")
        sid = _detect(client)["suggestions"][0]["suggestion_id"]  # type: ignore[index]
        rejected = client.post(
            f"/suggestions/{sid}/decide",
            json={"decision": "reject", "reject_scope": "counterparty", "note": "not a bill"},
        )
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["suggestion"]["status"] == "rejected"
        assert rejected.json()["schedule"] is None
        assert _detect(client)["suggestions"] == []
        assert client.get("/suggestions").json() == []
        assert client.get("/suggestions", params={"status": "rejected"}).json()[0]["id"] == sid
        # Deciding twice is refused.
        assert (
            client.post(f"/suggestions/{sid}/decide", json={"decision": "accept"}).status_code
            == 422
        )

    def test_snooze_hides_until_the_date_and_never_auto_accepts(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        _seed_recurring(migrated_engine, "Hulu")
        sid = _detect(client)["suggestions"][0]["suggestion_id"]  # type: ignore[index]
        snoozed = client.post(f"/suggestions/{sid}/decide", json={"decision": "snooze"})
        assert snoozed.status_code == 200, snoozed.text
        assert snoozed.json()["suggestion"]["status"] == "open"
        until = date.fromisoformat(snoozed.json()["suggestion"]["snooze_until"])
        assert until == date.today() + timedelta(days=30)
        assert client.get("/suggestions").json() == []
        assert len(client.get("/suggestions", params={"include_snoozed": True}).json()) == 1
        # Past date -> 422; the row is still open and still not a schedule.
        assert (
            client.post(
                f"/suggestions/{sid}/decide",
                json={"decision": "snooze", "snooze_until": date.today().isoformat()},
            ).status_code
            == 422
        )
        assert client.get("/schedules").json() == []

    def test_accept_applies_the_draft_deterministically(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        _seed_recurring(migrated_engine, "Gym")
        sid = _detect(client)["suggestions"][0]["suggestion_id"]  # type: ignore[index]
        draft = client.get(f"/suggestions/{sid}").json()["draft"]
        accepted = client.post(f"/suggestions/{sid}/decide", json={"decision": "accept"})
        assert accepted.status_code == 200, accepted.text
        schedule = accepted.json()["schedule"]
        assert schedule["status"] == "active"
        assert schedule["source"] == "detected"
        assert schedule["rrule"] == draft["rrule"]
        assert schedule["dtstart"] == draft["dtstart"]
        assert Decimal(schedule["amount"]) == Decimal(draft["amount"])
        assert schedule["amount_type"] == draft["amount_type"]
        assert accepted.json()["suggestion"]["result_entity_id"] == schedule["id"]
        # Materialized right away; the pattern is now covered, so detection
        # is quiet even though the rows are still there.
        assert client.get(f"/schedules/{schedule['id']}").json()["instances"]
        assert _detect(client)["suggestions"] == []

    def test_edit_then_accept_validates_through_the_draft_schema(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        _seed_recurring(migrated_engine, "Water Co")
        sid = _detect(client)["suggestions"][0]["suggestion_id"]  # type: ignore[index]
        bad = client.post(
            f"/suggestions/{sid}/decide",
            json={"decision": "edit_then_accept", "edits": {"rrule": "FREQ=HOURLY"}},
        )
        assert bad.status_code == 422
        assert client.get("/schedules").json() == []
        unknown = client.post(
            f"/suggestions/{sid}/decide",
            json={"decision": "edit_then_accept", "edits": {"colour": "blue"}},
        )
        assert unknown.status_code == 422
        good = client.post(
            f"/suggestions/{sid}/decide",
            json={
                "decision": "edit_then_accept",
                "edits": {
                    "name": "Water bill",
                    "amount_type": "fixed",
                    "amount": "-42.00",
                    "category": "expenses.housing",
                },
            },
        )
        assert good.status_code == 200, good.text
        schedule = good.json()["schedule"]
        assert schedule["name"] == "Water bill"
        assert schedule["amount_type"] == "fixed"
        assert Decimal(schedule["amount"]) == Decimal("-42.00")
        assert good.json()["suggestion"]["draft"]["name"] == "Water bill"  # the applied draft

    def test_propose_true_create_schedule_is_a_suggestion(
        self, seeded: None, client: TestClient
    ) -> None:
        agent = client.post("/tokens", json={"name": "agent", "scopes": ["read", "write"]})
        headers = {"Authorization": f"Bearer {agent.json()['token']}"}
        body = {
            "name": "Internet",
            "account": "assets.checking",
            "rrule": "FREQ=MONTHLY;BYMONTHDAY=3",
            "amount": "-80.00",
            "payee": "Comcast",
            "propose": True,
        }
        first = client.post("/schedules", json=body, headers=headers)
        assert first.status_code == 201, first.text
        assert first.json()["status"] == "proposed"
        sid = first.json()["suggestion_id"]
        # The same proposal again refreshes the row rather than duplicating.
        second = client.post("/schedules", json=body, headers=headers)
        assert second.json()["suggestion_id"] == sid
        inbox = client.get("/suggestions").json()
        assert [r["id"] for r in inbox] == [sid]
        assert inbox[0]["source"] == "agent"
        assert inbox[0]["seen_count"] == 2
        # A read-only token cannot decide; a write principal can.
        reader = client.post("/tokens", json={"name": "reader", "scopes": ["read"]})
        denied = client.post(
            f"/suggestions/{sid}/decide",
            json={"decision": "accept"},
            headers={"Authorization": f"Bearer {reader.json()['token']}"},
        )
        assert denied.status_code == 403
        accepted = client.post(f"/suggestions/{sid}/decide", json={"decision": "accept"})
        assert accepted.json()["schedule"]["source"] == "agent"
        assert accepted.json()["schedule"]["status"] == "active"

    def test_an_agent_proposal_has_no_confidence_and_keeps_its_note(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        """#250: a proposal was stored at confidence 1.000, so its card said
        "100% sure" with nothing behind it. Nothing scores an agent's
        proposal, so it carries NULL and sorts after scored rows; the note the
        agent attaches is its evidence, encrypted with the rest."""
        _seed_recurring(migrated_engine, "Netflix")
        assert _detect(client)["created"] == 1
        agent = client.post("/tokens", json={"name": "agent", "scopes": ["read", "write"]})
        headers = {"Authorization": f"Bearer {agent.json()['token']}"}
        note = "Statement 2026-09: Example Fiber internet, 80.00 due on the 3rd"
        proposed = client.post(
            "/schedules",
            json={
                "name": "Internet",
                "account": "assets.checking",
                "rrule": "FREQ=MONTHLY;BYMONTHDAY=3",
                "amount": "-80.00",
                "payee": "Example Fiber",
                "note": f"  {note}  ",
                "propose": True,
            },
            headers=headers,
        )
        assert proposed.status_code == 201, proposed.text
        sid = proposed.json()["suggestion_id"]

        detected, agents = client.get("/suggestions").json()
        assert detected["source"] == "detected" and detected["confidence"] is not None
        assert (agents["id"], agents["source"], agents["confidence"]) == (sid, "agent", None)
        assert agents["evidence_summary"]["note"] == note
        assert "note" not in detected["evidence_summary"]
        assert client.get(f"/suggestions/{sid}").json()["evidence"] == {"note": note}
        with migrated_engine.connect() as conn:
            stored = conn.execute(
                text("SELECT evidence_enc, confidence FROM suggestions WHERE id = :id"),
                {"id": sid},
            ).one()
        assert stored.confidence is None
        assert note.encode() not in bytes(stored.evidence_enc)
        (created,) = client.get(
            "/audit", params={"action": "suggestion.create", "entity_id": sid}
        ).json()["items"]
        assert created["before_after"]["after"]["confidence"] is None
        events = client.get("/events", params={"type": "suggestion.created"}).json()
        assert [e["payload"]["confidence"] for e in events if e["entity_id"] == sid] == [None]

    def test_a_note_past_200_characters_is_refused(self, seeded: None, client: TestClient) -> None:
        agent = client.post("/tokens", json={"name": "agent", "scopes": ["read", "write"]})
        headers = {"Authorization": f"Bearer {agent.json()['token']}"}
        body = {
            "name": "Internet",
            "account": "assets.checking",
            "rrule": "FREQ=MONTHLY;BYMONTHDAY=3",
            "amount": "-80.00",
            "payee": "Example Fiber",
            "note": "x" * 201,
        }
        assert client.post("/schedules", json=body, headers=headers).status_code == 422
        # The service enforces the same bound for MCP, which has no request model.
        assert service.agent_note("x" * 200) == "x" * 200
        assert service.agent_note("   ") is None
        with pytest.raises(service.SuggestionError, match="200 characters or fewer"):
            service.agent_note("x" * 201)
        assert client.get("/suggestions").json() == []

    def test_a_proposal_for_a_scheduled_payee_or_counterparty_is_refused(
        self, seeded: None, client: TestClient
    ) -> None:
        """#250: an agent could land an open duplicate of a payee the account
        already has a schedule for. The propose path now refuses it, naming
        the schedule, and lands nothing; a direct create is unaffected."""
        internet = {
            "name": "Internet",
            "account": "assets.checking",
            "rrule": "FREQ=MONTHLY;BYMONTHDAY=3",
            "amount": "-80.00",
            "payee": "Example Fiber",
        }
        card = {
            "name": "Card payment",
            "account": "assets.checking",
            "rrule": "FREQ=MONTHLY;BYMONTHDAY=20",
            "amount": "-500.00",
            "counterparty_account": "liabilities.citicard",
        }
        made = {}
        for body in (internet, card):
            resp = client.post("/schedules", json=body)
            assert resp.status_code == 201, resp.text
            made[body["name"]] = resp.json()["id"]
        agent = client.post("/tokens", json={"name": "agent", "scopes": ["read", "write"]})
        headers = {"Authorization": f"Bearer {agent.json()['token']}"}

        # A different name and amount for the same payee is still the payee.
        again = client.post(
            "/schedules",
            json={
                **internet,
                "propose": True,
                "name": "Fiber",
                "amount": "-85.00",
                "payee": "EXAMPLE FIBER",
            },
            headers=headers,
        )
        assert again.status_code == 422, again.text
        detail = again.json()["detail"]
        assert "assets.checking already has a schedule for EXAMPLE FIBER" in detail
        assert f"'Internet' (active, id {made['Internet']})" in detail
        transfer = client.post(
            "/schedules", json={**card, "name": "Citi autopay", "propose": True}, headers=headers
        )
        assert transfer.status_code == 422, transfer.text
        assert "already has a schedule for liabilities.citicard" in transfer.json()["detail"]
        assert f"'Card payment' (active, id {made['Card payment']})" in transfer.json()["detail"]
        assert client.get("/suggestions").json() == []
        assert client.get("/audit", params={"action": "suggestion.create"}).json()["items"] == []

        # A paused schedule still covers its payee.
        assert client.post(f"/schedules/{made['Internet']}/pause").status_code == 200
        paused = client.post("/schedules", json={**internet, "propose": True}, headers=headers)
        assert paused.status_code == 422
        assert "(paused," in paused.json()["detail"]
        # Another payee on the same account still proposes.
        other = client.post(
            "/schedules",
            json={**internet, "name": "Water", "payee": "Example Water", "propose": True},
            headers=headers,
        )
        assert other.status_code == 201, other.text

    def test_digest_and_recurring_carry_the_open_count(
        self, seeded: None, client: TestClient, migrated_engine: Engine, db_session: Session
    ) -> None:
        _seed_recurring(migrated_engine, "Netflix")
        _seed_recurring(migrated_engine, "Spotify")
        assert _detect(client)["created"] == 2
        block = digest.whats_new(db_session)["suggestions"]
        assert block["open"] == 2
        assert len(block["top"]) == 2
        assert all("headline" in t for t in block["top"])


class TestFingerprint:
    @settings(
        max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(
        key_a=st.text(min_size=1, max_size=30),
        key_b=st.text(min_size=1, max_size=30),
    )
    def test_fingerprint_is_stable_and_distinguishes_counterparties(
        self, key_a: str, key_b: str
    ) -> None:
        import uuid

        account = uuid.UUID(int=1)
        assert service.schedule_fingerprint(account, key_a) == service.schedule_fingerprint(
            account, key_a
        )
        if key_a != key_b:
            assert service.schedule_fingerprint(account, key_a) != service.schedule_fingerprint(
                account, key_b
            )
        assert service.schedule_fingerprint(
            uuid.UUID(int=2), key_a
        ) != service.schedule_fingerprint(account, key_a)


class TestServiceGuards:
    def test_decide_requires_write_scope(
        self, seeded: None, client: TestClient, migrated_engine: Engine, db_session: Session
    ) -> None:
        _seed_recurring(migrated_engine, "Netflix")
        sid = _detect(client)["suggestions"][0]["suggestion_id"]  # type: ignore[index]
        import uuid

        proposer = db_session.execute(
            text("SELECT id FROM api_tokens LIMIT 1")
        ).scalar_one()  # any row; scopes are what matter
        token = db_session.get(ApiToken, proposer)
        assert token is not None
        token.scopes = ["read"]
        with pytest.raises(service.SuggestionError, match="write"):
            service.decide(db_session, suggestion_id=uuid.UUID(sid), decision="accept", token=token)
        db_session.rollback()
        assert db_session.get(Suggestion, uuid.UUID(sid)) is not None


def _one_sided(engine: Engine, *, account_path: str, amount: str, on: date, payee: str) -> str:
    """A posted one-sided bank row on an account (import origin)."""
    with engine.begin() as conn:
        account_id = conn.execute(
            text("SELECT id FROM accounts WHERE path = :p"), {"p": account_path}
        ).scalar_one()
        cat_id = (
            conn.execute(
                text("SELECT id FROM accounts WHERE path = 'expenses.uncategorized'")
            ).scalar_one_or_none()
            or conn.execute(
                text(
                    "INSERT INTO accounts (name, type, is_virtual, path) VALUES "
                    "('Uncat', 'category_expense', true, 'expenses.uncategorized') RETURNING id"
                )
            ).scalar_one()
        )
        payee_id = (
            conn.execute(
                text("SELECT id FROM payees WHERE name_bidx = :nb"),
                {"nb": crypto.blind_index(payee.lower())},
            ).scalar_one_or_none()
            or conn.execute(
                text("INSERT INTO payees (name_enc, name_bidx) VALUES (:ne, :nb) RETURNING id"),
                {"ne": crypto.encrypt(payee), "nb": crypto.blind_index(payee.lower())},
            ).scalar_one()
        )
        tx_id = conn.execute(
            text(
                "INSERT INTO transactions (date, payee_id, description_enc, status, origin) "
                "VALUES (:d, :p, :desc, 'posted', 'import') RETURNING id"
            ),
            {"d": on, "p": payee_id, "desc": crypto.encrypt(payee.upper())},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO postings (transaction_id, account_id, amount) VALUES "
                "(:tx, :acct, :amt), (:tx, :cat, :neg)"
            ),
            {
                "tx": tx_id,
                "acct": account_id,
                "cat": cat_id,
                "amt": Decimal(amount),
                "neg": -Decimal(amount),
            },
        )
    return str(tx_id)


class TestDetectionV2:
    """Detection v2 through the service (#234): transfers by counterparty,
    the new-liability scan, account scoping, the MCP tool."""

    def test_merged_card_payments_become_a_transfer_suggestion(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        # Four monthly payments, each linked into a merged transfer, varying
        # 3x — a transfer schedule with the card as counterparty, sized from
        # the card's balance.
        for months_ago, amount in ((3, "2100.00"), (2, "6400.00"), (1, "3300.00"), (0, "5050.00")):
            on = _month_day(months_ago, 5)
            out_id = _one_sided(
                migrated_engine,
                account_path="assets.checking",
                amount=f"-{amount}",
                on=on,
                payee="CITI AUTOPAY",
            )
            in_id = _one_sided(
                migrated_engine,
                account_path="liabilities.citicard",
                amount=amount,
                on=on,
                payee="PAYMENT THANK YOU",
            )
            merged = client.post(
                "/matching/confirm-transfer", json={"outgoing_id": out_id, "incoming_id": in_id}
            )
            assert merged.status_code == 200, merged.text

        result = _detect(client)
        assert result["created"] == 1
        sid = result["suggestions"][0]["suggestion_id"]  # type: ignore[index]
        row = client.get(f"/suggestions/{sid}").json()
        assert row["draft"]["account"] == "assets.checking"
        assert row["draft"]["counterparty_account"] == "liabilities.citicard"
        assert row["draft"]["category"] is None
        assert row["draft"]["rrule"] == "FREQ=MONTHLY;BYMONTHDAY=5"
        assert row["draft"]["amount_type"] == "estimated"
        assert row["draft"]["estimate_basis"] == "counterparty_balance"
        assert row["evidence"]["amount"]["low"] == "-6400.0000"
        assert row["source"] == "detected"
        # Only the payer's side is proposed — never a mirror on the card.
        assert len(client.get("/suggestions", params={"kind": "schedule"}).json()) == 1
        assert row["draft"]["dtstart"] == _month_day(0, 5).isoformat()
        # The four manual confirms proposed ONE transfer_link rule (#271): one
        # fingerprint for the pair and payee, refreshed by each repeat confirm.
        (link,) = client.get("/suggestions", params={"kind": "transfer_link"}).json()
        assert link["source"] == "confirm" and link["seen_count"] == 4
        assert link["evidence_summary"]["count"] == 4

    def test_new_liability_scans_one_sided_payments_that_name_it(
        self, admin_client: TestClient, migrated_engine: Engine
    ) -> None:
        admin_client.post(
            "/accounts", json={"name": "Checking", "type": "checking", "path": "assets.checking"}
        )
        for months_ago in (2, 1, 0):
            _one_sided(
                migrated_engine,
                account_path="assets.checking",
                amount="-1455.48",
                on=_month_day(months_ago, 22),
                payee="AMEX EPAYMENT",
            )
            _one_sided(  # unrelated recurring inflow that must NOT be swept in
                migrated_engine,
                account_path="assets.checking",
                amount="2242.96",
                on=_month_day(months_ago, 6),
                payee="ACCENTURE PAYROLL",
            )
        # Adding the card is the trigger: its payments already sit on checking.
        created = admin_client.post(
            "/accounts",
            json={"name": "Amex Bonvoy", "type": "credit_card", "path": "liabilities.amex"},
        )
        assert created.status_code == 201, created.text
        inbox = admin_client.get("/suggestions").json()
        assert len(inbox) == 1
        row = inbox[0]
        assert row["source"] == "account_added"
        assert row["draft"]["account"] == "assets.checking"
        assert row["draft"]["counterparty_account"] == "liabilities.amex"
        assert row["draft"]["amount_type"] == "fixed"
        assert Decimal(row["draft"]["amount"]) == Decimal("-1455.48")
        # A checking account added later scans nothing (not a liability),
        # and the same card mapped again re-suggests nothing new.
        admin_client.post(
            "/accounts", json={"name": "Savings", "type": "savings", "path": "assets.savings"}
        )
        assert len(admin_client.get("/suggestions").json()) == 1

    def test_account_scoping(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        _seed_recurring(migrated_engine, "Netflix")
        client.post(
            "/accounts", json={"name": "Savings", "type": "savings", "path": "assets.savings"}
        )
        for months_ago in (2, 1, 0):
            _one_sided(
                migrated_engine,
                account_path="assets.savings",
                amount="-9.99",
                on=_month_day(months_ago, 10),
                payee="Spotify",
            )
        # REST detect is global; the service scopes by account.
        from purser import schedules as schedules_service
        from purser.db import get_sessionmaker

        with get_sessionmaker()() as session:
            only_savings = schedules_service.detect_schedules(
                session, actor_token_id=None, account_ref="assets.savings"
            )
            names = {r.draft["name"] for r in only_savings}
            session.rollback()
        assert names == {"Spotify"}
        assert _detect(client)["created"] == 2

    def test_nightly_job_registered(self) -> None:
        from purser import scheduler as scheduler_module

        scheduler_module._JOBS.clear()
        scheduler_module._register_builtin_jobs()
        times = {job_id: cron for job_id, cron, _ in scheduler_module._JOBS}
        assert times["schedules.detect_daily"] == {"hour": 7, "minute": 5}
