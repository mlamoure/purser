"""Feed freshness and bank errors (#480): one event per episode, nothing
while healthy, recovery re-arms; the digest names what is open now."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, select

from tests import test_feed
from tests.test_feed import FeedHarness, account_of
from tests.test_feed_v2 import CONN, as_v2

feed_env = test_feed.feed_env
harness = test_feed.harness

ACCOUNT = "Demo Checking"


def _days_ago(days: int) -> int:
    return int((datetime.now(UTC) - timedelta(days=days)).timestamp())


def _payload(
    *, balance_days_ago: int = 0, errlist: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return as_v2({"accounts": [account_of([], balance_date=_days_ago(balance_days_ago))]}, errlist)


def _events(kind: str) -> list[dict[str, Any]]:
    """Payloads of the events of one type, oldest first (payloads are
    encrypted at rest, so read them through the model)."""
    from purser import db
    from purser.models import Event

    with db.get_sessionmaker()() as session:
        rows = session.execute(select(Event).where(Event.type == kind).order_by(Event.at)).scalars()
        return [dict(row.payload) for row in rows]


def _connected(harness: FeedHarness, payload: dict[str, Any]) -> str:
    harness.use_payload(payload)
    connection_id = harness.connect()
    harness.sync(connection_id)
    harness.map_new(connection_id, ACCOUNT, "assets.checking")
    return connection_id


def _sync(harness: FeedHarness, connection_id: str, payload: dict[str, Any]) -> None:
    harness.use_payload(payload)
    harness.sync(connection_id)


def _feed_digest() -> dict[str, Any]:
    from purser import db, digest

    with db.get_sessionmaker()() as session:
        return dict(digest.whats_new(session)["feed"])


AUTH = [{"code": "con.auth", "msg": "Log in again", "conn_id": CONN["conn_id"]}]
FAILED = [{"code": "act.failed", "msg": "Try later", "account_id": ACCOUNT}]


class TestLoginEpisodes:
    def test_one_event_per_episode_and_recovery_rearms(self, harness: FeedHarness) -> None:
        connection_id = _connected(harness, _payload(errlist=AUTH))
        assert len(_events("feed.auth_required")) == 1
        _sync(harness, connection_id, _payload(errlist=AUTH))
        assert len(_events("feed.auth_required")) == 1  # still the same episode

        _sync(harness, connection_id, _payload())
        (recovered,) = _events("feed.recovered")
        assert recovered["kind"] == "auth"
        assert recovered["institution"] == CONN["name"]

        _sync(harness, connection_id, _payload(errlist=AUTH))
        events = _events("feed.auth_required")
        assert len(events) == 2
        assert events[-1]["institution"] == CONN["name"]
        assert events[-1]["code"] == "con.auth"

    def test_unprefixed_login_id_still_names_the_bank(self, harness: FeedHarness) -> None:
        """#559: an issue naming the login without its id prefix carries the
        bank's name, and its episode is keyed by the id the issue sent."""
        short = [{"code": "con.auth", "msg": "Log in again", "conn_id": "DEMO-1"}]
        connection_id = _connected(harness, _payload(errlist=short))
        (event,) = _events("feed.auth_required")
        assert (event["institution"], event["conn_id"]) == (CONN["name"], "DEMO-1")
        _sync(harness, connection_id, _payload(errlist=short))
        assert len(_events("feed.auth_required")) == 1

    def test_digest_names_the_login_until_it_is_fixed(self, harness: FeedHarness) -> None:
        connection_id = _connected(harness, _payload(errlist=AUTH))
        feed = _feed_digest()
        assert feed["needs_login"] == [
            {"institution": CONN["name"], "code": "con.auth", "message": "Log in again"}
        ]
        _sync(harness, connection_id, _payload())
        assert _feed_digest()["needs_login"] == []

    def test_bridge_usage_warning_is_no_login_problem(self, harness: FeedHarness) -> None:
        _connected(harness, _payload(errlist=[{"code": "gen.api", "msg": "Range too long"}]))
        assert _events("feed.auth_required") == []
        assert _feed_digest()["needs_login"] == []


class TestAccountFailures:
    def test_streak_of_two_opens_one_episode(self, harness: FeedHarness) -> None:
        connection_id = _connected(harness, _payload())
        _sync(harness, connection_id, _payload(errlist=FAILED))
        assert _events("feed.account_failed") == []  # one failure is not an episode
        _sync(harness, connection_id, _payload(errlist=FAILED))
        (opened,) = _events("feed.account_failed")
        assert opened["account"] == "assets.checking" and opened["code"] == "act.failed"
        _sync(harness, connection_id, _payload(errlist=FAILED))
        assert len(_events("feed.account_failed")) == 1
        assert _feed_digest()["failing"][0]["account"] == "assets.checking"

        _sync(harness, connection_id, _payload())
        (recovered,) = _events("feed.recovered")
        assert recovered["kind"] == "account_failed"

    def test_a_single_blip_neither_opens_nor_recovers(self, harness: FeedHarness) -> None:
        connection_id = _connected(harness, _payload())
        _sync(harness, connection_id, _payload(errlist=FAILED))
        _sync(harness, connection_id, _payload())
        assert _events("feed.account_failed") == []
        assert _events("feed.recovered") == []

    def test_an_unmapped_account_is_not_tracked(self, harness: FeedHarness) -> None:
        harness.use_payload(_payload(errlist=FAILED))
        connection_id = harness.connect()
        harness.sync(connection_id)
        _sync(harness, connection_id, _payload(errlist=FAILED))
        assert _events("feed.account_failed") == []


class TestStaleBalances:
    def test_frozen_balance_date_fires_once_and_recovers(self, harness: FeedHarness) -> None:
        connection_id = _connected(harness, _payload(balance_days_ago=10))
        # The mapping sync is the first that sees a mapped account.
        _sync(harness, connection_id, _payload(balance_days_ago=10))
        (stale,) = _events("feed.stale")
        assert stale["account"] == "assets.checking"
        assert stale["stale_days"] == 10
        _sync(harness, connection_id, _payload(balance_days_ago=10))
        assert len(_events("feed.stale")) == 1
        assert _feed_digest()["stale"][0]["days"] == 10

        _sync(harness, connection_id, _payload(balance_days_ago=0))
        (recovered,) = _events("feed.recovered")
        assert recovered["kind"] == "stale"
        assert _feed_digest()["stale"] == []

    def test_a_new_freeze_is_a_new_episode(self, harness: FeedHarness) -> None:
        connection_id = _connected(harness, _payload(balance_days_ago=10))
        _sync(harness, connection_id, _payload(balance_days_ago=10))
        _sync(harness, connection_id, _payload(balance_days_ago=6))
        assert len(_events("feed.stale")) == 2  # a later, still-old date

    def test_within_the_threshold_is_fresh(self, harness: FeedHarness) -> None:
        connection_id = _connected(harness, _payload(balance_days_ago=4))
        _sync(harness, connection_id, _payload(balance_days_ago=4))
        assert _events("feed.stale") == []  # default threshold: more than 4 days

    def test_threshold_is_a_runtime_setting(self, harness: FeedHarness) -> None:
        assert harness.client.put("/config", json={"feed_stale_days": 2}).status_code == 200
        assert harness.client.get("/config").json()["values"]["feed_stale_days"] == 2
        connection_id = _connected(harness, _payload(balance_days_ago=3))
        _sync(harness, connection_id, _payload(balance_days_ago=3))
        assert len(_events("feed.stale")) == 1


class TestIsolation:
    def test_a_health_bug_never_fails_a_sync(
        self, harness: FeedHarness, monkeypatch: pytest.MonkeyPatch, migrated_engine: Engine
    ) -> None:
        from purser.feed import health

        def broken(*args: object, **kwargs: object) -> None:
            raise RuntimeError("synthetic failure")

        monkeypatch.setattr(health, "observe", broken)
        connection_id = _connected(harness, _payload(errlist=AUTH))
        assert harness.health_of(connection_id)["status"] == "warning"
        assert _events("feed.auth_required") == []
        after = test_feed._last_sync_after()
        assert after["feed_health"] == {}
