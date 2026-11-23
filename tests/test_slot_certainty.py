# ruff: noqa: F811
"""Projected-slot evidence is metadata, not a ledger or estimator mutation (#518)."""

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from tests.conftest import today
from tests.test_mcp import LiveServer, _mcp_call, live_server  # noqa: F401
from tests.test_occurrence_certainty import evidence
from tests.test_schedules import seeded  # noqa: F401


def create(
    client: TestClient,
    *,
    amount: str = "-100",
    date_mode: str = "fixed",
    kind: str = "fixed",
    transfer: bool = False,
) -> tuple[str, date]:
    if transfer:
        assert (
            client.post(
                "/accounts",
                json={"name": "Example card", "type": "credit_card", "path": "liabilities.example"},
            ).status_code
            == 201
        )
    response = client.post(
        "/schedules",
        json={
            "name": "Example recurring model",
            "account": "assets.checking",
            "rrule": "FREQ=WEEKLY",
            "dtstart": today().isoformat(),
            "amount": amount,
            "amount_type": kind,
            "date_type": date_mode,
            **({"estimate_basis": "last"} if kind == "estimated" else {}),
            **(
                {"counterparty_account": "liabilities.example"}
                if transfer
                else {"category": "expenses.housing"}
            ),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"], today() + timedelta(days=70)


def path(sid: str, slot: date) -> str:
    return f"/schedules/{sid}/occurrences/{slot}/certainty"


def read(client: TestClient, sid: str, slot: date) -> dict:
    response = client.get(path(sid, slot))
    assert response.status_code == 200, response.text
    return response.json()


def write(client: TestClient, sid: str, slot: date, **body: object) -> dict:
    response = client.patch(path(sid, slot), json=body)
    assert response.status_code == 200, response.text
    return response.json()


def forecast(client: TestClient, account: str = "assets.checking") -> dict:
    response = client.get("/forecast", params={"account": account, "days": 90})
    assert response.status_code == 200, response.text
    return response.json()


def financial_shape(value: dict) -> dict:
    """Ignore certainty alone; all values, IDs, dates, count and balances must match."""
    from copy import deepcopy

    result = deepcopy(value)
    for day in result["days"]:
        for event in day["events"]:
            event.pop("certainty", None)
    return result


@pytest.mark.parametrize(
    "amount,date_mode,transfer",
    [
        ("-100", "estimated", True),
        ("150", "estimated", False),
        ("2000", "fixed", False),
        ("-75", "estimated", False),
    ],
)
def test_fixed_models_accept_slot_estimates_without_changing_forecast(
    seeded: None,
    client: TestClient,
    migrated_engine: Engine,
    amount: str,
    date_mode: str,
    transfer: bool,
) -> None:
    sid, slot = create(client, amount=amount, date_mode=date_mode, transfer=transfer)
    before = forecast(client)
    with migrated_engine.connect() as conn:
        ledger_before = conn.execute(
            text(
                "SELECT id, date, status, occurrence_date, schedule_pinned_at, superseded_by "
                "FROM transactions ORDER BY id"
            )
        ).all()
        postings_before = conn.execute(
            text("SELECT id, transaction_id, account_id, amount FROM postings ORDER BY id")
        ).all()
    result = write(client, sid, slot, amount=evidence("estimated", source="agent"))
    assert result["amount_estimated"] is True
    assert result["date_estimated"] == (date_mode == "estimated")
    assert result["slot"]["transaction_id"] is None
    assert Decimal(result["amount"]["value"]) == Decimal(amount)
    assert result["amount"]["note"] and result["amount"]["set_by"] and result["amount"]["set_at"]
    assert result["original"]["backfilled"] is True
    after = forecast(client)
    assert financial_shape(before) == financial_shape(after)
    event = next(
        e
        for day in after["days"]
        for e in day["events"]
        if (e.get("certainty", {}).get("slot") or {}).get("occurrence_date") == slot.isoformat()
    )
    assert event["basis"] == "fixed"
    assert event["certainty"]["amount_estimated"] is True
    assert read(client, sid, slot + timedelta(days=7))["amount_estimated"] is False
    assert client.get(f"/schedules/{sid}").json()["amount_type"] == "fixed"
    with migrated_engine.connect() as conn:
        assert (
            ledger_before
            == conn.execute(
                text(
                    "SELECT id, date, status, occurrence_date, schedule_pinned_at, superseded_by "
                    "FROM transactions ORDER BY id"
                )
            ).all()
        )
        assert (
            postings_before
            == conn.execute(
                text("SELECT id, transaction_id, account_id, amount FROM postings ORDER BY id")
            ).all()
        )
    if transfer:
        other = next(
            e
            for d in forecast(client, "liabilities.example")["days"]
            for e in d["events"]
            if (e.get("certainty", {}).get("slot") or {}).get("occurrence_date") == slot.isoformat()
        )
        assert Decimal(other["certainty"]["amount"]["value"]) == -Decimal(amount)
        assert Decimal(other["certainty"]["original"]["amount"]) == -Decimal(amount)


def test_evidence_and_first_retained_original_survive_materialization_and_release(
    seeded: None, client: TestClient, migrated_engine: Engine
) -> None:
    from purser import db, schedules
    from purser.models import Schedule

    sid, slot = create(client)
    first = write(
        client,
        sid,
        slot,
        amount=evidence("estimated", source="agent"),
        date=evidence("estimated", source="user"),
    )
    confirmed = write(client, sid, slot, amount=evidence("confirmed", source="invoice"))
    assert confirmed["original"] == first["original"]
    with db.get_sessionmaker()() as session:
        schedule = session.get(Schedule, uuid.UUID(sid))
        assert schedule is not None
        schedules.materialize_schedule(session, schedule, horizon_days=80)
        session.commit()
        assert schedules.materialize_schedule(session, schedule, horizon_days=80) == 0
        session.commit()
    materialized = read(client, sid, slot)
    assert materialized["slot"]["projected"] is False
    assert materialized["original"] == first["original"]
    assert materialized["amount"] == confirmed["amount"] | {"value_overridden": False}
    assert any(h.get("amount", {}).get("state") == "estimated" for h in materialized["history"])
    tx_id = materialized["slot"]["transaction_id"]
    # An estimated occurrence can be released/rebuilt by a rule value edit.
    write(client, sid, slot, amount=evidence("estimated", source="agent"))
    response = client.patch(f"/schedules/{sid}", json={"amount": "-120"})
    assert response.status_code == 200, response.text
    assert (
        client.patch(f"/schedules/{sid}", json={"rrule": "FREQ=WEEKLY;INTERVAL=3"}).status_code
        == 200
    )
    assert client.patch(f"/schedules/{sid}", json={"rrule": "FREQ=WEEKLY"}).status_code == 200
    with db.get_sessionmaker()() as session:
        schedule = session.get(Schedule, uuid.UUID(sid))
        schedules.materialize_schedule(session, schedule, horizon_days=80)
        session.commit()
    rebuilt = read(client, sid, slot)
    assert rebuilt["slot"]["transaction_id"] != tx_id
    assert rebuilt["original"] == first["original"]
    assert rebuilt["amount_estimated"] is True
    assert rebuilt["amount"]["note"] == first["amount"]["note"]
    assert Decimal(rebuilt["amount"]["value"]) == Decimal("-120")
    assert rebuilt["date_estimated"] is True
    with migrated_engine.connect() as conn:
        assert (
            conn.execute(
                text(
                    "SELECT count(*) FROM transactions WHERE schedule_id=:s AND occurrence_date=:d"
                ),
                {"s": sid, "d": slot},
            ).scalar_one()
            == 1
        )


@pytest.mark.parametrize("materialized", [False, True])
def test_slot_address_never_changes_values_and_clear_is_metadata_only(
    seeded: None, client: TestClient, materialized: bool
) -> None:
    sid, projected = create(client)
    slot = today() if materialized else projected
    before = financial_shape(forecast(client))
    for body in (
        {"amount": evidence("estimated", "-999")},
        {"date": evidence("estimated", (slot + timedelta(days=1)).isoformat())},
    ):
        response = client.patch(path(sid, slot), json=body)
        assert response.status_code == 422, response.text
    write(client, sid, slot, amount=evidence("estimated", "-100"))
    assert financial_shape(forecast(client)) == before
    cleared = write(client, sid, slot, clear=["amount"])
    assert cleared["amount_estimated"] is False
    assert financial_shape(forecast(client)) == before
    assert any(h["amount"]["state"] == "estimated" for h in read(client, sid, slot)["history"])


@pytest.mark.parametrize("offset", [-7, 71, 371])
def test_invalid_slots_rejected(seeded: None, client: TestClient, offset: int) -> None:
    sid, _ = create(client)
    response = client.patch(
        path(sid, today() + timedelta(days=offset)), json={"amount": evidence("estimated")}
    )
    assert response.status_code == 422, response.text


def test_first_projected_snapshot_is_immutable_and_notes_encrypted(
    seeded: None, client: TestClient, migrated_engine: Engine
) -> None:
    from sqlalchemy.exc import DBAPIError

    sid, slot = create(client)
    write(client, sid, slot, amount=evidence("estimated"))
    with (
        pytest.raises(DBAPIError, match="original projected estimate is immutable"),
        migrated_engine.begin() as conn,
    ):
        conn.execute(
            text("UPDATE slot_certainty SET original = '{}' WHERE schedule_id=:s"), {"s": sid}
        )
    with migrated_engine.connect() as conn:
        note = bytes(
            conn.execute(
                text("SELECT amount_note_enc FROM slot_certainty WHERE schedule_id=:s"), {"s": sid}
            ).scalar_one()
        )
        assert b"Synthetic invoice" not in note
        assert (
            conn.execute(
                text(
                    "SELECT count(*) FROM audit_log "
                    "WHERE action='schedule.slot_certainty_update' AND entity_id=:s"
                ),
                {"s": sid},
            ).scalar_one()
            == 1
        )


def test_stale_projected_confirmation_becomes_estimate_with_provenance(
    seeded: None, client: TestClient
) -> None:
    sid, slot = create(client)
    first = write(client, sid, slot, amount=evidence("confirmed"))
    assert client.patch(f"/schedules/{sid}", json={"amount": "-125"}).status_code == 200
    result = read(client, sid, slot)
    assert result["amount_estimated"] is True
    assert Decimal(result["amount"]["value"]) == Decimal("-125")
    assert Decimal(result["amount"]["evidence_value"]) == Decimal("-100")
    assert result["original"] == first["original"]
    assert result["amount"]["note"] == first["amount"]["note"]


def test_slot_mcp_write_read_and_privacy(
    seeded: None, client: TestClient, live_server: LiveServer
) -> None:
    sid, slot = create(client)
    token = client.post(
        "/tokens", json={"name": "Example agent", "scopes": ["read", "write"]}
    ).json()["token"]
    address = {"schedule_id": sid, "occurrence_date": slot.isoformat()}
    changed, readback = _mcp_call(
        live_server.url,
        token,
        [
            ("update_occurrence_certainty", address | {"amount": evidence("estimated")}),
            ("get_schedule_occurrence", address),
        ],
    )
    assert "error" not in changed, changed
    assert readback["amount_estimated"] is True
    assert Decimal(readback["original"]["amount"]) == Decimal("-100")
    assert client.patch(f"/schedules/{sid}", json={"amount": "-125"}).status_code == 200
    (private,) = _mcp_call(
        live_server.url,
        token,
        [
            (
                "get_schedule_occurrence",
                {**address, "withhold": ["payee", "description", "amount"]},
            )
        ],
    )
    assert "value" not in private["amount"]
    assert "evidence_value" not in private["amount"]
    assert "amount" not in private["original"]
    assert "note" not in private["amount"]
    assert private["slot"]["occurrence_date"] == slot.isoformat()


def test_fixed_value_with_actual_history_stays_fixed_and_preserves_match(
    seeded: None, client: TestClient, migrated_engine: Engine
) -> None:
    from tests.test_matching import _imported

    sid, slot = create(client)
    tx = client.get(f"/schedules/{sid}").json()["instances"][0]["transaction_id"]
    posted = _imported(
        client,
        migrated_engine,
        "assets.checking",
        "-175",
        today(),
        payee="Example recurring model",
    )
    assert (
        client.post("/matching/match", json={"expected_id": tx, "posted_id": posted}).status_code
        == 200
    )
    before = forecast(client)
    actual_before = client.get(f"/schedules/instances/{tx}/certainty").json()
    write(client, sid, slot, amount=evidence("estimated", source="agent"))
    assert financial_shape(forecast(client)) == financial_shape(before)
    assert Decimal(read(client, sid, slot)["amount"]["value"]) == Decimal("-100")
    assert client.get(f"/schedules/instances/{tx}/certainty").json() == actual_before


def test_history_refresh_keeps_estimate_provenance_and_calculation_basis(
    seeded: None, client: TestClient, migrated_engine: Engine
) -> None:
    from tests.test_matching import _imported

    sid, slot = create(client, kind="estimated")
    rows = client.get(f"/schedules/{sid}").json()["instances"]
    target = date.fromisoformat(rows[1]["occurrence_date"])
    write(client, sid, target, amount=evidence("estimated", source="agent"))
    posted = _imported(
        client,
        migrated_engine,
        "assets.checking",
        "-175",
        today(),
        payee="Example recurring model",
    )
    assert (
        client.post(
            "/matching/match", json={"expected_id": rows[0]["transaction_id"], "posted_id": posted}
        ).status_code
        == 200
    )
    assert client.post("/schedules/materialize").status_code == 200
    detail = read(client, sid, target)
    assert Decimal(detail["amount"]["value"]) == Decimal("-175")
    assert detail["amount_estimated"] is True
    assert detail["amount"]["note"] == evidence("estimated")["note"]
    assert Decimal(detail["amount"]["evidence_value"]) == Decimal("-100")
    write(client, sid, target, amount=evidence("estimated", source="agent"))
    instance = next(
        r
        for r in client.get(f"/schedules/{sid}").json()["instances"]
        if r["occurrence_date"] == target.isoformat()
    )
    assert instance["basis"] == "last"
    assert Decimal(read(client, sid, slot)["amount"]["value"]) == Decimal("-175")


def test_migration_changes_no_financial_or_existing_provenance_rows(
    seeded: None, client: TestClient, migrated_engine: Engine
) -> None:
    from importlib import import_module

    sid, _ = create(client)
    tx = client.get(f"/schedules/{sid}").json()["instances"][0]["transaction_id"]
    assert client.patch(f"/transactions/{tx}", json={"amount": "-123"}).status_code == 200
    migration = import_module("migrations.versions.0048_slot_certainty")
    with migrated_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text(migration.DOWNGRADE_DDL))
            tables = ("transactions", "postings", "schedules", "matches", "instance_certainty")

            def rows(table: str) -> list:
                # Names come only from the closed tuple above.
                return (
                    conn.execute(
                        text(f"SELECT to_jsonb(t) FROM {table} t ORDER BY to_jsonb(t)::text")
                    )
                    .scalars()
                    .all()
                )

            before = {table: rows(table) for table in tables}
            conn.execute(text(migration.UPGRADE_DDL))
            for table in tables:
                after = rows(table)
                if table == "instance_certainty":
                    for row in after:
                        assert row.pop("amount_overridden") is None
                assert after == before[table]
            assert conn.execute(text("SELECT count(*) FROM slot_certainty")).scalar_one() == 0
        finally:
            transaction.rollback()


def test_concurrent_materialization_and_evidence_never_strands_metadata(
    seeded: None, client: TestClient
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from sqlalchemy import select

    from purser import db, occurrences, schedules
    from purser.models import ApiToken, Schedule

    sid, slot = create(client)
    with db.get_sessionmaker()() as session:
        token_id = session.scalar(select(ApiToken.id).where(ApiToken.kind == "session"))
    barrier = Barrier(2)

    def attempt(operation: str) -> None:
        with db.get_sessionmaker()() as session:
            session.execute(text("SET LOCAL lock_timeout = '5s'"))
            schedule = session.get(Schedule, uuid.UUID(sid))
            token = session.get(ApiToken, token_id)
            assert schedule is not None and token is not None
            barrier.wait(timeout=10)
            if operation == "evidence":
                occurrences.update_slot(
                    session,
                    schedule_id=schedule.id,
                    slot=slot,
                    change=occurrences.CertaintyUpdate(
                        amount=occurrences.AmountEvidence(**evidence("estimated"))
                    ),
                    token=token,
                )
            else:
                schedules.materialize_schedule(session, schedule, horizon_days=80)
            session.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(attempt, ["evidence", "materialize"]))
    result = read(client, sid, slot)
    assert result["slot"]["projected"] is False
    assert result["amount_estimated"] is True
    assert result["amount"]["note"] == evidence("estimated")["note"]
    assert result["original"] is not None


def test_first_metadata_on_migrated_instance_keeps_fixed_basis(
    seeded: None, client: TestClient, migrated_engine: Engine
) -> None:
    sid, _ = create(client)
    with migrated_engine.begin() as conn:
        conn.execute(
            text("UPDATE instance_certainty SET amount_overridden=NULL WHERE schedule_id=:s"),
            {"s": sid},
        )
    before = financial_shape(forecast(client))
    write(client, sid, today(), amount=evidence("estimated"))
    assert financial_shape(forecast(client)) == before


def test_projected_evidence_write_scope_and_validation(seeded: None, client: TestClient) -> None:
    sid, slot = create(client)
    invalid = [
        {"amount": evidence("confirmed", source="history")},
        {"amount": evidence("estimated") | {"note": " "}},
        {"amount": evidence("estimated") | {"as_of": (today() + timedelta(days=1)).isoformat()}},
        {"amount": evidence("estimated"), "clear": ["amount"]},
        {},
    ]
    for payload in invalid:
        assert client.patch(path(sid, slot), json=payload).status_code == 422
    token = client.post("/tokens", json={"name": "Read only", "scopes": ["read"]}).json()["token"]
    client.cookies.clear()
    response = client.patch(
        path(sid, slot),
        headers={"Authorization": f"Bearer {token}"},
        json={"amount": evidence("estimated")},
    )
    assert response.status_code == 403, response.text


def test_reset_and_clear_keep_calculation_separate_from_evidence(
    seeded: None, client: TestClient
) -> None:
    sid, _ = create(client)
    tx = client.get(f"/schedules/{sid}").json()["instances"][0]["transaction_id"]

    def basis() -> str:
        return client.get(f"/schedules/{sid}").json()["instances"][0]["basis"]

    assert (
        client.patch(
            f"/schedules/instances/{tx}/certainty", json={"amount": evidence("estimated", "-125")}
        ).status_code
        == 200
    )
    assert basis() == "override"
    write(client, sid, today(), clear=["amount"])
    assert basis() == "override"
    assert client.post(f"/transactions/{tx}/reset-instance").status_code == 200
    write(client, sid, today(), amount=evidence("estimated"))
    assert basis() == "fixed"
    assert Decimal(read(client, sid, today())["amount"]["value"]) == Decimal("-100")


def test_slot_evidence_key_rotation_preserves_notes_history_and_original(
    seeded: None, client: TestClient, migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    import os

    from cryptography.fernet import Fernet
    from sqlalchemy.orm import Session

    from purser import crypto
    from purser.admin.rotate_key import rotate
    from purser.config import get_settings

    sid, slot = create(client)
    write(client, sid, slot, amount=evidence("estimated"), date=evidence("estimated"))
    write(client, sid, slot, amount=evidence("confirmed"))
    old_key = os.environ["PURSER_ENCRYPTION_KEY"]
    new_key = Fernet.generate_key().decode()
    try:
        monkeypatch.setenv("PURSER_ENCRYPTION_KEY", f"{new_key},{old_key}")
        get_settings.cache_clear()
        with Session(migrated_engine) as session:
            before = session.execute(
                text("SELECT original FROM slot_certainty WHERE schedule_id=:s"), {"s": sid}
            ).scalar_one()
            counts = rotate(session)
            assert counts["slot_certainty.amount_note_enc"] == 1
            assert counts["slot_certainty.date_note_enc"] == 1
            assert counts["slot_certainty.history_enc"] == 1
            row = session.execute(
                text(
                    "SELECT original, amount_note_enc, date_note_enc, history_enc "
                    "FROM slot_certainty WHERE schedule_id=:s"
                ),
                {"s": sid},
            ).one()
            monkeypatch.setenv("PURSER_ENCRYPTION_KEY", new_key)
            get_settings.cache_clear()
            assert crypto.decrypt(bytes(row.amount_note_enc)) == evidence("estimated")["note"]
            assert crypto.decrypt(bytes(row.date_note_enc)) == evidence("estimated")["note"]
            history = json.loads(crypto.decrypt(bytes(row.history_enc)))
            assert any(h.get("amount", {}).get("state") == "estimated" for h in history)
            assert row.original == before
            session.rollback()  # Rotation is exercised in this synthetic transaction only.
    finally:
        monkeypatch.setenv("PURSER_ENCRYPTION_KEY", old_key)
        get_settings.cache_clear()
        crypto.reset_dek_cache()


def test_slot_evidence_reactivates_after_its_instance_disappears(
    seeded: None, client: TestClient, migrated_engine: Engine
) -> None:
    from purser import db, schedules
    from purser.models import Schedule

    sid, slot = create(client)
    original = write(client, sid, slot, amount=evidence("estimated"))["original"]
    with db.get_sessionmaker()() as session:
        schedule = session.get(Schedule, uuid.UUID(sid))
        assert schedule is not None
        schedules.materialize_schedule(session, schedule, horizon_days=80)
        session.commit()
    tx = read(client, sid, slot)["slot"]["transaction_id"]
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM postings WHERE transaction_id=:t"), {"t": tx})
        conn.execute(text("DELETE FROM transactions WHERE id=:t"), {"t": tx})
    before = financial_shape(forecast(client))
    assert read(client, sid, slot)["original"] == original
    result = write(client, sid, slot, amount=evidence("estimated", source="user"))
    assert result["original"] == original
    after = forecast(client)
    assert financial_shape(after) == before
    detail = next(
        e["certainty"]
        for d in after["days"]
        for e in d["events"]
        if (e.get("certainty", {}).get("slot") or {}).get("occurrence_date") == slot.isoformat()
    )
    assert detail["amount"]["source"] == "user"
    assert detail["original"] == original
