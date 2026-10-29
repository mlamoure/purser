"""Feed layer: claim flow, raw-first archive, idempotent import, pending
lifecycle, discovery/mapping, traceability."""

import base64
import json
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, select, text

from purser.feed import drift, service, simplefin
from purser.feed.reconcile import PENDING_MATCH_WINDOW_DAYS
from tests.conftest import ADMIN_CREDENTIALS

RealHttpxClient = httpx.Client
ACCESS_URL = "https://user:secret@bridge.example.com/simplefin"
SETUP_TOKEN = base64.b64encode(b"https://bridge.example.com/simplefin/claim/TEST123").decode()
FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "simplefin" / "accounts_demo.json").read_text()
)


def _epoch(day: int) -> int:
    return int(datetime(2026, 6, day, tzinfo=UTC).timestamp())


def _recent_epoch(days_ago: int) -> int:
    """Pending-lifecycle tests need dates inside the resync overlap window
    (last_fetch_at - RESYNC_OVERLAP_DAYS), i.e. relative to now."""
    return int((datetime.now(UTC) - timedelta(days=days_ago)).timestamp())


def account_of(
    transactions: list[dict[str, Any]],
    *,
    account_id: str = "Demo Checking",
    balance: str | None = "1000.00",
    balance_date: int | None = None,
) -> dict:
    """One SimpleFIN account dict. ``balance-date`` is emitted only when given
    (the field is optional in practice; its absence disables the drift check's
    fast path)."""
    account: dict[str, Any] = {
        "id": account_id,
        "name": "Checking",
        "currency": "USD",
        "org": {"name": "Demo Bank"},
        "transactions": transactions,
    }
    if balance is not None:
        account["balance"] = balance
    if balance_date is not None:
        account["balance-date"] = balance_date
    return account


def multi_payload_of(accounts: list[dict[str, Any]]) -> dict:
    return {"errors": [], "accounts": accounts}


def payload_of(
    transactions: list[dict[str, Any]],
    account_id: str = "Demo Checking",
    *,
    balance: str | None = "1000.00",
    balance_date: int | None = None,
) -> dict:
    return multi_payload_of(
        [
            account_of(
                transactions,
                account_id=account_id,
                balance=balance,
                balance_date=balance_date,
            )
        ]
    )


def make_transport(payload: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/claim/TEST123"):
            return httpx.Response(200, text=ACCESS_URL)
        if request.url.path.endswith("/accounts"):
            return httpx.Response(200, json=payload)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def make_broken_transport(accounts_response: httpx.Response | Exception) -> httpx.MockTransport:
    """Claim succeeds; /accounts returns the given response or raises."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/claim/TEST123"):
            return httpx.Response(200, text=ACCESS_URL)
        if isinstance(accounts_response, Exception):
            raise accounts_response
        return accounts_response

    return httpx.MockTransport(handler)


def _param_to_date(request: httpx.Request, key: str) -> date | None:
    """Decode a start-date/end-date epoch param back to a calendar date.
    Inverts simplefin.fetch_accounts' ``int(datetime(y,m,d).timestamp())``."""
    raw = request.url.params.get(key)
    return None if raw is None else datetime.fromtimestamp(int(raw)).date()


Responder = Callable[[date | None, date | None], dict]


class RecordingBackend:
    """Mock SimpleFIN backend that records each /accounts (start, end) window and
    serves a payload chosen by ``responder(start, end)`` (default: empty)."""

    def __init__(self, responder: Responder | None = None, *, backfill: bool = True) -> None:
        self.calls: list[tuple[date | None, date | None]] = []
        # Repeatable `account` params seen per /accounts call (#154) — separate
        # attribute so the `calls` tuple shape existing tests assert survives.
        self.account_filters: list[list[str]] = []
        self._responder: Responder = responder or (lambda start, end: payload_of([]))
        # Backfill fetches must not carry pending=1; regular sync does (#218
        # reuses this backend for sync-side window assertions).
        self._assert_no_pending = backfill

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/claim/TEST123"):
                return httpx.Response(200, text=ACCESS_URL)
            if request.url.path.endswith("/accounts"):
                start = _param_to_date(request, "start-date")
                end = _param_to_date(request, "end-date")
                self.calls.append((start, end))
                self.account_filters.append(list(request.url.params.get_list("account")))
                if self._assert_no_pending:
                    assert "pending" not in request.url.params  # backfill: include_pending=False
                return httpx.Response(200, json=self._responder(start, end))
            return httpx.Response(404)

        return httpx.MockTransport(handler)


class FlakyBackend:
    """Serves non-empty windows but raises a transport error on the Nth
    /accounts call — drives the SimpleFinError-mid-run path."""

    def __init__(self, *, fail_on_call: int) -> None:
        self.fail_on_call = fail_on_call
        self.calls = 0

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/claim/TEST123"):
                return httpx.Response(200, text=ACCESS_URL)
            if request.url.path.endswith("/accounts"):
                self.calls += 1
                if self.calls >= self.fail_on_call:
                    raise httpx.ConnectError("bridge unreachable")
                # non-empty so the run doesn't stop early on an empty streak
                return httpx.Response(200, json=payload_of([_old_tx(150)]))
            return httpx.Response(404)

        return httpx.MockTransport(handler)


def _row_date(sf_tx: dict[str, Any]) -> date:
    """The ledger date the importer gives a SimpleFIN row (UTC)."""
    return datetime.fromtimestamp(int(sf_tx["posted"]), tz=UTC).date()


def _honouring_window(rows: list[dict[str, Any]]) -> Responder:
    """A bridge that serves only the rows inside the requested window — what
    makes "older than the resync overlap" observable (the static mocks serve
    every row whatever the window)."""

    def respond(start: date | None, end: date | None) -> dict:
        return payload_of(
            [
                row
                for row in rows
                if (start is None or _row_date(row) >= start)
                and (end is None or _row_date(row) <= end)
            ]
        )

    return respond


def _old_tx(days_ago: int, amount: str = "-10.00", desc: str = "Old txn") -> dict:
    """A historical (posted, non-pending) transaction dated ``days_ago``."""
    return {
        "id": f"old-{days_ago}",
        "posted": int((datetime.now(UTC) - timedelta(days=days_ago)).timestamp()),
        "amount": amount,
        "description": desc,
    }


def _seed_fetches(engine: Engine, connection_id: str, count: int) -> None:
    """Insert ``count`` raw_fetches for a connection (append-only friendly) to
    simulate today's fetch-budget usage; also anchors the backfill cursor."""
    from purser import crypto

    with engine.begin() as conn:
        for i in range(count):
            conn.execute(
                text(
                    "INSERT INTO raw_fetches (connection_id, content_hash, payload_enc) "
                    "VALUES (:cid, :h, :enc)"
                ),
                {
                    "cid": connection_id,
                    "h": f"seed-{connection_id}-{i}",
                    "enc": crypto.encrypt("{}"),
                },
            )


def _seed_manual_qif_row(account_path: str, on_date: date, amount: str) -> None:
    """A manual-import ledger row (origin=import, source_fetch_id NULL) — the QIF
    provenance the backfill overlap warning keys on."""
    from purser import db
    from purser import ledger as ledger_service
    from purser.core import ledger as core
    from purser.feed import importer

    with db.get_sessionmaker()() as session:
        category = importer._uncategorized(session, income=False)
        ledger_service.create_flat_transaction(
            session,
            account_ref=account_path,
            category_ref=str(category.id),
            amount=Decimal(amount),
            date=on_date,
            requested_status=core.Status.POSTED,
            origin=core.Origin.IMPORT,
            token=None,
            source_fetch_id=None,
            content_hash=f"manual-qif-{on_date.isoformat()}-{amount}",
        )
        session.commit()


@pytest.fixture
def feed_env(clean_db: None, migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch):
    """Pristine DB for feed tests. The encryption key is suite-wide (conftest);
    the harness below patches HTTP so no real network is touched."""
    yield


class FeedHarness:
    """Drives the API with a swappable mock SimpleFIN backend."""

    def __init__(self, client: TestClient, monkeypatch: pytest.MonkeyPatch):
        self.client = client
        self.monkeypatch = monkeypatch
        assert client.post("/auth/setup", json=ADMIN_CREDENTIALS).status_code == 201

    def use_payload(self, payload: dict) -> None:
        self.use_transport(make_transport(payload))

    def use_transport(self, transport: httpx.MockTransport) -> None:
        def patched_client(*args: object, **kwargs: object) -> httpx.Client:
            kwargs["transport"] = transport
            return RealHttpxClient(*args, **kwargs)  # type: ignore[arg-type]

        self.monkeypatch.setattr(httpx, "Client", patched_client)

    def connect(self) -> str:
        resp = self.client.post("/connections", json={"setup_token": SETUP_TOKEN})
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    def sync(self, connection_id: str) -> dict:
        resp = self.client.post(f"/connections/{connection_id}/sync")
        assert resp.status_code == 200, resp.text
        return resp.json()

    def map_new(self, connection_id: str, external_id: str, path: str) -> dict:
        resp = self.client.post(
            f"/connections/{connection_id}/map",
            json={
                "external_id": external_id,
                "create": {"name": external_id, "type": "checking", "path": path},
            },
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    def backfill(
        self,
        connection_id: str,
        *,
        windows: int | None = None,
        target_date: str | None = None,
        accounts: list[str] | None = None,
    ) -> dict:
        body: dict[str, Any] = {}
        if windows is not None:
            body["windows"] = windows
        if target_date is not None:
            body["target_date"] = target_date
        if accounts is not None:
            body["accounts"] = accounts
        resp = self.client.post(f"/connections/{connection_id}/backfill", json=body)
        assert resp.status_code == 200, resp.text
        return resp.json()

    def health_of(self, connection_id: str) -> dict:
        (row,) = [h for h in self.client.get("/connections").json() if h["id"] == connection_id]
        return row


@pytest.fixture
def harness(feed_env: None, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> FeedHarness:
    return FeedHarness(client, monkeypatch)


class TestClaimAndSecrets:
    def test_access_url_stored_encrypted_only(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        harness.use_payload(payload_of([]))
        connection_id = harness.connect()
        with migrated_engine.connect() as conn:
            enc = conn.execute(
                text("SELECT access_url_enc FROM connections WHERE id = :id"),
                {"id": connection_id},
            ).scalar_one()
        assert ACCESS_URL.encode() not in bytes(enc)
        assert b"secret" not in bytes(enc)

    def test_payload_stored_encrypted_only(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """The archived envelope is ciphertext at rest; content_hash stays
        SHA-256 over the canonical plaintext (idempotency unaffected)."""
        import hashlib

        from purser import crypto

        payload = payload_of(
            [
                {
                    "id": "tx-1",
                    "posted": _epoch(2),
                    "amount": "-12.34",
                    "description": "COFFEE ROASTERS OF EXAMPLEVILLE",
                }
            ]
        )
        harness.use_payload(payload)
        connection_id = harness.connect()
        result = harness.sync(connection_id)
        with migrated_engine.connect() as conn:
            row = conn.execute(
                text("SELECT content_hash, payload_enc FROM raw_fetches WHERE id = :id"),
                {"id": result["raw_fetch_id"]},
            ).one()
        raw = bytes(row.payload_enc)
        assert b"COFFEE ROASTERS" not in raw
        assert b"Demo Checking" not in raw
        assert crypto.decrypt_json(raw) == payload
        canonical = json.dumps(payload, sort_keys=True)
        assert row.content_hash == hashlib.sha256(canonical.encode()).hexdigest()

    def test_connection_response_never_contains_secrets(self, harness: FeedHarness) -> None:
        harness.use_payload(payload_of([]))
        harness.connect()
        listing = harness.client.get("/connections")
        assert "secret" not in listing.text
        assert "bridge.example.com" not in listing.text

    def test_no_credentialed_url_in_logs(
        self, harness: FeedHarness, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Guardrail 7 (#153): httpx's INFO request line printed the SimpleFIN
        access URL — userinfo credentials included — on every bridge fetch.
        configure_logging pins httpx/httpcore to WARNING; capture wide open at
        DEBUG and prove no fetch path (claim, sync, backfill) logs the secret."""
        harness.use_payload(payload_of([]))
        with caplog.at_level(logging.DEBUG):
            logging.getLogger("test-control").info("control marker")
            connection_id = harness.connect()
            harness.sync(connection_id)
            harness.backfill(connection_id, windows=1)
        assert "control marker" in caplog.text  # capture is live — not a vacuous pass
        assert "secret" not in caplog.text
        assert ACCESS_URL not in caplog.text
        assert "bridge.example.com" not in caplog.text

    def test_bad_setup_token_rejected(self, harness: FeedHarness) -> None:
        harness.use_payload(payload_of([]))
        resp = harness.client.post("/connections", json={"setup_token": "not-base64!!"})
        assert resp.status_code == 422

    def test_missing_encryption_key_is_clear_error(
        self, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import os

        import purser.config
        from tests.conftest import TEST_ENCRYPTION_KEY

        # Admin setup writes an (encrypted, #137) audit row, so it needs the key;
        # admin_client creates the admin first, THEN the key is dropped — the
        # assertion is that spending the setup token without a key is a clear,
        # named error.
        os.environ.pop("PURSER_ENCRYPTION_KEY", None)
        purser.config.get_settings.cache_clear()
        try:
            resp = admin_client.post("/connections", json={"setup_token": SETUP_TOKEN})
            assert resp.status_code in (409, 422)
            assert "PURSER_ENCRYPTION_KEY" in resp.text
        finally:
            os.environ["PURSER_ENCRYPTION_KEY"] = TEST_ENCRYPTION_KEY
            purser.config.get_settings.cache_clear()


sf_transactions = st.lists(
    st.builds(
        lambda day, cents, desc: {
            "id": f"unstable-{uuid.uuid4()}",  # ids are junk on purpose
            "posted": _epoch(day),
            "amount": str(Decimal(cents) / 100),
            "description": desc,
        },
        day=st.integers(min_value=1, max_value=28),
        cents=st.integers(min_value=-500000, max_value=500000).filter(lambda c: c != 0),
        desc=st.text(
            alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), max_codepoint=127),
            min_size=1,
            max_size=20,
        ),
    ),
    min_size=1,
    max_size=15,
)


class TestLongIdleGapSync:
    """#218: a sync range wider than BRIDGE_MAX_RANGE_DAYS is paged like
    backfill — older gap windows first (window_start=None), then the final
    bounded window that reconciles pendings as usual."""

    def test_gap_windows_cover_a_long_idle_connection(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        backend = RecordingBackend(backfill=False)
        harness.use_transport(backend.transport())
        connection_id = harness.connect()
        harness.sync(connection_id)
        backend.calls.clear()
        with migrated_engine.begin() as conn:
            conn.execute(text("UPDATE connections SET last_fetch_at = now() - interval '200 days'"))
        result = harness.sync(connection_id)
        # 200d idle + 7d resync overlap = 207d: two 90d gap windows + final.
        assert result["gap_windows"] == 2
        assert len(backend.calls) == 3
        for start, end in backend.calls[:-1]:
            assert start is not None and end is not None
            assert (end - start).days <= simplefin.BRIDGE_MAX_RANGE_DAYS
        final_start, final_end = backend.calls[-1]
        assert final_end is None  # the live window stays unbounded at the end
        assert final_start is not None
        assert (date.today() - final_start).days <= simplefin.BRIDGE_MAX_RANGE_DAYS

    def test_short_gap_stays_a_single_fetch(self, harness: FeedHarness) -> None:
        backend = RecordingBackend(backfill=False)
        harness.use_transport(backend.transport())
        connection_id = harness.connect()
        harness.sync(connection_id)
        backend.calls.clear()
        result = harness.sync(connection_id)
        assert result["gap_windows"] == 0
        assert len(backend.calls) == 1


class TestInjectedClientOwnership:
    """#216: helpers close only clients they created — a caller-owned client
    must survive the call (webhooks' process_due even loops over deliveries
    with one injected client, so closing it broke every delivery after the
    first)."""

    def test_fetch_accounts_leaves_injected_client_open(self) -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"accounts": []}))
        with RealHttpxClient(transport=transport) as client:
            simplefin.fetch_accounts(ACCESS_URL, client=client)
            assert not client.is_closed
            simplefin.fetch_accounts(ACCESS_URL, client=client)  # still usable
        assert client.is_closed  # the owner's exit still closes it

    def test_claim_leaves_injected_client_open(self) -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, text=ACCESS_URL))
        with RealHttpxClient(transport=transport) as client:
            assert simplefin.claim_access_url(SETUP_TOKEN, client=client) == ACCESS_URL
            assert not client.is_closed


class TestImportIdempotency:
    @settings(
        max_examples=10, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(transactions=sf_transactions)
    def test_reimport_creates_zero_new_rows(
        self,
        harness: FeedHarness,
        migrated_engine: Engine,
        transactions: list[dict[str, Any]],
    ) -> None:
        harness.use_payload(payload_of(transactions))
        connection_id = harness.connect()
        harness.sync(connection_id)
        harness.map_new(connection_id, "Demo Checking", f"assets.c{uuid.uuid4().hex[:8]}")

        def count() -> int:
            with migrated_engine.connect() as conn:
                return conn.execute(
                    text("SELECT COUNT(*) FROM transactions WHERE origin = 'import'")
                ).scalar_one()

        first = count()
        assert first > 0
        # Same payload, new fetch (fresh SimpleFIN ids prove ids are ignored).
        for tx in transactions:
            tx["id"] = f"changed-{uuid.uuid4()}"
        harness.use_payload(payload_of(transactions))
        second_sync = harness.sync(connection_id)
        assert count() == first
        assert second_sync["imported"] == 0

    def test_unmapped_accounts_skipped_then_imported_on_map(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        txns = [
            {"id": "1", "posted": _epoch(5), "amount": "-12.34", "description": "Coffee"},
        ]
        harness.use_payload(payload_of(txns))
        connection_id = harness.connect()
        sync1 = harness.sync(connection_id)
        assert sync1["imported"] == 0
        assert sync1["unmapped_accounts"] == 1
        # Mapping re-imports from the ARCHIVE — no refetch needed.
        harness.map_new(connection_id, "Demo Checking", "assets.checking")
        page = harness.client.get("/transactions").json()
        assert page["total"] == 1
        assert Decimal(page["items"][0]["amount"]) == Decimal("-12.34")


def _feed_hash(external_id: str, epoch: int, amount: str, description: str, n: int = 1) -> str:
    """The stored identity of a feed row (keyed form, #137). ``n`` = 1 is the
    recipe as it stood before the occurrence ordinal (#275)."""
    from purser import crypto
    from purser.core import ledger as core

    return crypto.keyed_hash(
        core.content_hash(
            external_account_id=external_id,
            date=datetime.fromtimestamp(epoch, tz=UTC).date(),
            amount=Decimal(amount),
            description=description,
            occurrence=n,
        )
    )


def _hashes_on(engine: Engine, path: str) -> list[str]:
    """Content hashes of every row touching the account, void included."""
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT DISTINCT t.content_hash FROM transactions t"
                    " JOIN postings p ON p.transaction_id = t.id"
                    " JOIN accounts a ON a.id = p.account_id WHERE a.path = :path"
                ),
                {"path": path},
            ).scalars()
        )


def _old_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reproduce the importer as it was before #275: every row occurrence 1,
    so the second of two identical rows dedups against the first."""
    from purser.core import ledger as core

    monkeypatch.setattr(core, "occurrence_ordinals", lambda keys: [1 for _ in keys])


class TestIdenticalRowsInOneFetch:
    """#275, decision 68: two identical same-day charges from one merchant in
    one fetch are occurrences 1 and 2 — two rows. Occurrence 1 keeps the hash
    every row had before, so existing history never re-imports, and the
    7-day overlap re-fetch lands on hashes that already exist."""

    COFFEE = "SQ *CORNER CAFE"

    def _coffee(self, epoch: int, tx_id: str, *, pending: bool = False) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": tx_id,
            "posted": epoch,
            "amount": "-4.50",
            "description": self.COFFEE,
        }
        if pending:
            row["pending"] = True
        return row

    def test_two_identical_rows_import_as_two_and_reimport_adds_none(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        epoch = _recent_epoch(2)
        rows = [
            self._coffee(epoch, "a"),
            {"id": "b", "posted": epoch, "amount": "-62.10", "description": "Grocer"},
            self._coffee(epoch, "c"),
        ]
        harness.use_payload(payload_of(rows))
        cid = harness.connect()
        harness.map_new(cid, "Demo Checking", "assets.checking")
        first = harness.sync(cid)
        assert (first["imported"], first["skipped"]) == (3, 0)
        page = harness.client.get("/transactions", params={"account": "assets.checking"}).json()
        assert page["total"] == 3
        coffees = [r for r in page["items"] if r["description"] == self.COFFEE]
        assert [Decimal(r["amount"]) for r in coffees] == [Decimal("-4.50")] * 2

        # The first coffee carries exactly the pre-#275 identity; the second
        # is occurrence 2.
        stored = set(_hashes_on(migrated_engine, "assets.checking"))
        assert _feed_hash("Demo Checking", epoch, "-4.50", self.COFFEE) in stored
        assert _feed_hash("Demo Checking", epoch, "-4.50", self.COFFEE, 2) in stored

        # Same payload again, fresh SimpleFIN ids: nothing new.
        for row in rows:
            row["id"] = f"changed-{uuid.uuid4()}"
        harness.use_payload(payload_of(rows))
        again = harness.sync(cid)
        assert again["imported"] == 0
        assert again["skipped"] == 3
        assert harness.client.get("/transactions").json()["total"] == 3

    def test_overlap_refetch_lands_the_dropped_twin_once_then_nothing(
        self, harness: FeedHarness, migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pair_day = _recent_epoch(2)
        history: list[dict[str, Any]] = [
            self._coffee(pair_day, "c1"),
            self._coffee(pair_day, "c2"),
            {"id": "g", "posted": _recent_epoch(3), "amount": "-62.10", "description": "Grocer"},
        ]

        def window(start: date | None, end: date | None) -> dict:
            def inside(row: dict[str, Any]) -> bool:
                day = datetime.fromtimestamp(row["posted"], tz=UTC).date()
                return (start is None or day >= start) and (end is None or day <= end)

            return payload_of([dict(row) for row in history if inside(row)])

        backend = RecordingBackend(window, backfill=False)
        harness.use_transport(backend.transport())
        cid = harness.connect()
        harness.map_new(cid, "Demo Checking", "assets.checking")

        # The ledger as the pre-#275 importer left it: the pair merged into one
        # row under the unqualified hash.
        with monkeypatch.context() as patched:
            _old_rule(patched)
            first = harness.sync(cid)
        assert (first["imported"], first["skipped"]) == (2, 1)
        assert set(_hashes_on(migrated_engine, "assets.checking")) == {
            _feed_hash("Demo Checking", pair_day, "-4.50", self.COFFEE),
            _feed_hash("Demo Checking", _recent_epoch(3), "-62.10", "Grocer"),
        }

        def coffees() -> int:
            page = harness.client.get("/transactions", params={"account": "assets.checking"})
            return sum(r["description"] == self.COFFEE for r in page.json()["items"])

        # The next sync re-fetches the 7-day overlap, pair included: the
        # dropped twin lands once, as occurrence 2; nothing else moves.
        fixup = harness.sync(cid)
        overlap_start, _ = backend.calls[-1]
        assert overlap_start is not None
        assert overlap_start <= datetime.fromtimestamp(pair_day, tz=UTC).date()
        assert (fixup["imported"], fixup["skipped"]) == (1, 2)
        assert coffees() == 2

        # Every later overlap re-fetch of the pair adds nothing...
        assert harness.sync(cid)["imported"] == 0
        # ...and only genuinely new rows land, a third identical coffee
        # included (occurrence 3 of that day).
        history.append(self._coffee(pair_day, "c3"))
        history.append(
            {"id": "n", "posted": _recent_epoch(0), "amount": "-9.00", "description": "Kiosk"}
        )
        later = harness.sync(cid)
        assert (later["imported"], later["skipped"]) == (2, 3)
        assert coffees() == 3
        assert harness.sync(cid)["imported"] == 0
        assert harness.client.get("/transactions").json()["total"] == 5

    def test_identical_pendings_keep_their_lifecycle(self, harness: FeedHarness) -> None:
        """Both pendings are still listed on the next fetch, so neither is
        voided — the sweep looks for the same occurrence-qualified identity
        the import stored. When they post, each is superseded by its own
        posted row."""
        epoch = _recent_epoch(2)
        pendings = [
            self._coffee(epoch, "p1", pending=True),
            self._coffee(epoch, "p2", pending=True),
        ]
        harness.use_payload(payload_of(pendings))
        cid = harness.connect()
        harness.map_new(cid, "Demo Checking", "assets.checking")
        harness.sync(cid)

        def by_status(status: str) -> list[dict[str, Any]]:
            params = {"account": "assets.checking", "status": status}
            return harness.client.get("/transactions", params=params).json()["items"]

        assert len(by_status("pending")) == 2

        still = harness.sync(cid)
        assert still["voided_pendings"] == 0
        assert len(by_status("pending")) == 2

        posted_day = _recent_epoch(1)
        harness.use_payload(
            payload_of(
                [
                    {
                        "id": "x1",
                        "posted": posted_day,
                        "amount": "-4.50",
                        "description": "CORNER CAFE",
                    },
                    {
                        "id": "x2",
                        "posted": posted_day,
                        "amount": "-4.50",
                        "description": "CORNER CAFE",
                    },
                ]
            )
        )
        settled = harness.sync(cid)
        assert settled["imported"] == 2
        assert settled["voided_pendings"] == 2
        voided = by_status("void")
        assert len(voided) == 2
        successors = {
            harness.client.get(f"/transactions/{row['id']}").json()["superseded_by"]
            for row in voided
        }
        assert len(successors) == 2 and None not in successors
        assert [Decimal(r["amount"]) for r in by_status("posted")] == [Decimal("-4.50")] * 2

    def test_masked_redelivery_of_an_identical_pair_adds_nothing(
        self, harness: FeedHarness
    ) -> None:
        epoch = _recent_epoch(2)
        unmasked = "EFT FUNDS RECEIVED ER13529574"
        masked = "EFT FUNDS RECEIVED ERXXXX9574"

        def pair(desc: str) -> dict:
            row = {"posted": epoch, "amount": "-750.00", "description": desc}
            return payload_of([{"id": "1", **row}, {"id": "2", **row}])

        harness.use_payload(pair(unmasked))
        cid = harness.connect()
        harness.map_new(cid, "Demo Checking", "assets.checking")
        assert harness.sync(cid)["imported"] == 2

        harness.use_payload(pair(masked))
        again = harness.sync(cid)
        assert again["imported"] == 0
        assert again["skipped_masked_duplicate"] == 2
        assert harness.client.get("/transactions").json()["total"] == 2


identity_pool = st.sampled_from(
    [
        (1, "-4.50", "Corner Cafe"),
        (1, "-4.50", "CORNER CAFE"),  # same identity once normalized
        (1, "-12.00", "Corner Cafe"),
        (2, "-4.50", "Corner Cafe"),
        (2, "250.00", "Payroll"),
    ]
)


class TestOccurrenceIdempotencyProperty:
    """Guardrail 3 with duplicates (decision 68): import a sequence of fetches
    whose rows repeat, within and across fetches. After each one, every
    identity is in the ledger exactly as often as the fetch that carried it
    most; re-importing any earlier fetch adds zero rows."""

    @settings(
        max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(fetches=st.lists(st.lists(identity_pool, max_size=6), min_size=1, max_size=4))
    def test_ledger_holds_the_max_count_and_reimport_adds_none(
        self, feed_env: None, fetches: list[list[tuple[int, str, str]]]
    ) -> None:
        from sqlalchemy import select

        from purser import db
        from purser import ledger as ledger_service
        from purser.core import ledger as core
        from purser.feed import importer
        from purser.models import Connection, Posting, Transaction

        external_id = f"prop-{uuid.uuid4().hex[:12]}"
        path = f"assets.p{uuid.uuid4().hex[:10]}"

        def payload(rows: list[tuple[int, str, str]]) -> dict:
            return payload_of(
                [
                    {"id": uuid.uuid4().hex, "posted": _epoch(day), "amount": amt, "description": d}
                    for day, amt, d in rows
                ],
                account_id=external_id,
            )

        def identity(row: tuple[int, str, str]) -> tuple[int, Decimal, str]:
            day, amt, desc = row
            return day, Decimal(amt), core.normalize_payee(desc)

        with db.get_sessionmaker()() as session:
            connection = Connection()
            session.add(connection)
            session.flush()
            account = ledger_service.create_account(
                session, name=external_id, type_="checking", path=path, actor_token_id=None
            )
            account.connection_id = connection.id
            account.external_id = external_id
            session.flush()

            def ledger_counts() -> dict[tuple[int, Decimal, str], int]:
                rows = session.execute(
                    select(Transaction.date, Posting.amount, Transaction.description)
                    .join(Posting, Posting.transaction_id == Transaction.id)
                    .where(Posting.account_id == account.id)
                ).all()
                counts: dict[tuple[int, Decimal, str], int] = {}
                for when, amount, description in rows:
                    key = (when.day, Decimal(amount), core.normalize_payee(description or ""))
                    counts[key] = counts.get(key, 0) + 1
                return counts

            most: dict[tuple[int, Decimal, str], int] = {}
            archived = []
            for rows in fetches:
                raw = service._archive_payload(session, connection.id, payload(rows))
                archived.append(raw)
                importer.import_fetch(session, raw, window_start=None)
                keys = [identity(r) for r in rows]
                for key in set(keys):
                    most[key] = max(most.get(key, 0), keys.count(key))
                assert ledger_counts() == most

            for raw in archived:
                stats = importer.import_fetch(session, raw, window_start=None)
                assert stats["imported"] == 0
            assert ledger_counts() == most
            session.rollback()


class TestPendingLifecycle:
    def test_pending_becomes_posted_supersedes(self, harness: FeedHarness) -> None:
        pending = {
            "id": "p1",
            "posted": _recent_epoch(2),
            "amount": "-50.00",
            "description": "Gas station",
            "pending": True,
        }
        harness.use_payload(payload_of([pending]))
        connection_id = harness.connect()
        harness.map_new(connection_id, "Demo Checking", "assets.checking")
        harness.sync(connection_id)

        page = harness.client.get("/transactions", params={"status": "pending"}).json()
        assert page["total"] == 1
        pending_id = page["items"][0]["id"]

        # Next fetch: the pending is gone, a posted row (different hash: the
        # date shifted a day, description changed slightly) replaced it.
        posted = {
            "id": "p2-completely-different",
            "posted": _recent_epoch(1),
            "amount": "-50.00",
            "description": "GAS STATION #42",
        }
        harness.use_payload(payload_of([posted]))
        harness.sync(connection_id)

        detail = harness.client.get(f"/transactions/{pending_id}").json()
        assert detail["status"] == "void"
        assert detail["superseded_by"] is not None
        replacement = harness.client.get(f"/transactions/{detail['superseded_by']}").json()
        assert replacement["status"] == "posted"
        assert Decimal(replacement["amount"]) == Decimal("-50.00")

    def test_vanished_pending_voids_with_event_and_audit(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        pending = {
            "id": "p1",
            "posted": _recent_epoch(1),
            "amount": "-9.99",
            "description": "Preauth hold",
            "pending": True,
        }
        harness.use_payload(payload_of([pending]))
        connection_id = harness.connect()
        harness.map_new(connection_id, "Demo Checking", "assets.checking")
        harness.sync(connection_id)

        harness.use_payload(payload_of([]))  # bank dropped it, nothing posted
        sync = harness.sync(connection_id)
        assert sync["voided_pendings"] == 1

        page = harness.client.get("/transactions", params={"status": "void"}).json()
        assert page["total"] == 1
        voided_id = page["items"][0]["id"]
        detail = harness.client.get(f"/transactions/{voided_id}").json()
        assert detail["superseded_by"] is None  # no posted replacement

        with migrated_engine.connect() as conn:
            event_count = conn.execute(
                text(
                    "SELECT COUNT(*) FROM events WHERE type = 'pending.vanished' "
                    "AND entity_id = :id"
                ),
                {"id": voided_id},
            ).scalar_one()
            audit_count = conn.execute(
                text(
                    "SELECT COUNT(*) FROM audit_log WHERE action = 'transaction.void' "
                    "AND entity_id = :id"
                ),
                {"id": voided_id},
            ).scalar_one()
        assert event_count == 1
        assert audit_count == 1

    def test_still_pending_is_left_alone(self, harness: FeedHarness) -> None:
        pending = {
            "id": "p1",
            "posted": _recent_epoch(1),
            "amount": "-9.99",
            "description": "Hold",
            "pending": True,
        }
        harness.use_payload(payload_of([pending]))
        connection_id = harness.connect()
        harness.map_new(connection_id, "Demo Checking", "assets.checking")
        harness.sync(connection_id)
        harness.sync(connection_id)  # same payload again
        page = harness.client.get("/transactions", params={"status": "pending"}).json()
        assert page["total"] == 1

    def test_old_pending_still_listed_stays_open(self, harness: FeedHarness) -> None:
        """A pending older than the resync overlap (a long hotel hold) is
        re-checked on every sync (#282): the fetch reaches back to it, and
        while the bank still lists it, it stays open."""
        pending = {
            "id": "p-old",
            "posted": _recent_epoch(30),  # inside first-sync 90d, outside resync 7d overlap
            "amount": "-250.00",
            "description": "Hotel hold",
            "pending": True,
        }
        harness.use_transport(
            RecordingBackend(_honouring_window([pending]), backfill=False).transport()
        )
        connection_id = harness.connect()
        harness.map_new(connection_id, "Demo Checking", "assets.checking")
        harness.sync(connection_id)  # first sync: 90-day window, pending imports

        backend = RecordingBackend(_honouring_window([pending]), backfill=False)
        harness.use_transport(backend.transport())
        sync = harness.sync(connection_id)
        (start, _end) = backend.calls[-1]
        assert start is not None and start <= _row_date(pending)
        assert sync["voided_pendings"] == 0
        page = harness.client.get("/transactions", params={"status": "pending"}).json()
        assert page["total"] == 1


def _bill_pending(
    amount: str, *, days_ago: int = 0, payee: str = "City Utilities", tx_id: str = "bill-p"
) -> dict[str, Any]:
    """A pending card/bank row for a utility bill (synthetic)."""
    return {
        "id": tx_id,
        "posted": _recent_epoch(days_ago),
        "amount": amount,
        "description": f"{payee.upper()} 0042",
        "payee": payee,
        "pending": True,
    }


class TestMatchedPendingLifecycle:
    """#273 (decision 69): the matcher pairs expected bills with PENDING bank
    rows too, so the feed voiding one must carry the match along — re-pointed
    to the posted successor, or released so the bill is expected again — and
    the schedule estimate counts posted amounts only."""

    def _setup(
        self, harness: FeedHarness, *, amount_type: str = "fixed", amount: str = "-80.00"
    ) -> tuple[str, dict[str, Any]]:
        harness.use_payload(payload_of([]))
        connection_id = harness.connect()
        harness.map_new(connection_id, "Demo Checking", "assets.checking")
        body: dict[str, Any] = {
            "name": "City Utilities",
            "account": "assets.checking",
            "rrule": "FREQ=WEEKLY",  # dtstart today: an instance today, more ahead
            "amount_type": amount_type,
            "amount": amount,
            "payee": "City Utilities",
        }
        if amount_type == "estimated":
            body["estimate_basis"] = "last"
        resp = harness.client.post("/schedules", json=body)
        assert resp.status_code == 201, resp.text
        return connection_id, resp.json()

    def _instances(self, harness: FeedHarness, schedule_id: str) -> list[dict[str, Any]]:
        return list(harness.client.get(f"/schedules/{schedule_id}").json()["instances"])

    def _match_of(self, engine: Engine, expected_id: str) -> Any:
        with engine.connect() as conn:
            return conn.execute(
                text("SELECT id, posted_id, state FROM matches WHERE expected_id = :e"),
                {"e": expected_id},
            ).one()

    def _audit_after(self, harness: FeedHarness, action: str) -> list[dict[str, Any]]:
        items = harness.client.get("/audit", params={"action": action}).json()["items"]
        return [item["before_after"]["after"] for item in items]

    def test_vanish_after_match_makes_the_instance_expected_again(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        connection_id, schedule = self._setup(harness)
        first = self._instances(harness, schedule["id"])[0]
        harness.use_payload(payload_of([_bill_pending("-80.00")]))
        harness.sync(connection_id)
        assert harness.client.post("/matching/run").json()["auto_matched"] == 1
        pending_id = self._match_of(migrated_engine, first["transaction_id"]).posted_id
        settled = harness.client.get(f"/transactions/{first['transaction_id']}").json()
        assert settled["status"] == "void"

        harness.use_payload(payload_of([]))  # the bank dropped it; nothing posted
        assert harness.sync(connection_id)["voided_pendings"] == 1

        assert harness.client.get(f"/transactions/{pending_id}").json()["status"] == "void"
        match = self._match_of(migrated_engine, first["transaction_id"])
        assert match.state == "rejected"  # no live match left on a dead row
        # The bill is due again: a fresh expected fills the SAME slot, at the
        # same amount, and next_due points back at it.
        live = self._instances(harness, schedule["id"])
        slot = [i for i in live if i["occurrence_date"] == first["occurrence_date"]]
        assert len(slot) == 1
        assert slot[0]["transaction_id"] != first["transaction_id"]
        assert slot[0]["amount"] == "-80.0000"
        clone = harness.client.get(f"/transactions/{slot[0]['transaction_id']}").json()
        assert clone["status"] == "expected"
        detail = harness.client.get(f"/schedules/{schedule['id']}").json()
        assert detail["next_due"] == live[0]["date"]
        (released,) = self._audit_after(harness, "match.unmatch")
        assert released["reason"] == "pending_vanished"
        assert released["recreated_expected_id"] == slot[0]["transaction_id"]
        # Materializing again creates nothing for the refilled slot.
        harness.client.post("/schedules/materialize")
        again = self._instances(harness, schedule["id"])
        assert [i["occurrence_date"] for i in again].count(first["occurrence_date"]) == 1

    def test_posted_successor_takes_the_match_and_the_estimate_follows_it(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        connection_id, schedule = self._setup(harness, amount_type="estimated", amount="-100.00")
        first = self._instances(harness, schedule["id"])[0]
        harness.use_payload(payload_of([_bill_pending("-80.00")]))
        harness.sync(connection_id)
        assert harness.client.post("/matching/run").json()["auto_matched"] == 1
        pending_id = self._match_of(migrated_engine, first["transaction_id"]).posted_id

        # Posted amount wins: a match to a still-PENDING row does not move the
        # estimate, so the other instances stay on the fallback.
        harness.client.post("/schedules/materialize")
        assert {i["amount"] for i in self._instances(harness, schedule["id"])} == {"-100.0000"}

        # The bank posts it under a new descriptor: the reconciler supersedes
        # the pending with the posted row, and the match follows.
        posted = {
            "id": "bill-q",
            "posted": _recent_epoch(0),
            "amount": "-80.00",
            "description": "CITY UTILITIES AUTOPAY 0042",
            "payee": "City Utilities",
        }
        harness.use_payload(payload_of([posted]))
        harness.sync(connection_id)
        pending = harness.client.get(f"/transactions/{pending_id}").json()
        assert pending["status"] == "void"
        successor_id = pending["superseded_by"]
        assert successor_id is not None
        match = self._match_of(migrated_engine, first["transaction_id"])
        assert match.state == "confirmed"
        assert str(match.posted_id) == successor_id
        settled = harness.client.get(f"/transactions/{first['transaction_id']}").json()
        assert settled["superseded_by"] == successor_id  # as match.carry does on a merge
        # The instance stays settled — no clone, the slot holds one live row.
        assert first["occurrence_date"] not in {
            i["occurrence_date"] for i in self._instances(harness, schedule["id"])
        }
        (carried,) = self._audit_after(harness, "match.carry")
        assert carried["posted_id"] == successor_id
        assert carried["reason"] == "pending_posted"

        harness.client.post("/schedules/materialize")
        assert {i["amount"] for i in self._instances(harness, schedule["id"])} == {"-80.0000"}
        with migrated_engine.connect() as conn:
            changes = conn.execute(
                text("SELECT COUNT(*) FROM events WHERE type = 'recurring.price_changed'")
            ).scalar_one()
        assert changes == 1  # the posted amount landing IS the price change

    def test_unmatched_pending_vanishing_leaves_matches_and_instances_alone(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        connection_id, schedule = self._setup(harness)
        before = self._instances(harness, schedule["id"])
        stray = _bill_pending("-12.34", payee="Corner Kiosk", tx_id="stray")
        harness.use_payload(payload_of([stray]))
        harness.sync(connection_id)
        assert harness.client.post("/matching/run").json()["auto_matched"] == 0

        harness.use_payload(payload_of([]))
        assert harness.sync(connection_id)["voided_pendings"] == 1

        assert self._instances(harness, schedule["id"]) == before
        with migrated_engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM matches")).scalar_one() == 0
            touched = conn.execute(
                text("SELECT COUNT(*) FROM audit_log WHERE action LIKE 'match.%'")
            ).scalar_one()
        assert touched == 0

    def test_vanished_pending_rescinds_its_proposal(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        connection_id, schedule = self._setup(harness, amount="-180.00")
        first = self._instances(harness, schedule["id"])[0]
        # Close-but-not-identical payee, far-off amount, three days out: a
        # mid-tier score, so the matcher only proposes.
        near = _bill_pending("-95.00", days_ago=3, payee="City Utilities Pmt")
        harness.use_payload(payload_of([near]))
        harness.sync(connection_id)
        assert harness.client.post("/matching/run").json()["proposed"] == 1

        harness.use_payload(payload_of([]))
        harness.sync(connection_id)

        match = self._match_of(migrated_engine, first["transaction_id"])
        assert match.state == "rejected"
        assert harness.client.get("/matching/proposed").json() == []
        # A proposal never voided the instance, so nothing is recreated.
        after = harness.client.get(f"/transactions/{first['transaction_id']}").json()
        assert after["status"] == "expected"
        (rescinded,) = self._audit_after(harness, "match.rescind")
        assert rescinded["reason"] == "pending_vanished"

    def test_posted_pending_has_its_proposal_decided_again(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """A proposal names a row that no longer exists once the pending
        posts, so it is rescinded and its expected matched again at once —
        against the posted row, as a merge does (decision 65)."""
        connection_id, schedule = self._setup(harness, amount="-180.00")
        first = self._instances(harness, schedule["id"])[0]
        near = _bill_pending("-95.00", days_ago=3, payee="City Utilities Pmt")
        harness.use_payload(payload_of([near]))
        harness.sync(connection_id)
        assert harness.client.post("/matching/run").json()["proposed"] == 1

        posted = {
            "id": "near-q",
            "posted": _recent_epoch(2),
            "amount": "-95.00",
            "description": "CITY UTILITIES PMT 0042 POSTED",
            "payee": "City Utilities Pmt",
        }
        harness.use_payload(payload_of([posted]))
        harness.sync(connection_id)

        (proposal,) = harness.client.get("/matching/proposed").json()
        successor = harness.client.get(f"/transactions/{proposal['posted_id']}").json()
        assert successor["status"] == "posted"
        assert proposal["expected_id"] == first["transaction_id"]
        with migrated_engine.connect() as conn:
            states = sorted(
                conn.execute(
                    text("SELECT state FROM matches WHERE expected_id = :e"),
                    {"e": first["transaction_id"]},
                ).scalars()
            )
        assert states == ["proposed", "rejected"]  # the old one rescinded, a fresh one decided
        (rescinded,) = self._audit_after(harness, "match.rescind")
        assert rescinded["reason"] == "pending_posted"

    def test_same_identity_post_keeps_the_bill_settled(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """#282 meets #273: the bank posts the matched pending under its own
        identity; the match follows the posted row and the bill stays paid."""
        connection_id, schedule = self._setup(harness)
        first = self._instances(harness, schedule["id"])[0]
        pending = _bill_pending("-80.00")
        harness.use_payload(payload_of([pending]))
        harness.sync(connection_id)
        assert harness.client.post("/matching/run").json()["auto_matched"] == 1
        pending_id = str(self._match_of(migrated_engine, first["transaction_id"]).posted_id)

        posted = {key: value for key, value in pending.items() if key != "pending"}
        harness.use_payload(payload_of([posted]))
        sync = harness.sync(connection_id)
        assert sync["imported"] == 1
        assert sync["voided_pendings"] == 1

        successor_id = harness.client.get(f"/transactions/{pending_id}").json()["superseded_by"]
        assert successor_id is not None
        match = self._match_of(migrated_engine, first["transaction_id"])
        assert (str(match.posted_id), match.state) == (successor_id, "confirmed")
        settled = harness.client.get(f"/transactions/{first['transaction_id']}").json()
        assert settled["superseded_by"] == successor_id
        assert first["occurrence_date"] not in {
            i["occurrence_date"] for i in self._instances(harness, schedule["id"])
        }

    def test_posting_a_matched_pending_by_hand_emits_the_price_change(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """#330: a person posts the matched pending before the bank does. The
        row is now a posted chain head, so the estimate moves exactly as when
        the feed posts it, and recurring.price_changed fires once."""
        connection_id, schedule = self._setup(harness, amount_type="estimated", amount="-100.00")
        first = self._instances(harness, schedule["id"])[0]
        harness.use_payload(payload_of([_bill_pending("-80.00")]))
        harness.sync(connection_id)
        assert harness.client.post("/matching/run").json()["auto_matched"] == 1
        match = self._match_of(migrated_engine, first["transaction_id"])
        assert match.state == "confirmed"
        harness.client.post("/schedules/materialize")
        assert {i["amount"] for i in self._instances(harness, schedule["id"])} == {"-100.0000"}
        assert (
            harness.client.get("/events", params={"type": "recurring.price_changed"}).json() == []
        )

        resp = harness.client.post(f"/transactions/{match.posted_id}/post")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "posted"

        harness.client.post("/schedules/materialize")
        assert {i["amount"] for i in self._instances(harness, schedule["id"])} == {"-80.0000"}
        (change,) = harness.client.get("/events", params={"type": "recurring.price_changed"}).json()
        assert change["entity_id"] == schedule["id"]
        payload = change["payload"]
        assert (Decimal(payload["old_estimate"]), Decimal(payload["new_estimate"])) == (
            Decimal("-100.00"),
            Decimal("-80.00"),
        )
        assert payload["match_id"] == str(match.id)

    def test_posting_an_unmatched_pending_by_hand_emits_no_price_change(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        connection_id, schedule = self._setup(harness, amount_type="estimated", amount="-100.00")
        stray = _bill_pending("-12.34", payee="Corner Kiosk", tx_id="stray")
        harness.use_payload(payload_of([stray]))
        harness.sync(connection_id)
        assert harness.client.post("/matching/run").json()["auto_matched"] == 0
        (row,) = harness.client.get("/transactions", params={"status": "pending"}).json()["items"]

        resp = harness.client.post(f"/transactions/{row['id']}/post")
        assert resp.status_code == 200, resp.text

        assert (
            harness.client.get("/events", params={"type": "recurring.price_changed"}).json() == []
        )
        harness.client.post("/schedules/materialize")
        assert {i["amount"] for i in self._instances(harness, schedule["id"])} == {"-100.0000"}


def _vanished_events(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT COUNT(*) FROM events WHERE type = 'pending.vanished'")
            ).scalar_one()
        )


class TestPendingRecheck:
    """#282 (decision 70): every open pending is re-checked on every sync, a
    vanished one is paired with its posted successor before it is voided, the
    bank posting a pending under the same identity supersedes it, and a
    pending voided with no successor reaches the digest."""

    def _connect(self, harness: FeedHarness, rows: list[dict[str, Any]]) -> str:
        harness.use_transport(RecordingBackend(_honouring_window(rows), backfill=False).transport())
        connection_id = harness.connect()
        harness.map_new(connection_id, "Demo Checking", "assets.checking")
        harness.sync(connection_id)  # first sync: the full 90-day window
        return connection_id

    def _serve(self, harness: FeedHarness, rows: list[dict[str, Any]]) -> RecordingBackend:
        backend = RecordingBackend(_honouring_window(rows), backfill=False)
        harness.use_transport(backend.transport())
        return backend

    def _only_pending_id(self, harness: FeedHarness) -> str:
        (row,) = harness.client.get("/transactions", params={"status": "pending"}).json()["items"]
        return str(row["id"])

    def test_pending_older_than_the_overlap_that_later_posts(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        hold = {
            "id": "hold",
            "posted": _recent_epoch(20),
            "amount": "-250.00",
            "description": "Harbor Hotel hold",
            "pending": True,
        }
        connection_id = self._connect(harness, [hold])
        pending_id = self._only_pending_id(harness)

        # Weeks later the bank posts it, dated before the 7-day overlap — a
        # row the overlap-only window would never have fetched.
        stay = {
            "id": "stay",
            "posted": _recent_epoch(18),
            "amount": "-250.00",
            "description": "HARBOR HOTEL",
        }
        backend = self._serve(harness, [stay])
        sync = harness.sync(connection_id)

        (start, _end) = backend.calls[-1]
        assert start is not None
        assert start <= _row_date(hold) - timedelta(days=PENDING_MATCH_WINDOW_DAYS)
        assert (sync["imported"], sync["voided_pendings"]) == (1, 1)
        voided = harness.client.get(f"/transactions/{pending_id}").json()
        assert voided["status"] == "void"
        successor = harness.client.get(f"/transactions/{voided['superseded_by']}").json()
        assert successor["status"] == "posted"
        assert Decimal(successor["amount"]) == Decimal("-250.00")
        assert _vanished_events(migrated_engine) == 0
        # Nothing open is left behind, so the window is back to the overlap.
        backend = self._serve(harness, [stay])
        harness.sync(connection_id)
        (start, _end) = backend.calls[-1]
        assert start is not None
        assert start >= date.today() - timedelta(days=service.RESYNC_OVERLAP_DAYS + 1)

    def test_pending_dropped_by_the_bank_is_voided_and_reported(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        refund = {
            "id": "refund",
            "posted": _recent_epoch(20),
            "amount": "86.40",
            "description": "Trailhead Outfitters refund",
            "pending": True,
        }
        connection_id = self._connect(harness, [refund])
        pending_id = self._only_pending_id(harness)

        self._serve(harness, [])  # the bank dropped it, and nothing posted
        assert harness.sync(connection_id)["voided_pendings"] == 1

        voided = harness.client.get(f"/transactions/{pending_id}").json()
        assert voided["status"] == "void"
        assert voided["superseded_by"] is None
        assert _vanished_events(migrated_engine) == 1
        # The morning digest names it: a dropped refund is money to chase.
        from purser import db, digest

        with db.get_sessionmaker()() as session:
            block = digest.whats_new(session)["pendings_vanished"]
        assert block == {
            "count": 1,
            "notable": [
                {
                    "payee": "Trailhead Outfitters refund",
                    "amount": "86.4000",
                    "transaction_id": pending_id,
                }
            ],
            "truncated": False,
        }

    def test_posted_row_with_the_pending_identity_supersedes_it(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        pending = {
            "id": "bakery",
            "posted": _recent_epoch(2),
            "amount": "-42.00",
            "description": "Corner Bakery",
            "pending": True,
        }
        connection_id = self._connect(harness, [pending])
        pending_id = self._only_pending_id(harness)
        with migrated_engine.connect() as conn:
            identity = conn.execute(
                text("SELECT content_hash FROM transactions WHERE id = :id"), {"id": pending_id}
            ).scalar_one()

        # Posted with the same date, amount and description: the same identity.
        posted = {key: value for key, value in pending.items() if key != "pending"}
        self._serve(harness, [posted])
        sync = harness.sync(connection_id)
        assert (sync["imported"], sync["voided_pendings"], sync["skipped"]) == (1, 1, 0)

        voided = harness.client.get(f"/transactions/{pending_id}").json()
        assert voided["status"] == "void"
        successor_id = voided["superseded_by"]
        successor = harness.client.get(f"/transactions/{successor_id}").json()
        assert successor["status"] == "posted"
        assert Decimal(successor["amount"]) == Decimal("-42.00")
        with migrated_engine.connect() as conn:
            hashes = dict(
                conn.execute(
                    text("SELECT id::text, content_hash FROM transactions WHERE id IN (:a, :b)"),
                    {"a": pending_id, "b": successor_id},
                ).all()
            )
        assert hashes == {successor_id: identity, pending_id: None}  # the identity moved
        assert _vanished_events(migrated_engine) == 0
        items = harness.client.get("/audit", params={"action": "transaction.void"}).json()["items"]
        (void_after,) = [i["before_after"]["after"] for i in items if i["entity_id"] == pending_id]
        assert void_after["reason"] == "pending_posted"
        assert void_after["identity_moved"] is True

        # Idempotent: the same payload again lands nothing and voids nothing.
        again = harness.sync(connection_id)
        assert (again["imported"], again["voided_pendings"], again["skipped"]) == (0, 0, 1)

    def test_two_identical_pendings_post_together_and_both_supersede(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """Two identical same-day charges are occurrences 1 and 2 of one
        content (decision 68). Pending, then posted in one payload, each
        posted row takes over ITS pending's identity — the occurrence-
        qualified hash from ``_row_hashes`` — so both supersede, none
        vanishes, and nothing is left open."""
        charge = {
            "posted": _recent_epoch(1),
            "amount": "-4.75",
            "description": "Harbor Coffee",
            "pending": True,
        }
        pendings = [{**charge, "id": "c1"}, {**charge, "id": "c2"}]
        connection_id = self._connect(harness, pendings)
        page = harness.client.get("/transactions", params={"status": "pending"}).json()
        pending_ids = sorted(row["id"] for row in page["items"])
        assert len(pending_ids) == 2
        with migrated_engine.connect() as conn:
            identities = dict(
                conn.execute(
                    text("SELECT id::text, content_hash FROM transactions WHERE id IN (:a, :b)"),
                    {"a": pending_ids[0], "b": pending_ids[1]},
                ).all()
            )
        assert len(set(identities.values())) == 2  # occurrences 1 and 2

        posted = [{k: v for k, v in row.items() if k != "pending"} for row in pendings]
        self._serve(harness, posted)
        sync = harness.sync(connection_id)
        assert (sync["imported"], sync["voided_pendings"], sync["skipped"]) == (2, 2, 0)

        for pending_id in pending_ids:
            voided = harness.client.get(f"/transactions/{pending_id}").json()
            assert voided["status"] == "void"
            successor_id = voided["superseded_by"]
            successor = harness.client.get(f"/transactions/{successor_id}").json()
            assert successor["status"] == "posted"
            with migrated_engine.connect() as conn:
                moved = conn.execute(
                    text("SELECT content_hash FROM transactions WHERE id = :id"),
                    {"id": successor_id},
                ).scalar_one()
            assert moved == identities[pending_id]  # its own occurrence's identity
        assert (
            harness.client.get("/transactions", params={"status": "pending"}).json()["total"] == 0
        )
        assert _vanished_events(migrated_engine) == 0
        again = harness.sync(connection_id)
        assert (again["imported"], again["voided_pendings"], again["skipped"]) == (0, 0, 2)

    def test_successor_that_already_settles_a_bill_still_supersedes(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """Some banks list the posted row while the pending is still listed;
        the matcher can settle a bill with it before the pending goes. The
        bill's lineage link to the posted row is no claim on the bank line,
        so the pending is still paired with it rather than voided as
        vanished (#282)."""
        pending = {
            "id": "water-p",
            "posted": _recent_epoch(1),
            "amount": "-64.00",
            "description": "METRO WATER",
            "payee": "Metro Water",
            "pending": True,
        }
        posted = {
            "id": "water-q",
            "posted": _recent_epoch(0),
            "amount": "-64.00",
            "description": "METRO WATER UTIL 77",
            "payee": "Metro Water",
        }
        connection_id = self._connect(harness, [pending, posted])
        pending_id = self._only_pending_id(harness)
        (posted_row,) = harness.client.get("/transactions", params={"status": "posted"}).json()[
            "items"
        ]
        bill = harness.client.post(
            "/transactions",
            json={
                "account": "assets.checking",
                "category": "expenses.uncategorized",
                "amount": "-64.00",
                "date": date.today().isoformat(),
                "payee": "Metro Water",
                "status": "expected",
            },
        )
        assert bill.status_code == 201, bill.text
        matched = harness.client.post(
            "/matching/match",
            json={"expected_id": bill.json()["id"], "posted_id": posted_row["id"]},
        )
        assert matched.status_code == 200, matched.text

        self._serve(harness, [posted])  # now the pending is gone
        harness.sync(connection_id)

        voided = harness.client.get(f"/transactions/{pending_id}").json()
        assert voided["status"] == "void"
        assert voided["superseded_by"] == posted_row["id"]
        assert _vanished_events(migrated_engine) == 0
        with migrated_engine.connect() as conn:
            state = conn.execute(
                text("SELECT state FROM matches WHERE posted_id = :p"), {"p": posted_row["id"]}
            ).scalar_one()
        assert state == "confirmed"  # the bill stays settled

    def test_a_pending_beyond_the_bridge_range_widens_nothing(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """One live fetch serves at most BRIDGE_MAX_RANGE_DAYS: a pending
        older than that is beyond the feed's reach, so it neither widens the
        window (every sync would pay for a fetch that can never settle it)
        nor gets voided by a fetch that could not see it."""
        ancient = {
            "id": "ancient",
            "posted": _recent_epoch(simplefin.BRIDGE_MAX_RANGE_DAYS + 30),
            "amount": "-75.00",
            "description": "Old preauthorization",
            "pending": True,
        }
        harness.use_payload(payload_of([ancient]))  # a static mock serves any date
        connection_id = harness.connect()
        harness.map_new(connection_id, "Demo Checking", "assets.checking")
        harness.sync(connection_id)
        pending_id = self._only_pending_id(harness)

        backend = self._serve(harness, [])
        sync = harness.sync(connection_id)
        assert len(backend.calls) == 1  # no gap windows either
        (start, _end) = backend.calls[-1]
        assert start is not None
        assert start >= date.today() - timedelta(days=service.RESYNC_OVERLAP_DAYS + 1)
        assert sync["voided_pendings"] == 0
        assert harness.client.get(f"/transactions/{pending_id}").json()["status"] == "pending"


# A bank's view of one account over a few syncs: a small pool of identities
# (so the same line recurs, as pending and as posted), dated up to six weeks
# back (so most fall outside the 7-day resync overlap).
_IDENTITIES = st.lists(
    st.tuples(
        st.integers(min_value=0, max_value=40),
        st.sampled_from(["-12.00", "-40.00", "25.00"]),
        st.sampled_from(["Cafe", "Grocer", "Refund desk"]),
    ),
    min_size=1,
    max_size=5,
    unique=True,
)
# Each bank state lists some identities, each as pending or as posted.
_BANK_STATES = st.lists(
    st.lists(st.tuples(st.integers(min_value=0, max_value=4), st.booleans()), max_size=5),
    min_size=1,
    max_size=4,
)


class TestPendingReviewCarry:
    """#342 (decision 82) end to end: a pending someone reviewed does not
    come back for review once the bank posts it — through the successor
    pairing (a re-described posted row) or the identity move (the same
    row, ``pending`` dropped) — while an unreviewed pending's successor
    stays in the set."""

    def _connect(self, harness: FeedHarness, rows: list[dict[str, Any]]) -> str:
        harness.use_transport(RecordingBackend(_honouring_window(rows), backfill=False).transport())
        connection_id = harness.connect()
        harness.map_new(connection_id, "Demo Checking", "assets.checking")
        harness.sync(connection_id)
        return connection_id

    def _serve(self, harness: FeedHarness, rows: list[dict[str, Any]]) -> None:
        harness.use_transport(RecordingBackend(_honouring_window(rows), backfill=False).transport())

    def _pending_id(self, harness: FeedHarness) -> str:
        (row,) = harness.client.get("/transactions", params={"status": "pending"}).json()["items"]
        return str(row["id"])

    def _mark(self, harness: FeedHarness, tx_id: str) -> None:
        resp = harness.client.post(
            "/transactions/mark-reviewed",
            json={"transaction_ids": [tx_id], "note": "looked at it"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["marked"] == 1

    def _unreviewed(self, harness: FeedHarness) -> set[str]:
        page = harness.client.get("/transactions", params={"reviewed": False, "limit": 200}).json()
        return {str(item["id"]) for item in page["items"]}

    def _carried(self, engine: Engine, tx_id: str) -> Any:
        with engine.connect() as conn:
            return conn.execute(
                text(
                    "SELECT via, reviewed_by, reviewed_at, carried_from::text AS carried_from"
                    " FROM transaction_reviews WHERE transaction_id = :t"
                ),
                {"t": tx_id},
            ).one_or_none()

    def test_identity_move_carries_the_review(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        pending = {
            "id": "bakery",
            "posted": _recent_epoch(2),
            "amount": "-42.00",
            "description": "Corner Bakery",
            "pending": True,
        }
        connection_id = self._connect(harness, [pending])
        pending_id = self._pending_id(harness)
        self._mark(harness, pending_id)
        source = self._carried(migrated_engine, pending_id)

        self._serve(harness, [{k: v for k, v in pending.items() if k != "pending"}])
        sync = harness.sync(connection_id)
        assert (sync["imported"], sync["voided_pendings"]) == (1, 1)

        successor_id = harness.client.get(f"/transactions/{pending_id}").json()["superseded_by"]
        assert successor_id is not None
        assert successor_id not in self._unreviewed(harness)
        assert harness.client.get(f"/transactions/{successor_id}").json()["needs_review"] is False
        carried = self._carried(migrated_engine, successor_id)
        assert (carried.via, carried.reviewed_by, carried.reviewed_at) == (
            "explicit",
            source.reviewed_by,
            source.reviewed_at,
        )
        assert carried.carried_from == pending_id

    def test_successor_pairing_carries_the_review(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        hold = {
            "id": "hold",
            "posted": _recent_epoch(3),
            "amount": "-64.10",
            "description": "Harbor Market hold",
            "pending": True,
        }
        connection_id = self._connect(harness, [hold])
        pending_id = self._pending_id(harness)
        self._mark(harness, pending_id)

        # The bank drops the pending and posts a re-described row instead.
        posted = {
            "id": "posted",
            "posted": _recent_epoch(2),
            "amount": "-64.10",
            "description": "HARBOR MARKET 0042",
        }
        self._serve(harness, [posted])
        sync = harness.sync(connection_id)
        assert (sync["imported"], sync["voided_pendings"]) == (1, 1)

        successor_id = harness.client.get(f"/transactions/{pending_id}").json()["superseded_by"]
        assert successor_id is not None
        assert successor_id not in self._unreviewed(harness)
        assert self._carried(migrated_engine, successor_id).carried_from == pending_id

    @pytest.mark.parametrize("posted_as", ["Corner Bakery", "CORNER BAKERY 7"])
    def test_an_unreviewed_pendings_successor_stays_unreviewed(
        self, harness: FeedHarness, migrated_engine: Engine, posted_as: str
    ) -> None:
        """Both paths: the same description is the identity move, a new one
        the successor pairing."""
        pending = {
            "id": "bakery",
            "posted": _recent_epoch(2),
            "amount": "-42.00",
            "description": "Corner Bakery",
            "pending": True,
        }
        connection_id = self._connect(harness, [pending])
        pending_id = self._pending_id(harness)

        posted = {"id": "posted", "posted": _recent_epoch(2), "amount": "-42.00"}
        self._serve(harness, [{**posted, "description": posted_as}])
        assert harness.sync(connection_id)["voided_pendings"] == 1

        successor_id = harness.client.get(f"/transactions/{pending_id}").json()["superseded_by"]
        assert successor_id is not None
        assert successor_id in self._unreviewed(harness)
        assert self._carried(migrated_engine, successor_id) is None


class TestPendingLifecycleProperties:
    """The pending lifecycle as a property (guardrail 3, #282): whatever the
    bank lists, sync by sync, (a) re-importing the fetch just archived is a
    no-op, (b) no open pending survives a sync that no longer lists it as
    pending — however old it is, (c) no open pending holds an identity the
    bank lists as posted, and (d) every voided feed pending is explained, by
    a posted successor or by a ``pending.vanished`` event.

    (a) is about the archived payload, not a second sync: a sync's window
    follows the open pendings, so the next sync may legitimately reach
    further back and import rows the previous fetch never covered."""

    @settings(
        max_examples=10, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(identities=_IDENTITIES, states=_BANK_STATES)
    def test_every_sync_settles_every_open_pending(
        self,
        harness: FeedHarness,
        migrated_engine: Engine,
        identities: list[tuple[int, str, str]],
        states: list[list[tuple[int, bool]]],
    ) -> None:
        from purser import db
        from purser.feed import importer
        from purser.models import RawFetch

        # Examples share the test's database: a fresh external id keeps each
        # example's identities (hashes) its own.
        external = f"Card {uuid.uuid4().hex[:8]}"
        now = datetime.now(UTC)

        def rows_for(state: list[tuple[int, bool]]) -> list[dict[str, Any]]:
            # An identity may be listed twice: identical rows are occurrences
            # 1 and 2 of one content (decision 68), pending or posted alike.
            rows = []
            for index, pending in state:
                days_ago, amount, description = identities[index % len(identities)]
                row: dict[str, Any] = {
                    "id": f"unstable-{uuid.uuid4()}",
                    "posted": int((now - timedelta(days=days_ago)).timestamp()),
                    "amount": amount,
                    "description": description,
                }
                if pending:
                    row["pending"] = True
                rows.append(row)
            return rows

        def identities_of(rows: list[dict[str, Any]]) -> list[str]:
            """The stored identity of each row: the importer's own per-row
            hashes, occurrence ordinals included — never a fresh recipe call."""
            return importer._row_hashes({"id": external, "transactions": rows})

        def serve(rows: list[dict[str, Any]]) -> RecordingBackend:
            def respond(start: date | None, end: date | None) -> dict:
                return payload_of(
                    [
                        row
                        for row in rows
                        if (start is None or _row_date(row) >= start)
                        and (end is None or _row_date(row) <= end)
                    ],
                    account_id=external,
                )

            backend = RecordingBackend(respond, backfill=False)
            harness.use_transport(backend.transport())
            return backend

        serve([])
        connection_id = harness.connect()
        account_id = harness.map_new(connection_id, external, f"assets.p{uuid.uuid4().hex[:8]}")[
            "account_id"
        ]
        for state in states:
            rows = rows_for(state)
            backend = serve(rows)
            harness.sync(connection_id)
            (window_start, _end) = backend.calls[-1]
            with db.get_sessionmaker()() as session:
                raw = session.execute(
                    select(RawFetch)
                    .where(RawFetch.connection_id == uuid.UUID(connection_id))
                    .order_by(RawFetch.fetched_at.desc(), RawFetch.id.desc())
                    .limit(1)
                ).scalar_one()
                again = importer.import_fetch(session, raw, window_start=window_start)
                session.commit()
            assert (again["imported"], again["voided_pendings"]) == (0, 0)  # (a)

            hashes = identities_of(rows)
            listed_pending = {h for r, h in zip(rows, hashes, strict=True) if r.get("pending")}
            listed_posted = {h for r, h in zip(rows, hashes, strict=True) if not r.get("pending")}
            with migrated_engine.connect() as conn:
                feed_rows = conn.execute(
                    text(
                        "SELECT t.id, t.status, t.content_hash, t.superseded_by IS NOT NULL AS "
                        "succeeded, EXISTS (SELECT 1 FROM events e WHERE e.type = "
                        "'pending.vanished' AND e.entity_id = t.id) AS vanished "
                        "FROM transactions t JOIN postings p ON p.transaction_id = t.id "
                        "WHERE p.account_id = :a AND t.origin = 'import'"
                    ),
                    {"a": account_id},
                ).all()
            open_pending = {r.content_hash for r in feed_rows if r.status == "pending"}
            assert open_pending <= listed_pending  # (b)
            assert not open_pending & listed_posted  # (c)
            for r in feed_rows:
                if r.status == "void":
                    assert r.succeeded or r.vanished  # (d)


def _serve_accounts(
    harness: FeedHarness, accounts: dict[str, list[dict[str, Any]]]
) -> RecordingBackend:
    """A bridge serving several accounts, each only the rows inside the
    requested window (the multi-account ``_honouring_window``)."""

    def respond(start: date | None, end: date | None) -> dict:
        return multi_payload_of(
            [
                account_of(
                    [
                        row
                        for row in rows
                        if (start is None or _row_date(row) >= start)
                        and (end is None or _row_date(row) <= end)
                    ],
                    account_id=external_id,
                )
                for external_id, rows in accounts.items()
            ]
        )

    backend = RecordingBackend(respond, backfill=False)
    harness.use_transport(backend.transport())
    return backend


def _map_typed(
    harness: FeedHarness, connection_id: str, external_id: str, path: str, type_: str
) -> None:
    resp = harness.client.post(
        f"/connections/{connection_id}/map",
        json={
            "external_id": external_id,
            "create": {"name": external_id, "type": type_, "path": path},
        },
    )
    assert resp.status_code == 200, resp.text


def _last_sync_after() -> dict[str, Any]:
    """The ``after`` block of the newest ``connection.sync`` audit row."""
    from purser import db
    from purser.models import AuditLog

    with db.get_sessionmaker()() as session:
        row = session.execute(
            select(AuditLog)
            .where(AuditLog.action == "connection.sync")
            .order_by(AuditLog.at.desc())
            .limit(1)
        ).scalar_one()
        assert row.before_after is not None
        return dict(row.before_after["after"])


def _live_rows(harness: FeedHarness, path: str) -> list[dict[str, Any]]:
    return harness.client.get(
        "/transactions", params={"account": path, "status": ["pending", "posted"]}
    ).json()["items"]


class TestWidenedTail:
    """#331 (decision 74): only accounts whose balance is the postings sum
    widen a sync's fetch, and the widened tail (rows dated before the resync
    overlap) skips a re-described copy of a posted row the ledger holds."""

    CARD = "Demo Card"
    CARD_PATH = "liabilities.card"

    def _connect_card(self, harness: FeedHarness, rows: list[dict[str, Any]]) -> str:
        _serve_accounts(harness, {self.CARD: rows})
        connection_id = harness.connect()
        _map_typed(harness, connection_id, self.CARD, self.CARD_PATH, "credit_card")
        harness.sync(connection_id)  # first sync: the full 90-day window
        return connection_id

    @staticmethod
    def _rows() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """(hold, original, redescribed), dated from now. The hold is an open
        card pending the bank keeps listing, so every sync after the first is
        widened back to 22 days ago. The original is a posted card credit 20
        days back, past the 7-day overlap; the redescribed row is that credit
        under the description the bank gave it later."""
        hold = {
            "id": "hold",
            "posted": _recent_epoch(15),
            "amount": "-19.00",
            "description": "Garage parking hold",
            "pending": True,
        }
        original = {
            "id": "refund",
            "posted": _recent_epoch(20),
            "amount": "48.00",
            "description": "HARBOR BOOKS PORTLANDOR",
        }
        redescribed = {**original, "id": "refund-2", "description": "HARBOR BOOKS PORTLAND CREDIT"}
        return hold, original, redescribed

    @pytest.mark.parametrize(
        ("valued_type", "valued_path"),
        [
            ("brokerage", "assets.retirement"),
            ("loan", "liabilities.mortgage"),
            ("property", "assets.house"),
        ],
    )
    def test_only_postings_balance_accounts_widen_the_fetch(
        self, harness: FeedHarness, valued_type: str, valued_path: str
    ) -> None:
        """A brokerage feed can list every fund purchase as pending forever;
        loans and property are valued by valuations. Their open pendings move
        no balance and widen nothing, while a card's does."""
        purchase = {
            "id": "purchase",
            "posted": _recent_epoch(40),
            "amount": "-500.00",
            "description": "Fund purchase",
            "pending": True,
        }
        hold, _original, _redescribed = self._rows()
        hold["posted"] = _recent_epoch(20)
        _serve_accounts(harness, {"Demo Valued": [purchase], self.CARD: [hold]})
        connection_id = harness.connect()
        _map_typed(harness, connection_id, "Demo Valued", valued_path, valued_type)
        _map_typed(harness, connection_id, self.CARD, self.CARD_PATH, "credit_card")
        harness.sync(connection_id)

        # The card hold widens the next fetch; the older purchase does not.
        backend = _serve_accounts(harness, {"Demo Valued": [purchase], self.CARD: []})
        sync = harness.sync(connection_id)
        (start, _end) = backend.calls[-1]
        assert start == _row_date(hold) - timedelta(days=PENDING_MATCH_WINDOW_DAYS)
        assert sync["voided_pendings"] == 1  # the bank dropped the hold

        # Nothing open on the card: back to the overlap, though the
        # purchase is still pending.
        backend = _serve_accounts(harness, {"Demo Valued": [purchase], self.CARD: []})
        harness.sync(connection_id)
        (start, _end) = backend.calls[-1]
        assert start is not None
        assert start >= date.today() - timedelta(days=service.RESYNC_OVERLAP_DAYS + 1)
        (still_open,) = _live_rows(harness, valued_path)
        assert still_open["status"] == "pending"

    def test_redescribed_copy_in_the_tail_imports_nothing(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        hold, credit, redescribed = self._rows()
        connection_id = self._connect_card(harness, [credit, hold])
        (original,) = [r for r in _live_rows(harness, self.CARD_PATH) if r["status"] == "posted"]

        # The bank re-described the credit; the widened fetch reaches it.
        backend = _serve_accounts(harness, {self.CARD: [redescribed, hold]})
        sync = harness.sync(connection_id)
        (start, _end) = backend.calls[-1]
        assert start is not None and start < _row_date(credit)  # widened past it
        assert (sync["imported"], sync["skipped_redescribed"]) == (0, 1)
        assert sorted(r["status"] for r in _live_rows(harness, self.CARD_PATH)) == [
            "pending",
            "posted",
        ]

        after = _last_sync_after()
        assert after["skipped_redescribed"] == 1
        assert after["overlap_start"] > after["window_start"]  # ISO dates compare as text
        assert after["redescribed"] == [
            {
                "account_id": _account_uuid(migrated_engine, self.CARD_PATH),
                "date": _row_date(credit).isoformat(),
                "amount": "48.0000",
                "original_id": original["id"],
            }
        ]

        # Re-fetching the same widened payload imports nothing new either.
        again = harness.sync(connection_id)
        assert (again["imported"], again["skipped_redescribed"]) == (0, 1)
        assert len(_live_rows(harness, self.CARD_PATH)) == 2

    def test_late_row_with_no_neighbour_imports(self, harness: FeedHarness) -> None:
        hold, credit, _redescribed = self._rows()
        connection_id = self._connect_card(harness, [credit, hold])

        late = {
            "id": "late",
            "posted": _recent_epoch(18),
            "amount": "-33.00",
            "description": "Ferry",
        }
        _serve_accounts(harness, {self.CARD: [credit, late, hold]})
        sync = harness.sync(connection_id)
        assert (sync["imported"], sync["skipped_redescribed"]) == (1, 0)
        assert len(_live_rows(harness, self.CARD_PATH)) == 3

    def test_late_row_beside_a_neighbour_the_bank_still_lists_imports(
        self, harness: FeedHarness
    ) -> None:
        """Same date, same amount, another description — but the original is
        still listed, so this is a second purchase that posted late, not the
        original re-described."""
        hold, credit, redescribed = self._rows()
        connection_id = self._connect_card(harness, [credit, hold])

        _serve_accounts(harness, {self.CARD: [credit, redescribed, hold]})
        sync = harness.sync(connection_id)
        assert (sync["imported"], sync["skipped_redescribed"]) == (1, 0)

    def test_masked_redelivery_keeps_its_original_listed(self, harness: FeedHarness) -> None:
        """A payload listing the original only with its reference masked
        (decision 44) still lists it: a distinct same-day row beside it is a
        late row, not the original re-described."""
        transfer = {
            "id": "t1",
            "posted": _recent_epoch(20),
            "amount": "-750.00",
            "description": "TRANSFER REF 1234",
        }
        masked = {**transfer, "id": "t1m", "description": "TRANSFER REF XX34"}
        second = {**transfer, "id": "t2", "description": "TRANSFER REF 5678"}
        hold, _credit, _redescribed = self._rows()
        connection_id = self._connect_card(harness, [transfer, hold])

        _serve_accounts(harness, {self.CARD: [second, masked, hold]})
        sync = harness.sync(connection_id)
        assert sync["skipped_masked_duplicate"] == 1
        assert (sync["imported"], sync["skipped_redescribed"]) == (1, 0)

    def test_rows_inside_the_overlap_are_not_checked(self, harness: FeedHarness) -> None:
        hold, credit, _redescribed = self._rows()
        recent = {**credit, "id": "recent", "posted": _recent_epoch(3)}
        connection_id = self._connect_card(harness, [recent, hold])

        # Widened by the hold, but the re-described row sits inside the
        # 7-day overlap: it lands as before #331.
        renamed = {**recent, "id": "recent-2", "description": "HARBOR BOOKS PORTLAND CREDIT"}
        _serve_accounts(harness, {self.CARD: [renamed, hold]})
        sync = harness.sync(connection_id)
        assert (sync["imported"], sync["skipped_redescribed"]) == (1, 0)

    def test_a_row_a_vanished_pending_may_take_is_kept(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """#282's successor pairing comes first: a tail row that a pending the
        bank no longer lists may take as its posted successor (same amount,
        within 7 days) is imported, even beside an original it would
        otherwise replace — here the original already settled another
        pending, so the row is the only successor left."""
        water = {
            "id": "w0",
            "posted": _recent_epoch(20),
            "amount": "-64.00",
            "description": "CITY WATER PRE",
            "pending": True,
        }
        sewer = {
            "id": "s0",
            "posted": _recent_epoch(19),
            "amount": "-64.00",
            "description": "CITY SEWER PRE",
            "pending": True,
        }
        connection_id = self._connect_card(harness, [water, sewer])
        pendings = {r["description"]: r["id"] for r in _live_rows(harness, self.CARD_PATH)}

        # The water pending posts as `posted`, which then holds its own line.
        posted = {
            "id": "w1",
            "posted": _recent_epoch(20),
            "amount": "-64.00",
            "description": "CITY WATER",
        }
        _serve_accounts(harness, {self.CARD: [posted, sewer]})
        assert harness.sync(connection_id)["voided_pendings"] == 1

        # Next, the bank lists one row for that day and amount under a new
        # description, and no longer lists the sewer pending.
        renamed = {**posted, "id": "w2", "description": "CITY UTILITIES 77"}
        _serve_accounts(harness, {self.CARD: [renamed]})
        sync = harness.sync(connection_id)
        assert (sync["imported"], sync["skipped_redescribed"]) == (1, 0)
        sewer_row = harness.client.get(f"/transactions/{pendings['CITY SEWER PRE']}").json()
        assert sewer_row["status"] == "void"
        successor = harness.client.get(f"/transactions/{sewer_row['superseded_by']}").json()
        assert successor["description"] == "CITY UTILITIES 77"
        assert _vanished_events(migrated_engine) == 0

    def test_one_original_stands_in_for_one_copy(self, harness: FeedHarness) -> None:
        """Two new rows beside one original the bank no longer lists: one is
        the original re-described, the other a distinct row — skip one."""
        hold, credit, redescribed = self._rows()
        connection_id = self._connect_card(harness, [credit, hold])

        other = {**credit, "id": "other", "description": "CORNER STATIONERS"}
        _serve_accounts(harness, {self.CARD: [redescribed, other, hold]})
        sync = harness.sync(connection_id)
        assert (sync["imported"], sync["skipped_redescribed"]) == (1, 1)
        again = harness.sync(connection_id)
        assert (again["imported"], again["skipped_redescribed"]) == (0, 1)

    def test_reimports_of_the_widened_fetch_skip_what_the_sync_skipped(
        self, harness: FeedHarness
    ) -> None:
        """Guardrail 3 on every path: mapping another account re-imports the
        latest fetch, and replay re-imports them all. Neither brings back the
        copy the sync skipped (both take the overlap behind the fetch)."""
        from purser import db
        from purser.admin import feed_repair

        hold, credit, redescribed = self._rows()
        connection_id = self._connect_card(harness, [credit, hold])
        _serve_accounts(harness, {self.CARD: [redescribed, hold], "Demo Checking": [_old_tx(12)]})
        assert harness.sync(connection_id)["skipped_redescribed"] == 1

        harness.map_new(connection_id, "Demo Checking", "assets.checking")  # re-imports it
        assert len(_live_rows(harness, "assets.checking")) == 1
        assert len(_live_rows(harness, self.CARD_PATH)) == 2

        with db.get_sessionmaker()() as session:
            _connection, fetch_ids = feed_repair.replay_plan(session, connection_id, None)
            totals = feed_repair.execute_replay(session, fetch_ids, actor_token_id=None)
        assert totals["imported"] == 0
        assert totals["skipped_redescribed"] == 1
        assert len(_live_rows(harness, self.CARD_PATH)) == 2


# One account's posted history in the tail (8-40 days back, past the 7-day
# overlap), drawn from a small pool so dates and amounts collide.
_TAIL_HELD = st.lists(
    st.tuples(
        st.integers(min_value=8, max_value=40),
        st.sampled_from(["-12.00", "30.00"]),
        st.sampled_from(["Cafe", "Grocer", "Book shop"]),
    ),
    min_size=1,
    max_size=6,
    unique=True,
)
# What the bank lists for each held row on the widened sync: as is, under a
# new description, or not at all.
_FATES = st.lists(st.sampled_from(["listed", "redescribed", "gone"]), min_size=6, max_size=6)
# Rows the bank lists late in the tail, never seen before.
_TAIL_LATE = st.lists(
    st.tuples(
        st.integers(min_value=8, max_value=40),
        st.sampled_from(["-12.00", "30.00", "-7.25"]),
        st.sampled_from(["Late cafe", "Late grocer"]),
    ),
    max_size=3,
    unique=True,
)


class TestWidenedTailProperties:
    """The re-description guard as a property (#331, guardrail 3): whatever a
    widened sync carries in its tail, (a) re-fetching the same payload and
    re-importing the archived fetch import nothing new, (b) every skipped row
    names an original on its date for its amount that the payload no longer
    lists, at most one skip per original, and (c) a late row with no posted
    row on its date for its amount always lands."""

    @settings(
        max_examples=12, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(held=_TAIL_HELD, fates=_FATES, late=_TAIL_LATE)
    def test_refetching_a_widened_payload_imports_nothing(
        self,
        harness: FeedHarness,
        migrated_engine: Engine,
        held: list[tuple[int, str, str]],
        fates: list[str],
        late: list[tuple[int, str, str]],
    ) -> None:
        from purser import db
        from purser.feed import importer
        from purser.models import RawFetch

        external = f"Card {uuid.uuid4().hex[:8]}"  # examples share the database
        now = datetime.now(UTC)

        def row(days_ago: int, amount: str, description: str, **extra: Any) -> dict[str, Any]:
            return {
                "id": f"unstable-{uuid.uuid4()}",
                "posted": int((now - timedelta(days=days_ago)).timestamp()),
                "amount": amount,
                "description": description,
                **extra,
            }

        # Keeps every sync after the first widened to 52 days back.
        hold = row(45, "-1.00", "Long hold", pending=True)
        history = [row(*h) for h in held]
        late_rows = [row(*r) for r in late]
        listed: list[dict[str, Any]] = [hold]
        for h, fate in zip(history, fates, strict=False):
            if fate == "listed":
                listed.append(h)
            elif fate == "redescribed":
                listed.append({**h, "id": "x", "description": f"{h['description']} credit"})
        listed += late_rows

        def serve(rows: list[dict[str, Any]]) -> None:
            harness.use_payload(payload_of(rows, account_id=external))

        serve([*history, hold])
        connection_id = harness.connect()
        path = f"liabilities.p{uuid.uuid4().hex[:8]}"
        _map_typed(harness, connection_id, external, path, "credit_card")
        harness.sync(connection_id)

        serve(listed)
        widened = harness.sync(connection_id)
        rows_after = len(_live_rows(harness, path))

        again = harness.sync(connection_id)  # the same payload, fetched again
        assert again["imported"] == 0  # (a)
        with db.get_sessionmaker()() as session:
            raw = session.execute(
                select(RawFetch)
                .where(RawFetch.connection_id == uuid.UUID(connection_id))
                .order_by(RawFetch.fetched_at.desc(), RawFetch.id.desc())
                .offset(1)
                .limit(1)
            ).scalar_one()  # the widened sync's own fetch
            # As map re-import and replay do: no window, the default tail.
            reimport = importer.import_fetch(session, raw)
            session.commit()
        assert reimport["imported"] == 0  # (a)
        assert len(_live_rows(harness, path)) == rows_after

        identities = set(importer._row_hashes({"id": external, "transactions": listed}))
        with migrated_engine.connect() as conn:
            stored = dict(
                conn.execute(
                    text(
                        "SELECT id::text, content_hash FROM transactions "
                        "WHERE content_hash IS NOT NULL"
                    )
                ).all()
            )
        skips = _last_sync_after().get("redescribed", [])
        assert len(skips) == again["skipped_redescribed"] == widened["skipped_redescribed"]
        originals = [skip["original_id"] for skip in skips]
        assert len(originals) == len(set(originals))  # (b) one skip per original
        for skip in skips:
            assert stored[skip["original_id"]] not in identities  # (b) no longer listed
            original = harness.client.get(f"/transactions/{skip['original_id']}").json()
            assert original["status"] == "posted"
            assert (original["date"], Decimal(original["amount"])) == (
                skip["date"],
                Decimal(skip["amount"]),
            )

        held_keys = {(days, Decimal(amount)) for days, amount, _ in held}
        stored_identities = set(stored.values())
        for late_row, (days, amount, _) in zip(late_rows, late, strict=True):
            if (days, Decimal(amount)) not in held_keys:
                (late_hash,) = importer._row_hashes({"id": external, "transactions": [late_row]})
                assert late_hash in stored_identities  # (c)


class TestTraceability:
    def test_raw_payload_for_ledger_row_in_one_query(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """One query still finds the row's source envelope; the payload is
        ciphertext at rest (issue #93), so reading it is a decrypt away."""
        from purser import crypto

        txns = [{"id": "1", "posted": _epoch(5), "amount": "-42.00", "description": "Widget"}]
        harness.use_payload(payload_of(txns))
        connection_id = harness.connect()
        harness.map_new(connection_id, "Demo Checking", "assets.checking")
        harness.sync(connection_id)

        page = harness.client.get("/transactions").json()
        tx_id = page["items"][0]["id"]
        with migrated_engine.connect() as conn:
            payload_enc = conn.execute(
                text(
                    "SELECT rf.payload_enc FROM transactions t "
                    "JOIN raw_fetches rf ON rf.id = t.source_fetch_id "
                    "WHERE t.id = :id"
                ),
                {"id": tx_id},
            ).scalar_one()
        payload = crypto.decrypt_json(payload_enc)
        assert any(
            sf_tx["description"] == "Widget"
            for acct in payload["accounts"]
            for sf_tx in acct["transactions"]
        )


class TestDiscoveryAndHealth:
    def test_discovery_lists_accounts_with_mapping_state(self, harness: FeedHarness) -> None:
        harness.use_payload(FIXTURE)
        connection_id = harness.connect()
        harness.sync(connection_id)
        discovered = harness.client.get(f"/connections/{connection_id}/discovered").json()
        assert {d["external_id"] for d in discovered} == {
            "Demo Savings",
            "Demo Checking",
            "Demo Empty Account",
        }
        assert all(d["mapped_account_id"] is None for d in discovered)

        harness.map_new(connection_id, "Demo Checking", "assets.checking")
        discovered = harness.client.get(f"/connections/{connection_id}/discovered").json()
        checking = next(d for d in discovered if d["external_id"] == "Demo Checking")
        assert checking["mapped_account_path"] == "assets.checking"

    def test_demo_fixture_imports(self, harness: FeedHarness) -> None:
        harness.use_payload(FIXTURE)
        connection_id = harness.connect()
        harness.map_new(connection_id, "Demo Checking", "assets.checking")
        harness.map_new(connection_id, "Demo Savings", "assets.savings")
        sync = harness.sync(connection_id)
        assert sync["imported"] == 16  # 8 per mapped account in the fixture

    def test_health_shows_status_and_fetch_budget(self, harness: FeedHarness) -> None:
        harness.use_payload(payload_of([]))
        connection_id = harness.connect()
        harness.sync(connection_id)
        health = harness.client.get("/connections").json()
        (row,) = [h for h in health if h["id"] == connection_id]
        assert row["status"] == "active"
        assert row["fetches_today"] == 1
        assert row["last_fetch_at"] is not None

    def test_rate_limit_guard(self, harness: FeedHarness, migrated_engine: Engine) -> None:
        from purser import crypto

        harness.use_payload(payload_of([]))
        connection_id = harness.connect()
        # Simulate having burned the day's budget (rows only; append-only is fine).
        with migrated_engine.begin() as conn:
            for i in range(24):
                conn.execute(
                    text(
                        "INSERT INTO raw_fetches (connection_id, content_hash, payload_enc) "
                        "VALUES (:cid, :h, :enc)"
                    ),
                    {"cid": connection_id, "h": f"h{i}", "enc": crypto.encrypt("{}")},
                )
        resp = harness.client.post(f"/connections/{connection_id}/sync")
        assert resp.status_code == 422
        assert "rate limit" in resp.text


class TestSyncFailureVisibility:
    """Issue #38: transport/parse failures surface as FeedError with sanitized
    messages, and the error status + sync.failed event survive the commit."""

    LEAKY_ERROR = httpx.ConnectError(f"connect refused: {ACCESS_URL}/accounts")

    def _assert_sanitized(self, text_blob: str) -> None:
        assert "secret" not in text_blob
        assert "bridge.example.com" not in text_blob

    def test_transport_error_is_sanitized_feed_error(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        harness.use_payload(payload_of([]))
        connection_id = harness.connect()
        harness.use_transport(make_broken_transport(self.LEAKY_ERROR))

        resp = harness.client.post(f"/connections/{connection_id}/sync")
        assert resp.status_code == 422
        assert "ConnectError" in resp.text  # exception type only, never str(exc)
        self._assert_sanitized(resp.text)

        health = harness.client.get("/connections").json()
        (row,) = [h for h in health if h["id"] == connection_id]
        assert row["status"] == "error"
        from purser import crypto

        with migrated_engine.connect() as conn:
            encs = (
                conn.execute(
                    text(
                        "SELECT payload_enc FROM events "
                        "WHERE type = 'sync.failed' AND entity_id = :id"
                    ),
                    {"id": connection_id},
                )
                .scalars()
                .all()
            )
        payloads = [crypto.decrypt_json(bytes(e)) for e in encs]
        assert len(payloads) == 1
        self._assert_sanitized(json.dumps(payloads[0]))

    def test_non_json_200_body_is_feed_error(self, harness: FeedHarness) -> None:
        harness.use_payload(payload_of([]))
        connection_id = harness.connect()
        harness.use_transport(
            make_broken_transport(httpx.Response(200, text="<html>gateway timeout</html>"))
        )
        resp = harness.client.post(f"/connections/{connection_id}/sync")
        assert resp.status_code == 422
        assert "not valid JSON" in resp.text
        health = harness.client.get("/connections").json()
        (row,) = [h for h in health if h["id"] == connection_id]
        assert row["status"] == "error"

    def test_sync_all_active_persists_failure_state(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """The scheduled path must COMMIT (not roll back) the error status and
        sync.failed event that sync_connection flushed."""
        from purser.feed.service import sync_all_active

        harness.use_payload(payload_of([]))
        connection_id = harness.connect()
        harness.use_transport(make_broken_transport(self.LEAKY_ERROR))

        sync_all_active()  # own session + commit; must not swallow the failure

        health = harness.client.get("/connections").json()
        (row,) = [h for h in health if h["id"] == connection_id]
        assert row["status"] == "error"
        with migrated_engine.connect() as conn:
            events_count = conn.execute(
                text("SELECT COUNT(*) FROM events WHERE type = 'sync.failed' AND entity_id = :id"),
                {"id": connection_id},
            ).scalar_one()
        assert events_count == 1


class TestRemapping:
    """Issue #39: remapping an external account releases the old holder so a
    single (connection_id, external_id) match remains and sync keeps working."""

    def test_remap_then_sync_succeeds(self, harness: FeedHarness, migrated_engine: Engine) -> None:
        txns = [
            {"id": "1", "posted": _recent_epoch(2), "amount": "-12.34", "description": "Coffee"},
        ]
        harness.use_payload(payload_of(txns))
        connection_id = harness.connect()
        account_a = harness.map_new(connection_id, "Demo Checking", "assets.checking_a")
        harness.sync(connection_id)

        account_b = harness.map_new(connection_id, "Demo Checking", "assets.checking_b")

        # Exactly one holder remains, and it is B.
        discovered = harness.client.get(f"/connections/{connection_id}/discovered").json()
        checking = next(d for d in discovered if d["external_id"] == "Demo Checking")
        assert checking["mapped_account_id"] == account_b["account_id"]
        with migrated_engine.connect() as conn:
            holders = conn.execute(
                text("SELECT COUNT(*) FROM accounts WHERE external_id = 'Demo Checking'")
            ).scalar_one()
            a_external = conn.execute(
                text("SELECT external_id FROM accounts WHERE id = :id"),
                {"id": account_a["account_id"]},
            ).scalar_one()
            unmap_enc = conn.execute(
                text(
                    "SELECT before_after_enc FROM audit_log "
                    "WHERE action = 'account.unmap' AND entity_id = :id"
                ),
                {"id": account_a["account_id"]},
            ).scalar_one()
        from purser import crypto

        unmap_audit = crypto.decrypt_json(bytes(unmap_enc))
        assert holders == 1
        assert a_external is None
        assert unmap_audit["before"]["external_id"] == "Demo Checking"
        assert unmap_audit["after"]["external_id"] is None
        assert unmap_audit["after"]["moved_to_account_id"] == account_b["account_id"]

        # The regression: this sync raised MultipleResultsFound (a 500) before.
        sync = harness.sync(connection_id)
        assert sync["unmapped_accounts"] == 0


class TestBackfill:
    """Issue #126: page historical windows BACKWARD from the first sync's start,
    ≤90-day ranges with a 1-day overlap, budget shared with sync, and NEVER a
    live-pending reconciliation (window_start=None)."""

    def test_windows_walk_backward_with_overlap(self, harness: FeedHarness) -> None:
        harness.use_payload(payload_of([]))
        cid = harness.connect()
        harness.sync(cid)  # seeds raw_fetches (cursor anchor) + last_fetch_at

        backend = RecordingBackend(lambda s, e: payload_of([_old_tx(200)]))
        harness.use_transport(backend.transport())
        result = harness.backfill(cid, windows=3)

        assert result["windows_fetched"] == 3
        assert result["stopped_because"] == "windows_exhausted"
        assert len(backend.calls) == 3
        # Every window is a 90-day range (bridge cap).
        for start, end in backend.calls:
            assert start is not None and end is not None
            assert (end - start).days == 90
        # Consecutive windows overlap by exactly one day...
        for (s0, _e0), (_s1, e1) in zip(backend.calls, backend.calls[1:], strict=False):
            assert e1 == s0 + timedelta(days=1)
        # ...and march strictly backward.
        starts = [s for s, _ in backend.calls]
        assert starts == sorted(starts, reverse=True)
        assert starts[0] > starts[1] > starts[2]
        # Cursor persisted at the oldest start reached.
        assert harness.health_of(cid)["backfill_cursor"] == backend.calls[-1][0].isoformat()

    def test_cursor_resumes_across_runs(self, harness: FeedHarness) -> None:
        harness.use_payload(payload_of([]))
        cid = harness.connect()
        harness.sync(cid)

        backend = RecordingBackend(lambda s, e: payload_of([_old_tx(200)]))
        harness.use_transport(backend.transport())

        first = harness.backfill(cid, windows=2)
        assert harness.health_of(cid)["backfill_cursor"] == first["coverage_start"]

        second = harness.backfill(cid, windows=2)
        assert len(backend.calls) == 4
        # The second run continues contiguously: its first window ends one day
        # into the first run's oldest start.
        assert backend.calls[2][1] == backend.calls[1][0] + timedelta(days=1)
        starts = [s for s, _ in backend.calls]
        assert starts == sorted(starts, reverse=True)
        assert harness.health_of(cid)["backfill_cursor"] == second["coverage_start"]
        assert second["coverage_start"] < first["coverage_start"]

    def test_zero_budget_is_rate_limited(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        harness.use_payload(payload_of([]))
        cid = harness.connect()
        # Burn the budget down to the reserve: 24 - reserve fetches already today.
        _seed_fetches(
            migrated_engine,
            cid,
            simplefin.BRIDGE_MAX_FETCHES_PER_DAY - service.BACKFILL_RESERVE_FETCHES,
        )
        resp = harness.client.post(f"/connections/{cid}/backfill", json={"windows": 4})
        assert resp.status_code == 422
        assert "rate limit" in resp.text

    def test_budget_truncates_windows(self, harness: FeedHarness, migrated_engine: Engine) -> None:
        harness.use_payload(payload_of([]))
        cid = harness.connect()
        # Leave exactly room for 2 backfill fetches.
        _seed_fetches(
            migrated_engine,
            cid,
            simplefin.BRIDGE_MAX_FETCHES_PER_DAY - service.BACKFILL_RESERVE_FETCHES - 2,
        )
        backend = RecordingBackend(lambda s, e: payload_of([_old_tx(200)]))
        harness.use_transport(backend.transport())
        result = harness.backfill(cid, windows=4)
        assert result["windows_fetched"] == 2
        assert result["stopped_because"] == "rate_budget"
        assert len(backend.calls) == 2

    def test_empty_history_completes_and_further_runs_noop(self, harness: FeedHarness) -> None:
        harness.use_payload(payload_of([]))
        cid = harness.connect()
        harness.sync(cid)

        backend = RecordingBackend()  # default responder: always empty
        harness.use_transport(backend.transport())
        result = harness.backfill(cid, windows=4)
        assert result["backfill_complete"] is True
        assert result["stopped_because"] == "empty_history"
        assert result["windows_fetched"] == service.BACKFILL_EMPTY_STREAK_STOP
        assert result["empty_windows"] == service.BACKFILL_EMPTY_STREAK_STOP
        assert len(backend.calls) == service.BACKFILL_EMPTY_STREAK_STOP

        # A completed connection no-ops without fetching.
        again = harness.backfill(cid, windows=4)
        assert again["stopped_because"] == "complete"
        assert again["windows_fetched"] == 0
        assert len(backend.calls) == service.BACKFILL_EMPTY_STREAK_STOP  # unchanged
        assert harness.health_of(cid)["backfill_complete"] is True

    def test_live_pending_survives_backfill_unvoided(self, harness: FeedHarness) -> None:
        """The window_start=None guarantee: a backfill NEVER reconciles live
        pendings against an old payload (which would wrongly void them)."""
        pending = {
            "id": "hold-1",
            "posted": _recent_epoch(2),
            "amount": "-40.00",
            "description": "Card hold",
            "pending": True,
        }
        harness.use_payload(payload_of([pending]))
        cid = harness.connect()
        harness.map_new(cid, "Demo Checking", "assets.checking")
        harness.sync(cid)
        page = harness.client.get("/transactions", params={"status": "pending"}).json()
        assert page["total"] == 1
        pending_id = page["items"][0]["id"]

        # Old, pending-free windows that DO include the mapped account. If
        # reconciliation ran (it must NOT), the live pending — absent here —
        # would be voided.
        backend = RecordingBackend()  # empty payloads for "Demo Checking"
        harness.use_transport(backend.transport())
        harness.backfill(cid, windows=1)

        detail = harness.client.get(f"/transactions/{pending_id}").json()
        assert detail["status"] == "pending"  # NOT void

    def test_overlap_reimport_is_idempotent(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """The 1-day window overlap re-fetches one boundary day; content-hash
        dedup makes the re-import free (zero duplicate rows)."""
        harness.use_payload(payload_of([]))
        cid = harness.connect()
        harness.map_new(cid, "Demo Checking", "assets.checking")
        harness.sync(cid)

        fixed = _old_tx(179, amount="-77.00", desc="Recurring")
        backend = RecordingBackend(lambda s, e: payload_of([fixed]))
        harness.use_transport(backend.transport())
        result = harness.backfill(cid, windows=2)

        assert result["imported"] == 1
        assert result["skipped"] >= 1
        with migrated_engine.connect() as conn:
            count = conn.execute(
                text("SELECT COUNT(*) FROM transactions WHERE origin = 'import'")
            ).scalar_one()
        assert count == 1

    def test_last_fetch_at_untouched_by_backfill(self, harness: FeedHarness) -> None:
        harness.use_payload(payload_of([]))
        cid = harness.connect()
        harness.sync(cid)
        before = harness.health_of(cid)["last_fetch_at"]
        assert before is not None

        backend = RecordingBackend(lambda s, e: payload_of([_old_tx(200)]))
        harness.use_transport(backend.transport())
        harness.backfill(cid, windows=2)
        assert harness.health_of(cid)["last_fetch_at"] == before

    def test_target_date_stops_and_clamps(self, harness: FeedHarness) -> None:
        harness.use_payload(payload_of([]))
        cid = harness.connect()
        harness.sync(cid)
        target = datetime.now(UTC).date() - timedelta(days=200)

        backend = RecordingBackend(lambda s, e: payload_of([_old_tx(120)]))
        harness.use_transport(backend.transport())
        result = harness.backfill(cid, windows=6, target_date=target.isoformat())

        assert result["stopped_because"] == "target_reached"
        assert result["coverage_start"] == target.isoformat()
        # The final window's start is clamped exactly to the target.
        assert backend.calls[-1][0] == target
        assert result["windows_fetched"] < 6

    def test_fetch_error_midrun_keeps_progress(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        harness.use_payload(payload_of([]))
        cid = harness.connect()
        harness.sync(cid)
        status_before = harness.health_of(cid)["status"]

        backend = FlakyBackend(fail_on_call=2)  # window 1 ok, window 2 errors
        harness.use_transport(backend.transport())
        result = harness.backfill(cid, windows=4)

        assert result["stopped_because"] == "fetch_error"
        assert result["windows_fetched"] == 1
        assert result["coverage_start"] is not None
        # Cursor kept at the one window that succeeded; status untouched; no event.
        assert harness.health_of(cid)["backfill_cursor"] == result["coverage_start"]
        assert harness.health_of(cid)["status"] == status_before  # never flipped to "error"
        with migrated_engine.connect() as conn:
            events = conn.execute(
                text("SELECT COUNT(*) FROM events WHERE type = 'sync.failed' AND entity_id = :id"),
                {"id": cid},
            ).scalar_one()
        assert events == 0

    def test_qif_overlap_is_reported(self, harness: FeedHarness) -> None:
        harness.use_payload(payload_of([]))
        cid = harness.connect()
        harness.map_new(cid, "Demo Checking", "assets.checking")
        harness.sync(cid)

        overlap_date = datetime.now(UTC).date() - timedelta(days=150)
        _seed_manual_qif_row("assets.checking", overlap_date, "-33.00")

        backend = RecordingBackend()  # empty windows; overlap is about EXISTING rows
        harness.use_transport(backend.transport())
        result = harness.backfill(cid, windows=1)

        assert result["qif_overlap"] == {"assets.checking": 1}

    def test_backfill_requires_prior_sync(self, harness: FeedHarness) -> None:
        harness.use_payload(payload_of([]))
        cid = harness.connect()  # no sync yet -> no raw_fetches
        resp = harness.client.post(f"/connections/{cid}/backfill", json={"windows": 2})
        assert resp.status_code == 422
        assert "sync" in resp.text

    def test_propose_token_backfills_and_health_exposes_fields(self, harness: FeedHarness) -> None:
        harness.use_payload(payload_of([]))
        cid = harness.connect()
        harness.sync(cid)

        # Fresh connection: fields present and unset in the health payload.
        health = harness.health_of(cid)
        assert health["backfill_complete"] is False
        assert health["backfill_cursor"] is None

        backend = RecordingBackend(lambda s, e: payload_of([_old_tx(200)]))
        harness.use_transport(backend.transport())

        minted = harness.client.post(
            "/tokens", json={"name": "agent", "scopes": ["read", "write"]}
        ).json()
        headers = {"Authorization": f"Bearer {minted['token']}"}
        resp = harness.client.post(
            f"/connections/{cid}/backfill", json={"windows": 2}, headers=headers
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body) == {
            "windows_fetched",
            "coverage_start",
            "coverage_end",
            "imported",
            "skipped",
            "skipped_before_feed_start",
            "skipped_masked_duplicate",
            "skipped_redescribed",
            "empty_windows",
            "backfill_complete",
            "stopped_because",
            "qif_overlap",
        }
        assert body["windows_fetched"] == 2
        assert harness.health_of(cid)["backfill_cursor"] == body["coverage_start"]


def _drift_events(engine: Engine) -> list[str]:
    """dedup_keys of all balance.drift events, oldest first."""
    with engine.connect() as conn:
        return [
            row[0]
            for row in conn.execute(
                text("SELECT dedup_key FROM events WHERE type = 'balance.drift' ORDER BY at")
            )
        ]


def _account_uuid(engine: Engine, path: str) -> str:
    with engine.connect() as conn:
        return str(
            conn.execute(
                text("SELECT id FROM accounts WHERE path::text = :p"), {"p": path}
            ).scalar_one()
        )


def _drift_block(connection_id: str) -> dict[str, Any]:
    """The balance_drift observation block from the latest connection.sync
    audit row — the check's cross-sync memory (DESIGN §7.3)."""
    from purser import db

    with db.get_sessionmaker()() as session:
        return drift._previous_observations(session, uuid.UUID(connection_id))


def _drift_payloads() -> list[dict[str, Any]]:
    """Decrypted payloads of all balance.drift events, oldest first."""
    from sqlalchemy import select

    from purser import db
    from purser.models import Event

    with db.get_sessionmaker()() as session:
        return [
            event.payload
            for event in session.execute(
                select(Event).where(Event.type == "balance.drift").order_by(Event.at)
            ).scalars()
        ]


class TestBalanceDrift:
    """Post-sync reported-vs-ledger drift check (issue #149, DESIGN §7.3):
    dual-basis comparison, gated balance.drift warning events, never a gate."""

    def _connect_mapped(self, harness: FeedHarness, payload: dict) -> str:
        harness.use_payload(payload)
        connection_id = harness.connect()
        harness.map_new(connection_id, "Demo Checking", "assets.checking")
        return connection_id

    def test_clean_pending_inclusive_match_no_event(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        posted = {"id": "t1", "posted": _recent_epoch(3), "amount": "-50.00", "description": "G"}
        pending = {
            "id": "p1",
            "posted": _recent_epoch(1),
            "amount": "-10.00",
            "description": "Hold",
            "pending": True,
        }
        cid = self._connect_mapped(harness, payload_of([posted, pending], balance="-60.00"))
        harness.sync(cid)
        assert _drift_events(migrated_engine) == []
        (obs,) = _drift_block(cid).values()
        assert obs["delta"] is None
        assert obs["emitted"] is False

    def test_clean_posted_only_match_no_event(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """The bank reports a posted-basis balance while a pending is open:
        either-basis-matches means clean."""
        posted = {"id": "t1", "posted": _recent_epoch(3), "amount": "-50.00", "description": "G"}
        pending = {
            "id": "p1",
            "posted": _recent_epoch(1),
            "amount": "-10.00",
            "description": "Hold",
            "pending": True,
        }
        cid = self._connect_mapped(harness, payload_of([posted, pending], balance="-50.00"))
        harness.sync(cid)
        harness.sync(cid)
        assert _drift_events(migrated_engine) == []

    def test_inflight_pending_stale_balance_date_no_false_positive(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """Bank balance reflects a transaction its transaction list doesn't
        carry yet, and the balance-date is not strictly fresher than ledger
        activity: no emission; the transaction lands next sync; clean. Zero
        events across the whole lifecycle."""
        t1 = {"id": "t1", "posted": _recent_epoch(1), "amount": "-50.00", "description": "A"}
        cid = self._connect_mapped(harness, payload_of([t1], balance="-50.00"))
        harness.sync(cid)

        harness.use_payload(payload_of([t1], balance="-70.00", balance_date=_recent_epoch(1)))
        harness.sync(cid)
        assert _drift_events(migrated_engine) == []
        (obs,) = _drift_block(cid).values()
        assert obs["delta"] == "-20.0000"  # candidate recorded, not emitted
        assert obs["emitted"] is False

        t2 = {"id": "t2", "posted": _recent_epoch(1), "amount": "-20.00", "description": "B"}
        harness.use_payload(payload_of([t1, t2], balance="-70.00"))
        harness.sync(cid)
        assert _drift_events(migrated_engine) == []

    def test_standing_drift_fresh_balance_date_emits_first_sync(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """The #146 case: a quiet account with a standing historical gap and a
        balance-date strictly fresher than all ledger activity alerts on the
        first sync that sees it."""
        posted = {"id": "t1", "posted": _recent_epoch(5), "amount": "-25.00", "description": "G"}
        cid = self._connect_mapped(
            harness, payload_of([posted], balance="1000.00", balance_date=_recent_epoch(0))
        )
        harness.sync(cid)
        account_id = _account_uuid(migrated_engine, "assets.checking")
        assert _drift_events(migrated_engine) == [f"{account_id}:1025.0000"]

    def test_drift_without_balance_date_confirms_on_second_sync(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        posted = {"id": "t1", "posted": _recent_epoch(5), "amount": "-25.00", "description": "G"}
        cid = self._connect_mapped(harness, payload_of([posted], balance="1000.00"))
        harness.sync(cid)
        assert _drift_events(migrated_engine) == []
        (obs,) = _drift_block(cid).values()
        assert obs["delta"] == "1025.0000"
        assert obs["emitted"] is False

        harness.sync(cid)  # same drift on consecutive syncs -> confirmed
        account_id = _account_uuid(migrated_engine, "assets.checking")
        assert _drift_events(migrated_engine) == [f"{account_id}:1025.0000"]

    def test_same_day_balance_date_waits_for_consecutive(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """Strict-> boundary: a same-day balance-date cannot order intraday
        events, so it does not confirm; the consecutive rule does."""
        posted = {"id": "t1", "posted": _recent_epoch(0), "amount": "-25.00", "description": "G"}
        cid = self._connect_mapped(
            harness, payload_of([posted], balance="1000.00", balance_date=_recent_epoch(0))
        )
        harness.sync(cid)
        assert _drift_events(migrated_engine) == []
        harness.sync(cid)
        assert len(_drift_events(migrated_engine)) == 1

    def test_onboarding_shrinking_delta_stays_quiet(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """A newly mapped account whose ledger is still filling toward the bank
        balance: the delta changes every sync and never confirms — zero events
        while history lands, none once it converges."""
        d1 = {"id": "d1", "posted": _recent_epoch(0), "amount": "100.00", "description": "D1"}
        d2 = {"id": "d2", "posted": _recent_epoch(0), "amount": "150.00", "description": "D2"}
        d3 = {"id": "d3", "posted": _recent_epoch(0), "amount": "250.00", "description": "D3"}
        cid = self._connect_mapped(
            harness, payload_of([d1], balance="500.00", balance_date=_recent_epoch(0))
        )
        harness.sync(cid)
        harness.use_payload(payload_of([d1, d2], balance="500.00", balance_date=_recent_epoch(0)))
        harness.sync(cid)
        harness.use_payload(
            payload_of([d1, d2, d3], balance="500.00", balance_date=_recent_epoch(0))
        )
        harness.sync(cid)
        harness.sync(cid)  # converged and stable: clean, still nothing
        assert _drift_events(migrated_engine) == []

    def test_multi_account_payload_independent_drift(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        clean_tx = {"id": "a1", "posted": _recent_epoch(3), "amount": "-25.00", "description": "A"}
        quiet_tx = {"id": "b1", "posted": _recent_epoch(6), "amount": "-10.00", "description": "B"}
        harness.use_payload(
            multi_payload_of(
                [
                    account_of([clean_tx], account_id="Acct A", balance="-25.00"),
                    account_of(
                        [quiet_tx],
                        account_id="Acct B",
                        balance="750.00",
                        balance_date=_recent_epoch(0),
                    ),
                    account_of([], account_id="Acct C", balance="123.45"),
                ]
            )
        )
        cid = harness.connect()
        harness.map_new(cid, "Acct A", "assets.checking")
        harness.map_new(cid, "Acct B", "assets.savings")
        harness.sync(cid)

        savings_id = _account_uuid(migrated_engine, "assets.savings")
        assert _drift_events(migrated_engine) == [f"{savings_id}:760.0000"]
        block = _drift_block(cid)
        checking_id = _account_uuid(migrated_engine, "assets.checking")
        assert set(block) == {checking_id, savings_id}  # unmapped Acct C absent
        assert block[checking_id]["delta"] is None
        assert block[savings_id]["emitted"] is True

    def test_persistent_drift_emits_exactly_once(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        posted = {"id": "t1", "posted": _recent_epoch(5), "amount": "-25.00", "description": "G"}
        cid = self._connect_mapped(
            harness, payload_of([posted], balance="1000.00", balance_date=_recent_epoch(0))
        )
        harness.sync(cid)
        harness.sync(cid)
        harness.sync(cid)
        account_id = _account_uuid(migrated_engine, "assets.checking")
        assert _drift_events(migrated_engine) == [f"{account_id}:1025.0000"]

    def test_changed_delta_emits_fresh(self, harness: FeedHarness, migrated_engine: Engine) -> None:
        posted = {"id": "t1", "posted": _recent_epoch(5), "amount": "-25.00", "description": "G"}
        cid = self._connect_mapped(
            harness, payload_of([posted], balance="1000.00", balance_date=_recent_epoch(0))
        )
        harness.sync(cid)
        harness.use_payload(payload_of([posted], balance="990.00", balance_date=_recent_epoch(0)))
        harness.sync(cid)
        account_id = _account_uuid(migrated_engine, "assets.checking")
        assert _drift_events(migrated_engine) == [
            f"{account_id}:1025.0000",
            f"{account_id}:1015.0000",
        ]

    def test_resolved_then_reappearing_drift_emits_new_episode(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """A drift that resolves and later reappears at the same delta is a new
        episode and alerts again (same dedup_key, two events) — a bare
        existence check would suppress the second signal forever."""
        posted = {"id": "t1", "posted": _recent_epoch(5), "amount": "-25.00", "description": "G"}
        drifted = payload_of([posted], balance="1000.00", balance_date=_recent_epoch(0))
        cid = self._connect_mapped(harness, drifted)
        harness.sync(cid)
        harness.use_payload(payload_of([posted], balance="-25.00"))
        harness.sync(cid)  # resolved: clean observation recorded
        harness.use_payload(drifted)
        harness.sync(cid)  # same delta reappears -> fresh episode
        account_id = _account_uuid(migrated_engine, "assets.checking")
        key = f"{account_id}:1025.0000"
        assert _drift_events(migrated_engine) == [key, key]

    def test_loan_and_property_excluded(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """loan/property reported balances feed the valuation path (DESIGN §8),
        never the drift check."""
        harness.use_payload(
            multi_payload_of(
                [
                    account_of(
                        [],
                        account_id="Loan X",
                        balance="-50000.00",
                        balance_date=_recent_epoch(0),
                    ),
                    account_of(
                        [],
                        account_id="House Y",
                        balance="400000.00",
                        balance_date=_recent_epoch(0),
                    ),
                ]
            )
        )
        cid = harness.connect()
        for external_id, type_, path in [
            ("Loan X", "loan", "liabilities.mortgage"),
            ("House Y", "property", "assets.house"),
        ]:
            resp = harness.client.post(
                f"/connections/{cid}/map",
                json={
                    "external_id": external_id,
                    "create": {"name": external_id, "type": type_, "path": path},
                },
            )
            assert resp.status_code == 200, resp.text
        harness.sync(cid)
        assert _drift_events(migrated_engine) == []
        assert _drift_block(cid) == {}
        with migrated_engine.connect() as conn:
            valuations = conn.execute(text("SELECT COUNT(*) FROM valuations")).scalar_one()
        assert valuations == 2

    def test_unmapped_account_ignored(self, harness: FeedHarness, migrated_engine: Engine) -> None:
        harness.use_payload(payload_of([], balance="999.99", balance_date=_recent_epoch(0)))
        cid = harness.connect()
        harness.sync(cid)
        assert _drift_events(migrated_engine) == []
        assert _drift_block(cid) == {}

    def test_backfill_and_map_reimport_never_emit(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """The check runs at the sync seam only: mapping's archive re-import
        and backfill pages never emit, whatever balances their payloads carry."""
        clean_tx = {"id": "a1", "posted": _recent_epoch(2), "amount": "-50.00", "description": "A"}
        harness.use_payload(
            multi_payload_of(
                [
                    account_of([clean_tx], account_id="Acct A", balance="-50.00"),
                    account_of(
                        [],
                        account_id="Acct B",
                        balance="777777.00",
                        balance_date=_recent_epoch(0),
                    ),
                ]
            )
        )
        cid = harness.connect()
        harness.map_new(cid, "Acct A", "assets.checking")
        harness.sync(cid)
        assert _drift_events(migrated_engine) == []

        # Mapping B re-imports the archived (drifty) payload for it — no drift.
        harness.map_new(cid, "Acct B", "assets.savings")
        assert _drift_events(migrated_engine) == []

        backend = RecordingBackend(
            lambda start, end: payload_of(
                [_old_tx(150)], balance="777777.00", balance_date=_recent_epoch(0)
            )
        )
        harness.use_transport(backend.transport())
        harness.backfill(cid, windows=2)
        assert _drift_events(migrated_engine) == []

    def test_zero_transaction_account_emits_first_sight(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """No ledger activity means nothing can be in flight: a fresh mapped,
        empty account with a nonzero bank balance alerts immediately, even
        without a balance-date."""
        cid = self._connect_mapped(harness, payload_of([], balance="250.00"))
        harness.sync(cid)
        account_id = _account_uuid(migrated_engine, "assets.checking")
        assert _drift_events(migrated_engine) == [f"{account_id}:250.0000"]

    def test_credit_card_negative_balance_no_sign_flip(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """SimpleFIN reports card balances negative-when-owed — the same
        convention as Purser liability postings, so a matching card is clean
        and a drifting one reports the raw signed delta."""
        charge = {"id": "c1", "posted": _recent_epoch(2), "amount": "-100.00", "description": "C"}
        harness.use_payload(payload_of([charge], balance="-100.00"))
        cid = harness.connect()
        resp = harness.client.post(
            f"/connections/{cid}/map",
            json={
                "external_id": "Demo Checking",
                "create": {"name": "Card", "type": "credit_card", "path": "liabilities.card"},
            },
        )
        assert resp.status_code == 200, resp.text
        harness.sync(cid)
        assert _drift_events(migrated_engine) == []

        harness.use_payload(payload_of([charge], balance="-150.00", balance_date=_recent_epoch(0)))
        harness.sync(cid)
        card_id = _account_uuid(migrated_engine, "liabilities.card")
        assert _drift_events(migrated_engine) == [f"{card_id}:-50.0000"]

    def test_failed_sync_does_not_reset_consecutive_gate(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """A failed sync writes no connection.sync audit row, so the previous
        successful sync's observation still anchors the consecutive rule."""
        posted = {"id": "t1", "posted": _recent_epoch(5), "amount": "-25.00", "description": "G"}
        drifted = payload_of([posted], balance="1000.00")
        cid = self._connect_mapped(harness, drifted)
        harness.sync(cid)
        assert _drift_events(migrated_engine) == []

        harness.use_transport(make_broken_transport(httpx.ConnectError("down")))
        assert harness.client.post(f"/connections/{cid}/sync").status_code >= 400

        harness.use_payload(drifted)
        harness.sync(cid)  # same drift as the last SUCCESSFUL sync -> confirmed
        assert len(_drift_events(migrated_engine)) == 1

    def test_discovered_reports_live_drift_fields(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """GET /connections/{id}/discovered carries the live (ungated)
        reported-vs-ledger comparison: tri-state ledger_balance/drift."""
        clean_tx = {"id": "a1", "posted": _recent_epoch(3), "amount": "-25.00", "description": "A"}
        harness.use_payload(
            multi_payload_of(
                [
                    account_of([clean_tx], account_id="Acct A", balance="-25.00"),
                    account_of(
                        [],
                        account_id="Acct B",
                        balance="750.00",
                        balance_date=_recent_epoch(0),
                    ),
                    account_of([], account_id="Acct C", balance="123.45"),
                    account_of([], account_id="Loan X", balance="-50000.00"),
                ]
            )
        )
        cid = harness.connect()
        harness.map_new(cid, "Acct A", "assets.checking")
        harness.map_new(cid, "Acct B", "assets.savings")
        resp = harness.client.post(
            f"/connections/{cid}/map",
            json={
                "external_id": "Loan X",
                "create": {"name": "Loan", "type": "loan", "path": "liabilities.loan"},
            },
        )
        assert resp.status_code == 200, resp.text
        harness.sync(cid)

        rows = {
            d["external_id"]: d for d in harness.client.get(f"/connections/{cid}/discovered").json()
        }
        assert rows["Acct A"]["ledger_balance"] == "-25.0000"
        assert rows["Acct A"]["drift"] is None  # clean: either basis matched
        assert rows["Acct B"]["ledger_balance"] == "0.0000"
        assert rows["Acct B"]["drift"] == "750.0000"
        assert rows["Acct B"]["balance_date"] is not None
        # No pendings anywhere: the bases never differ, so the per-sync rule's
        # tie-break (pending-inclusive) names the basis (decision 64).
        assert rows["Acct A"]["drift_basis"] == "pending_inclusive"
        assert rows["Acct B"]["drift_basis"] == "pending_inclusive"
        assert rows["Acct C"]["ledger_balance"] is None  # unmapped: not applicable
        assert rows["Acct C"]["drift"] is None
        assert rows["Acct C"]["drift_basis"] is None
        assert rows["Loan X"]["ledger_balance"] is None  # valuation path (§8)
        assert rows["Loan X"]["drift"] is None
        assert rows["Loan X"]["drift_basis"] is None

    # -- basis per account from sync history (issue #283, decision 64) ------

    def _connect_card(self, harness: FeedHarness, payload: dict) -> str:
        harness.use_payload(payload)
        cid = harness.connect()
        resp = harness.client.post(
            f"/connections/{cid}/map",
            json={
                "external_id": "Demo Checking",
                "create": {"name": "Card", "type": "credit_card", "path": "liabilities.card"},
            },
        )
        assert resp.status_code == 200, resp.text
        return cid

    @staticmethod
    def _card_lifecycle() -> list[list[dict[str, Any]]]:
        """Five syncs of one card: a posted history, then pendings that come,
        stack up, post, and vanish. No balance-date, so only the consecutive
        rule can confirm an emission."""
        t1 = {"id": "t1", "posted": _recent_epoch(5), "amount": "-500.00", "description": "A"}
        p1 = {
            "id": "p1",
            "posted": _recent_epoch(1),
            "amount": "-15.00",
            "description": "Hold one",
            "pending": True,
        }
        p2 = {
            "id": "p2",
            "posted": _recent_epoch(0),
            "amount": "-8.00",
            "description": "Hold two",
            "pending": True,
        }
        t2 = {"id": "t2", "posted": _recent_epoch(0), "amount": "-15.00", "description": "B"}
        return [[t1], [t1, p1], [t1, p1, p2], [t1, t2, p2], [t1, t2]]

    def test_bank_excluding_pendings_drift_holds_on_posted_basis(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """The #283 acceptance case: the bank's balance leaves pendings out and
        sits a constant 20.00 off the posted ledger. The old smaller-|delta|
        rule picked the pending-inclusive basis (|-5| < |-20| on the second
        sync) and the reported delta moved with every pending; history picks
        posted-only, so the delta holds and ONE episode emits."""
        syncs = self._card_lifecycle()
        # posted ledger after each sync: -500, -500, -500, -515, -515; the bank
        # reports it minus a 20.00 gap and never counts the pendings.
        balances = ["-520.00", "-520.00", "-520.00", "-535.00", "-535.00"]
        cid = self._connect_card(harness, payload_of(syncs[0], balance=balances[0]))
        observations = []
        for transactions, balance in zip(syncs, balances, strict=True):
            harness.use_payload(payload_of(transactions, balance=balance))
            harness.sync(cid)
            (obs,) = _drift_block(cid).values()
            observations.append(obs)

        card_id = _account_uuid(migrated_engine, "liabilities.card")
        assert _drift_events(migrated_engine) == [f"{card_id}:-20.0000"]
        (payload,) = _drift_payloads()
        assert payload["mode"] == "posted_only"
        assert payload["delta"] == "-20.0000"

        # First sync: no pendings, the bases agree, the per-sync rule names one.
        assert observations[0]["basis"] == "pending_inclusive"
        assert [o["basis"] for o in observations[1:]] == ["posted_only"] * 4
        assert {o["delta"] for o in observations} == {"-20.0000"}
        # ...while the pending-inclusive delta moved with every pending.
        pending_deltas = [Decimal(o["reported"]) - Decimal(o["pending"]) for o in observations]
        assert len(set(pending_deltas)) == 4
        assert all(o["emitted"] for o in observations[1:])

        (row,) = harness.client.get(f"/connections/{cid}/discovered").json()
        assert row["drift"] == "-20.0000"
        assert row["drift_basis"] == "posted_only"
        assert row["ledger_balance"] == "-515.0000"  # stays pending-inclusive

    def test_bank_including_pendings_drift_holds_on_pending_basis(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """The mirror case: the bank counts pendings and sits a constant 10.00
        off the pending-inclusive ledger. The old rule would have reported the
        posted-only delta here (|-5| < |10|), moving with every pending;
        history keeps pending-inclusive."""
        syncs = self._card_lifecycle()
        # pending-inclusive ledger after each sync: -500, -515, -523, -523,
        # -515; the bank reports it plus a 10.00 gap.
        balances = ["-490.00", "-505.00", "-513.00", "-513.00", "-505.00"]
        cid = self._connect_card(harness, payload_of(syncs[0], balance=balances[0]))
        observations = []
        for transactions, balance in zip(syncs, balances, strict=True):
            harness.use_payload(payload_of(transactions, balance=balance))
            harness.sync(cid)
            (obs,) = _drift_block(cid).values()
            observations.append(obs)

        card_id = _account_uuid(migrated_engine, "liabilities.card")
        assert _drift_events(migrated_engine) == [f"{card_id}:10.0000"]
        (payload,) = _drift_payloads()
        assert payload["mode"] == "pending_inclusive"
        assert {o["basis"] for o in observations} == {"pending_inclusive"}
        assert {o["delta"] for o in observations} == {"10.0000"}
        posted_deltas = [Decimal(o["reported"]) - Decimal(o["posted"]) for o in observations]
        assert len(set(posted_deltas)) > 1  # the posted-only delta did move

        (row,) = harness.client.get(f"/connections/{cid}/discovered").json()
        assert row["drift"] == "10.0000"
        assert row["drift_basis"] == "pending_inclusive"

    def test_short_history_falls_back_to_smaller_delta_rule(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """With no history to tell the bases apart, the per-sync rule picks
        (smaller |delta|), per account; a repeat sync that moves neither basis
        keeps the recorded basis rather than re-deciding."""
        posted = {"id": "a1", "posted": _recent_epoch(5), "amount": "-100.00", "description": "A"}
        hold = {
            "id": "h1",
            "posted": _recent_epoch(1),
            "amount": "-15.00",
            "description": "Hold",
            "pending": True,
        }
        b_posted = dict(posted, id="b1")
        b_hold = dict(hold, id="h2")
        payload = multi_payload_of(
            [
                # posted-only delta -20, pending-inclusive -5 -> pending-inclusive
                account_of([posted, hold], account_id="Acct A", balance="-120.00"),
                # posted-only delta -5, pending-inclusive 10 -> posted-only
                account_of([b_posted, b_hold], account_id="Acct B", balance="-105.00"),
            ]
        )
        harness.use_payload(payload)
        cid = harness.connect()
        harness.map_new(cid, "Acct A", "assets.checking")
        harness.map_new(cid, "Acct B", "assets.savings")
        a_id = _account_uuid(migrated_engine, "assets.checking")
        b_id = _account_uuid(migrated_engine, "assets.savings")

        for _ in range(2):
            harness.sync(cid)
            block = _drift_block(cid)
            assert block[a_id]["basis"] == "pending_inclusive"
            assert block[a_id]["delta"] == "-5.0000"
            assert block[b_id]["basis"] == "posted_only"
            assert block[b_id]["delta"] == "-5.0000"
        rows = {
            d["external_id"]: d for d in harness.client.get(f"/connections/{cid}/discovered").json()
        }
        assert rows["Acct A"]["drift_basis"] == "pending_inclusive"
        assert rows["Acct B"]["drift_basis"] == "posted_only"


D = Decimal
_MONEY = st.decimals(
    min_value=D("-5000"), max_value=D("5000"), places=2, allow_nan=False, allow_infinity=False
)


def _simulate_basis(
    account_id: uuid.UUID, syncs: list[tuple[Decimal, Decimal, Decimal]]
) -> list[drift.DriftComparison]:
    """Run ``compare_with_history`` over (reported, posted-only sum, pending-
    inclusive sum) syncs exactly as the post-sync check does: each sync sees
    the previous ``BASIS_WINDOW_SYNCS - 1`` stored observation blocks, newest
    first, and stores its own observation (with its basis) for the next."""
    blocks: list[dict[str, Any]] = []
    out = []
    for reported, posted, pending in syncs:
        comp = drift.compare_with_history(
            blocks[: drift.BASIS_WINDOW_SYNCS - 1],
            account_id=account_id,
            reported=reported,
            posted=posted,
            pending=pending,
            balance_date=None,
        )
        out.append(comp)
        obs = {
            "reported": str(reported),
            "posted": drift.canonical(posted),
            "pending": drift.canonical(pending),
            "basis": comp.basis,
        }
        blocks.insert(0, {str(account_id): obs})
    return out


class TestDriftBasisRule:
    """The per-account basis rule (issue #283, decision 64) as pure functions:
    the basis whose delta changed on fewer consecutive syncs wins; equal counts
    keep the previous basis; nothing recorded -> the per-sync smaller-|delta|
    rule (tie -> pending-inclusive)."""

    def test_posted_stable_pending_moving_picks_posted_only(self) -> None:
        history = [(D("-20.00"), D("-20.00")), (D("-20.00"), D("-5.00")), (D("-20.00"), D("3.00"))]
        assert drift.choose_basis(history, previous=None) == drift.POSTED_ONLY
        # Evidence beats the recorded basis: the tie-break only breaks ties.
        assert drift.choose_basis(history, previous=drift.PENDING_INCLUSIVE) == drift.POSTED_ONLY

    def test_pending_stable_posted_moving_picks_pending_inclusive(self) -> None:
        history = [(D("10.00"), D("10.00")), (D("-5.00"), D("10.00")), (D("2.00"), D("10.00"))]
        assert drift.choose_basis(history, previous=drift.POSTED_ONLY) == drift.PENDING_INCLUSIVE

    def test_no_history_uses_smaller_delta_tie_pending_inclusive(self) -> None:
        assert drift.choose_basis([(D("-20"), D("-5"))], previous=None) == drift.PENDING_INCLUSIVE
        assert drift.choose_basis([(D("-5"), D("10"))], previous=None) == drift.POSTED_ONLY
        assert drift.choose_basis([(D("10"), D("-10"))], previous=None) == drift.PENDING_INCLUSIVE

    def test_equal_counts_keep_previous_basis(self) -> None:
        # Both bases moved together (the bank's number moved, the ledger did
        # not): no evidence either way, so the recorded basis stands even
        # where the per-sync rule would pick the other one.
        history = [(D("-20"), D("-5")), (D("10"), D("25"))]
        assert drift.choose_basis(history, previous=drift.PENDING_INCLUSIVE) == (
            drift.PENDING_INCLUSIVE
        )
        assert drift.choose_basis(history, previous=None) == drift.POSTED_ONLY
        assert drift.choose_basis(history, previous="bogus") == drift.POSTED_ONLY

    def test_flapping_pending_counts_every_step(self) -> None:
        """A recurring hold that comes and goes has only two distinct deltas
        but changes on every sync — it loses to a basis that moved once (a
        real missed transaction)."""
        history = [
            (D("0"), D("0")),
            (D("-25"), D("-35")),
            (D("-25"), D("-25")),
            (D("-25"), D("-35")),
            (D("-25"), D("-25")),
        ]
        assert drift.choose_basis(history, previous=drift.PENDING_INCLUSIVE) == drift.POSTED_ONLY

    def test_compare_clean_if_either_basis_matches(self) -> None:
        for basis in drift.BASES:
            comp = drift.compare(
                reported=D("-50.00"),
                posted=D("-50.0000"),
                pending=D("-60.0000"),
                balance_date=None,
                basis=basis,
            )
            assert comp.delta == 0
            assert comp.basis == basis

    def test_compare_reports_delta_on_basis(self) -> None:
        kwargs: dict[str, Any] = {
            "reported": D("-520.00"),
            "posted": D("-500.0000"),
            "pending": D("-515.0000"),
            "balance_date": None,
        }
        assert drift.compare(**kwargs, basis=drift.POSTED_ONLY).delta == D("-20")
        assert drift.compare(**kwargs, basis=drift.PENDING_INCLUSIVE).delta == D("-5")

    def test_history_skips_malformed_observations(self) -> None:
        account_id = uuid.uuid4()
        key = str(account_id)
        blocks: list[dict[str, Any]] = [
            {key: {"reported": "-520.00", "posted": "-500.0000", "pending": "-523.0000"}},
            {key: {"reported": "NaN", "posted": "-500.0000", "pending": "-500.0000"}},
            {key: {"reported": "-520.00"}},
            {key: "garbage"},
            {key: {"reported": "-520.00", "posted": "-500.0000", "pending": "-515.0000"}},
        ]
        comp = drift.compare_with_history(
            blocks,
            account_id=account_id,
            reported=D("-520.00"),
            posted=D("-500.0000"),
            pending=D("-500.0000"),
            balance_date=None,
        )
        assert comp.basis == drift.POSTED_ONLY
        assert comp.delta == D("-20")

    @settings(max_examples=150, deadline=None)
    @given(
        gap=_MONEY.filter(lambda v: v != 0),
        posted=st.lists(_MONEY, min_size=2, max_size=30),
        pendings=st.lists(_MONEY, min_size=2, max_size=30),
    )
    def test_bank_excluding_pendings_settles_on_constant_posted_delta(
        self, gap: Decimal, posted: list[Decimal], pendings: list[Decimal]
    ) -> None:
        """Bank reports posted + gap and never counts pendings; posted history
        and pending totals move arbitrarily. Once the pending total has moved
        once, every later sync reports the gap on the posted-only basis (or is
        coincidentally clean) — the reported delta can no longer move with the
        pendings, so no new episodes."""
        n = min(len(posted), len(pendings))
        posted, pendings = posted[:n], pendings[:n]
        assume(any(a != b for a, b in pairwise(pendings)))
        first_move = next(i for i in range(1, n) if pendings[i] != pendings[i - 1])
        comps = _simulate_basis(
            uuid.uuid4(),
            [(p + gap, p, p + pend) for p, pend in zip(posted, pendings, strict=True)],
        )
        for comp, pend in list(zip(comps, pendings, strict=True))[first_move:]:
            assert comp.basis == drift.POSTED_ONLY
            assert comp.delta == (0 if pend == gap else gap)

    @settings(max_examples=150, deadline=None)
    @given(
        gap=_MONEY.filter(lambda v: v != 0),
        posted=st.lists(_MONEY, min_size=2, max_size=30),
        pendings=st.lists(_MONEY, min_size=2, max_size=30),
    )
    def test_bank_including_pendings_settles_on_constant_pending_delta(
        self, gap: Decimal, posted: list[Decimal], pendings: list[Decimal]
    ) -> None:
        """Mirror: the bank counts pendings (reports pending-inclusive + gap)."""
        n = min(len(posted), len(pendings))
        posted, pendings = posted[:n], pendings[:n]
        assume(any(a != b for a, b in pairwise(pendings)))
        first_move = next(i for i in range(1, n) if pendings[i] != pendings[i - 1])
        comps = _simulate_basis(
            uuid.uuid4(),
            [(p + pend + gap, p, p + pend) for p, pend in zip(posted, pendings, strict=True)],
        )
        for comp, pend in list(zip(comps, pendings, strict=True))[first_move:]:
            assert comp.basis == drift.PENDING_INCLUSIVE
            assert comp.delta == (0 if pend == -gap else gap)


class TestZeroAmountFeedRows:
    """Investment feeds send zero-amount marker rows; the importer must filter
    them (feed-side twin of the CSV planner's skipped_zero_amount, #151) —
    hit live: an unfiltered zero row 500'd the whole backfill request."""

    def test_zero_amount_feed_row_skipped_not_fatal(self, harness: FeedHarness) -> None:
        real = {"id": "t1", "posted": _recent_epoch(2), "amount": "-25.00", "description": "Buy"}
        marker = {
            "id": "t2",
            "posted": _recent_epoch(2),
            "amount": "0.00",
            "description": "REINVESTMENT MARKER",
        }
        harness.use_payload(payload_of([real, marker], balance="-25.00"))
        cid = harness.connect()
        harness.map_new(cid, "Demo Checking", "assets.checking")
        sync = harness.sync(cid)
        assert sync["imported"] == 1
        assert sync["skipped_zero_amount"] == 1
        page = harness.client.get("/transactions").json()
        assert page["total"] == 1

    def test_backfill_window_with_zero_rows_survives(self, harness: FeedHarness) -> None:
        real = {"id": "t1", "posted": _recent_epoch(2), "amount": "-25.00", "description": "Buy"}
        harness.use_payload(payload_of([real], balance="-25.00"))
        cid = harness.connect()
        harness.map_new(cid, "Demo Checking", "assets.checking")
        harness.sync(cid)

        old_marker = dict(_old_tx(150), amount="0.00", id="marker-150")
        backend = RecordingBackend(
            lambda start, end: payload_of([_old_tx(150), old_marker], balance="-25.00")
        )
        harness.use_transport(backend.transport())
        result = harness.backfill(cid, windows=2)
        assert result["imported"] == 1  # the real old row, once; marker filtered


def _account_id_by_path(client: TestClient, path: str) -> str:
    (row,) = [a for a in client.get("/accounts").json() if a["path"] == path]
    return str(row["id"])


def _set_feed_start(client: TestClient, account_id: str, value: str | None) -> None:
    resp = client.patch(f"/accounts/{account_id}", json={"feed_start": value})
    assert resp.status_code == 200, resp.text


class TestFeedStart:
    """Per-account feed sync-from (#154, decision 40): rows dated strictly
    before ``accounts.feed_start`` never import — enforced at the single
    ``_import_account`` chokepoint (sync, backfill, map re-import), before
    ``auto_link_pass`` can pair anything."""

    def test_boundary_is_inclusive_on_feed_side(self, harness: FeedHarness) -> None:
        # manual < boundary <= feed: D-1 skips, D and D+1 import.
        epoch_before, epoch_on, epoch_after = (
            _recent_epoch(6),
            _recent_epoch(5),
            _recent_epoch(4),
        )
        boundary = datetime.fromtimestamp(epoch_on, tz=UTC).date()
        rows = [
            {"id": "t-before", "posted": epoch_before, "amount": "-1.00", "description": "old"},
            {"id": "t-on", "posted": epoch_on, "amount": "-2.00", "description": "boundary"},
            {"id": "t-after", "posted": epoch_after, "amount": "-3.00", "description": "new"},
        ]
        harness.use_payload(payload_of(rows))
        cid = harness.connect()
        harness.map_new(cid, "Demo Checking", "assets.checking")
        _set_feed_start(
            harness.client,
            _account_id_by_path(harness.client, "assets.checking"),
            boundary.isoformat(),
        )
        sync = harness.sync(cid)
        assert sync["imported"] == 2
        assert sync["skipped_before_feed_start"] == 1
        assert harness.client.get("/transactions").json()["total"] == 2

    def test_null_feed_start_behaves_as_before(self, harness: FeedHarness) -> None:
        rows = [
            {
                "id": f"t{i}",
                "posted": _recent_epoch(6 - i),
                "amount": f"-{i}.00",
                "description": "x",
            }
            for i in (1, 2, 3)
        ]
        harness.use_payload(payload_of(rows))
        cid = harness.connect()
        harness.map_new(cid, "Demo Checking", "assets.checking")
        sync = harness.sync(cid)
        assert sync["imported"] == 3
        assert sync["skipped_before_feed_start"] == 0

    def test_latch_reset_repage_guarded_and_autolink_pairs_nothing(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """The 2026-07-14 incident replay: reset the backfill latch, re-page a
        window whose rows had been manually re-owned — the guarded account
        re-imports nothing, so auto-link has no duplicate flat to consume."""
        payload = multi_payload_of(
            [
                account_of([], account_id="Demo Checking"),
                account_of([], account_id="Demo Savings"),
            ]
        )
        harness.use_payload(payload)
        cid = harness.connect()
        harness.map_new(cid, "Demo Checking", "assets.checking")
        harness.map_new(cid, "Demo Savings", "assets.savings")
        harness.sync(cid)
        # Exhaust history: empty responder latches backfill_complete.
        harness.use_transport(RecordingBackend().transport())
        assert harness.backfill(cid, windows=4)["backfill_complete"] is True

        _set_feed_start(
            harness.client,
            _account_id_by_path(harness.client, "assets.checking"),
            (datetime.now(UTC) - timedelta(days=100)).date().isoformat(),
        )
        # The ops latch-reset recipe (no API on purpose — raw SQL, as run live).
        with migrated_engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE connections SET backfill_complete = false, "
                    "backfill_cursor = CURRENT_DATE WHERE id = :cid"
                ),
                {"cid": cid},
            )
        # Re-page serves a transfer-shaped equal-and-opposite pair, 150d old —
        # the checking leg is pre-boundary (skips), the savings leg imports.
        pair_epoch = int((datetime.now(UTC) - timedelta(days=150)).timestamp())
        repage = RecordingBackend(
            lambda start, end: multi_payload_of(
                [
                    account_of(
                        [
                            {
                                "id": "chk-dup",
                                "posted": pair_epoch,
                                "amount": "-412.86",
                                "description": "TARGET CIRCLE PAYMENT",
                            }
                        ],
                        account_id="Demo Checking",
                    ),
                    account_of(
                        [
                            {
                                "id": "sav-leg",
                                "posted": pair_epoch,
                                "amount": "412.86",
                                "description": "TARGET CIRCLE PAYMENT",
                            }
                        ],
                        account_id="Demo Savings",
                    ),
                ]
            )
        )
        harness.use_transport(repage.transport())
        result = harness.backfill(cid, windows=1)
        assert result["imported"] == 1
        assert result["skipped_before_feed_start"] == 1
        assert harness.client.get("/transactions").json()["total"] == 1
        with migrated_engine.connect() as conn:
            system_rows = conn.execute(
                text("SELECT count(*) FROM transactions WHERE origin = 'system'")
            ).scalar_one()
        assert system_rows == 0  # nothing existed for auto_link_pass to pair

    def test_map_reimport_respects_feed_start(self, harness: FeedHarness) -> None:
        epoch_old, epoch_new = _recent_epoch(10), _recent_epoch(2)
        boundary = (datetime.now(UTC) - timedelta(days=5)).date()
        rows = [
            {"id": "t-old", "posted": epoch_old, "amount": "-1.00", "description": "pre"},
            {"id": "t-new", "posted": epoch_new, "amount": "-2.00", "description": "post"},
        ]
        harness.use_payload(payload_of(rows))
        cid = harness.connect()
        sync = harness.sync(cid)  # unmapped: archives the fetch, imports nothing
        assert sync["unmapped_accounts"] == 1
        created = harness.client.post(
            "/accounts", json={"name": "Checking", "type": "checking", "path": "assets.checking"}
        )
        assert created.status_code == 201, created.text
        account_id = created.json()["id"]
        _set_feed_start(harness.client, account_id, boundary.isoformat())
        mapped = harness.client.post(
            f"/connections/{cid}/map",
            json={"external_id": "Demo Checking", "account_id": account_id},
        )
        assert mapped.status_code == 200, mapped.text  # map re-imports the archive
        assert harness.client.get("/transactions").json()["total"] == 1

    def test_backfill_account_filter_sends_repeatable_params(self, harness: FeedHarness) -> None:
        harness.use_payload(payload_of([]))
        cid = harness.connect()
        harness.map_new(cid, "Demo Checking", "assets.checking")
        harness.sync(cid)
        backend = RecordingBackend()
        harness.use_transport(backend.transport())
        harness.backfill(cid, windows=1, accounts=["Demo Checking", "Demo Savings"])
        assert backend.account_filters[-1] == ["Demo Checking", "Demo Savings"]
        harness.backfill(cid, windows=1)
        assert backend.account_filters[-1] == []

    def test_bridge_ignoring_filter_still_guarded_and_never_latches(
        self, harness: FeedHarness
    ) -> None:
        """Composition: a bridge that ignores the account filter returns every
        account — feed_start still guards. And a FILTERED empty streak stops
        without latching backfill_complete (unfiltered emptiness still does)."""
        payload = multi_payload_of(
            [
                account_of([], account_id="Demo Checking"),
                account_of([], account_id="Demo Savings"),
            ]
        )
        harness.use_payload(payload)
        cid = harness.connect()
        harness.map_new(cid, "Demo Checking", "assets.checking")
        harness.map_new(cid, "Demo Savings", "assets.savings")
        harness.sync(cid)
        _set_feed_start(
            harness.client,
            _account_id_by_path(harness.client, "assets.checking"),
            (datetime.now(UTC) - timedelta(days=100)).date().isoformat(),
        )
        old_epoch = int((datetime.now(UTC) - timedelta(days=150)).timestamp())
        ignoring = RecordingBackend(
            lambda start, end: multi_payload_of(
                [
                    account_of(
                        [{"id": "c1", "posted": old_epoch, "amount": "-5.00", "description": "a"}],
                        account_id="Demo Checking",
                    ),
                    account_of(
                        [{"id": "s1", "posted": old_epoch, "amount": "-6.00", "description": "b"}],
                        account_id="Demo Savings",
                    ),
                ]
            )
        )
        harness.use_transport(ignoring.transport())
        result = harness.backfill(cid, windows=1, accounts=["Demo Checking"])
        assert result["imported"] == 1  # savings row; checking guarded
        assert result["skipped_before_feed_start"] == 1

        # Filtered empty streak: stops, but must NOT latch the connection.
        harness.use_transport(RecordingBackend().transport())
        filtered = harness.backfill(cid, windows=4, accounts=["Demo Checking"])
        assert filtered["stopped_because"] == "empty_history"
        assert filtered["backfill_complete"] is False
        assert harness.health_of(cid)["backfill_complete"] is False
        # Unfiltered emptiness latches, as before.
        unfiltered = harness.backfill(cid, windows=4)
        assert unfiltered["stopped_because"] == "empty_history"
        assert unfiltered["backfill_complete"] is True


def _set_feed_invert(client: TestClient, account_id: str, value: bool) -> None:
    resp = client.patch(f"/accounts/{account_id}", json={"feed_invert": value})
    assert resp.status_code == 200, resp.text


class TestFeedInvert:
    """Per-account feed sign inversion (#170, decision 43): the importer
    negates amounts strictly AFTER content-hashing, so identity is the bytes
    the bank sent and a flag flip never re-imports history."""

    def test_inverted_import_flips_amounts_and_classification(self, harness: FeedHarness) -> None:
        rows = [
            # The Fidelity shape: money received reported negative.
            {
                "id": "t-dep",
                "posted": _recent_epoch(5),
                "amount": "-750.00",
                "description": "MONEY LINE RECEIVED",
            },
            {
                "id": "t-fee",
                "posted": _recent_epoch(4),
                "amount": "25.00",
                "description": "ADVISORY FEE",
            },
        ]
        harness.use_payload(payload_of(rows))
        cid = harness.connect()
        harness.map_new(cid, "Demo Checking", "assets.brokerage_cash")
        _set_feed_invert(
            harness.client,
            _account_id_by_path(harness.client, "assets.brokerage_cash"),
            True,
        )
        sync = harness.sync(cid)
        assert sync["imported"] == 2
        items = harness.client.get(
            "/transactions", params={"account": "assets.brokerage_cash"}
        ).json()["items"]
        by_desc = {t["description"]: t for t in items}
        # Signs corrected — and the income/expense classification follows the
        # corrected sign, not the raw feed sign.
        assert Decimal(by_desc["MONEY LINE RECEIVED"]["amount"]) == Decimal("750.00")
        assert by_desc["MONEY LINE RECEIVED"]["category_path"] == "income.uncategorized"
        assert Decimal(by_desc["ADVISORY FEE"]["amount"]) == Decimal("-25.00")
        assert by_desc["ADVISORY FEE"]["category_path"] == "expenses.uncategorized"

    def test_flag_flip_never_reimports_history(self, harness: FeedHarness) -> None:
        """Hash-on-raw pins import idempotency across flag changes: rows
        imported un-inverted dedup exactly when the flag turns on later."""
        rows = [
            {
                "id": "t-1",
                "posted": _recent_epoch(6),
                "amount": "-100.00",
                "description": "CONTRIBUTION",
            }
        ]
        harness.use_payload(payload_of(rows))
        cid = harness.connect()
        harness.map_new(cid, "Demo Checking", "assets.brokerage_cash")
        first = harness.sync(cid)
        assert first["imported"] == 1

        _set_feed_invert(
            harness.client,
            _account_id_by_path(harness.client, "assets.brokerage_cash"),
            True,
        )
        second = harness.sync(cid)
        assert second["imported"] == 0
        assert second["skipped"] >= 1
        # The existing row is never rewritten — repair is a deliberate,
        # separate admin operation.
        items = harness.client.get(
            "/transactions", params={"account": "assets.brokerage_cash"}
        ).json()["items"]
        assert [t["amount"] for t in items] == ["-100.0000"]

    def test_inverted_import_is_idempotent(self, harness: FeedHarness) -> None:
        rows = [
            {
                "id": "t-1",
                "posted": _recent_epoch(6),
                "amount": "-100.00",
                "description": "CONTRIBUTION",
            }
        ]
        harness.use_payload(payload_of(rows))
        cid = harness.connect()
        harness.map_new(cid, "Demo Checking", "assets.brokerage_cash")
        _set_feed_invert(
            harness.client,
            _account_id_by_path(harness.client, "assets.brokerage_cash"),
            True,
        )
        assert harness.sync(cid)["imported"] == 1
        again = harness.sync(cid)
        assert again["imported"] == 0
        assert harness.client.get("/transactions").json()["total"] == 1

    def test_feed_invert_requires_admin(self, harness: FeedHarness) -> None:
        harness.use_payload(payload_of([]))
        cid = harness.connect()
        harness.map_new(cid, "Demo Checking", "assets.checking")
        account_id = _account_id_by_path(harness.client, "assets.checking")
        resp = harness.client.post("/tokens", json={"name": "agent", "scopes": ["read", "write"]})
        headers = {"Authorization": f"Bearer {resp.json()['token']}"}
        denied = harness.client.patch(
            f"/accounts/{account_id}", json={"feed_invert": True}, headers=headers
        )
        assert denied.status_code == 403
        # Rename via the same route stays commit-scope.
        renamed = harness.client.patch(
            f"/accounts/{account_id}", json={"name": "Renamed"}, headers=headers
        )
        assert renamed.status_code == 200, renamed.text


class TestMaskedDuplicateGuard:
    """Masked-reference dedup at import (#171, decision 44): a provider that
    masks reference digits between fetches must not land a duplicate row."""

    def _row(self, desc: str, *, epoch: int, amount: str = "-750.00") -> dict:
        return {"id": "irrelevant", "posted": epoch, "amount": amount, "description": desc}

    def test_masked_redelivery_is_skipped(self, harness: FeedHarness) -> None:
        epoch = _recent_epoch(2)
        unmasked = "MONEY LINE RECEIVED EFT FUNDS RECEIVED ER13529574 (Cash)"
        masked = "MONEY LINE RECEIVED EFT FUNDS RECEIVED ERXXXX9574 (Cash)"
        harness.use_payload(payload_of([self._row(unmasked, epoch=epoch)]))
        cid = harness.connect()
        harness.map_new(cid, "Demo Checking", "assets.brokerage_cash")
        assert harness.sync(cid)["imported"] == 1

        harness.use_payload(payload_of([self._row(masked, epoch=epoch)]))
        again = harness.sync(cid)
        assert again["imported"] == 0
        assert again["skipped_masked_duplicate"] == 1
        assert harness.client.get("/transactions").json()["total"] == 1

    def test_masked_arrives_first_then_unmasked(self, harness: FeedHarness) -> None:
        epoch = _recent_epoch(2)
        unmasked = "REF 1234 TRANSFER"
        masked = "REF XX34 TRANSFER"
        harness.use_payload(payload_of([self._row(masked, epoch=epoch)]))
        cid = harness.connect()
        harness.map_new(cid, "Demo Checking", "assets.brokerage_cash")
        assert harness.sync(cid)["imported"] == 1

        harness.use_payload(payload_of([self._row(unmasked, epoch=epoch)]))
        again = harness.sync(cid)
        assert again["imported"] == 0
        assert again["skipped_masked_duplicate"] == 1

    def test_genuinely_distinct_references_both_import(self, harness: FeedHarness) -> None:
        epoch = _recent_epoch(2)
        harness.use_payload(payload_of([self._row("REF 1111 TRANSFER", epoch=epoch)]))
        cid = harness.connect()
        harness.map_new(cid, "Demo Checking", "assets.brokerage_cash")
        assert harness.sync(cid)["imported"] == 1

        # Same day, same amount, different visible reference: a legitimate
        # second transfer — never skipped.
        harness.use_payload(payload_of([self._row("REF 2222 TRANSFER", epoch=epoch)]))
        again = harness.sync(cid)
        assert again["imported"] == 1
        assert again["skipped_masked_duplicate"] == 0
        assert harness.client.get("/transactions").json()["total"] == 2

    def test_amount_or_date_mismatch_is_not_a_masked_dup(self, harness: FeedHarness) -> None:
        harness.use_payload(payload_of([self._row("REF 1234 TRANSFER", epoch=_recent_epoch(3))]))
        cid = harness.connect()
        harness.map_new(cid, "Demo Checking", "assets.brokerage_cash")
        assert harness.sync(cid)["imported"] == 1

        # Masked twin but a different amount: imports.
        harness.use_payload(
            payload_of([self._row("REF XX34 TRANSFER", epoch=_recent_epoch(3), amount="-751.00")])
        )
        assert harness.sync(cid)["imported"] == 1
        # Masked twin but a different date: imports.
        harness.use_payload(payload_of([self._row("REF XX34 TRANSFER", epoch=_recent_epoch(1))]))
        assert harness.sync(cid)["imported"] == 1

    def test_guard_composes_with_feed_invert(self, harness: FeedHarness) -> None:
        """Post-invert amounts are what the guard compares — an inverted
        account still dedups its masked re-delivery."""
        epoch = _recent_epoch(2)
        harness.use_payload(payload_of([self._row("EFT ER1234 (Cash)", epoch=epoch)]))
        cid = harness.connect()
        harness.map_new(cid, "Demo Checking", "assets.brokerage_cash")
        _set_feed_invert(
            harness.client,
            _account_id_by_path(harness.client, "assets.brokerage_cash"),
            True,
        )
        assert harness.sync(cid)["imported"] == 1

        harness.use_payload(payload_of([self._row("EFT ERXX34 (Cash)", epoch=epoch)]))
        again = harness.sync(cid)
        assert again["imported"] == 0
        assert again["skipped_masked_duplicate"] == 1


class TestManualHoldingsSource:
    """Issue #390, decision 88: a brokerage account whose holdings are tracked
    by hand takes no feed snapshot; its cash rows import as usual, and a
    re-sync stays idempotent."""

    def _manual_then_feed(
        self, harness: FeedHarness, held_on: date, *, switch: bool = True
    ) -> tuple[str, str]:
        """A mapped brokerage account tracked by hand (ACME held from
        ``held_on``, priced), then switched back to the feed today."""
        harness.use_payload(self._payload())
        connection_id = harness.connect()
        account_id = harness.client.post(
            "/accounts", json={"name": "Plan", "type": "brokerage", "path": "assets.plan"}
        ).json()["id"]
        resp = harness.client.patch(f"/accounts/{account_id}", json={"holdings_source": "manual"})
        assert resp.status_code == 200, resp.text
        resp = harness.client.post(
            f"/connections/{connection_id}/map",
            json={"external_id": "SF-PLAN", "account_id": account_id},
        )
        assert resp.status_code == 200, resp.text
        resp = harness.client.post(
            f"/accounts/{account_id}/positions",
            json={"symbol": "ACME", "quantity": "3", "as_of": held_on.isoformat()},
        )
        assert resp.status_code == 201, resp.text
        resp = harness.client.post(
            "/prices", json={"symbol": "ACME", "date": held_on.isoformat(), "close": "100"}
        )
        assert resp.status_code == 200, resp.text
        if switch:
            self._switch_back(harness, account_id)
        return connection_id, account_id

    @staticmethod
    def _switch_back(harness: FeedHarness, account_id: str) -> None:
        resp = harness.client.patch(f"/accounts/{account_id}", json={"holdings_source": "feed"})
        assert resp.status_code == 200, resp.text

    @staticmethod
    def _value(account_id: str, on: date) -> Decimal | None:
        """The account's holdings addend on ``on`` (None: it adds nothing)."""
        from purser import db, networth

        with db.get_sessionmaker()() as session:
            addend = networth.holdings_addends(session, [uuid.UUID(account_id)], as_of=on)
        return addend[uuid.UUID(account_id)][0] if uuid.UUID(account_id) in addend else None

    @staticmethod
    def _one_row_per_symbol(migrated_engine: Engine, account_id: str) -> None:
        with migrated_engine.connect() as conn:
            ties = conn.execute(
                text(
                    "SELECT as_of, symbol, count(*) FROM holdings_snapshots WHERE account_id = :a"
                    " GROUP BY as_of, symbol HAVING count(*) > 1"
                ),
                {"a": account_id},
            ).all()
            sources = conn.execute(
                text(
                    "SELECT as_of FROM holdings_snapshots WHERE account_id = :a"
                    " GROUP BY as_of HAVING count(DISTINCT source) > 1"
                ),
                {"a": account_id},
            ).all()
        assert ties == [] and sources == []

    @staticmethod
    def _import_dated(connection_id: str, payload: dict, on: date) -> dict:
        from purser import crypto, db
        from purser.feed import importer
        from purser.models import RawFetch

        now = datetime.now(UTC)
        # Today's fetch is taken now (after any switch the test just made);
        # another day's at noon.
        fetched_at = (
            now
            if on == now.date()
            else datetime.combine(on, datetime.min.time(), tzinfo=UTC) + timedelta(hours=12)
        )
        with db.get_sessionmaker()() as session:
            raw = RawFetch(
                connection_id=uuid.UUID(connection_id),
                fetched_at=fetched_at,
                content_hash=f"test-{uuid.uuid4().hex}",
                payload_enc=crypto.encrypt(json.dumps(payload, sort_keys=True)),
            )
            session.add(raw)
            session.flush()
            stats = importer.import_fetch(session, raw)
            session.commit()
        return stats

    @staticmethod
    def _rows_on(migrated_engine: Engine, account_id: str, on: date) -> list[tuple]:
        with migrated_engine.connect() as conn:
            return [
                tuple(r)
                for r in conn.execute(
                    text(
                        "SELECT symbol, source, quantity FROM holdings_snapshots"
                        " WHERE account_id = :a AND as_of = :d ORDER BY source, symbol"
                    ),
                    {"a": account_id, "d": on},
                )
            ]

    def test_the_first_fetch_after_the_switch_ends_the_manual_period(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """The switch touches no row; the feed's first fetch on or after the
        switch day ends the manual period with its own set. A position it
        reports needs no zero row: one row per (date, symbol), one source per
        date, so the per-symbol latest view never ties."""
        from purser import db, networth

        today = datetime.now(UTC).date()
        connection_id, account_id = self._manual_then_feed(harness, today - timedelta(days=3))
        stats = self._import_dated(connection_id, self._payload(), today)
        assert stats["holdings_snapshots"] == 2 and stats["holdings_skipped_manual"] == 0
        assert self._rows_on(migrated_engine, account_id, today) == [
            ("ACME", "feed", Decimal("3.000000")),
            ("TEST", "feed", Decimal("1.000000")),
        ]
        self._one_row_per_symbol(migrated_engine, account_id)
        with migrated_engine.connect() as conn:
            view = conn.execute(
                text(
                    "SELECT symbol, as_of FROM readmodel.v1_holdings_latest"
                    " WHERE account_path = 'assets.plan' ORDER BY symbol"
                )
            ).all()
        assert [tuple(r) for r in view] == [("ACME", today), ("TEST", today)]
        with db.get_sessionmaker()() as session:
            latest = networth.latest_holdings(session, uuid.UUID(account_id))
            assert sorted((h.symbol, h.source) for h in latest) == [
                ("ACME", "feed"),
                ("TEST", "feed"),
            ]

    def test_a_cash_only_fetch_ends_every_hand_tracked_position(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """The cash-sweep shape: a feed that sends cash and no holdings ends the
        manual period on its first fetch after the switch, with a zero row
        for each hand-tracked position; the history before stays."""
        today = datetime.now(UTC).date()
        connection_id, account_id = self._manual_then_feed(harness, today - timedelta(days=3))
        assert self._value(account_id, today) == Decimal("300.0000")  # no dip at the switch
        cash_only = self._payload()
        cash_only["accounts"][0]["holdings"] = []
        stats = self._import_dated(connection_id, cash_only, today + timedelta(days=1))
        assert stats["holdings_snapshots"] == 0
        # A manual tombstone ends the position: it never moves the feed floor.
        assert self._rows_on(migrated_engine, account_id, today + timedelta(days=1)) == [
            ("ACME", "manual", Decimal("0.000000"))
        ]
        assert self._value(account_id, today + timedelta(days=1)) is None
        assert self._value(account_id, today + timedelta(days=90)) is None
        assert self._value(account_id, today) == Decimal("300.0000")  # history stays
        self._one_row_per_symbol(migrated_engine, account_id)

    def test_the_switch_day_keeps_its_value_until_the_next_fetch(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """Switched back after the day's sync ran (the account still manual):
        the switch day keeps the hand-tracked value, no dip, and the next
        day's fetch ends the period from its own date."""
        today = datetime.now(UTC).date()
        connection_id, account_id = self._manual_then_feed(
            harness, today - timedelta(days=3), switch=False
        )
        skipped = self._import_dated(connection_id, self._payload(), today)
        assert skipped["holdings_skipped_manual"] == 2  # today's sync, still manual
        self._switch_back(harness, account_id)
        assert self._value(account_id, today) == Decimal("300.0000")
        nxt = self._import_dated(connection_id, self._payload(), today + timedelta(days=1))
        assert nxt["holdings_snapshots"] == 2
        assert self._value(account_id, today) == Decimal("300.0000")
        assert self._value(account_id, today + timedelta(days=1)) == Decimal("310.0000")
        self._one_row_per_symbol(migrated_engine, account_id)

    def test_the_fetch_replaces_a_hand_tracked_set_of_its_own_date(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """A hand-tracked set dated the switch day (a close recorded today)
        stays through the switch; the feed's set for that day replaces it."""
        today = datetime.now(UTC).date()
        connection_id, account_id = self._manual_then_feed(
            harness, today - timedelta(days=3), switch=False
        )
        resp = harness.client.post(
            "/prices", json={"symbol": "ACME", "date": today.isoformat(), "close": "120"}
        )
        assert resp.status_code == 200, resp.text
        self._switch_back(harness, account_id)
        assert self._rows_on(migrated_engine, account_id, today) == [
            ("ACME", "manual", Decimal("3.000000"))
        ]
        assert self._value(account_id, today) == Decimal("360.0000")
        self._import_dated(connection_id, self._payload(), today)
        assert self._rows_on(migrated_engine, account_id, today) == [
            ("ACME", "feed", Decimal("3.000000")),
            ("TEST", "feed", Decimal("1.000000")),
        ]
        assert self._value(account_id, today) == Decimal("310.0000")
        # A second fetch that day adds no second row for any symbol.
        again = self._import_dated(connection_id, self._payload(), today)
        assert again["holdings_snapshots"] == 0
        self._one_row_per_symbol(migrated_engine, account_id)

    def test_a_fetch_dated_before_the_switch_adds_nothing(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """The map path re-imports the connection's latest fetch: dated
        yesterday, inside the manual period, on a day with no hand-tracked
        set of its own. The set in effect that day is the hand-tracked one,
        so the fetch adds nothing."""
        from purser import db, networth

        today = datetime.now(UTC).date()
        yesterday = today - timedelta(days=1)
        connection_id, account_id = self._manual_then_feed(harness, today - timedelta(days=3))
        assert self._rows_on(migrated_engine, account_id, yesterday) == []
        stats = self._import_dated(connection_id, self._payload(), yesterday)
        assert stats["holdings_snapshots"] == 0 and stats["holdings_skipped_manual"] == 2
        assert self._rows_on(migrated_engine, account_id, yesterday) == []
        with db.get_sessionmaker()() as session:
            latest = networth.latest_holdings(session, uuid.UUID(account_id), yesterday)
            assert [(h.symbol, h.source) for h in latest] == [("ACME", "manual")]

    def test_a_sold_out_hand_tracked_date_takes_no_feed_set(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """Sold out by hand yesterday (the set holds only a zero row), then
        switched back today: a fetch dated yesterday still adds nothing."""
        today = datetime.now(UTC).date()
        yesterday = today - timedelta(days=1)
        connection_id, account_id = self._manual_then_feed(
            harness, today - timedelta(days=4), switch=False
        )
        positions = harness.client.get(f"/accounts/{account_id}/positions").json()
        resp = harness.client.post(
            f"/positions/{positions[0]['id']}/events",
            json={"kind": "set_quantity", "occurred_on": yesterday.isoformat(), "quantity": "0"},
        )
        assert resp.status_code == 201, resp.text
        self._switch_back(harness, account_id)
        stats = self._import_dated(connection_id, self._payload(), yesterday)
        assert stats["holdings_snapshots"] == 0 and stats["holdings_skipped_manual"] == 2
        assert self._rows_on(migrated_engine, account_id, yesterday) == [
            ("ACME", "manual", Decimal("0.000000"))
        ]
        assert self._value(account_id, yesterday) is None

    def test_a_reimport_never_adds_feed_rows_to_a_hand_tracked_date(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """Re-importing a fetch dated inside the manual period (the map path
        re-imports the latest fetch) must not put feed rows beside the
        hand-tracked set of that date: one source per date."""
        today = datetime.now(UTC).date()
        yesterday = today - timedelta(days=1)
        connection_id, account_id = self._manual_then_feed(harness, yesterday)
        before = self._rows_on(migrated_engine, account_id, yesterday)
        assert before == [("ACME", "manual", Decimal("3.000000"))]
        stats = self._import_dated(connection_id, self._payload(), yesterday)
        assert stats["holdings_snapshots"] == 0 and stats["holdings_skipped_manual"] == 2
        assert self._rows_on(migrated_engine, account_id, yesterday) == before

    @staticmethod
    def _cash_only(payload: dict) -> dict:
        payload["accounts"][0]["holdings"] = []
        return payload

    def test_a_round_trip_across_a_cash_only_fetch_leaves_no_hole(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """manual -> feed -> manual across one cash-only fetch (the cash-sweep shape):
        the period-end rows are manual tombstones, which never move the feed
        floor, so the projection re-derives the hand-tracked history and that
        date holds the shares again."""
        today = datetime.now(UTC).date()
        connection_id, account_id = self._manual_then_feed(harness, today - timedelta(days=3))
        self._import_dated(connection_id, self._cash_only(self._payload()), today)
        assert self._value(account_id, today) is None  # the feed holds nothing
        resp = harness.client.patch(f"/accounts/{account_id}", json={"holdings_source": "manual"})
        assert resp.status_code == 200, resp.text
        assert self._value(account_id, today) == Decimal("300.0000")
        assert self._rows_on(migrated_engine, account_id, today) == []  # the tombstone went
        self._one_row_per_symbol(migrated_engine, account_id)

    def test_a_fetch_taken_before_the_switch_never_ends_the_period(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """The day's sync ran while the account was still manual; after the
        switch, mapping another account re-imports that fetch. It was taken
        before the switch, so the switch day keeps its hand-tracked value."""
        today = datetime.now(UTC).date()
        payload = self._payload()
        payload["accounts"].append(account_of([], account_id="SF-CHK", balance="0.00"))
        connection_id, account_id = self._manual_then_feed(
            harness, today - timedelta(days=3), switch=False
        )
        self._import_dated(connection_id, self._cash_only(payload), today)  # still manual
        self._switch_back(harness, account_id)
        resp = harness.client.post(
            f"/connections/{connection_id}/map",
            json={
                "external_id": "SF-CHK",
                "create": {"name": "Chk", "type": "checking", "path": "assets.chk"},
            },
        )
        assert resp.status_code == 200, resp.text
        assert self._value(account_id, today) == Decimal("300.0000")
        assert self._rows_on(migrated_engine, account_id, today) == []

    def test_a_later_fetch_that_day_replaces_a_tombstone(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """The first fetch after the switch omits ACME (a tombstone ends it); a
        later fetch the same day reports it: its row replaces the tombstone,
        as a later fetch adds an omitted symbol on any other date."""
        today = datetime.now(UTC).date()
        connection_id, account_id = self._manual_then_feed(harness, today - timedelta(days=3))
        first = self._import_dated(connection_id, self._cash_only(self._payload()), today)
        assert first["manual_period_tombstones"] == 1
        self._import_dated(connection_id, self._payload(), today)
        assert self._rows_on(migrated_engine, account_id, today) == [
            ("ACME", "feed", Decimal("3.000000")),
            ("TEST", "feed", Decimal("1.000000")),
        ]
        assert self._value(account_id, today) == Decimal("310.0000")
        self._one_row_per_symbol(migrated_engine, account_id)

    def test_the_period_end_is_audited_and_counted(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        from purser import db
        from purser.models import AuditLog

        today = datetime.now(UTC).date()
        connection_id, account_id = self._manual_then_feed(harness, today - timedelta(days=3))
        stats = self._import_dated(connection_id, self._cash_only(self._payload()), today)
        assert stats["manual_period_tombstones"] == 1
        assert stats["manual_period_rows_deleted"] == 0
        with db.get_sessionmaker()() as session:
            (row,) = session.execute(
                select(AuditLog).where(
                    AuditLog.entity_id == uuid.UUID(account_id),
                    AuditLog.action == "account.manual_period_end",
                )
            ).scalars()
        after = row.before_after["after"]
        assert after["tombstones_written"] == 1 and after["raw_fetch_id"] is not None
        assert after["on"] == today.isoformat() and after["reason"] == "fetch"
        # Once: the next fetch finds the period ended.
        again = self._import_dated(connection_id, self._cash_only(self._payload()), today)
        assert again["manual_period_tombstones"] == 0

    def test_a_feed_row_holding_zero_shares_stays_visible(self, harness: FeedHarness) -> None:
        """The bank's own zero rows (a money-market sweep at 0 shares) are part
        of the set net worth sums: GET /holdings lists them."""
        payload = self._payload()
        payload["accounts"][0]["holdings"].append(
            {"symbol": "VMFXX", "shares": "0", "market_value": "0.00"}
        )
        harness.use_payload(payload)
        connection_id = harness.connect()
        account_id = harness.client.post(
            "/accounts", json={"name": "Plan", "type": "brokerage", "path": "assets.plan"}
        ).json()["id"]
        resp = harness.client.post(
            f"/connections/{connection_id}/map",
            json={"external_id": "SF-PLAN", "account_id": account_id},
        )
        assert resp.status_code == 200, resp.text
        harness.sync(connection_id)
        listed = harness.client.get("/holdings", params={"account": "assets.plan"}).json()
        assert sorted(h["symbol"] for h in listed) == ["ACME", "TEST", "VMFXX"]

    def test_losing_the_feed_mapping_ends_an_open_period_that_day(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """Back on the feed but never fetched, then its feed account is mapped
        onto another account: no fetch will end this one's period, so it ends
        the day it is unmapped, with tombstones and an audit row."""
        from purser import db
        from purser.models import AuditLog

        today = datetime.now(UTC).date()
        connection_id, account_id = self._manual_then_feed(harness, today - timedelta(days=3))
        other = harness.client.post(
            "/accounts", json={"name": "Plan 2", "type": "brokerage", "path": "assets.plan2"}
        ).json()["id"]
        resp = harness.client.post(
            f"/connections/{connection_id}/map",
            json={"external_id": "SF-PLAN", "account_id": other},
        )
        assert resp.status_code == 200, resp.text
        assert self._value(account_id, today) is None
        assert self._rows_on(migrated_engine, account_id, today) == [
            ("ACME", "manual", Decimal("0.000000"))
        ]
        with db.get_sessionmaker()() as session:
            (row,) = session.execute(
                select(AuditLog).where(
                    AuditLog.entity_id == uuid.UUID(account_id),
                    AuditLog.action == "account.manual_period_end",
                )
            ).scalars()
        assert row.before_after["after"]["reason"] == "unmapped"

    def test_remapping_an_open_period_keeps_the_set_until_the_new_feed_fetches(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        """#422 item 1: an account whose period is open, remapped onto ANOTHER
        feed account, stays mapped, so the new mapping's first fetch after the
        switch ends its period. Until then the hand-tracked set stays in effect:
        no blank day (the re-import of the latest fetch, taken before the switch,
        adds nothing)."""
        today = datetime.now(UTC).date()
        connection_id, account_id = self._manual_then_feed(harness, today - timedelta(days=3))
        before = self._value(account_id, today)
        assert before is not None
        two = self._payload()
        second = json.loads(json.dumps(two["accounts"][0]))
        second["id"] = "SF-PLAN2"
        second["holdings"] = [{"symbol": "ACME", "shares": "5", "market_value": "500.00"}]
        two["accounts"].append(second)
        self._import_dated(connection_id, two, today - timedelta(days=1))
        resp = harness.client.post(
            f"/connections/{connection_id}/map",
            json={"external_id": "SF-PLAN2", "account_id": account_id},
        )
        assert resp.status_code == 200, resp.text
        assert self._value(account_id, today) == before
        assert ("ACME", "manual", Decimal("0.000000")) not in self._rows_on(
            migrated_engine, account_id, today
        )
        self._import_dated(connection_id, two, today)
        assert self._rows_on(migrated_engine, account_id, today) == [
            ("ACME", "feed", Decimal("5.000000"))
        ]
        self._one_row_per_symbol(migrated_engine, account_id)

    def _payload(self) -> dict:
        account = account_of(
            [
                {
                    "id": "sweep-1",
                    "posted": _recent_epoch(2),
                    "amount": "-250.00",
                    "description": "ACH DISBURSEMENT",
                }
            ],
            account_id="SF-PLAN",
            balance="12.34",
        )
        account["holdings"] = [
            {"symbol": "ACME", "shares": "3", "market_value": "300.00", "cost_basis": "200.00"},
            {"symbol": "TEST", "shares": "1", "market_value": "10.00"},
        ]
        return multi_payload_of([account])

    def test_manual_account_gets_no_feed_snapshot_but_keeps_its_cash(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        harness.use_payload(self._payload())
        connection_id = harness.connect()
        created = harness.client.post(
            "/accounts", json={"name": "Plan", "type": "brokerage", "path": "assets.plan"}
        ).json()
        assert (
            harness.client.patch(
                f"/accounts/{created['id']}", json={"holdings_source": "manual"}
            ).status_code
            == 200
        )
        resp = harness.client.post(
            f"/connections/{connection_id}/map",
            json={"external_id": "SF-PLAN", "account_id": created["id"]},
        )
        assert resp.status_code == 200, resp.text

        first = harness.sync(connection_id)
        assert first["holdings_snapshots"] == 0
        assert first["holdings_skipped_manual"] == 2
        assert first["imported"] == 1  # the cash row imports as usual
        second = harness.sync(connection_id)
        assert second["imported"] == 0 and second["holdings_snapshots"] == 0

        with migrated_engine.connect() as conn:
            snapshots = conn.execute(
                text("SELECT count(*) FROM holdings_snapshots WHERE account_id = :a"),
                {"a": created["id"]},
            ).scalar_one()
            cash = conn.execute(
                text(
                    "SELECT count(*), sum(p.amount) FROM postings p"
                    " JOIN transactions t ON t.id = p.transaction_id"
                    " WHERE p.account_id = :a AND t.status IN ('pending', 'posted')"
                ),
                {"a": created["id"]},
            ).one()
        assert snapshots == 0
        assert cash == (1, Decimal("-250.0000"))

    def test_feed_account_still_snapshots(self, harness: FeedHarness) -> None:
        harness.use_payload(self._payload())
        connection_id = harness.connect()
        created = harness.client.post(
            "/accounts", json={"name": "Plan", "type": "brokerage", "path": "assets.plan"}
        ).json()
        harness.client.post(
            f"/connections/{connection_id}/map",
            json={"external_id": "SF-PLAN", "account_id": created["id"]},
        )
        result = harness.sync(connection_id)
        assert result["holdings_snapshots"] == 2 and result["holdings_skipped_manual"] == 0
