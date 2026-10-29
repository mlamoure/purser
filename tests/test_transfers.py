"""Deterministic transfer auto-link + absorption (DESIGN §5, issue #108).

The name signal promotes amount+window candidate pairs to automatic merges;
absorption claims late-arriving counterparts of existing transfers. Every
auto action is evented, audited with a NULL (system) actor, and leaves the
originals' content hashes in place so re-imports stay no-ops.
"""

import base64
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import Engine, text

from purser import crypto
from purser.core import matching as core_matching
from tests.conftest import ADMIN_CREDENTIALS

RealHttpxClient = httpx.Client
SETUP_TOKEN = base64.b64encode(b"https://bridge.example.com/simplefin/claim/TEST123").decode()
ACCESS_URL = "https://user:secret@bridge.example.com/simplefin"


def _epoch(day: int) -> int:
    return int(datetime(2026, 6, day, tzinfo=UTC).timestamp())


def two_account_payload(
    checking_txns: list[dict[str, Any]], savings_txns: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "errors": [],
        "accounts": [
            {
                "id": "chk-1",
                "name": "Checking",
                "currency": "USD",
                "balance": "1000.00",
                "org": {"name": "Demo Bank"},
                "transactions": checking_txns,
            },
            {
                "id": "sav-1",
                "name": "Savings",
                "currency": "USD",
                "balance": "5000.00",
                "org": {"name": "Demo Bank"},
                "transactions": savings_txns,
            },
        ],
    }


class Harness:
    """FeedHarness (tests/test_feed.py) specialized for two-account payloads."""

    def __init__(self, client: TestClient, monkeypatch: pytest.MonkeyPatch):
        self.client = client
        self.monkeypatch = monkeypatch
        assert client.post("/auth/setup", json=ADMIN_CREDENTIALS).status_code == 201

    def use_payload(self, payload: dict[str, Any]) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/claim/TEST123"):
                return httpx.Response(200, text=ACCESS_URL)
            if request.url.path.endswith("/accounts"):
                return httpx.Response(200, json=payload)
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)

        def patched_client(*args: object, **kwargs: object) -> httpx.Client:
            kwargs["transport"] = transport
            return RealHttpxClient(*args, **kwargs)  # type: ignore[arg-type]

        self.monkeypatch.setattr(httpx, "Client", patched_client)

    def connect_and_map_both(self) -> str:
        resp = self.client.post("/connections", json={"setup_token": SETUP_TOKEN})
        assert resp.status_code == 201, resp.text
        connection_id = resp.json()["id"]
        assert self.client.post(f"/connections/{connection_id}/sync").status_code == 200
        for external_id, path in (("chk-1", "assets.checking"), ("sav-1", "assets.savings")):
            resp = self.client.post(
                f"/connections/{connection_id}/map",
                json={
                    "external_id": external_id,
                    "create": {"name": external_id, "type": "checking", "path": path},
                },
            )
            assert resp.status_code == 200, resp.text
        return connection_id

    def sync(self, connection_id: str) -> dict[str, Any]:
        resp = self.client.post(f"/connections/{connection_id}/sync")
        assert resp.status_code == 200, resp.text
        return resp.json()


@pytest.fixture
def harness(clean_db: None, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(client, monkeypatch)


def _event_count(engine: Engine, type_: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT COUNT(*) FROM events WHERE type = :t"), {"t": type_}
            ).scalar_one()
        )


class TestSignal:
    def test_keyword_tokens_hit(self) -> None:
        for token_text in ("TRANSFER TO SAVINGS", "CITI AUTOPAY", "E-PYMT received", "ACH PMT"):
            assert core_matching.has_transfer_signal(text=token_text, counterparty_name="")

    def test_keyword_is_token_not_substring(self) -> None:
        assert not core_matching.has_transfer_signal(
            text="loan repayment plan", counterparty_name=""
        )
        assert not core_matching.has_transfer_signal(text="pmtx corp", counterparty_name="")

    def test_counterparty_name_matches_across_spacing(self) -> None:
        # Whole-token phrase form.
        assert core_matching.has_transfer_signal(
            text="TO CITI CARD ENDING 4433", counterparty_name="Citi Card"
        )
        # Space-squashed bank spelling ("CITICARD" ~ "Citi Card").
        assert core_matching.has_transfer_signal(
            text="CITICARD ONLINE 8842", counterparty_name="Citi Card"
        )
        assert not core_matching.has_transfer_signal(
            text="CHIPOTLE 2419 DENVER", counterparty_name="Savings"
        )

    def test_counterparty_never_matches_inside_a_word(self) -> None:
        # An account named "Chase" must not hit "PURCHASE RETURN".
        assert not core_matching.has_transfer_signal(
            text="PURCHASE RETURN 9921", counterparty_name="Chase"
        )
        assert core_matching.has_transfer_signal(
            text="CHASE EPAY RECEIVED", counterparty_name="Chase"
        )

    def test_short_counterparty_never_signals(self) -> None:
        # A 1-3 char squashed name would prefix-match everywhere.
        assert not core_matching.has_transfer_signal(text="ACME PAYROLL X1", counterparty_name="X1")
        assert not core_matching.has_transfer_signal(
            text="HSA CONTRIBUTION EMPLOYER", counterparty_name="HSA"
        )

    @given(text=st.text(max_size=200), name=st.text(max_size=50))
    def test_never_crashes(self, text: str, name: str) -> None:
        core_matching.has_transfer_signal(text=text, counterparty_name=name)

    @given(name=st.text(max_size=50))
    def test_empty_text_never_signals(self, name: str) -> None:
        assert not core_matching.has_transfer_signal(text="", counterparty_name=name)


class TestAutoLinkAtImport:
    SIGNAL_PAYLOAD = two_account_payload(
        [
            {
                "id": "t-out",
                "posted": _epoch(10),
                "amount": "-500.00",
                "description": "TRANSFER TO SAVINGS",
            }
        ],
        [
            {
                "id": "t-in",
                "posted": _epoch(10),
                "amount": "500.00",
                "description": "TRANSFER FROM CHECKING",
            }
        ],
    )

    def test_signal_pair_auto_links_on_sync(
        self, harness: Harness, client: TestClient, migrated_engine: Engine
    ) -> None:
        harness.use_payload(self.SIGNAL_PAYLOAD)
        harness.connect_and_map_both()

        page = client.get("/transactions", params={"account": "assets.checking"}).json()
        (row,) = page["items"]
        assert row["is_transfer"] is True
        assert row["origin"] == "system"
        assert Decimal(row["amount"]) == Decimal("-500.00")
        assert row["transfer_account_path"] == "assets.savings"

        with migrated_engine.connect() as conn:
            voided = conn.execute(
                text("SELECT status, content_hash FROM transactions WHERE superseded_by = :m"),
                {"m": row["id"]},
            ).all()
            actor = conn.execute(
                text("SELECT actor_token_id FROM audit_log WHERE action = 'transfer.auto_link'")
            ).scalar_one()
            payload_enc = conn.execute(
                text("SELECT payload_enc FROM events WHERE type = 'transfer.auto_linked'")
            ).scalar_one()
        assert len(voided) == 2
        assert all(status == "void" for status, _ in voided)
        assert all(content_hash is not None for _, content_hash in voided)
        assert actor is None
        event = crypto.decrypt_json(bytes(payload_enc))
        assert event["outgoing_status"] == "posted"
        assert event["incoming_status"] == "posted"
        assert event["pair_key"]

        accounts = client.get("/accounts", params={"include_balances": True}).json()
        checking = next(a for a in accounts if a["path"] == "assets.checking")
        savings = next(a for a in accounts if a["path"] == "assets.savings")
        assert Decimal(checking["balance"]) == Decimal("-500.00")
        assert Decimal(savings["balance"]) == Decimal("500.00")

    def test_reimport_after_auto_link_creates_zero_rows(
        self, harness: Harness, client: TestClient, migrated_engine: Engine
    ) -> None:
        harness.use_payload(self.SIGNAL_PAYLOAD)
        connection_id = harness.connect_and_map_both()

        def _tx_count() -> int:
            with migrated_engine.connect() as conn:
                return int(conn.execute(text("SELECT COUNT(*) FROM transactions")).scalar_one())

        before = _tx_count()
        result = harness.sync(connection_id)
        assert result["imported"] == 0
        assert result["skipped"] == 2  # both hashes live on the voided originals
        assert _tx_count() == before
        assert _event_count(migrated_engine, "transfer.auto_linked") == 1

    def test_no_signal_pair_stays_candidate(
        self, harness: Harness, client: TestClient, migrated_engine: Engine
    ) -> None:
        harness.use_payload(
            two_account_payload(
                [
                    {
                        "id": "n-out",
                        "posted": _epoch(10),
                        "amount": "-321.00",
                        "description": "CHECK 1042",
                    }
                ],
                [
                    {
                        "id": "n-in",
                        "posted": _epoch(11),
                        "amount": "321.00",
                        "description": "MOBILE CREDIT 8891",
                    }
                ],
            )
        )
        harness.connect_and_map_both()
        assert _event_count(migrated_engine, "transfer.auto_linked") == 0
        for path in ("assets.checking", "assets.savings"):
            (row,) = client.get("/transactions", params={"account": path}).json()["items"]
            assert row["is_transfer"] is False
            assert row["status"] == "posted"
        candidates = client.get("/matching/transfer-candidates").json()
        assert len(candidates) == 1

    def test_kill_switch_disables_pass_and_sweep_catches_up(
        self, harness: Harness, client: TestClient, migrated_engine: Engine
    ) -> None:
        resp = client.put("/config", json={"transfer_auto_link": False})
        assert resp.status_code == 200, resp.text
        assert resp.json()["values"]["transfer_auto_link"] is False

        harness.use_payload(self.SIGNAL_PAYLOAD)
        connection_id = harness.connect_and_map_both()
        assert _event_count(migrated_engine, "transfer.auto_linked") == 0

        assert client.put("/config", json={"transfer_auto_link": True}).status_code == 200
        # Next import-triggered pass links the already-imported pair.
        harness.sync(connection_id)
        assert _event_count(migrated_engine, "transfer.auto_linked") == 1

    def test_bank_pending_pairs_are_not_auto_linked(
        self, harness: Harness, client: TestClient, migrated_engine: Engine
    ) -> None:
        """Pending rows can come back posted under a new hash; auto paths
        wait for posted (DESIGN §5)."""
        payload = two_account_payload(
            [
                {
                    "id": "p-out",
                    "posted": 0,
                    "transacted_at": _epoch(12),
                    "pending": True,
                    "amount": "-77.00",
                    "description": "TRANSFER TO SAVINGS",
                }
            ],
            [
                {
                    "id": "p-in",
                    "posted": 0,
                    "transacted_at": _epoch(12),
                    "pending": True,
                    "amount": "77.00",
                    "description": "TRANSFER FROM CHECKING",
                }
            ],
        )
        harness.use_payload(payload)
        harness.connect_and_map_both()
        assert _event_count(migrated_engine, "transfer.auto_linked") == 0
        for path in ("assets.checking", "assets.savings"):
            (row,) = client.get("/transactions", params={"account": path}).json()["items"]
            assert row["status"] == "pending"
            assert row["is_transfer"] is False


class TestAbsorb:
    def _manual_transfer(self, client: TestClient, *, status: str = "pending") -> str:
        resp = client.post(
            "/transactions/transfer",
            json={
                "from_account": "assets.checking",
                "to_account": "assets.savings",
                "amount": "500.00",
                "date": "2026-06-15",
                "status": status,
            },
        )
        assert resp.status_code == 201, resp.text
        tx_id: str = resp.json()["id"]
        return tx_id

    def test_late_counterpart_with_signal_is_absorbed(
        self, harness: Harness, client: TestClient, migrated_engine: Engine
    ) -> None:
        harness.use_payload(two_account_payload([], []))
        connection_id = harness.connect_and_map_both()
        transfer_id = self._manual_transfer(client, status="pending")

        harness.use_payload(
            two_account_payload(
                [],
                [
                    {
                        "id": "late-in",
                        "posted": _epoch(15),
                        "amount": "500.00",
                        "description": "TRANSFER FROM CHECKING",
                    }
                ],
            )
        )
        harness.sync(connection_id)

        with migrated_engine.connect() as conn:
            absorbed = conn.execute(
                text(
                    "SELECT status, superseded_by::text FROM transactions"
                    " WHERE content_hash IS NOT NULL AND origin = 'import'"
                )
            ).one()
        assert absorbed.status == "void"
        assert absorbed.superseded_by == transfer_id
        assert _event_count(migrated_engine, "transfer.absorbed") == 1

        # The bank posting one side is authoritative for the whole transfer.
        assert client.get(f"/transactions/{transfer_id}").json()["status"] == "posted"

        accounts = client.get("/accounts", params={"include_balances": True}).json()
        savings = next(a for a in accounts if a["path"] == "assets.savings")
        assert Decimal(savings["balance"]) == Decimal("500.00")  # no duplicate leg

    def test_late_counterpart_without_signal_is_left_alone(
        self, harness: Harness, client: TestClient, migrated_engine: Engine
    ) -> None:
        harness.use_payload(two_account_payload([], []))
        connection_id = harness.connect_and_map_both()
        transfer_id = self._manual_transfer(client, status="posted")

        harness.use_payload(
            two_account_payload(
                [],
                [
                    {
                        "id": "plain-in",
                        "posted": _epoch(15),
                        "amount": "500.00",
                        "description": "ATM CREDIT 512",
                    }
                ],
            )
        )
        harness.sync(connection_id)

        assert _event_count(migrated_engine, "transfer.absorbed") == 0
        rows = client.get("/transactions", params={"account": "assets.savings"}).json()["items"]
        assert len(rows) == 2  # the transfer leg AND the untouched import
        assert client.get(f"/transactions/{transfer_id}").json()["status"] == "posted"
