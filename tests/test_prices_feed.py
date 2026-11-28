"""The price job asks the provider for zero-valued feed rows too (#680,
extending decisions 89 and 92).

A feed that reports a held fund with no value is valued on read from the
stored close in effect on the row's own date. The daily job now fetches
those closes from the configured provider, for each such row's date and the
week before it; a ticker the provider does not know (a plan-only fund) is
quiet: answered, reported in ``not_covered``, never a failure.

Every provider call goes through an httpx MockTransport. Synthetic tickers,
amounts and key only; Decimal throughout.
"""

import uuid
from datetime import date, timedelta

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from purser import db
from purser.holdings import overlay, prices
from purser.models import Event
from tests.conftest import today
from tests.test_feed_overlay import _feed_account, _row
from tests.test_prices import _client, _closes, _hold, _stored, _tiingo, _windows, broker  # noqa: F401


def _symbol() -> str:
    return f"Z{uuid.uuid4().hex[:6].upper()}"


def _ago(days: int) -> date:
    return today() - timedelta(days=days)


def _feed(symbol: str, *rows: tuple[int, str | None]) -> uuid.UUID:
    """A feed account holding ``symbol`` in one row per (days ago, value)."""
    with db.get_sessionmaker()() as session:
        account = _feed_account(session)
        for days_ago, market in rows:
            _row(session, account.id, _ago(days_ago), symbol, "3", market)
        session.commit()
        return account.id


def _failed_events(symbol: str) -> list[Event]:
    with db.get_sessionmaker()() as session:
        return [
            e
            for e in session.execute(
                select(Event).where(Event.type == prices.FETCH_FAILED_EVENT)
            ).scalars()
            if e.payload.get("symbol") == symbol
        ]


def _unpriced(symbol: str) -> bool:
    with db.get_sessionmaker()() as session:
        return any(u.symbol == symbol for u in overlay.unpriced_holdings(session, today=today()))


class TestFeedRowsAreFetched:
    def test_each_zero_row_date_and_the_week_before_it_is_asked(
        self, admin_client: TestClient
    ) -> None:
        _tiingo(admin_client)
        symbol = _symbol()
        _feed(symbol, (20, "0"), (5, None))
        assert _unpriced(symbol)

        asked: list[tuple[str, str]] = []
        result = prices.run_fetch(today=today(), client=_client(_windows(asked)))

        assert sorted(asked) == [
            (_ago(27).isoformat(), _ago(20).isoformat()),
            (_ago(12).isoformat(), _ago(5).isoformat()),
        ]
        assert result.failed == [] and result.not_covered == []
        stored = {d for d, _c, source in _stored(symbol) if source == "tiingo"}
        assert _ago(5) in stored and _ago(20) in stored
        assert not _unpriced(symbol)  # the provider's close values the row

    def test_a_row_the_feed_values_is_not_asked(self, admin_client: TestClient) -> None:
        _tiingo(admin_client)
        symbol = _symbol()
        _feed(symbol, (3, "31.5"))
        asked: list[tuple[str, str]] = []
        prices.run_fetch(today=today(), client=_client(_windows(asked)))
        assert _stored(symbol) == []

    def test_a_symbol_that_is_no_ticker_is_never_sent(self, admin_client: TestClient) -> None:
        _tiingo(admin_client)
        name = f"Stable Value Trust {uuid.uuid4().hex[:4]}"
        _feed(name, (3, "0"))
        sent: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            sent.append(request.url.path)
            return httpx.Response(200, text=_closes())

        prices.run_fetch(today=today(), client=_client(handler))
        assert not any("stable" in path for path in sent)


class TestNotCovered:
    def test_a_not_found_for_a_feed_only_ticker_is_quiet_and_asked_once(
        self, admin_client: TestClient
    ) -> None:
        _tiingo(admin_client)
        symbol = _symbol()
        _feed(symbol, (2, "0"))
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if symbol.lower() in request.url.path:
                return httpx.Response(404, text='{"detail": "Not found."}')
            return httpx.Response(200, text=_closes())

        result = prices.run_fetch(today=today(), client=_client(handler))
        assert result.not_covered == [symbol]
        assert all(f["symbol"] != symbol for f in result.failed)
        assert _failed_events(symbol) == []

        mine = [c for c in calls if symbol.lower() in c]
        again = prices.run_fetch(today=today(), client=_client(handler))
        assert [c for c in calls if symbol.lower() in c] == mine  # not asked again
        assert again.not_covered == []

    def test_a_not_found_is_still_a_failure_when_a_manual_position_needs_it(
        self,
        broker: str,  # noqa: F811  (the fixture imported above)
        admin_client: TestClient,
    ) -> None:
        _tiingo(admin_client)
        symbol = _symbol()
        _feed(symbol, (2, "0"))
        _hold(broker, symbol, "1", days_ago=2)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, text='{"detail": "Not found."}')

        result = prices.run_fetch(today=today(), client=_client(handler))
        assert result.not_covered == []
        assert any(f["symbol"] == symbol and f["status"] == 404 for f in result.failed)
        assert _failed_events(symbol) != []

    def test_fetch_now_reports_not_covered(self, admin_client: TestClient) -> None:
        """POST /prices/fetch carries ``not_covered`` (no provider here: empty)."""
        body = admin_client.post("/prices/fetch").json()
        assert body["not_covered"] == []
