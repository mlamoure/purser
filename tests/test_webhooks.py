"""Events, audit completeness, webhook delivery with retries, bill.late."""

import hashlib
import hmac
import json
import uuid
from datetime import date, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from purser.webhooks import MAX_ATTEMPTS


@pytest.fixture
def seeded(admin_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # Encryption key comes suite-wide from conftest (required at startup).
    admin_client.post(
        "/accounts", json={"name": "Checking", "type": "checking", "path": "assets.checking"}
    )
    admin_client.post(
        "/categories",
        json={"name": "Dining", "type": "category_expense", "path": "expenses.dining"},
    )
    yield


class Receiver:
    """Collects webhook posts; can be told to fail N times first."""

    def __init__(self, fail_first: int = 0):
        self.received: list[httpx.Request] = []
        self.fail_first = fail_first

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            if len(self.received) < self.fail_first and self.fail_first > 0:
                self.received.append(request)
                return httpx.Response(500)
            self.received.append(request)
            return httpx.Response(200)

        return httpx.MockTransport(handler)


def _patch_http(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    real_client = httpx.Client

    def patched(*args: object, **kwargs: object) -> httpx.Client:
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "Client", patched)


def _spend(client: TestClient, amount: str = "-10.00", status: str = "posted") -> str:
    resp = client.post(
        "/transactions",
        json={
            "account": "assets.checking",
            "category": "expenses.dining",
            "amount": amount,
            "date": date.today().isoformat(),
            "status": status,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


class TestEventReplay:
    def test_downed_subscriber_catches_up_from_cursor(
        self, seeded: None, client: TestClient
    ) -> None:
        _spend(client)  # -> transaction.posted event
        first_page = client.get("/events").json()
        assert any(e["type"] == "transaction.posted" for e in first_page)
        cursor = first_page[-1]["id"]

        # "A day passes while the subscriber is down."
        _spend(client, amount="-20.00")
        _spend(client, amount="-30.00")

        caught_up = client.get("/events", params={"since_id": cursor}).json()
        assert len(caught_up) == 2
        assert all(e["type"] == "transaction.posted" for e in caught_up)

    def test_type_filter(self, seeded: None, client: TestClient) -> None:
        _spend(client)
        rows = client.get("/events", params={"type": "transaction.posted"}).json()
        assert rows and all(e["type"] == "transaction.posted" for e in rows)

    def test_same_timestamp_events_after_cursor_are_not_skipped(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        """Event.at is transaction time: a batch insert shares one now().
        Paging from a mid-batch cursor must return the remaining events of
        that same timestamp, in (at, id) order — not silently skip them."""
        from purser import crypto

        empty = crypto.encrypt_json({})  # payload is encrypted at rest now (#137)
        with migrated_engine.begin() as conn:  # one transaction = one now()
            for _ in range(4):
                conn.execute(
                    text("INSERT INTO events (type, payload_enc) VALUES ('test.batch', :p)"),
                    {"p": empty},
                )

        rows = client.get("/events", params={"type": "test.batch"}).json()
        assert len(rows) == 4
        assert len({e["at"] for e in rows}) == 1, "precondition: same timestamp"

        cursor = rows[1]["id"]  # mid-batch anchor
        page = client.get("/events", params={"since_id": cursor, "type": "test.batch"}).json()
        assert [e["id"] for e in page] == [e["id"] for e in rows[2:]]


class TestWebhookDelivery:
    def test_delivery_with_hmac_signature(
        self, seeded: None, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        receiver = Receiver()
        sub = client.post(
            "/webhooks",
            json={
                "url": "https://claw-bridge.example/hook",
                "event_types": ["transaction.posted"],
                "secret": "hook-secret-123",
            },
        ).json()
        assert sub["has_secret"] is True
        _spend(client)

        _patch_http(monkeypatch, receiver.transport())
        stats = client.post("/webhooks/deliver").json()
        assert stats["delivered"] == 1
        (request,) = receiver.received
        body = request.content
        expected_sig = hmac.new(b"hook-secret-123", body, hashlib.sha256).hexdigest()
        assert request.headers["X-Purser-Signature"] == expected_sig
        assert request.headers["X-Purser-Event"] == "transaction.posted"
        payload = json.loads(body)
        assert payload["type"] == "transaction.posted"

    def test_failed_delivery_retries_with_backoff_then_succeeds(
        self,
        seeded: None,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        migrated_engine: Engine,
    ) -> None:
        receiver = Receiver(fail_first=1)
        client.post(
            "/webhooks",
            json={"url": "https://flaky.example/hook", "event_types": ["*"]},
        )
        _spend(client)
        _patch_http(monkeypatch, receiver.transport())

        stats = client.post("/webhooks/deliver").json()
        assert stats["retried"] == 1
        # Force the retry due now (simulate backoff elapsed).
        with migrated_engine.begin() as conn:
            conn.execute(text("UPDATE webhook_deliveries SET next_attempt_at = now()"))
        stats = client.post("/webhooks/deliver").json()
        assert stats["delivered"] == 1

        subs = client.get("/webhooks").json()
        deliveries = client.get(f"/webhooks/{subs[0]['id']}/deliveries").json()
        (delivery,) = deliveries
        assert delivery["status"] == "delivered"
        assert delivery["attempts"] == 2

    def test_event_type_filtering(
        self, seeded: None, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        receiver = Receiver()
        client.post(
            "/webhooks",
            json={"url": "https://narrow.example/hook", "event_types": ["forecast.shortfall"]},
        )
        _spend(client)  # transaction.posted — not subscribed
        _patch_http(monkeypatch, receiver.transport())
        stats = client.post("/webhooks/deliver").json()
        assert stats["deliveries_created"] == 0
        assert receiver.received == []


class TestFanOutCursor:
    """#219 / decision 54: fan_out scans from a per-subscription cursor
    instead of anti-joining the full events table each pass."""

    def test_new_subscription_gets_no_historical_backlog(
        self, seeded: None, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _spend(client)  # a pre-subscription event
        client.post("/webhooks", json={"url": "https://late.example/hook", "event_types": ["*"]})
        receiver = Receiver()
        _patch_http(monkeypatch, receiver.transport())
        stats = client.post("/webhooks/deliver").json()
        assert stats["deliveries_created"] == 0
        assert receiver.received == []

    def test_post_subscription_events_deliver_once_and_cursor_advances(
        self, seeded: None, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client.post("/webhooks", json={"url": "https://fresh.example/hook", "event_types": ["*"]})
        _spend(client)
        receiver = Receiver()
        _patch_http(monkeypatch, receiver.transport())
        first = client.post("/webhooks/deliver").json()
        assert first["deliveries_created"] == 1
        # Re-pass: the cursor advanced; the >=-boundary overlap is eaten by
        # ON CONFLICT DO NOTHING rather than re-created or IntegrityError'd.
        second = client.post("/webhooks/deliver").json()
        assert second["deliveries_created"] == 0
        assert len(receiver.received) == 1


def _corrupt_secret(migrated_engine: Engine, subscription_id: str) -> None:
    """Simulate a rotated/lost Fernet key: the stored secret no longer decrypts."""
    with migrated_engine.begin() as conn:
        conn.execute(
            text("UPDATE webhook_subscriptions SET secret_enc = :garbage WHERE id = :id"),
            {"garbage": b"not-a-fernet-token", "id": uuid.UUID(subscription_id)},
        )


class TestPoisonDelivery:
    def test_poison_delivery_does_not_wedge_the_batch(
        self,
        seeded: None,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        migrated_engine: Engine,
    ) -> None:
        """An undecryptable secret on one subscription must not abort the
        batch: the healthy subscriber still gets its delivery, the poison
        one records a failed attempt with backoff, and nothing raises."""
        receiver = Receiver()
        poison = client.post(
            "/webhooks",
            json={
                "url": "https://poison.example/hook",
                "event_types": ["*"],
                "secret": "soon-to-be-garbage",
            },
        ).json()
        healthy = client.post(
            "/webhooks",
            json={"url": "https://healthy.example/hook", "event_types": ["*"]},
        ).json()
        _spend(client)
        _corrupt_secret(migrated_engine, poison["id"])
        _patch_http(monkeypatch, receiver.transport())

        stats = client.post("/webhooks/deliver").json()  # must not 500
        assert stats["delivered"] == 1
        assert stats["retried"] == 1

        assert {r.url.host for r in receiver.received} == {"healthy.example"}
        (delivered,) = client.get(f"/webhooks/{healthy['id']}/deliveries").json()
        assert delivered["status"] == "delivered"

        (poisoned,) = client.get(f"/webhooks/{poison['id']}/deliveries").json()
        assert poisoned["status"] == "pending"
        assert poisoned["attempts"] == 1
        assert poisoned["response_status"] is None
        assert datetime.fromisoformat(poisoned["next_attempt_at"]) > datetime.fromisoformat(
            poisoned["last_attempt_at"]
        )

    def test_poison_delivery_fails_after_max_attempts(
        self,
        seeded: None,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        migrated_engine: Engine,
    ) -> None:
        receiver = Receiver()
        sub = client.post(
            "/webhooks",
            json={
                "url": "https://poison.example/hook",
                "event_types": ["*"],
                "secret": "soon-to-be-garbage",
            },
        ).json()
        _spend(client)
        _corrupt_secret(migrated_engine, sub["id"])
        _patch_http(monkeypatch, receiver.transport())

        for _ in range(MAX_ATTEMPTS):
            client.post("/webhooks/deliver")
            with migrated_engine.begin() as conn:  # force the backoff elapsed
                conn.execute(
                    text(
                        "UPDATE webhook_deliveries SET next_attempt_at = now() "
                        "WHERE status = 'pending'"
                    )
                )

        (delivery,) = client.get(f"/webhooks/{sub['id']}/deliveries").json()
        assert delivery["status"] == "failed"
        assert delivery["attempts"] == MAX_ATTEMPTS
        assert receiver.received == []  # decryption fails before any HTTP call


class TestBillLate:
    def test_late_bill_event_fires_from_aging(
        self, seeded: None, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An expected bill dated 4 days ago, never matched.
        resp = client.post(
            "/transactions",
            json={
                "account": "assets.checking",
                "category": "expenses.dining",
                "amount": "-60.00",
                "date": (date.today() - timedelta(days=4)).isoformat(),
                "payee": "Trash Service",
                "status": "expected",
            },
        )
        tx_id = resp.json()["id"]
        _patch_http(monkeypatch, Receiver().transport())
        stats = client.post("/webhooks/deliver").json()
        assert stats["late_bills_emitted"] == 1
        rows = client.get("/events", params={"type": "bill.late"}).json()
        (event,) = rows
        assert event["entity_id"] == tx_id
        assert event["payload"]["aging_days"] == 4

        # Deduped: the same late bill does not re-emit.
        stats = client.post("/webhooks/deliver").json()
        assert stats["late_bills_emitted"] == 0


class TestAuditCompleteness:
    def test_every_mutation_answers_who_what_when(
        self, seeded: None, client: TestClient, migrated_engine: Engine
    ) -> None:
        """Drive one mutation of each kind; every one must land in audit_log
        with actor, action, entity, and before/after."""
        tx_id = _spend(client)
        client.patch(f"/transactions/{tx_id}", json={"description": "edited"})
        client.post("/tokens", json={"name": "agent", "scopes": ["read", "write"]})
        client.post(
            "/schedules",
            json={
                "name": "Rent",
                "account": "assets.checking",
                "rrule": "FREQ=MONTHLY;BYMONTHDAY=1",
                "amount_type": "fixed",
                "amount": "-2000.00",
            },
        )
        client.post("/webhooks", json={"url": "https://x.example/h", "event_types": ["*"]})
        rows = client.get("/audit", params={"limit": 200}).json()["items"]
        actions = {r["action"] for r in rows}
        assert {
            "user.create",
            "auth.login",
            "token.create",
            "account.create",
            "transaction.create",
            "transaction.correct",
            "schedule.create",
            "webhook.create",
        } <= actions
        for row in rows:
            assert row["action"]
            assert row["entity_type"]
            assert row["at"]
        # authenticated mutations carry their actor token (system-origin rows,
        # e.g. schedule materialization, legitimately have none)
        webhook_rows = [r for r in rows if r["action"] == "webhook.create"]
        assert webhook_rows and all(r["actor_token_id"] is not None for r in webhook_rows)
        tx_rows = [r for r in rows if r["action"] == "transaction.create"]
        assert any(r["actor_token_id"] is not None for r in tx_rows)
