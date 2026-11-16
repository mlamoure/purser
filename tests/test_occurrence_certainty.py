# ruff: noqa: F811
"""Occurrence evidence lifecycle (#512); all identities and amounts synthetic."""

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import Engine, text

from purser.core import matching
from tests.test_matching import _imported
from tests.test_mcp import LiveServer, _mcp_call, live_server  # noqa: F401
from tests.test_schedules import seeded  # noqa: F401


def create(
    client: TestClient,
    *,
    amount: str = "-100",
    date_mode: str = "estimated",
    name: str = "Example landscaping",
) -> tuple[str, list[dict]]:
    resp = client.post(
        "/schedules",
        json={
            "name": name,
            "account": "assets.checking",
            "rrule": "FREQ=WEEKLY",
            "dtstart": date.today().isoformat(),
            "amount": amount,
            "amount_type": "estimated",
            "estimate_basis": "last",
            "date_type": date_mode,
            "category": "expenses.housing",
        },
    )
    assert resp.status_code == 201, resp.text
    sid = resp.json()["id"]
    return sid, client.get(f"/schedules/{sid}").json()["instances"]


def evidence(state: str, value: str | None = None, source: str = "invoice") -> dict:
    return {
        "state": state,
        "source": source,
        "as_of": date.today().isoformat(),
        "note": "Synthetic invoice for this occurrence",
        **({"value": value} if value else {}),
    }


def get(client: TestClient, tx: str) -> dict:
    response = client.get(f"/schedules/instances/{tx}/certainty")
    assert response.status_code == 200, response.text
    return response.json()


def update(client: TestClient, tx: str, **changes: dict) -> dict:
    response = client.patch(f"/schedules/instances/{tx}/certainty", json=changes)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize(
    "amount_state,date_state",
    [
        ("estimated", "estimated"),
        ("confirmed", "estimated"),
        ("estimated", "confirmed"),
        ("confirmed", "confirmed"),
    ],
)
def test_independent_dimensions_and_occurrence_isolation(
    seeded: None, client: TestClient, amount_state: str, date_state: str
) -> None:
    sid, rows = create(client)
    tx = rows[0]["transaction_id"]
    first = update(client, tx, amount=evidence(amount_state, "-125"))
    assert first["date_estimated"] is True
    result = update(client, tx, date=evidence(date_state))
    assert result["amount_estimated"] == (amount_state == "estimated")
    assert result["date_estimated"] == (date_state == "estimated")
    assert result["status"] == (
        "confirmed" if amount_state == date_state == "confirmed" else "estimated"
    )
    assert Decimal(result["original"]["amount"]) == Decimal("-100")
    assert client.get(f"/transactions/{tx}").json()["status"] == "expected"
    assert get(client, rows[1]["transaction_id"])["amount_estimated"] is True
    assert get(client, rows[1]["transaction_id"])["date_estimated"] is True
    assert client.get(f"/schedules/{sid}").json()["amount_type"] == "estimated"
    assert client.get(f"/schedules/{sid}").json()["instances"][0]["basis"] == "override"


def test_date_confirmation_does_not_freeze_amount_refresh(seeded: None, client: TestClient) -> None:
    sid, rows = create(client, name="Example payroll", amount="1000")
    tx = rows[0]["transaction_id"]
    moved = (date.today() + timedelta(days=2)).isoformat()
    update(client, tx, date=evidence("confirmed", moved, "payroll"))
    response = client.patch(f"/schedules/{sid}", json={"amount": "1100"})
    assert response.status_code == 200, response.text
    current = get(client, tx)
    assert Decimal(current["amount"]["value"]) == Decimal("1100")
    assert current["date"]["value"] == moved
    assert current["date"]["state"] == "confirmed"
    assert Decimal(current["original"]["amount"]) == Decimal("1000")
    update(client, tx, amount=evidence("confirmed", "1150", "payroll"))
    client.patch(f"/schedules/{sid}", json={"amount": "1200"})
    assert Decimal(get(client, tx)["amount"]["value"]) == Decimal("1150")
    client.post(f"/transactions/{tx}/reset-instance")
    reset = get(client, tx)
    assert Decimal(reset["amount"]["value"]) == Decimal("1200")
    assert Decimal(reset["original"]["amount"]) == Decimal("1000")
    assert any(row["amount"]["state"] == "confirmed" for row in reset["history"])


def test_ambiguous_reimbursement_review_match_unmatch_and_replay(
    seeded: None, client: TestClient, migrated_engine: Engine
) -> None:
    sid, rows = create(client, amount="100", name="Example reimbursement")
    tx = rows[0]["transaction_id"]
    originals = get(client, tx)["original"]
    posted = [
        _imported(
            client,
            migrated_engine,
            "assets.checking",
            "100",
            date.today(),
            payee="Example reimbursement",
        )
        for _ in range(2)
    ]
    stats = client.post("/matching/run").json()
    assert stats["auto_matched"] == 0
    assert stats["proposed"] == 1
    assert get(client, tx)["status"] == "pending_review"
    before = client.get("/forecast", params={"account": "assets.checking", "days": 2}).json()
    assert before["coverage"]["matches_pending"] == 1
    assert not any(e["transaction_id"] == tx for d in before["days"] for e in d["events"])
    proposal = client.get("/matching/proposed").json()[0]
    assert client.post(f"/matching/{proposal['id']}/reject").status_code == 200
    rejected = client.get("/forecast", params={"account": "assets.checking", "days": 2}).json()
    assert any(e["transaction_id"] == tx for d in rejected["days"] for e in d["events"])
    matched = client.post("/matching/match", json={"expected_id": tx, "posted_id": posted[0]})
    assert matched.status_code == 200, matched.text
    result = get(client, tx)
    assert result["status"] == "matched"
    assert result["actual"]["variance"]["days"] == 0
    assert result["original"] == originals
    assert client.post("/matching/run").json()["auto_matched"] == 0
    restored = client.post(f"/matching/{matched.json()['id']}/unmatch")
    assert restored.status_code == 200, restored.text
    current_rows = client.get(f"/schedules/{sid}").json()["instances"]
    clone = next(r for r in current_rows if r["occurrence_date"] == rows[0]["occurrence_date"])
    restored_detail = get(client, clone["transaction_id"])
    assert restored_detail["original"] == originals
    assert any(
        h.get("actual", {}).get("transaction_id") == posted[0] for h in restored_detail["history"]
    )
    client.post("/schedules/materialize")
    after = client.get(f"/schedules/{sid}").json()["instances"]
    assert len([r for r in after if r["occurrence_date"] == rows[0]["occurrence_date"]]) == 1
    with migrated_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM matches WHERE state = 'confirmed' AND posted_id = :p"),
                {"p": posted[0]},
            ).scalar_one()
            == 0
        )


def test_history_cannot_be_confirmation_and_read_token_cannot_write(
    seeded: None, client: TestClient
) -> None:
    _, rows = create(client)
    tx = rows[0]["transaction_id"]
    path = f"/schedules/instances/{tx}/certainty"
    assert (
        client.patch(path, json={"amount": evidence("confirmed", source="history")}).status_code
        == 422
    )
    assert client.patch(path, json={}).status_code == 422
    token = client.post("/tokens", json={"name": "reader", "scopes": ["read"]}).json()["token"]
    assert (
        client.patch(
            path,
            json={"amount": evidence("confirmed")},
            headers={"Authorization": f"Bearer {token}"},
        ).status_code
        == 403
    )


def test_original_survives_override_pause_resume(seeded: None, client: TestClient) -> None:
    sid, rows = create(client)
    tx = rows[0]["transaction_id"]
    original = get(client, tx)["original"]
    client.post(f"/schedules/{sid}/pause")
    client.post(f"/schedules/{sid}/resume")
    first = client.get(f"/schedules/{sid}").json()["instances"][0]
    assert get(client, first["transaction_id"])["original"] == original
    client.patch(f"/transactions/{first['transaction_id']}", json={"amount": "-130"})
    assert get(client, first["transaction_id"])["original"] == original


@given(
    st.decimals(
        min_value=Decimal("0.01"),
        max_value=Decimal("100000"),
        places=2,
        allow_nan=False,
        allow_infinity=False,
    )
)
def test_direction_and_ambiguity_are_hard_matching_invariants(amount: Decimal) -> None:
    expected = matching.MatchInput(uuid.uuid4(), date(2026, 11, 15), -amount, "example")
    refund = matching.MatchInput(uuid.uuid4(), expected.date, amount, "example")
    assert matching.score(expected, refund).tier == matching.Tier.NONE
    candidates = [
        matching.MatchInput(uuid.uuid4(), expected.date, -amount, "example") for _ in range(2)
    ]
    best = matching.best_match(expected, candidates)
    assert best is not None and best.tier == matching.Tier.PROPOSED


def test_card_statement_and_actual_keep_original_on_both_perspectives(
    seeded: None, client: TestClient, migrated_engine: Engine
) -> None:
    card = client.post(
        "/accounts",
        json={"name": "Example card", "type": "credit_card", "path": "liabilities.example"},
    )
    assert card.status_code == 201
    response = client.post(
        "/schedules",
        json={
            "name": "Example card payment",
            "account": "assets.checking",
            "counterparty_account": "liabilities.example",
            "rrule": "FREQ=WEEKLY",
            "dtstart": date.today().isoformat(),
            "amount": "-100",
            "amount_type": "estimated",
            "estimate_basis": "last",
            "date_type": "estimated",
        },
    )
    assert response.status_code == 201, response.text
    sid = response.json()["id"]
    rows = client.get(f"/schedules/{sid}").json()["instances"]
    tx = rows[0]["transaction_id"]
    statement = client.post(
        "/schedules/statement-balance",
        json={
            "transaction_id": tx,
            "statement_balance": "125",
            "statement_date": date.today().isoformat(),
        },
    )
    assert statement.status_code == 200, statement.text
    stated = get(client, tx)
    assert stated["amount"]["state"] == "confirmed"
    assert stated["date"]["state"] == "estimated"
    assert Decimal(stated["original"]["amount"]) == Decimal("-100")
    card_forecast = client.get(
        "/forecast", params={"account": "liabilities.example", "days": 2}
    ).json()
    card_event = next(
        e
        for e in (e for day in card_forecast["days"] for e in day["events"])
        if e.get("transaction_id") == tx
    )
    assert Decimal(card_event["certainty"]["original"]["amount"]) == Decimal("100")
    assert Decimal(card_event["certainty"]["amount"]["value"]) == Decimal("125")
    from purser import db, ledger
    from purser.core import ledger as core

    with db.get_sessionmaker()() as session:
        actual = ledger.create_transfer(
            session,
            from_account_ref="assets.checking",
            to_account_ref="liabilities.example",
            amount=Decimal("125"),
            date=date.today(),
            requested_status=core.Status.POSTED,
            origin=core.Origin.SYSTEM,
            token=None,
        )
        actual_id = str(actual.id)
        session.commit()
    matched = client.post("/matching/match", json={"expected_id": tx, "posted_id": actual_id})
    assert matched.status_code == 200, matched.text
    result = get(client, tx)
    assert Decimal(result["actual"]["variance"]["amount"]) == Decimal("-25")
    for account, balance in (("assets.checking", "-125"), ("liabilities.example", "125")):
        forecast = client.get("/forecast", params={"account": account, "days": 2}).json()
        assert Decimal(forecast["days"][-1]["balance"]) == Decimal(balance)
        assert not any(e["transaction_id"] == tx for d in forecast["days"] for e in d["events"])
    assert (
        client.post(
            "/matching/match",
            json={"expected_id": rows[1]["transaction_id"], "posted_id": actual_id},
        ).status_code
        == 422
    )
    with migrated_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM transactions WHERE id = :id AND status = 'posted'"),
                {"id": actual_id},
            ).scalar_one()
            == 1
        )


def test_real_mcp_independent_updates_and_privacy(
    seeded: None, client: TestClient, live_server: LiveServer
) -> None:
    _, rows = create(client)
    tx = rows[0]["transaction_id"]
    token = client.post(
        "/tokens", json={"name": "occurrence-agent", "scopes": ["read", "write"]}
    ).json()["token"]
    changed, read = _mcp_call(
        live_server.url,
        token,
        [
            (
                "update_occurrence_certainty",
                {"transaction_id": tx, "amount": evidence("confirmed", "-125")},
            ),
            ("get_schedule_occurrence", {"transaction_id": tx}),
        ],
    )
    assert "error" not in changed, changed
    assert read["amount_estimated"] is False
    assert read["date_estimated"] is True
    assert (
        client.put("/config", json={"privacy_purchase": True, "privacy_amount": True}).status_code
        == 200
    )
    (private,) = _mcp_call(
        live_server.url, token, [("get_schedule_occurrence", {"transaction_id": tx})]
    )
    assert "error" not in private, private
    assert "value" not in private["amount"]
    assert "amount" not in private["original"]
    assert "note" not in private["amount"]
    assert private["amount"]["state"] == "confirmed"
    assert private["date_estimated"] is True
    assert "amount" in private["withheld"]


def test_original_columns_immutable_and_evidence_audited(
    seeded: None, client: TestClient, migrated_engine: Engine
) -> None:
    _, rows = create(client)
    tx = rows[0]["transaction_id"]
    update(client, tx, amount=evidence("confirmed", "-130"))
    from sqlalchemy.exc import DBAPIError

    with (
        pytest.raises(DBAPIError, match="original occurrence estimate is immutable"),
        migrated_engine.begin() as conn,
    ):
        conn.execute(
            text("UPDATE instance_certainty SET original_amount = -9 WHERE transaction_id = :t"),
            {"t": tx},
        )
    with migrated_engine.connect() as conn:
        assert (
            conn.execute(
                text(
                    "SELECT count(*) FROM audit_log WHERE action = 'schedule.certainty_update' "
                    "AND entity_id = :t"
                ),
                {"t": tx},
            ).scalar_one()
            == 1
        )


@pytest.mark.parametrize("change", ["direction", "currency", "account"])
def test_incompatible_actuals_cannot_settle_even_manually(
    seeded: None, client: TestClient, migrated_engine: Engine, change: str
) -> None:
    _, rows = create(client)
    path = "assets.checking"
    if change == "account":
        assert (
            client.post(
                "/accounts", json={"name": "Other", "type": "checking", "path": "assets.other"}
            ).status_code
            == 201
        )
        path = "assets.other"
    posted = _imported(
        client,
        migrated_engine,
        path,
        "100" if change == "direction" else "-100",
        date.today(),
        payee="Example landscaping",
        currency="EUR" if change == "currency" else "USD",
    )
    tx = rows[0]["transaction_id"]
    result = client.post("/matching/match", json={"expected_id": tx, "posted_id": posted})
    assert result.status_code == 422, result.text
    assert get(client, tx)["status"] == "estimated"
    assert client.post("/matching/run").json()["auto_matched"] == 0


def test_one_posted_movement_has_one_winner_under_concurrent_matches(
    seeded: None, client: TestClient, migrated_engine: Engine
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy import select

    from purser import db
    from purser import matching as service
    from purser.ledger import LedgerError
    from purser.models import ApiToken

    _, rows = create(client)
    _, others = create(client, name="Another service")
    posted = _imported(
        client,
        migrated_engine,
        "assets.checking",
        "-100",
        date.today(),
        payee="Example landscaping",
    )
    # Use the same authenticated actor as normal writes, with two independent sessions.
    with db.get_sessionmaker()() as session:
        token_id = session.scalar(select(ApiToken.id).where(ApiToken.kind == "session"))
    barrier = Barrier(2)

    def attempt(tx_id: str) -> str:
        with db.get_sessionmaker()() as session:
            token = session.get(ApiToken, token_id)
            assert token is not None
            barrier.wait(timeout=10)
            try:
                service.manual_match(
                    session, expected_id=uuid.UUID(tx_id), posted_id=uuid.UUID(posted), token=token
                )
                session.commit()
                return "matched"
            except LedgerError:
                session.rollback()
                return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [rows[0]["transaction_id"], others[0]["transaction_id"]]))
    assert sorted(results) == ["matched", "refused"]


def test_date_variance_and_projected_uncertainty(
    seeded: None, client: TestClient, migrated_engine: Engine
) -> None:
    sid, rows = create(client)
    tx = rows[0]["transaction_id"]
    posted = _imported(
        client,
        migrated_engine,
        "assets.checking",
        "-125",
        date.today() + timedelta(days=2),
        payee="Example landscaping",
    )
    assert (
        client.post("/matching/match", json={"expected_id": tx, "posted_id": posted}).status_code
        == 200
    )
    actual = get(client, tx)["actual"]
    assert actual["variance"]["days"] == 2
    assert Decimal(actual["variance"]["amount"]) == Decimal("-25")
    forecast = client.get("/forecast", params={"account": "assets.checking", "days": 100}).json()
    projected = [e for d in forecast["days"] for e in d["events"] if e["projected"]]
    assert projected
    assert all(
        e["certainty"]["amount_estimated"] and e["certainty"]["date_estimated"] for e in projected
    )
    assert (
        client.get(f"/schedules/{sid}").json()["matched_instances"][0]["certainty"]["status"]
        == "matched"
    )


def test_upgrade_retains_current_snapshot_without_rewriting_ledger(
    seeded: None, client: TestClient, migrated_engine: Engine
) -> None:
    """Exercise the exact upgrade DDL on populated 0046 tables, rolled back.

    A pre-upgrade override is only a first retained snapshot, never a
    reconstructed original or evidence that an obligation was confirmed.
    """
    from importlib import import_module

    _, rows = create(client)
    tx = rows[0]["transaction_id"]
    moved = date.today() + timedelta(days=2)
    changed = client.patch(
        f"/transactions/{tx}", json={"amount": "-123", "date": moved.isoformat()}
    )
    assert changed.status_code == 200, changed.text
    migration = import_module("migrations.versions.0047_instance_certainty")
    with migrated_engine.connect() as conn:
        transaction = conn.begin()
        try:
            before = (
                conn.execute(text("SELECT to_jsonb(t) FROM transactions t ORDER BY id"))
                .scalars()
                .all()
            )
            postings = (
                conn.execute(text("SELECT to_jsonb(p) FROM postings p ORDER BY id")).scalars().all()
            )
            conn.execute(text(migration.DOWNGRADE_DDL))
            conn.execute(text(migration.UPGRADE_DDL))
            snapshot = (
                conn.execute(
                    text("SELECT * FROM instance_certainty WHERE transaction_id = :t"), {"t": tx}
                )
                .mappings()
                .one()
            )
            assert snapshot["original_amount"] == Decimal("-123")
            assert snapshot["original_date"] == moved
            assert snapshot["occurrence_date"] == date.today()
            assert snapshot["original_amount_basis"] == "override"
            assert snapshot["original_backfilled"] is True
            assert snapshot["amount_state"] is None and snapshot["date_state"] is None
            assert (
                conn.execute(text("SELECT to_jsonb(t) FROM transactions t ORDER BY id"))
                .scalars()
                .all()
                == before
            )
            assert (
                conn.execute(text("SELECT to_jsonb(p) FROM postings p ORDER BY id")).scalars().all()
                == postings
            )
        finally:
            transaction.rollback()


def test_pinned_old_account_leg_remains_inspectable(seeded: None, client: TestClient) -> None:
    sid, rows = create(client)
    tx = rows[0]["transaction_id"]
    assert client.patch(f"/transactions/{tx}", json={"amount": "-123"}).status_code == 200
    assert (
        client.post(
            "/accounts", json={"name": "Secondary", "path": "assets.secondary", "type": "checking"}
        ).status_code
        == 201
    )
    converted = client.patch(f"/transactions/{tx}", json={"category": "[assets.secondary]"})
    assert converted.status_code == 200, converted.text
    response = client.patch(
        f"/transactions/{tx}", json={"category": "expenses.housing", "account": "assets.secondary"}
    )
    assert response.status_code == 200, response.text
    detail = client.get(f"/schedules/{sid}")
    assert detail.status_code == 200, detail.text
    retained = next(i for i in detail.json()["instances"] if i["transaction_id"] == tx)
    assert retained["amount"] is None
    assert retained["certainty"]["amount"]["value"] is None
    assert retained["certainty"]["amount_estimated"] is True
    assert Decimal(retained["certainty"]["original"]["amount"]) == Decimal("-100")
