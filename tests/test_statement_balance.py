# ruff: noqa: F811  (pytest fixtures re-exported from sibling modules)
"""Card statement balances on payment instances (issue #351, decision 80).

A card-payment schedule estimates from the card's CURRENT balance
(``counterparty_balance``); the statement balance a user actually pays is
recorded per instance — by a person in the app or by an agent over MCP, one
call addressed by card and due date — and every forecast surface says where
each amount comes from (``basis``).
"""

import itertools
import json
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, text

from tests.test_mcp import (  # noqa: F401 (fixture re-export)
    TODAY,
    LiveServer,
    _mcp_call,
    live_server,
    seeded_tokens,
)
from tests.test_mcp_apps import _mcp_call_raw, _read_resource
from tests.test_schedules import (  # noqa: F401 (fixture re-export)
    _card_purchase,
    _import_row,
    _legs,
    _pay_the_card,
    seeded,
    with_card,
)

CARD = "liabilities.citicard"


def _instances(client: TestClient, schedule_id: str) -> list[dict[str, Any]]:
    return list(client.get(f"/schedules/{schedule_id}").json()["instances"])


def _record(client: TestClient, **body: object) -> Any:
    return client.post("/schedules/statement-balance", json=body)


def _audit(client: TestClient, action: str) -> list[dict[str, Any]]:
    return list(client.get("/audit", params={"action": action}).json()["items"])


def _statement_rows(engine: Engine) -> dict[str, Any]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT transaction_id::text, statement_balance, statement_date, source"
                " FROM instance_statements"
            )
        ).all()
    return {r[0]: r for r in rows}


def _card_schedule(
    client: TestClient, *, rrule: str, dtstart: date, card: str = CARD, name: str = "Citi payment"
) -> str:
    resp = client.post(
        "/schedules",
        json={
            "name": name,
            "account": "assets.checking",
            "counterparty_account": card,
            "rrule": rrule,
            "dtstart": dtstart.isoformat(),
            "amount_type": "estimated",
            "estimate_basis": "counterparty_balance",
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


class TestRecordStatement:
    def test_by_id_sets_both_legs_pins_and_the_next_cycle_keeps_the_balance(
        self, with_card: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        _card_purchase(client, "-340.00")
        sid = _pay_the_card(client)
        first, second = _instances(client, sid)[:2]
        assert (first["basis"], first["statement_balance"]) == ("counterparty_balance", None)
        closed = (date.today() - timedelta(days=20)).isoformat()

        resp = _record(
            client,
            transaction_id=first["transaction_id"],
            statement_balance="300.00",
            statement_date=closed,
        )
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert out == {
            **out,
            "transaction_id": first["transaction_id"],
            "schedule_id": sid,
            "amount": "-300.0000",
            "previous_amount": "-340.0000",
            "pinned": True,
            "basis": "statement",
            "statement_balance": "300.0000",
            "statement_date": closed,
            "statement_source": "human",
        }
        assert _legs(client, first["transaction_id"]) == {
            "assets.checking": Decimal("-300.00"),
            CARD: Decimal("300.00"),
        }

        # The card moves: the recorded payment stays put, the next cycle
        # keeps following the balance.
        _card_purchase(client, "-60.00")
        client.post("/schedules/materialize")
        live = {i["transaction_id"]: i for i in _instances(client, sid)}
        assert live[first["transaction_id"]]["amount"] == "-300.0000"
        assert live[first["transaction_id"]]["basis"] == "statement"
        assert live[second["transaction_id"]]["amount"] == "-400.0000"
        assert live[second["transaction_id"]]["basis"] == "counterparty_balance"
        assert live[second["transaction_id"]]["statement_balance"] is None

        (row,) = _audit(client, "schedule.statement_balance")
        assert row["entity_id"] == first["transaction_id"]
        assert row["before_after"]["before"] is None
        assert row["before_after"]["after"] == {
            "statement_balance": "300.0000",
            "statement_date": closed,
            "source": "human",
            "previous_amount": "-340.0000",
            "amount": "-300.0000",
        }
        (update,) = [
            a
            for a in _audit(client, "transaction.update")
            if a["entity_id"] == first["transaction_id"]
        ]
        assert update["request_id"] == row["request_id"]
        assert set(_statement_rows(migrated_engine)) == {first["transaction_id"]}

    def test_by_card_and_due_date_takes_the_nearest_payment(
        self, with_card: None, client: TestClient
    ) -> None:
        _card_purchase(client, "-500.00")
        start = date.today() + timedelta(days=2)
        sid = _card_schedule(client, rrule="FREQ=WEEKLY", dtstart=start)
        by_date = {i["date"]: i for i in _instances(client, sid)}
        target = by_date[(start + timedelta(days=7)).isoformat()]

        resp = _record(
            client,
            account=CARD,
            due_date=(start + timedelta(days=9)).isoformat(),
            statement_balance="412.17",
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["transaction_id"] == target["transaction_id"]
        assert resp.json()["amount"] == "-412.1700"
        assert [i["basis"] for i in _instances(client, sid)].count("statement") == 1

        # Outside the 14-day window: refused, naming the live payments.
        far = (start + timedelta(days=200)).isoformat()
        miss = _record(client, account=CARD, due_date=far, statement_balance="1.00")
        assert miss.status_code == 422
        assert "within 14 days" in miss.json()["detail"]
        assert start.isoformat() in miss.json()["detail"]

    def test_an_equally_near_pair_is_refused_not_guessed(
        self, with_card: None, client: TestClient
    ) -> None:
        _card_purchase(client, "-80.00")
        start = date.today() + timedelta(days=1)
        _card_schedule(client, rrule="FREQ=DAILY;INTERVAL=4", dtstart=start)
        resp = _record(
            client,
            account=CARD,
            due_date=(start + timedelta(days=2)).isoformat(),
            statement_balance="75.00",
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert "equally near" in detail
        assert start.isoformat() in detail
        assert (start + timedelta(days=4)).isoformat() in detail

    def test_refusals(self, with_card: None, client: TestClient) -> None:
        _card_purchase(client, "-340.00")
        sid = _pay_the_card(client)
        first, second = _instances(client, sid)[:2]
        tid = first["transaction_id"]
        client.post(
            "/accounts", json={"name": "Car loan", "type": "loan", "path": "liabilities.carloan"}
        )
        loan_payment = client.post(
            "/schedules",
            json={
                "name": "Car loan",
                "account": "assets.checking",
                "counterparty_account": "liabilities.carloan",
                "rrule": "FREQ=WEEKLY",
                "amount": "-250.00",
            },
        ).json()["id"]
        bill = client.post(
            "/schedules",
            json={
                "name": "Rent",
                "account": "assets.checking",
                "category": "expenses.housing",
                "rrule": "FREQ=WEEKLY",
                "amount": "-900.00",
            },
        ).json()["id"]
        due = first["date"]

        def refused(body: dict[str, object], phrase: str) -> None:
            resp = _record(client, **body)
            assert resp.status_code == 422, resp.text
            assert phrase in str(resp.json()["detail"]), resp.json()

        refused({"account": "assets.checking", "due_date": due, "statement_balance": "1"}, "not a")
        refused(
            {"account": "liabilities.carloan", "due_date": due, "statement_balance": "1"},
            "override_instance",
        )
        for other in (loan_payment, bill):
            refused(
                {
                    "transaction_id": _instances(client, other)[0]["transaction_id"],
                    "statement_balance": "1",
                },
                "only on a payment to a credit card",
            )
        refused({"transaction_id": tid, "statement_balance": "0"}, "skip_instance")
        refused({"transaction_id": tid, "statement_balance": "-300.00"}, "skip_instance")
        refused({"transaction_id": tid, "statement_balance": "300.00001"}, "decimal places")
        refused(
            {
                "transaction_id": tid,
                "statement_balance": "300.00",
                "statement_date": (date.today() + timedelta(days=1)).isoformat(),
            },
            "in the future",
        )
        refused({"statement_balance": "300.00"}, "address the payment")
        refused(
            {"transaction_id": tid, "account": CARD, "statement_balance": "1"},
            "address the payment",
        )
        refused({"account": CARD, "statement_balance": "1"}, "due_date is required")
        refused({"transaction_id": tid, "due_date": due, "statement_balance": "1"}, "leave it out")
        # A skipped (void) instance is not live.
        assert client.post(f"/transactions/{second['transaction_id']}/void").status_code == 200
        refused(
            {"transaction_id": second["transaction_id"], "statement_balance": "1"},
            "not a live schedule instance",
        )
        # A statement cannot close after the payment is due.
        moved = (date.today() - timedelta(days=10)).isoformat()
        assert client.patch(f"/transactions/{tid}", json={"date": moved}).status_code == 200
        refused(
            {
                "transaction_id": tid,
                "statement_balance": "300.00",
                "statement_date": (date.today() - timedelta(days=5)).isoformat(),
            },
            "after the payment's due date",
        )
        # Commit scope.
        agent = client.post("/tokens", json={"name": "agent", "scopes": ["read"]})
        headers = {"Authorization": f"Bearer {agent.json()['token']}"}
        resp = client.post(
            "/schedules/statement-balance",
            json={"transaction_id": tid, "statement_balance": "300.00"},
            headers=headers,
        )
        assert resp.status_code == 403
        # Nothing above recorded anything or moved the payment's amount.
        assert not _audit(client, "schedule.statement_balance")
        assert _legs(client, tid)["assets.checking"] == Decimal("-340.00")

    def test_rerecording_replaces_and_an_override_or_reset_takes_over(
        self, with_card: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        _card_purchase(client, "-340.00")
        sid = _pay_the_card(client)
        tid = _instances(client, sid)[0]["transaction_id"]
        assert _record(client, transaction_id=tid, statement_balance="300.00").status_code == 200
        again = _record(
            client,
            transaction_id=tid,
            statement_balance="310.50",
            statement_date=(date.today() - timedelta(days=3)).isoformat(),
        )
        assert again.status_code == 200, again.text
        assert (again.json()["amount"], again.json()["previous_amount"]) == (
            "-310.5000",
            "-300.0000",
        )
        latest, _first = _audit(client, "schedule.statement_balance")  # newest first
        assert latest["before_after"]["before"] == {
            "statement_balance": "300.0000",
            "statement_date": None,
            "source": "human",
        }
        assert latest["before_after"]["after"]["statement_balance"] == "310.5000"
        assert len(_statement_rows(migrated_engine)) == 1

        # A generic override to another amount: the statement stays on record,
        # but the amount is now the user's.
        assert client.patch(f"/transactions/{tid}", json={"amount": "-299.99"}).status_code == 200
        (row,) = _instances(client, sid)[:1]
        assert (row["basis"], row["pinned"], row["statement_balance"]) == (
            "override",
            True,
            "310.5000",
        )

        # Reset hands it back to the current balance and drops the statement.
        assert client.post(f"/transactions/{tid}/reset-instance").status_code == 200
        (row,) = _instances(client, sid)[:1]
        assert (row["basis"], row["pinned"], row["amount"], row["statement_balance"]) == (
            "counterparty_balance",
            False,
            "-340.0000",
            None,
        )
        assert _statement_rows(migrated_engine) == {}
        (reset,) = _audit(client, "schedule.instance_reset")
        assert reset["before_after"]["before"]["statement"]["statement_balance"] == "310.5000"

    def test_the_feed_taking_a_pending_back_keeps_the_statement_and_unmatch_does_not(
        self, with_card: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        """``release_settlement`` copies the statement onto the clone when it
        keeps the pin (the feed voided the pending the payment matched,
        decision 69), and a user's unmatch re-opens the payment on the rule
        as before."""
        from purser import matching
        from purser.db import get_sessionmaker
        from purser.models import Transaction

        _card_purchase(client, "-340.00")
        sid = _pay_the_card(client)
        first = _instances(client, sid)[0]
        tid, slot = first["transaction_id"], first["occurrence_date"]
        closed = (date.today() - timedelta(days=20)).isoformat()
        recorded = _record(
            client, transaction_id=tid, statement_balance="300.00", statement_date=closed
        )
        assert recorded.status_code == 200, recorded.text

        def pay_with_a_pending() -> tuple[str, str]:
            pending = _import_row(
                migrated_engine,
                account_path="assets.checking",
                amount="-300.00",
                on=date.today(),
                payee="Citi Autopay",
                description="CITI AUTOPAY",
                status="pending",
            )
            live = {i["occurrence_date"]: i for i in _instances(client, sid)}[slot]
            resp = client.post(
                "/matching/match",
                json={"expected_id": live["transaction_id"], "posted_id": pending},
            )
            assert resp.status_code == 200, resp.text
            assert slot not in {i["occurrence_date"] for i in _instances(client, sid)}
            return str(resp.json()["id"]), pending

        _match_id, pending = pay_with_a_pending()
        with get_sessionmaker()() as session:
            row = session.get(Transaction, uuid.UUID(pending))
            assert row is not None
            live = matching.live_match_on(session, row)
            assert live is not None
            row.status = "void"
            session.flush()
            assert matching.settle_voided_pending(session, live, successor=None) == "released"
            session.commit()
        clone = {i["occurrence_date"]: i for i in _instances(client, sid)}[slot]
        assert clone["transaction_id"] != tid
        assert (clone["amount"], clone["pinned"], clone["basis"]) == (
            "-300.0000",
            True,
            "statement",
        )
        assert (clone["statement_balance"], clone["statement_date"]) == ("300.0000", closed)
        assert clone["statement_source"] == "human"

        match_id, _pending = pay_with_a_pending()
        assert client.post(f"/matching/{match_id}/unmatch").status_code == 200
        reopened = {i["occurrence_date"]: i for i in _instances(client, sid)}[slot]
        assert (reopened["pinned"], reopened["statement_balance"]) == (False, None)
        client.post("/schedules/materialize")
        reopened = {i["occurrence_date"]: i for i in _instances(client, sid)}[slot]
        assert (reopened["amount"], reopened["basis"]) == ("-340.0000", "counterparty_balance")

    def test_deleting_the_transaction_takes_the_statement_with_it(
        self, with_card: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        from purser.admin.account_delete import guarded_delete
        from purser.db import get_sessionmaker

        _card_purchase(client, "-340.00")
        sid = _pay_the_card(client)
        tid = _instances(client, sid)[0]["transaction_id"]
        assert _record(client, transaction_id=tid, statement_balance="300.00").status_code == 200
        with get_sessionmaker()() as session:
            counts = guarded_delete(session, [uuid.UUID(tid)])
            session.commit()
        assert counts["transactions_deleted"] == 1
        assert _statement_rows(migrated_engine) == {}


@pytest.mark.parametrize("record", [False, True])
def test_the_exact_statement_lets_the_card_payment_vouch_for_its_bank_pair(
    clean_db: None,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    migrated_engine: Engine,
    record: bool,
) -> None:
    """Decision 65 vouches only for an instance's exact amount, so a payment
    estimated from the card's current balance links nothing; recording the
    statement it actually pays is what lets the schedule link the two bank
    rows (opaque descriptors: no transfer keyword, no account name) and
    settle the instance."""
    from purser import ledger as ledger_service
    from purser.core import ledger as core
    from purser.db import get_sessionmaker
    from tests.test_schedule_transfer_signal import _auto_links, _row
    from tests.test_transfers import SETUP_TOKEN, Harness, _epoch, two_account_payload

    harness = Harness(client, monkeypatch)
    harness.use_payload(two_account_payload([], []))
    resp = client.post("/connections", json={"setup_token": SETUP_TOKEN})
    assert resp.status_code == 201, resp.text
    connection_id = resp.json()["id"]
    assert client.post(f"/connections/{connection_id}/sync").status_code == 200
    for external_id, kind, path in (
        ("chk-1", "checking", "assets.checking"),
        ("sav-1", "credit_card", CARD),
    ):
        resp = client.post(
            f"/connections/{connection_id}/map",
            json={
                "external_id": external_id,
                "create": {"name": external_id, "type": kind, "path": path},
            },
        )
        assert resp.status_code == 200, resp.text
    schedule_id = _card_schedule(
        client, rrule="FREQ=MONTHLY;BYMONTHDAY=20", dtstart=date(2026, 6, 20)
    )
    # The June slot, written as materialization writes it, at the
    # current-balance estimate (the card owed more than the statement).
    with get_sessionmaker()() as session:
        instance = ledger_service.create_transfer(
            session,
            from_account_ref="assets.checking",
            to_account_ref=CARD,
            amount=Decimal("412.33"),
            date=date(2026, 6, 20),
            description="Citi payment",
            requested_status=core.Status.EXPECTED,
            origin=core.Origin.RULE,
            token=None,
            schedule_id=uuid.UUID(schedule_id),
            occurrence_date=date(2026, 6, 20),
        )
        session.commit()
        instance_id = str(instance.id)
    if record:
        resp = _record(
            client,
            transaction_id=instance_id,
            statement_balance="275.00",
            statement_date="2026-05-28",
        )
        assert resp.status_code == 200, resp.text

    out_row = {
        "id": "pay-out",
        "posted": _epoch(20),
        "amount": "-275.00",
        "description": "ACH DEBIT WEB 0042",
    }
    in_row = {
        "id": "pay-in",
        "posted": _epoch(21),
        "amount": "275.00",
        "description": "ELECTRONIC CREDIT RCVD",
    }
    harness.use_payload(two_account_payload([out_row], [in_row]))
    harness.sync(connection_id)

    if not record:
        assert _auto_links(migrated_engine) == []
        assert _row(migrated_engine, instance_id).superseded_by is None
        return
    (link,) = _auto_links(migrated_engine)
    assert link["signal"] == f"schedule:{schedule_id}"
    assert _row(migrated_engine, instance_id).status == "void"
    assert _row(migrated_engine, instance_id).superseded_by is not None


class TestForecastBasis:
    def test_rest_forecast_names_every_basis(self, with_card: None, client: TestClient) -> None:
        _card_purchase(client, "-340.00")
        sid = _pay_the_card(client)
        first, second = _instances(client, sid)[:2]
        assert (
            _record(
                client, transaction_id=first["transaction_id"], statement_balance="300.00"
            ).status_code
            == 200
        )
        rent = client.post(
            "/schedules",
            json={
                "name": "Rent",
                "account": "assets.checking",
                "category": "expenses.housing",
                "rrule": "FREQ=MONTHLY;BYMONTHDAY=1",
                "amount": "-900.00",
            },
        ).json()["id"]
        rent_first = _instances(client, rent)[0]["transaction_id"]
        moved = date.fromisoformat(_instances(client, rent)[0]["date"]) + timedelta(days=1)
        client.patch(f"/transactions/{rent_first}", json={"date": moved.isoformat()})
        one_off = client.post(
            "/transactions",
            json={
                "account": "assets.checking",
                "category": "expenses.housing",
                "amount": "-42.00",
                "date": (date.today() + timedelta(days=5)).isoformat(),
                "payee": "Plumber",
                "status": "expected",
            },
        ).json()["id"]

        forecast = client.get("/forecast", params={"account": "assets.checking", "days": 120})
        assert forecast.status_code == 200, forecast.text
        events = [e for d in forecast.json()["days"] for e in d["events"]]
        by_id = {e["transaction_id"]: e for e in events if e["transaction_id"]}
        assert by_id[first["transaction_id"]]["basis"] == "statement"
        assert by_id[second["transaction_id"]]["basis"] == "counterparty_balance"
        assert by_id[rent_first]["basis"] == "override"
        assert by_id[one_off]["basis"] == "fixed"
        projected = [e for e in events if e["projected"]]
        assert projected and {e["basis"] for e in projected} == {"counterparty_balance", "fixed"}
        # The card's own forecast sees the same instance from its side.
        card = client.get("/forecast", params={"account": CARD, "days": 30}).json()
        card_events = {e["transaction_id"]: e for d in card["days"] for e in d["events"]}
        assert card_events[first["transaction_id"]]["basis"] == "statement"
        assert Decimal(card_events[first["transaction_id"]]["amount"]) == Decimal("300")


# --------------------------------------------------------------------------
# Guardrail 3: random statements keep the instance zero-sum
# --------------------------------------------------------------------------

_CARD_SEQ = itertools.count()
_STATEMENTS = st.decimals(
    min_value=Decimal("0.0001"), max_value=Decimal("99999.9999"), places=4, allow_nan=False
)


class TestStatementInvariants:
    @settings(
        max_examples=10, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(statement=_STATEMENTS, purchase=st.integers(min_value=1, max_value=999))
    def test_a_statement_is_both_legs_and_survives_the_estimate(
        self, seeded: None, client: TestClient, statement: Decimal, purchase: int
    ) -> None:
        # The fixture runs once for all examples: each example pays its own card.
        card = f"liabilities.card_{next(_CARD_SEQ)}"
        resp = client.post("/accounts", json={"name": "Card", "type": "credit_card", "path": card})
        assert resp.status_code == 201, resp.text
        _card_purchase(client, "-340.00", card)
        sid = _pay_the_card(client, card)
        first, second = _instances(client, sid)[:2]
        resp = _record(
            client, account=card, due_date=first["date"], statement_balance=str(statement)
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["transaction_id"] == first["transaction_id"]

        def check() -> None:
            legs = _legs(client, first["transaction_id"])
            assert sum(legs.values()) == 0
            assert legs == {"assets.checking": -statement, card: statement}
            row = {i["transaction_id"]: i for i in _instances(client, sid)}
            assert (
                row[first["transaction_id"]]["basis"],
                row[first["transaction_id"]]["pinned"],
            ) == (
                "statement",
                True,
            )

        check()
        _card_purchase(client, f"-{purchase}.00", card)
        client.post("/schedules/materialize")
        check()
        follower = {i["transaction_id"]: i for i in _instances(client, sid)}[
            second["transaction_id"]
        ]
        assert Decimal(follower["amount"]) == -(Decimal("340") + purchase)


# --------------------------------------------------------------------------
# MCP: one call from a statement email; basis on the forecast surfaces
# --------------------------------------------------------------------------


def _mcp_card(client: TestClient, *, owed: str = "412.00") -> tuple[str, list[dict[str, Any]]]:
    resp = client.post(
        "/accounts",
        json={"name": "Amex Platinum", "type": "credit_card", "path": "liabilities.amex"},
    )
    assert resp.status_code == 201, resp.text
    client.post(
        "/transactions",
        json={
            "account": "liabilities.amex",
            "category": "expenses.groceries",
            "amount": f"-{owed}",
            "date": TODAY.isoformat(),
            "status": "posted",
        },
    )
    created = client.post(
        "/schedules",
        json={
            "name": "Amex payment",
            "account": "assets.checking",
            "counterparty_account": "liabilities.amex",
            "rrule": "FREQ=WEEKLY",
            "dtstart": (TODAY + timedelta(days=2)).isoformat(),
            "amount_type": "estimated",
            "estimate_basis": "counterparty_balance",
        },
    ).json()
    return str(created["id"]), _instances(client, created["id"])


class TestMcp:
    def test_one_call_from_a_statement_email(
        self, seeded_tokens: dict[str, str], live_server: LiveServer, client: TestClient
    ) -> None:
        sid, instances = _mcp_card(client)
        first = instances[0]
        due = (date.fromisoformat(first["date"]) + timedelta(days=1)).isoformat()
        closed = (TODAY - timedelta(days=21)).isoformat()
        args = {
            "statement_balance": "347.00",
            "account": "liabilities.amex",
            "due_date": due,
            "statement_date": closed,
        }
        (denied,) = _mcp_call(
            live_server.url, seeded_tokens["reader"], [("record_statement_balance", args)]
        )
        assert "error" in denied and "requires scope: write" in denied["error"], denied

        (recorded,) = _mcp_call(
            live_server.url, seeded_tokens["operator"], [("record_statement_balance", args)]
        )
        assert recorded == {
            "transaction_id": first["transaction_id"],
            "schedule": "Amex payment",
            "date": first["date"],
            "amount": "-347.0000",
            "previous_amount": "-412.0000",
            "statement_balance": "347.0000",
            "statement_date": closed,
            "basis": "statement",
            "pinned": True,
        }
        row = _instances(client, sid)[0]
        assert (row["basis"], row["statement_source"]) == ("statement", "agent")
        # One request: the transfer edit, the statement, the tool call.
        (change,) = _audit(client, "schedule.statement_balance")
        (update,) = [
            a
            for a in _audit(client, "transaction.update")
            if a["entity_id"] == first["transaction_id"]
        ]
        assert change["request_id"] == update["request_id"]
        # The denied call is audited too; the recording call shares the request.
        calls = _audit(client, "mcp.record_statement_balance")
        assert change["request_id"] in {c["request_id"] for c in calls}

        bad = [
            ({**args, "statement_balance": "347.00001"}, "4 decimal places"),
            ({**args, "statement_balance": "lots"}, "decimal string"),
            ({**args, "statement_balance": "0"}, "skip_instance"),
            ({**args, "account": "assets.checking"}, "not a credit card"),
            ({**args, "due_date": (TODAY + timedelta(days=300)).isoformat()}, "within 14 days"),
        ]
        results = _mcp_call(
            live_server.url,
            seeded_tokens["operator"],
            [("record_statement_balance", a) for a, _ in bad],
        )
        for (_, phrase), result in zip(bad, results, strict=True):
            assert "error" in result and phrase in result["error"], result

    def test_forecast_events_carry_basis(
        self, seeded_tokens: dict[str, str], live_server: LiveServer, client: TestClient
    ) -> None:
        _sid, instances = _mcp_card(client)
        first, second, third = instances[:3]
        _mcp_call(
            live_server.url,
            seeded_tokens["operator"],
            [
                (
                    "record_statement_balance",
                    {
                        "statement_balance": "347.00",
                        "account": "liabilities.amex",
                        "due_date": first["date"],
                    },
                ),
                (
                    "override_instance",
                    {"transaction_id": second["transaction_id"], "amount": "-399.00"},
                ),
            ],
        )
        got = _mcp_call_raw(
            live_server.url,
            seeded_tokens["reader"],
            "forecast_balance",
            {"account": "assets.checking", "days": 30, "materiality": "300"},
        )
        events = {e["transaction_id"]: e for e in got["structured"]["events"]}
        assert events[first["transaction_id"]]["basis"] == "statement"
        assert events[second["transaction_id"]]["basis"] == "override"
        # An estimate names no basis on MCP: it is the schedule's.
        assert "basis" not in events[third["transaction_id"]]

        daily = json.loads(
            _read_resource(
                live_server.url, seeded_tokens["reader"], got["structured"]["daily_uri"]
            )["text"]
        )
        every = {
            e["transaction_id"]: e
            for d in daily["days"]
            for e in d["events"]
            if e["transaction_id"]
        }
        assert every[first["transaction_id"]]["basis"] == "statement"
        assert every[second["transaction_id"]]["basis"] == "override"
        assert every[third["transaction_id"]]["basis"] == "counterparty_balance"
        assert all("basis" in e for d in daily["days"] for e in d["events"])

    def test_budget_holds_when_every_event_carries_a_basis(
        self, seeded_tokens: dict[str, str], live_server: LiveServer, client: TestClient
    ) -> None:
        """#354's fitter keeps the payload inside the budget whatever the
        keys; this measures a realistic worst case for the new one: long
        names, every materialized instance pinned (basis 'override') and card
        payments recorded from statements (basis 'statement')."""
        names = [
            "Northeastern Regional Electric Utility Monthly Service Charge",
            "Metropolitan Water and Sewer Authority Quarterly Billing Cycle",
            "Employer Payroll Direct Deposit Semi-Monthly Net Compensation",
        ]
        for i, name in enumerate(names):
            resp = client.post(
                "/schedules",
                json={
                    "name": name,
                    "account": "assets.checking",
                    "rrule": "FREQ=WEEKLY",
                    "amount": "2400.00" if i == 2 else f"-{100 + i * 37}.25",
                    "category": "expenses.groceries" if i < 2 else None,
                    "payee": name,
                },
            )
            assert resp.status_code == 201, resp.text
            for instance in _instances(client, resp.json()["id"]):
                assert (
                    client.patch(
                        f"/transactions/{instance['transaction_id']}",
                        json={"amount": "2401.00" if i == 2 else f"-{101 + i * 37}.25"},
                    ).status_code
                    == 200
                )
        _sid, card_instances = _mcp_card(client)
        for instance in card_instances:
            resp = _record(
                client, transaction_id=instance["transaction_id"], statement_balance="388.88"
            )
            assert resp.status_code == 200, resp.text

        got = _mcp_call_raw(
            live_server.url,
            seeded_tokens["reader"],
            "forecast_balance",
            {"account": "assets.checking", "days": 45},
        )
        data = got["structured"]
        size = len(json.dumps(data))
        assert size <= 1900  # the fitted payload's budget (_FORECAST_BUDGET_CHARS)
        assert size / 4 < 500  # guardrail 6, as the contract tests measure it
        assert data["truncated"] is True
        listed = data["events"]
        # The basis costs events, not the budget: the low plus the largest
        # movements still fit, each saying where its amount came from.
        assert len(listed) >= 5
        assert all(e.get("basis") in ("override", "statement") for e in listed)


# --------------------------------------------------------------------------
# Migration 0031 up/down (scratch database, never the suite's)
# --------------------------------------------------------------------------


def test_migration_0031_round_trips(database_url: str) -> None:
    import os

    import purser.config
    from purser import crypto
    from tests.conftest import TEST_ENCRYPTION_KEY

    os.environ["PURSER_ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
    purser.config.get_settings.cache_clear()
    admin = sa.create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text("DROP DATABASE IF EXISTS purser_statement_mig WITH (FORCE)"))
        conn.execute(text("CREATE DATABASE purser_statement_mig"))
    admin.dispose()
    url = database_url.rsplit("/", 1)[0] + "/purser_statement_mig"
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    engine = sa.create_engine(url)

    def tables() -> set[str]:
        with engine.connect() as conn:
            return set(
                conn.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                ).scalars()
            )

    try:
        command.upgrade(cfg, "0031")
        assert "instance_statements" in tables()
        with engine.begin() as conn:
            tx = conn.execute(
                text(
                    "INSERT INTO transactions (date, description_enc, status, origin)"
                    " VALUES (:d, :desc, 'expected', 'rule') RETURNING id"
                ),
                {"d": date(2026, 10, 19), "desc": crypto.encrypt("Synthetic payment")},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO instance_statements (transaction_id, statement_balance, source)"
                    " VALUES (:t, 8347.00, 'agent')"
                ),
                {"t": tx},
            )
        for bad in ("0, 'agent'", "1.00, 'feed'"):
            with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
                other = conn.execute(
                    text(
                        "INSERT INTO transactions (date, status, origin)"
                        " VALUES ('2026-10-19', 'expected', 'rule') RETURNING id"
                    )
                ).scalar_one()
                conn.execute(
                    text(
                        "INSERT INTO instance_statements"
                        f" (transaction_id, statement_balance, source) VALUES (:t, {bad})"
                    ),
                    {"t": other},
                )
        # The row goes with its transaction.
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM transactions WHERE id = :t"), {"t": tx})
            assert conn.execute(text("SELECT COUNT(*) FROM instance_statements")).scalar() == 0

        command.downgrade(cfg, "-1")
        assert "instance_statements" not in tables()
        command.upgrade(cfg, "0031")
        assert "instance_statements" in tables()
    finally:
        engine.dispose()
