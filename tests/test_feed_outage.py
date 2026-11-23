"""Outage repair (#481): rows a bank outage left behind are re-fetched when
the bank recovers, and on demand through POST /connections/{id}/refetch."""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, text

from purser.feed import simplefin
from tests import test_feed
from tests.conftest import now
from tests.test_feed import ACCESS_URL, FeedHarness, _param_to_date, account_of
from tests.test_feed_v2 import as_v2

feed_env = test_feed.feed_env
harness = test_feed.harness

ACCOUNT = "Demo Checking"
PATH = "assets.checking"


def _ago(days: int) -> datetime:
    return now() - timedelta(days=days)


def _row(days_ago: int, amount: str, desc: str) -> dict[str, Any]:
    return {
        "id": f"r-{days_ago}-{desc}",
        "posted": int(_ago(days_ago).timestamp()),
        "amount": amount,
        "description": desc,
    }


@dataclass
class Bank:
    """A bridge that serves the rows inside the requested window (end-date
    exclusive, as the protocol says) with a settable balance date — frozen
    during an outage, current after it."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    balance_days_ago: int = 0
    fail_focused: bool = False
    requests: list[dict[str, Any]] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/claim/TEST123"):
                return httpx.Response(200, text=ACCESS_URL)
            start = _param_to_date(request, "start-date")
            end = _param_to_date(request, "end-date")
            focused = list(request.url.params.get_list("account"))
            self.requests.append(
                {
                    "start": start,
                    "end": end,
                    "accounts": focused,
                    "pending": request.url.params.get("pending"),
                }
            )
            if focused and self.fail_focused:
                raise httpx.ConnectError("bridge unreachable")
            served = [
                row
                for row in self.rows
                if (start is None or _date(row) >= start) and (end is None or _date(row) < end)
            ]
            account = account_of(served, balance_date=int(_ago(self.balance_days_ago).timestamp()))
            return httpx.Response(200, json=as_v2({"accounts": [account]}))

        return httpx.MockTransport(handler)


def _date(row: dict[str, Any]) -> date:
    return datetime.fromtimestamp(int(row["posted"])).date()


def _descriptions(engine: Engine) -> list[str]:
    from purser import db
    from purser.models import Transaction

    with db.get_sessionmaker()() as session:
        rows = session.query(Transaction).filter(Transaction.origin == "import").all()
        return sorted(str(row.description) for row in rows)


def _frozen_then_mapped(harness: FeedHarness, bank: Bank, *, frozen_days: int) -> str:
    """Connect while the bank is frozen, map the account, and sync once more
    so a completed sync has seen the mapped account's frozen balance date."""
    bank.balance_days_ago = frozen_days
    harness.use_transport(bank.transport())
    connection_id = harness.connect()
    harness.sync(connection_id)
    harness.map_new(connection_id, ACCOUNT, PATH)
    harness.sync(connection_id)
    return connection_id


class TestAutomaticRepair:
    def test_recovery_fetches_what_the_outage_left(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        bank = Bank()
        connection_id = _frozen_then_mapped(harness, bank, frozen_days=30)
        # The bank recovers: it now lists rows from during the outage.
        bank.balance_days_ago = 0
        bank.rows = [_row(20, "-55.00", "During outage"), _row(2, "-10.00", "Recent")]
        before = len(bank.requests)
        result = harness.sync(connection_id)

        repair = result["gap_repair"]
        assert repair["accounts"] == [PATH]
        assert repair["imported"] == 1
        assert repair["deferred"] is None
        assert _descriptions(migrated_engine) == ["During outage", "Recent"]
        main, focused = bank.requests[before:]
        assert focused["accounts"] == [ACCOUNT]
        assert focused["pending"] is None  # a gap window carries no pendings
        assert focused["start"] <= _ago(31).date()
        assert focused["end"] > main["start"]  # overlaps the sync's own window

        after = test_feed._last_sync_after()
        assert after["gap_repair"]["imported"] == 1
        assert after["feed_health"]["repairs"] == {}

        # Nothing more is owed: the next sync is a single fetch.
        before = len(bank.requests)
        assert harness.sync(connection_id).get("gap_repair") is None
        assert len(bank.requests) == before + 1

    def test_a_short_gap_inside_the_window_needs_no_repair(self, harness: FeedHarness) -> None:
        bank = Bank()
        connection_id = _frozen_then_mapped(harness, bank, frozen_days=3)
        bank.balance_days_ago = 0
        before = len(bank.requests)
        assert harness.sync(connection_id).get("gap_repair") is None
        assert len(bank.requests) == before + 1

    def test_a_repair_over_budget_is_carried_to_the_next_sync(
        self,
        harness: FeedHarness,
        migrated_engine: Engine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bank = Bank()
        connection_id = _frozen_then_mapped(harness, bank, frozen_days=30)
        bank.balance_days_ago = 0
        bank.rows = [_row(20, "-55.00", "During outage")]
        with migrated_engine.connect() as conn:
            fetched = conn.execute(
                text("SELECT COUNT(*) FROM raw_fetches WHERE connection_id = :c"),
                {"c": connection_id},
            ).scalar_one()
        # Room for the sync's own fetch, none for the repair.
        monkeypatch.setattr(simplefin, "BRIDGE_MAX_FETCHES_PER_DAY", fetched + 1)
        repair = harness.sync(connection_id)["gap_repair"]
        assert repair["deferred"] == "rate_budget"
        assert _descriptions(migrated_engine) == []
        assert test_feed._last_sync_after()["feed_health"]["repairs"]

        monkeypatch.setattr(simplefin, "BRIDGE_MAX_FETCHES_PER_DAY", 1000)
        repair = harness.sync(connection_id)["gap_repair"]
        assert repair["deferred"] is None and repair["imported"] == 1
        assert _descriptions(migrated_engine) == ["During outage"]

    def test_a_failed_repair_fetch_never_fails_the_sync(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        bank = Bank()
        connection_id = _frozen_then_mapped(harness, bank, frozen_days=30)
        bank.balance_days_ago = 0
        bank.rows = [_row(2, "-10.00", "Recent")]
        bank.fail_focused = True
        result = harness.sync(connection_id)
        assert result["gap_repair"]["deferred"] == "fetch_error"
        assert _descriptions(migrated_engine) == ["Recent"]
        assert harness.health_of(connection_id)["status"] == "active"

    def test_an_outage_beyond_the_bridge_reach_says_so(self, harness: FeedHarness) -> None:
        bank = Bank()
        connection_id = _frozen_then_mapped(harness, bank, frozen_days=120)
        bank.balance_days_ago = 0
        repair = harness.sync(connection_id)["gap_repair"]
        assert repair["unreachable_before"] is not None
        assert date.fromisoformat(repair["start"]) >= _ago(simplefin.BRIDGE_MAX_RANGE_DAYS).date()


class TestManualRefetch:
    def _ready(self, harness: FeedHarness, bank: Bank) -> str:
        harness.use_transport(bank.transport())
        connection_id = harness.connect()
        harness.sync(connection_id)
        harness.map_new(connection_id, ACCOUNT, PATH)
        return connection_id

    def test_refetch_imports_a_missed_range_once(
        self, harness: FeedHarness, migrated_engine: Engine
    ) -> None:
        bank = Bank()
        connection_id = self._ready(harness, bank)
        bank.rows = [_row(40, "-70.00", "Missed"), _row(80, "-5.00", "Outside")]
        body = {
            "start": _ago(50).date().isoformat(),
            "end": _ago(30).date().isoformat(),
            "accounts": [PATH],
        }
        resp = harness.client.post(f"/connections/{connection_id}/refetch", json=body)
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert out["imported"] == 1 and out["accounts"] == [PATH]
        assert bank.requests[-1]["accounts"] == [ACCOUNT]
        assert _descriptions(migrated_engine) == ["Missed"]

        again = harness.client.post(f"/connections/{connection_id}/refetch", json=body).json()
        assert again["imported"] == 0
        assert _descriptions(migrated_engine) == ["Missed"]

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            ({"start": "2026-01-01", "end": "2026-06-01"}, "at most"),
            ({"start": "2026-06-01", "end": "2026-05-01"}, "before start"),
        ],
    )
    def test_refetch_refuses_bad_requests(
        self, harness: FeedHarness, body: dict[str, Any], message: str
    ) -> None:
        connection_id = self._ready(harness, Bank())
        resp = harness.client.post(f"/connections/{connection_id}/refetch", json=body)
        assert resp.status_code == 422
        assert message in resp.text

    def test_refetch_refuses_an_account_of_no_mapping(self, harness: FeedHarness) -> None:
        connection_id = self._ready(harness, Bank())
        body = {"start": _ago(10).date().isoformat(), "accounts": ["assets.nope"]}
        resp = harness.client.post(f"/connections/{connection_id}/refetch", json=body)
        assert resp.status_code == 422
        assert "not an account mapped" in resp.text


class TestNarrowFetchesDoNotHideAccounts:
    def test_discovery_and_health_read_the_last_sync_not_a_refetch(
        self, harness: FeedHarness
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/claim/TEST123"):
                return httpx.Response(200, text=ACCESS_URL)
            focused = list(request.url.params.get_list("account"))
            accounts = [account_of([], account_id=ACCOUNT), account_of([], account_id="Savings")]
            if focused:
                accounts = [a for a in accounts if a["id"] in focused]
                return httpx.Response(200, json=as_v2({"accounts": accounts}))
            errlist = [{"code": "gen.api", "msg": "note from the sync"}]
            return httpx.Response(200, json=as_v2({"accounts": accounts}, errlist))

        harness.use_transport(httpx.MockTransport(handler))
        connection_id = harness.connect()
        harness.sync(connection_id)
        harness.map_new(connection_id, ACCOUNT, PATH)
        body = {"start": _ago(10).date().isoformat(), "accounts": [PATH]}
        assert (
            harness.client.post(f"/connections/{connection_id}/refetch", json=body).status_code
            == 200
        )
        rows = harness.client.get(f"/connections/{connection_id}/discovered").json()
        assert sorted(r["external_id"] for r in rows) == [ACCOUNT, "Savings"]
        assert harness.health_of(connection_id)["last_errors"] == ["note from the sync"]
