"""Account read authority includes bank truth, without connection administration (#505)."""

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import func, select

from purser import audit, crypto, db, ledger
from purser.feed import simplefin
from purser.feed.snapshot import _bank_time, _money
from purser.models import (
    Account,
    AccountReconciliation,
    AuditLog,
    Connection,
    RawFetch,
    Transaction,
)
from tests import test_mcp

live_server = test_mcp.live_server
PATH = "assets.snapshot"
EXTERNAL = "BANK-PRIVATE-123456789"
BANK_AT = datetime(2026, 9, 10, 15, 42, 31, tzinfo=UTC)


def _bank(**changes: Any) -> dict[str, Any]:
    return {
        "id": EXTERNAL,
        "name": "Private Bank Account 123456789",
        "balance": "100.00",
        "available-balance": "75.00",
        "currency": "USD",
        "balance-date": int(BANK_AT.timestamp()),
        **changes,
    }


def _archive(connection_id: uuid.UUID, accounts: list[dict[str, Any]]) -> uuid.UUID:
    with db.get_sessionmaker()() as session:
        raw = RawFetch(
            connection_id=connection_id,
            content_hash=uuid.uuid4().hex,
            payload_enc=crypto.encrypt_json({"accounts": accounts}),
        )
        session.add(raw)
        session.commit()
        return raw.id


@pytest.fixture
def connected(admin_client: TestClient) -> tuple[uuid.UUID, uuid.UUID]:
    with db.get_sessionmaker()() as session:
        connection = Connection(provider="simplefin", status="active")
        session.add(connection)
        session.flush()
        account = ledger.create_account(
            session,
            name="Snapshot",
            type_="checking",
            path=PATH,
            connection_id=connection.id,
            external_id=EXTERNAL,
            actor_token_id=None,
        )
        ledger.create_account(
            session,
            name="Groceries",
            type_="category_expense",
            path="expenses.groceries",
            actor_token_id=None,
        )
        session.commit()
        return account.id, connection.id


def _token(client: TestClient, scopes: list[str]) -> str:
    response = client.post("/tokens", json={"name": "snapshot-reader", "scopes": scopes})
    assert response.status_code == 201, response.text
    return str(response.json()["token"])


def _snapshot(client: TestClient, account_id: uuid.UUID, token: str) -> dict[str, Any]:
    response = client.get(
        f"/accounts/{account_id}/balance", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200, response.text
    return response.json()["bank_snapshot"]


def _financial_counts() -> tuple[int, ...]:
    with db.get_sessionmaker()() as session:
        counts = [
            session.scalar(select(func.count()).select_from(model)) or 0
            for model in (Transaction, RawFetch, AccountReconciliation)
        ]
        counts.append(
            session.scalar(
                select(func.count()).select_from(AuditLog).where(AuditLog.kind == "change")
            )
            or 0
        )
        return tuple(counts)


def test_read_contract_rest_mcp_and_no_financial_writes(
    connected: tuple[uuid.UUID, uuid.UUID],
    admin_client: TestClient,
    live_server: test_mcp.LiveServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id, connection_id = connected
    _archive(connection_id, [_bank(), _bank(id="OTHER-ACCOUNT", balance="987654.32")])
    reader = _token(admin_client, ["read"])

    def no_network(*args: Any, **kwargs: Any) -> None:
        pytest.fail("a balance read must not call SimpleFIN")

    monkeypatch.setattr(simplefin, "fetch_accounts", no_network)
    before = _financial_counts()
    headers = {"Authorization": f"Bearer {reader}"}
    snapshot = _snapshot(admin_client, account_id, reader)
    detail = admin_client.get(f"/accounts/{account_id}", headers=headers).json()
    assert detail["bank_snapshot"] == snapshot
    assert detail["external_id"] is None and detail["connection_id"] is None
    result, historical = test_mcp._mcp_call(
        live_server.url,
        reader,
        [
            ("account_balance", {"account": PATH}),
            ("account_balance", {"account": str(account_id), "as_of": "2020-01-01"}),
        ],
    )
    assert result["bank_snapshot"] == historical["bank_snapshot"] == snapshot
    assert historical["as_of"] == "2020-01-01"
    assert snapshot["kind"] == "latest"
    assert snapshot["balance"] == "100.00"
    assert snapshot["available_balance"] == "75.00"
    assert snapshot["currency"] == "USD"
    assert snapshot["source"] == "simplefin"
    assert snapshot["balance_at"] == BANK_AT.isoformat().replace("+00:00", "Z")
    assert snapshot["fetched_at"] != snapshot["balance_at"]
    assert snapshot["time_precision"] == "timestamp"
    assert snapshot["comparison"]["ledger_cutoff"] == "all_recorded_dates"
    for secret in (EXTERNAL, "123456789", "OTHER-ACCOUNT", "987654.32", str(connection_id)):
        assert secret not in json.dumps(result)
    assert len(json.dumps(result)) < 2000  # compact account answer, not a feed dump
    assert _financial_counts() == before
    assert (
        admin_client.get(f"/connections/{connection_id}/discovered", headers=headers).status_code
        == 403
    )
    assert (
        admin_client.patch(
            f"/accounts/{account_id}", headers=headers, json={"drift_basis_override": "posted_only"}
        ).status_code
        == 403
    )
    # Read auditing remains present; it is not a financial mutation.
    with db.get_sessionmaker()() as session:
        assert session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.kind == "read", AuditLog.action == "mcp.account_balance")
        )


def test_unauthorized_and_unknown_accounts(
    connected: tuple[uuid.UUID, uuid.UUID],
    admin_client: TestClient,
    live_server: test_mcp.LiveServer,
) -> None:
    account_id, _ = connected
    reader = _token(admin_client, ["read"])
    writer = _token(admin_client, ["write"])
    for suffix in ("", "/balance"):
        url = f"/accounts/{account_id}{suffix}"
        assert admin_client.get(url, headers={"Authorization": "Bearer invalid"}).status_code == 401
        assert (
            admin_client.get(url, headers={"Authorization": f"Bearer {writer}"}).status_code == 403
        )
        assert (
            admin_client.get(
                f"/accounts/{uuid.uuid4()}{suffix}", headers={"Authorization": f"Bearer {reader}"}
            ).status_code
            == 404
        )
    (refused,) = test_mcp._mcp_call(
        live_server.url, writer, [("account_balance", {"account": PATH})]
    )
    assert "error" in refused
    admin_client.cookies.clear()
    assert admin_client.get(f"/accounts/{account_id}/balance").status_code == 401


@pytest.mark.parametrize(
    "pin,selected,difference",
    [
        ("posted_only", "100", "0"),
        ("pending_inclusive", "85", "15"),
    ],
)
def test_basis_exact_difference_even_when_other_basis_matches(
    connected: tuple[uuid.UUID, uuid.UUID],
    admin_client: TestClient,
    pin: str,
    selected: str,
    difference: str,
) -> None:
    account_id, connection_id = connected
    _archive(connection_id, [_bank()])
    for amount, status in (("100", "posted"), ("-15", "pending")):
        response = admin_client.post(
            "/transactions",
            json={
                "account": PATH,
                "category": "expenses.groceries",
                "amount": amount,
                # Future dates are intentionally part of the declared comparison.
                "date": (date.today() + timedelta(days=5)).isoformat(),
                "status": status,
            },
        )
        assert response.status_code == 201, response.text
    assert (
        admin_client.patch(
            f"/accounts/{account_id}", json={"drift_basis_override": pin}
        ).status_code
        == 200
    )
    result = _snapshot(admin_client, account_id, _token(admin_client, ["read"]))
    comparison = result["comparison"]
    assert Decimal(comparison["posted_balance"]) == Decimal("100")
    assert Decimal(comparison["pending_inclusive_balance"]) == Decimal("85")
    assert Decimal(comparison["ledger_balance"]) == Decimal(selected)
    assert Decimal(comparison["difference"]) == Decimal(difference)
    assert comparison["basis"] == pin and comparison["basis_pinned"] is True
    assert comparison["basis_learned"] == "posted_only"
    assert comparison["matches_either_basis"] is True


@pytest.mark.parametrize(
    "value,state",
    [(None, "missing"), ("100.00", "reported"), ("0.00", "reported"), ("NaN", "invalid")],
)
def test_available_is_independent(
    connected: tuple[uuid.UUID, uuid.UUID],
    admin_client: TestClient,
    value: str | None,
    state: str,
) -> None:
    account_id, connection_id = connected
    _archive(connection_id, [_bank(**{"available-balance": value})])
    result = _snapshot(admin_client, account_id, _token(admin_client, ["read"]))
    assert result["available_balance_status"] == state
    assert result["available_balance"] == (value if state == "reported" else None)


@pytest.mark.parametrize(
    "changes,status,reason",
    [
        ({"balance": None}, "balance_missing", "no_bank_balance"),
        ({"balance": "Infinity"}, "balance_invalid", "no_bank_balance"),
        ({"currency": "EUR"}, "ok", "currency_mismatch"),
        ({"currency": None}, "ok", "currency_unknown"),
    ],
)
def test_unknown_values_never_become_zero(
    connected: tuple[uuid.UUID, uuid.UUID],
    admin_client: TestClient,
    changes: dict[str, Any],
    status: str,
    reason: str,
) -> None:
    account_id, connection_id = connected
    _archive(connection_id, [_bank(**changes)])
    result = _snapshot(admin_client, account_id, _token(admin_client, ["read"]))
    assert result["status"] == status
    assert result["comparison"] is None and result["comparison_unavailable"] == reason
    if "balance" in changes:
        assert result["balance"] is None


@pytest.mark.parametrize(
    "state",
    [
        "not_connected",
        "account_closed",
        "connection_disabled",
        "no_snapshot",
        "account_missing",
        "ambiguous_account",
    ],
)
def test_unavailable_states(
    connected: tuple[uuid.UUID, uuid.UUID],
    admin_client: TestClient,
    state: str,
) -> None:
    account_id, connection_id = connected
    if state != "no_snapshot":
        _archive(
            connection_id,
            []
            if state == "account_missing"
            else [_bank(), _bank()]
            if state == "ambiguous_account"
            else [_bank()],
        )
    with db.get_sessionmaker()() as session:
        account = session.get(Account, account_id)
        assert account is not None
        if state == "not_connected":
            account.connection_id = None
            account.external_id = None
        elif state == "account_closed":
            account.closed_at = datetime.now(UTC)
        elif state == "connection_disabled":
            connection = session.get(Connection, connection_id)
            assert connection is not None
            connection.status = "disabled"
        session.commit()
    result = _snapshot(admin_client, account_id, _token(admin_client, ["read"]))
    assert result["status"] == result["comparison_unavailable"] == state
    assert result["balance"] is None and result["comparison"] is None


def test_completed_sync_wins_over_newer_partial_fetch(
    connected: tuple[uuid.UUID, uuid.UUID],
    admin_client: TestClient,
) -> None:
    account_id, connection_id = connected
    full = _archive(connection_id, [_bank()])
    with db.get_sessionmaker()() as session:
        audit.record(
            session,
            actor_token_id=None,
            action="connection.sync",
            entity_type="connection",
            entity_id=connection_id,
            after={"raw_fetch_id": str(full)},
        )
        session.commit()
    _archive(connection_id, [_bank(id="OTHER", balance="999.00")])
    result = _snapshot(admin_client, account_id, _token(admin_client, ["read"]))
    assert result["balance"] == "100.00"


@pytest.mark.parametrize(
    "raw,precision",
    [("2026-09-10", "date"), (None, "unknown"), ("nonsense", "unknown"), (10**30, "unknown")],
)
def test_time_uncertainty_and_freshness(
    connected: tuple[uuid.UUID, uuid.UUID],
    admin_client: TestClient,
    raw: object,
    precision: str,
) -> None:
    account_id, connection_id = connected
    _archive(connection_id, [_bank(**{"balance-date": raw})])
    result = _snapshot(admin_client, account_id, _token(admin_client, ["read"]))
    assert result["time_precision"] == precision and result["balance_at"] is None
    assert result["freshness"] == ("stale" if precision == "date" else "unknown")


@given(st.integers(min_value=1, max_value=253402300799))
def test_epoch_precision_is_preserved(epoch: int) -> None:
    instant, day = _bank_time(epoch)
    assert instant is not None and int(instant.timestamp()) == epoch
    assert day == instant.date()


@pytest.mark.parametrize("raw", [None, True, "NaN", "Infinity", "not money"])
def test_invalid_money_is_unknown(raw: object) -> None:
    assert _money(raw) is None


def test_offset_timestamp_does_not_invent_bank_timezone() -> None:
    instant, day = _bank_time("2026-09-10T23:42:31-04:00")
    assert instant is not None and instant.isoformat() == "2026-09-10T23:42:31-04:00"
    assert day == date(2026, 9, 11)


def test_fractional_epoch_retains_precision() -> None:
    instant, _ = _bank_time("1789054951.125")
    assert instant is not None and instant.microsecond == 125000


@pytest.mark.parametrize("raw", ["1E+999999", "0.00001", "10000000000000000"])
def test_money_outside_ledger_precision_is_unknown(raw: str) -> None:
    assert _money(raw) is None


@pytest.mark.parametrize(
    "account_type,cash,comparable",
    [
        ("credit_card", False, True),
        ("brokerage", False, False),
        ("brokerage", True, True),
        ("loan", False, False),
    ],
)
def test_account_type_does_not_hide_reported_balances(
    connected: tuple[uuid.UUID, uuid.UUID],
    admin_client: TestClient,
    account_type: str,
    cash: bool,
    comparable: bool,
) -> None:
    account_id, connection_id = connected
    _archive(connection_id, [_bank(balance="-100.00", **{"available-balance": "900.00"})])
    with db.get_sessionmaker()() as session:
        account = session.get(Account, account_id)
        assert account is not None
        account.type = account_type
        if cash:
            account.holdings_source = "manual"
            account.feed_balance_is_cash = True
        session.commit()
    result = _snapshot(admin_client, account_id, _token(admin_client, ["read"]))
    assert result["balance"] == "-100.00"
    assert result["available_balance"] == "900.00"
    assert (result["comparison"] is not None) == comparable
    assert result["comparison_unavailable"] == (None if comparable else "account_type")


def test_reused_provider_id_in_another_connection_cannot_leak(
    connected: tuple[uuid.UUID, uuid.UUID],
    admin_client: TestClient,
) -> None:
    account_id, connection_id = connected
    _archive(connection_id, [_bank()])
    with db.get_sessionmaker()() as session:
        other = Connection(provider="simplefin", status="active")
        session.add(other)
        session.commit()
        other_id = other.id
    _archive(other_id, [_bank(balance="987654.32")])
    assert (
        _snapshot(admin_client, account_id, _token(admin_client, ["read"]))["balance"] == "100.00"
    )


def test_basis_history_is_read_but_not_changed(
    connected: tuple[uuid.UUID, uuid.UUID],
    admin_client: TestClient,
) -> None:
    account_id, connection_id = connected
    raw_id = _archive(connection_id, [_bank()])
    with db.get_sessionmaker()() as session:
        # Pending-inclusive delta holds steady; posted-only moves. The last
        # observation alone would prefer posted-only by absolute difference.
        for i, (posted, pending) in enumerate((("70", "90"), ("80", "90"), ("95", "90"))):
            session.add(
                AuditLog(
                    at=BANK_AT + timedelta(seconds=i),
                    action="connection.sync",
                    entity_type="connection",
                    entity_id=connection_id,
                    before_after={
                        "after": {
                            "raw_fetch_id": str(raw_id),
                            "balance_drift": {
                                str(account_id): {
                                    "reported": "100",
                                    "posted": posted,
                                    "pending": pending,
                                    "basis": "pending_inclusive",
                                }
                            },
                        }
                    },
                )
            )
        session.commit()
    reader = _token(admin_client, ["read"])
    before = _financial_counts()
    first = _snapshot(admin_client, account_id, reader)
    second = _snapshot(admin_client, account_id, reader)
    assert first == second
    assert first["comparison"]["basis_learned"] == "pending_inclusive"
    assert first["comparison"]["basis_pinned"] is False
    assert _financial_counts() == before
