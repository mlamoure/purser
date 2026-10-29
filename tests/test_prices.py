"""Prices for manually tracked holdings (issue #390, decision 89): the Tiingo
provider, the daily fetch, staleness, and the key's secrecy.

Every provider call goes through an httpx MockTransport — no test reaches the
network. Fixtures are synthetic: TEST / ACME-style tickers, made-up closes,
and an API key that is obviously fake.
"""

import json
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text

from purser import appsettings, db
from purser.digest import whats_new
from purser.holdings import prices, service
from purser.holdings.providers import PriceFetchError, TiingoProvider
from purser.models import AuditLog, Event, HoldingsSnapshot, Position, SecurityPrice

TODAY = datetime.now(UTC).date()
FAKE_KEY = "test-key-not-real-0123456789abcdef"


def _symbol() -> str:
    return f"Q{uuid.uuid4().hex[:7].upper()}"


Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _closes(*rows: tuple[date, str]) -> str:
    """A Tiingo-shaped body. Closes are written as JSON numbers, exactly as
    the provider sends them — the parser must read them as Decimal."""
    body = ",".join(
        f'{{"date": "{d.isoformat()}T00:00:00.000Z", "close": {close}, "adjClose": 1.5}}'
        for d, close in rows
    )
    return f"[{body}]"


class TestTiingoProvider:
    def test_request_carries_only_the_ticker_dates_and_key(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, text=_closes((TODAY, "123.456789")))

        provider = TiingoProvider(FAKE_KEY, client=_client(handler))
        closes = provider.closes("BRK.B", TODAY - timedelta(days=3), TODAY)
        (request,) = seen
        assert request.url.host == "api.tiingo.com"
        assert request.url.path == "/tiingo/daily/brk-b/prices"
        assert dict(request.url.params) == {
            "startDate": (TODAY - timedelta(days=3)).isoformat(),
            "endDate": TODAY.isoformat(),
        }
        assert request.headers["Authorization"] == f"Token {FAKE_KEY}"
        assert request.content == b""  # nothing but the ticker leaves Purser
        assert closes == [(TODAY, Decimal("123.456789"))]
        assert type(closes[0][1]) is Decimal

    def test_numbers_parse_as_decimal_never_float(self) -> None:
        """0.1 + 0.2 style artefacts would appear if the body went through
        float: 1234567.123456 is not representable in binary."""
        body = _closes((TODAY, "1234567.123456"), (TODAY - timedelta(days=1), "7"))
        provider = TiingoProvider(
            FAKE_KEY, client=_client(lambda r: httpx.Response(200, text=body))
        )
        assert dict(provider.closes("ACME", TODAY - timedelta(days=5), TODAY)) == {
            TODAY: Decimal("1234567.123456"),
            TODAY - timedelta(days=1): Decimal("7"),
        }

    def test_bad_rows_are_dropped(self) -> None:
        body = json.dumps(
            [
                {"date": TODAY.isoformat(), "close": "12.5"},  # a string, not a number
                {"date": "nonsense", "close": 1},
                {"date": TODAY.isoformat(), "close": 0},
                {"date": (TODAY + timedelta(days=9)).isoformat(), "close": 3},  # outside the range
                {"date": TODAY.isoformat(), "close": True},
            ]
        )
        provider = TiingoProvider(
            FAKE_KEY, client=_client(lambda r: httpx.Response(200, text=body))
        )
        assert provider.closes("ACME", TODAY - timedelta(days=1), TODAY) == []

    @pytest.mark.parametrize(
        ("response", "status"),
        [
            (httpx.Response(401, json={"detail": "Invalid token."}), 401),
            (httpx.Response(404, json={"detail": "Ticker not found"}), 404),
            (httpx.Response(200, text="<html>"), 200),
            (httpx.Response(200, json={"detail": "x"}), 200),
        ],
    )
    def test_failures_name_the_symbol_and_status_only(
        self, response: httpx.Response, status: int
    ) -> None:
        provider = TiingoProvider(FAKE_KEY, client=_client(lambda r: response))
        with pytest.raises(PriceFetchError) as raised:
            provider.closes("ACME", TODAY, TODAY)
        assert raised.value.status == status
        assert FAKE_KEY not in str(raised.value)
        assert FAKE_KEY not in repr(provider)

    @pytest.mark.parametrize(
        "raw",
        [
            "1e22",
            "10000000000000000000000",
            "1E+400",
            "99999999999999.9999995",  # under the limit, but rounds UP to 10^14
        ],
    )
    def test_an_out_of_range_close_is_dropped_and_counted(self, raw: str) -> None:
        """One absurd row costs that row, never the response: the others are
        returned and the drop is counted (decision 89). The range is checked
        after rounding to six places."""
        body = _closes((TODAY - timedelta(days=1), "10.5"), (TODAY, raw))
        provider = TiingoProvider(
            FAKE_KEY, client=_client(lambda r: httpx.Response(200, text=body))
        )
        closes = provider.closes("ACME", TODAY - timedelta(days=1), TODAY)
        assert closes == [(TODAY - timedelta(days=1), Decimal("10.5"))]
        assert closes.dropped == 1

    def test_the_largest_close_that_fits_is_kept(self) -> None:
        body = _closes((TODAY, "99999999999999.9999994"))
        provider = TiingoProvider(
            FAKE_KEY, client=_client(lambda r: httpx.Response(200, text=body))
        )
        closes = provider.closes("ACME", TODAY, TODAY)
        assert closes == [(TODAY, Decimal("99999999999999.999999"))] and closes.dropped == 0

    def test_transport_errors_do_not_chain(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"refused, headers={dict(request.headers)}")

        provider = TiingoProvider(FAKE_KEY, client=_client(handler))
        with pytest.raises(PriceFetchError) as raised:
            provider.closes("ACME", TODAY, TODAY)
        assert raised.value.status is None and raised.value.reason == "ConnectError"
        assert raised.value.__cause__ is None and raised.value.__suppress_context__
        assert FAKE_KEY not in str(raised.value)


@pytest.fixture
def broker(admin_client: TestClient) -> str:
    resp = admin_client.post(
        "/accounts", json={"name": "Broker", "type": "brokerage", "path": "assets.broker"}
    )
    account_id = resp.json()["id"]
    assert (
        admin_client.patch(
            f"/accounts/{account_id}", json={"holdings_source": "manual"}
        ).status_code
        == 200
    )
    return account_id


def _tiingo(admin_client: TestClient) -> None:
    resp = admin_client.put("/config", json={"price_provider": "tiingo", "price_api_key": FAKE_KEY})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["price_api_key_set"] is True and body["values"]["price_provider"] == "tiingo"
    assert FAKE_KEY not in resp.text  # write-only


def _hold(account_id: str, symbol: str, quantity: str, days_ago: int) -> None:
    with db.get_sessionmaker()() as session:
        service.record_position(
            session,
            account_ref=account_id,
            symbol=symbol,
            quantity=Decimal(quantity),
            as_of=TODAY - timedelta(days=days_ago),
            token=None,
        )
        session.commit()


def _stored(symbol: str) -> list[tuple[date, Decimal, str]]:
    with db.get_sessionmaker()() as session:
        return [
            (p.price_date, Decimal(p.close), p.source)
            for p in session.execute(
                select(SecurityPrice)
                .where(SecurityPrice.symbol == symbol)
                .order_by(SecurityPrice.price_date)
            ).scalars()
        ]


class TestFetch:
    def test_backfills_from_opened_on_then_continues_from_the_newest(
        self, broker: str, admin_client: TestClient
    ) -> None:
        _tiingo(admin_client)
        symbol = _symbol()
        _hold(broker, symbol, "2", days_ago=4)
        windows: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            start = date.fromisoformat(request.url.params["startDate"])
            end = date.fromisoformat(request.url.params["endDate"])
            windows.append((start.isoformat(), end.isoformat()))
            days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
            return httpx.Response(200, text=_closes(*[(d, "10.5") for d in days]))

        first = prices.run_fetch(today=TODAY, client=_client(handler))
        # From a week before the first day: the close in effect on a weekend
        # or holiday first day is dated before it.
        assert first.fetched == 12 and first.failed == []
        assert windows == [((TODAY - timedelta(days=11)).isoformat(), TODAY.isoformat())]
        second = prices.run_fetch(today=TODAY, client=_client(handler))
        assert second.fetched == 0 and len(windows) == 1  # nothing left to ask for
        assert [s for _d, _c, s in _stored(symbol)] == ["tiingo"] * 12

        positions = admin_client.get(f"/accounts/{broker}/positions").json()
        (row,) = [p for p in positions if p["symbol"] == symbol]
        assert Decimal(row["market_value"]) == Decimal("21")
        assert row["price_source"] == "tiingo"

        with db.get_sessionmaker()() as session:
            audit = (
                session.execute(
                    select(AuditLog).where(AuditLog.action == "price.fetch").order_by(AuditLog.at)
                )
                .scalars()
                .all()
            )
        assert [a.before_after["after"]["fetched"] for a in audit] == [12, 0]

    def test_the_stored_closes_are_audited_with_their_own_insert(
        self, broker: str, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#405: the provider closes and an audit row recording them commit
        together, so a worker that dies before the run's summary row (here,
        in the re-projection step) leaves no stored close unaudited."""
        from purser.holdings import projection

        _tiingo(admin_client)
        symbol = _symbol()
        _hold(broker, symbol, "2", days_ago=1)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=_closes((TODAY, "10.5")))

        def die(*_args: object, **_kwargs: object) -> list[uuid.UUID]:
            raise RuntimeError("worker killed after the closes committed")

        monkeypatch.setattr(projection, "manual_accounts_with_positions", die)
        with pytest.raises(RuntimeError):
            prices.run_fetch(today=TODAY, client=_client(handler))
        assert [s for _d, _c, s in _stored(symbol)] == ["tiingo"]
        with db.get_sessionmaker()() as session:
            (row,) = session.execute(
                select(AuditLog).where(AuditLog.action == "price.closes")
            ).scalars()
        after = row.before_after["after"]
        assert after["provider"] == "tiingo"
        assert after["symbols"] == {
            symbol: {"inserted": 1, "first": TODAY.isoformat(), "last": TODAY.isoformat()}
        }

    def test_one_failing_symbol_never_blocks_the_others(
        self, broker: str, admin_client: TestClient
    ) -> None:
        _tiingo(admin_client)
        good, bad = _symbol(), _symbol()
        _hold(broker, good, "1", days_ago=1)
        _hold(broker, bad, "1", days_ago=1)

        def handler(request: httpx.Request) -> httpx.Response:
            if bad.lower() in request.url.path:
                return httpx.Response(404, json={"detail": "not found"})
            return httpx.Response(200, text=_closes((TODAY, "3")))

        result = prices.run_fetch(today=TODAY, client=_client(handler))
        assert result.failed == [{"symbol": bad, "status": 404, "reason": "not found"}]
        assert _stored(good) == [(TODAY, Decimal("3"), "tiingo")]
        prices.run_fetch(today=TODAY, client=_client(handler))  # fails again the same day
        with db.get_sessionmaker()() as session:
            failures = (
                session.execute(select(Event).where(Event.type == "price.fetch_failed"))
                .scalars()
                .all()
            )
        assert [(e.payload["symbol"], e.payload["status"]) for e in failures] == [(bad, 404)]

    def test_any_error_from_one_symbol_fails_that_symbol_only(
        self, broker: str, admin_client: TestClient
    ) -> None:
        """Isolation is not limited to PriceFetchError: whatever one symbol's
        provider call raises becomes that symbol's price.fetch_failed, and the
        others are still stored, projected and audited."""
        _tiingo(admin_client)
        first, second, third = sorted(_symbol() for _ in range(3))
        for symbol in (first, second, third):
            _hold(broker, symbol, "1", days_ago=1)

        def handler(request: httpx.Request) -> httpx.Response:
            if second.lower() in request.url.path:
                raise RuntimeError("the transport broke")
            if third.lower() in request.url.path:
                return httpx.Response(200, text=_closes((TODAY, "1e22")))
            return httpx.Response(200, text=_closes((TODAY, "3")))

        result = prices.run_fetch(today=TODAY, client=_client(handler))
        assert result.failed == [
            {"symbol": second, "status": None, "reason": "RuntimeError"},
            {"symbol": third, "status": 200, "reason": "1 close(s) out of range dropped"},
        ]
        assert _stored(first) == [(TODAY, Decimal("3"), "tiingo")]
        with db.get_sessionmaker()() as session:
            failed = {
                e.payload["symbol"]: e.payload["reason"]
                for e in session.execute(
                    select(Event).where(Event.type == "price.fetch_failed")
                ).scalars()
            }
            audits = session.execute(
                select(AuditLog).where(AuditLog.action == "price.fetch")
            ).scalars()
            assert len(list(audits)) == 1
        assert failed == {second: "RuntimeError", third: "1 close(s) out of range dropped"}

    def test_a_key_with_spaces_or_non_ascii_is_refused(self, admin_client: TestClient) -> None:
        """Such a key cannot travel in an HTTP header; refusing it at entry
        keeps it from failing every fetch. The 422 never echoes the key."""
        for bad in ("tiingo key", "tiingo-key\u200b", "tiingo-k\u00e9y", " tiingo"):
            resp = admin_client.put("/config", json={"price_api_key": bad})
            assert resp.status_code == 422, (bad, resp.text)
            assert bad.strip() not in resp.text
        assert admin_client.get("/config").json()["price_api_key_set"] is False

    def test_the_key_never_reaches_logs_audit_or_events(
        self,
        broker: str,
        admin_client: TestClient,
        caplog: pytest.LogCaptureFixture,
        migrated_engine: Engine,
    ) -> None:
        _tiingo(admin_client)
        symbols = [_symbol(), _symbol(), _symbol()]
        for s in symbols:
            _hold(broker, s, "1", days_ago=0)
        calls = iter(["500", "raise", "ok"])

        def handler(request: httpx.Request) -> httpx.Response:
            step = next(calls)
            if step == "500":
                return httpx.Response(500, text=f"echo {request.headers['Authorization']}")
            if step == "raise":
                raise httpx.ReadTimeout(f"timeout {request.headers['Authorization']}")
            return httpx.Response(200, text=_closes((TODAY, "1")))

        caplog.set_level(logging.DEBUG)
        result = prices.run_fetch(today=TODAY, client=_client(handler))
        assert len(result.failed) == 2
        assert FAKE_KEY not in caplog.text
        with migrated_engine.connect() as conn:
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM app_settings"
                        " WHERE key = 'price_api_key' AND value IS NULL"
                    )
                ).scalar_one()
                == 1
            )  # stored encrypted, never as plain JSON
        with db.get_sessionmaker()() as session:
            for row in session.execute(select(AuditLog)).scalars():
                assert FAKE_KEY not in json.dumps(row.before_after, default=str)
                assert FAKE_KEY not in json.dumps(row.detail, default=str)
            for event in session.execute(select(Event)).scalars():
                assert FAKE_KEY not in json.dumps(event.payload, default=str)
        assert FAKE_KEY not in admin_client.get("/config").text
        assert FAKE_KEY not in admin_client.get("/prices/status").text

    def test_no_key_means_no_fetch(self, broker: str, admin_client: TestClient) -> None:
        assert admin_client.put("/config", json={"price_provider": "tiingo"}).status_code == 200
        _hold(broker, _symbol(), "1", days_ago=0)

        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("no key: the provider must not be called")

        result = prices.run_fetch(today=TODAY, client=_client(handler))
        assert result.skipped is not None and "no API key" in result.skipped


def _windows(windows: list[tuple[str, str]], *, listed: date | None = None) -> Handler:
    """A provider that records each request's window and answers a close for
    every day in it — from ``listed`` on, when given (a later listing)."""

    def handler(request: httpx.Request) -> httpx.Response:
        start = date.fromisoformat(request.url.params["startDate"])
        end = date.fromisoformat(request.url.params["endDate"])
        windows.append((start.isoformat(), end.isoformat()))
        days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
        return httpx.Response(
            200, text=_closes(*[(d, "10.5") for d in days if listed is None or d >= listed])
        )

    return handler


def _unpriced(symbol: str) -> list[tuple[date, Decimal]]:
    """Manual rows of ``symbol`` that hold shares but have no price."""
    with db.get_sessionmaker()() as session:
        rows = session.execute(
            select(HoldingsSnapshot.as_of, HoldingsSnapshot.quantity).where(
                HoldingsSnapshot.symbol == symbol,
                HoldingsSnapshot.source == "manual",
                HoldingsSnapshot.market_value.is_(None),
                HoldingsSnapshot.quantity > 0,
            )
        ).all()
    return [(r.as_of, Decimal(r.quantity)) for r in rows]


def _day(days_ago: int) -> str:
    return (TODAY - timedelta(days=days_ago)).isoformat()


class TestBackfill:
    """The provider is asked for every date a manual position needs, not only
    for what follows the newest stored close (decision 89)."""

    def test_a_backdate_after_the_first_fetch_is_backfilled(
        self, broker: str, admin_client: TestClient
    ) -> None:
        _tiingo(admin_client)
        symbol = _symbol()
        _hold(broker, symbol, "2", days_ago=4)
        windows: list[tuple[str, str]] = []
        prices.run_fetch(today=TODAY, client=_client(_windows(windows)))
        assert windows == [(_day(11), _day(0))]

        _hold(broker, symbol, "2", days_ago=20)  # backdated: the history grows earlier
        prices.run_fetch(today=TODAY, client=_client(_windows(windows)))
        assert windows[1:] == [(_day(27), _day(12))]  # the head gap, once
        assert _unpriced(symbol) == []
        prices.run_fetch(today=TODAY, client=_client(_windows(windows)))
        assert len(windows) == 2

    def test_a_second_account_holding_it_earlier_is_backfilled(
        self, broker: str, admin_client: TestClient
    ) -> None:
        _tiingo(admin_client)
        symbol = _symbol()
        _hold(broker, symbol, "2", days_ago=4)
        windows: list[tuple[str, str]] = []
        prices.run_fetch(today=TODAY, client=_client(_windows(windows)))

        other = admin_client.post(
            "/accounts", json={"name": "Second", "type": "brokerage", "path": "assets.second"}
        ).json()["id"]
        resp = admin_client.patch(f"/accounts/{other}", json={"holdings_source": "manual"})
        assert resp.status_code == 200, resp.text
        _hold(other, symbol, "3", days_ago=30)
        prices.run_fetch(today=TODAY, client=_client(_windows(windows)))
        assert windows[1:] == [(_day(37), _day(12))]
        assert _unpriced(symbol) == []

    def test_a_position_closed_before_the_first_fetch_is_priced(
        self, broker: str, admin_client: TestClient
    ) -> None:
        symbol = _symbol()
        _hold(broker, symbol, "5", days_ago=30)
        _hold(broker, symbol, "0", days_ago=10)  # sold out
        with db.get_sessionmaker()() as session:
            position = session.execute(
                select(Position).where(Position.symbol == symbol)
            ).scalar_one()
            service.update_position(session, position_id=position.id, close=True, token=None)
            session.commit()
        _tiingo(admin_client)
        windows: list[tuple[str, str]] = []
        prices.run_fetch(today=TODAY, client=_client(_windows(windows)))
        # Its history only: up to the last day it held shares.
        assert windows == [(_day(37), _day(11))]
        assert _unpriced(symbol) == []
        prices.run_fetch(today=TODAY, client=_client(_windows(windows)))
        assert len(windows) == 1

    def test_a_range_before_the_listing_is_asked_for_once(
        self, broker: str, admin_client: TestClient
    ) -> None:
        """The provider has nothing before the symbol listed: the attempt is
        remembered per (symbol, source), so that range is not asked for every
        day."""
        _tiingo(admin_client)
        symbol = _symbol()
        _hold(broker, symbol, "2", days_ago=6)
        windows: list[tuple[str, str]] = []
        listed = TODAY - timedelta(days=2)
        prices.run_fetch(today=TODAY, client=_client(_windows(windows, listed=listed)))
        assert windows == [(_day(13), _day(0))]
        assert min(d for d, _c, _s in _stored(symbol)) == listed
        prices.run_fetch(today=TODAY, client=_client(_windows(windows, listed=listed)))
        assert len(windows) == 1
        # A backdate still asks for the dates before the earlier answer.
        _hold(broker, symbol, "2", days_ago=9)
        prices.run_fetch(today=TODAY, client=_client(_windows(windows, listed=listed)))
        assert windows[1:] == [(_day(16), _day(14))]


class TestWindows:
    """Each window a symbol needs is asked and recorded on its own: a head
    window the provider refuses never starves the tail (today's close)."""

    def test_a_failing_head_window_never_starves_the_tail(
        self, broker: str, admin_client: TestClient
    ) -> None:
        _tiingo(admin_client)
        symbol = _symbol()
        _hold(broker, symbol, "2", days_ago=4)
        prices.run_fetch(today=TODAY, client=_client(_windows([])))
        _hold(broker, symbol, "2", days_ago=20)  # backdated: a head window opens
        asked: list[tuple[str, str]] = []
        oldest_stored = min(d for d, _c, _s in _stored(symbol))

        def refuse_the_head(request: httpx.Request) -> httpx.Response:
            start = date.fromisoformat(request.url.params["startDate"])
            if start < oldest_stored:
                asked.append((start.isoformat(), request.url.params["endDate"]))
                return httpx.Response(500, json={"detail": "upstream"})
            return _windows(asked)(request)

        tomorrow = TODAY + timedelta(days=1)
        result = prices.run_fetch(today=tomorrow, client=_client(refuse_the_head))
        assert asked[0] == (tomorrow.isoformat(), tomorrow.isoformat())  # the tail first
        assert len(asked) == 2  # and the head after it
        assert max(d for d, _c, _s in _stored(symbol)) == tomorrow  # the tail is stored
        assert result.failed == [{"symbol": symbol, "status": 500, "reason": "refused"}]  # once
        assert result.fetched == 1
        # The head is not recorded as asked: it is asked again the next day.
        again: list[tuple[str, str]] = []
        prices.run_fetch(today=tomorrow + timedelta(days=1), client=_client(_windows(again)))
        assert len(again) == 2


def test_a_store_error_on_a_final_not_found_fails_one_window(
    broker: str, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recording a settled not-found runs in the window's own try: a database
    error there fails that window, and the run, the other symbols and the
    audit row go on."""
    from sqlalchemy.exc import OperationalError

    _tiingo(admin_client)
    unknown, known = sorted((_symbol(), _symbol()))
    _hold(broker, unknown, "5", days_ago=30)
    _hold(broker, unknown, "0", days_ago=20)
    _hold(broker, known, "5", days_ago=4)
    record = prices._record_answered

    def failing(session, symbol, source, start, through):  # type: ignore[no-untyped-def]
        if symbol == unknown:
            raise OperationalError("INSERT INTO price_answered", {}, Exception("lock timeout"))
        return record(session, symbol, source, start, through)

    monkeypatch.setattr(prices, "_record_answered", failing)
    result = prices.run_fetch(today=TODAY, client=_client(Exchange(unknown=frozenset({unknown}))))
    assert {"symbol": unknown, "status": None, "reason": "store failed"} in result.failed
    assert result.fetched > 0 and _stored(known) != []
    with db.get_sessionmaker()() as session:
        audit = session.execute(
            select(AuditLog).where(AuditLog.action == "price.fetch")
        ).scalar_one()
    assert audit.before_after["after"]["failed"] == result.failed


class Exchange:
    """A provider shaped like a real exchange feed: a close for Monday to
    Friday only, and nothing for the run day or later (the job runs before
    the close is published). Unknown tickers answer 404."""

    def __init__(self, unknown: frozenset[str] = frozenset()) -> None:
        self.run_day = TODAY
        self.unknown = unknown
        self.asked: list[tuple[str, date, date]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        ticker = request.url.path.split("/")[3].upper()
        start = date.fromisoformat(request.url.params["startDate"])
        end = date.fromisoformat(request.url.params["endDate"])
        self.asked.append((ticker, start, end))
        if ticker in self.unknown:
            return httpx.Response(404, json={"detail": f"Error: Ticker '{ticker}' not found"})
        days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
        rows = [(d, self.close(d)) for d in days if d.weekday() < 5 and d < self.run_day]
        return httpx.Response(200, text=_closes(*rows))

    @staticmethod
    def close(day: date) -> str:
        return f"{100 + day.toordinal() % 50}.25"

    def run(self, day: date) -> prices.FetchResult:
        self.run_day = day
        return prices.run_fetch(today=day, client=_client(self))

    def requests(self, symbol: str) -> list[tuple[date, date]]:
        return [(s, e) for t, s, e in self.asked if t == symbol]


def _weekday_back(weekday: int, at_least: int) -> date:
    day = TODAY - timedelta(days=at_least)
    while day.weekday() != weekday:
        day -= timedelta(days=1)
    return day


def _ago_of(day: date) -> int:
    return (TODAY - day).days


def _close_position(symbol: str) -> None:
    with db.get_sessionmaker()() as session:
        position = session.execute(
            select(Position).where(Position.symbol == symbol, Position.closed_at.is_(None))
        ).scalar_one()
        service.update_position(session, position_id=position.id, close=True, token=None)
        session.commit()


class TestCoverage:
    """What the price job asks for, against a provider that answers trading
    days only (#390 fix round 2): every date a position holds shares gets
    its close in effect, and a range answered for good is not asked again."""

    def test_a_weekend_first_day_is_valued_at_the_friday_close(
        self, broker: str, admin_client: TestClient
    ) -> None:
        _tiingo(admin_client)
        saturday = _weekday_back(5, 14)
        symbol = _symbol()
        _hold(broker, symbol, "10", days_ago=_ago_of(saturday))
        Exchange().run(TODAY)
        with db.get_sessionmaker()() as session:
            row = session.execute(
                select(HoldingsSnapshot).where(
                    HoldingsSnapshot.symbol == symbol, HoldingsSnapshot.as_of == saturday
                )
            ).scalar_one()
        assert row.price_as_of == saturday - timedelta(days=1)  # Friday's close
        assert row.market_value is not None

    def test_a_symbol_sold_out_on_a_weekend_is_asked_once(
        self, broker: str, admin_client: TestClient
    ) -> None:
        """Its need ends on the last day it held shares (the Friday), so the
        weekend that has no close is never asked again and again."""
        _tiingo(admin_client)
        saturday = _weekday_back(5, 12)
        gone, zero = _symbol(), _symbol()
        for symbol in (gone, zero):
            _hold(broker, symbol, "5", days_ago=_ago_of(saturday) + 12)
            _hold(broker, symbol, "0", days_ago=_ago_of(saturday))
        _close_position(gone)  # zero stays open, holding nothing
        exchange = Exchange()
        day = saturday + timedelta(days=1)
        while day <= TODAY:
            exchange.run(day)
            day += timedelta(days=1)
        for symbol in (gone, zero):
            assert len(exchange.requests(symbol)) == 1, exchange.requests(symbol)

    def test_an_unknown_ticker_of_a_closed_position_is_asked_once(
        self, broker: str, admin_client: TestClient
    ) -> None:
        """A not-found for dates old enough is a final answer: one request,
        one price.fetch_failed, not one a day."""
        _tiingo(admin_client)
        unknown = _symbol()
        _hold(broker, unknown, "5", days_ago=30)
        _hold(broker, unknown, "0", days_ago=20)
        _close_position(unknown)
        exchange = Exchange(unknown=frozenset({unknown}))
        for back in range(5, -1, -1):
            exchange.run(TODAY - timedelta(days=back))
        assert len(exchange.requests(unknown)) == 1
        with db.get_sessionmaker()() as session:
            failures = [
                e
                for e in session.execute(
                    select(Event).where(Event.type == "price.fetch_failed")
                ).scalars()
                if e.payload["symbol"] == unknown
            ]
        assert len(failures) == 1

    def test_an_empty_answer_too_recent_is_asked_again(
        self, broker: str, admin_client: TestClient
    ) -> None:
        """Fetched on a Monday before its close is published, then sold on
        Tuesday: Monday's close is asked again on Tuesday and stored."""
        _tiingo(admin_client)
        monday = _weekday_back(0, 10)
        tuesday = monday + timedelta(days=1)
        symbol = _symbol()
        _hold(broker, symbol, "10", days_ago=_ago_of(monday))
        exchange = Exchange()
        exchange.run(monday)  # Monday's close is not published yet
        _hold(broker, symbol, "0", days_ago=_ago_of(tuesday))
        day = tuesday
        while day <= TODAY:
            exchange.run(day)
            day += timedelta(days=1)
        assert monday in {d for d, _c, _s in _stored(symbol)}
        with db.get_sessionmaker()() as session:
            row = session.execute(
                select(HoldingsSnapshot).where(
                    HoldingsSnapshot.symbol == symbol, HoldingsSnapshot.as_of == monday
                )
            ).scalar_one()
        assert row.price_as_of == monday

    def test_a_closed_account_is_priced_up_to_its_close(
        self, broker: str, admin_client: TestClient
    ) -> None:
        """A position still holding shares when its account closed needs
        closes up to the close date: after a provider outage, the gap is
        asked for though the account is closed by then."""
        _tiingo(admin_client)
        symbol = _symbol()
        _hold(broker, symbol, "5", days_ago=30)
        windows: list[tuple[str, str]] = []
        prices.run_fetch(today=TODAY - timedelta(days=20), client=_client(_windows(windows)))

        def down(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"detail": "down"})

        prices.run_fetch(today=TODAY - timedelta(days=15), client=_client(down))
        resp = admin_client.post(f"/accounts/{broker}/close", json={})
        assert resp.status_code == 200, resp.text
        recovery: list[tuple[str, str]] = []
        prices.run_fetch(today=TODAY, client=_client(_windows(recovery)))
        assert recovery == [(_day(19), _day(0))]


def _row_on(symbol: str, day: date) -> HoldingsSnapshot:
    with db.get_sessionmaker()() as session:
        return session.execute(
            select(HoldingsSnapshot).where(
                HoldingsSnapshot.symbol == symbol,
                HoldingsSnapshot.as_of == day,
                HoldingsSnapshot.source == "manual",
            )
        ).scalar_one()


def _island(broker: str, symbol: str, exchange: "Exchange") -> uuid.UUID:
    """A mis-dated history (held 300 to 292 days ago) priced and then
    reversed: its closes stay, an island far from anything held now.
    Returns the position."""
    _hold(broker, symbol, "10", days_ago=300)
    _hold(broker, symbol, "0", days_ago=291)
    exchange.run(TODAY)
    with db.get_sessionmaker()() as session:
        position = session.execute(select(Position).where(Position.symbol == symbol)).scalar_one()
        for event in reversed(service.list_events(session, position.id)):
            service.reverse_event(session, event_id=event.id, token=None)
        session.commit()
        return position.id


class TestAnsweredRanges:
    """Coverage is the set of ranges the provider answered, never the span
    between the oldest and the newest close (#390 round 3, finding 2)."""

    def test_a_backdate_next_to_an_island_of_old_closes_is_asked(
        self, broker: str, admin_client: TestClient
    ) -> None:
        _tiingo(admin_client)
        symbol = _symbol()
        exchange = Exchange()
        position_id = _island(broker, symbol, exchange)
        _hold(broker, symbol, "10", days_ago=10)  # the real count
        exchange.run(TODAY)
        assert exchange.requests(symbol)[-1] == (TODAY - timedelta(days=17), TODAY)
        with db.get_sessionmaker()() as session:
            service.record_event(
                session,
                position_id=position_id,
                kind="buy",
                occurred_on=TODAY - timedelta(days=100),
                quantity=Decimal("5"),
                token=None,
            )
            session.commit()
        exchange.run(TODAY)
        assert exchange.requests(symbol)[-1] == (
            TODAY - timedelta(days=107),
            TODAY - timedelta(days=18),
        )
        row = _row_on(symbol, TODAY - timedelta(days=100))
        assert row.price_as_of is not None
        assert (TODAY - timedelta(days=100) - row.price_as_of).days <= 4  # not the island's
        asked = len(exchange.requests(symbol))
        exchange.run(TODAY)
        # Answered for good: only the unsettled last days are asked again.
        assert all(
            start >= TODAY - timedelta(days=3) for start, _end in exchange.requests(symbol)[asked:]
        )

    def test_a_weekend_start_after_an_island_gets_the_friday_close(
        self, broker: str, admin_client: TestClient
    ) -> None:
        _tiingo(admin_client)
        symbol = _symbol()
        exchange = Exchange()
        _island(broker, symbol, exchange)
        sunday = _weekday_back(6, 14)
        _hold(broker, symbol, "10", days_ago=_ago_of(sunday))
        exchange.run(TODAY)
        assert _row_on(symbol, sunday).price_as_of == sunday - timedelta(days=2)

    def test_a_backdate_into_the_gap_after_an_account_left_manual(
        self, broker: str, admin_client: TestClient
    ) -> None:
        """V5b: an account priced as manual goes back to the feed, so its
        closes are an island; another account's backdate into the gap
        between them and today is asked for."""
        from purser.models import Account, Connection

        _tiingo(admin_client)
        symbol = _symbol()
        exchange = Exchange()
        _hold(broker, symbol, "4", days_ago=200)
        exchange.run(TODAY - timedelta(days=150))  # priced up to then
        with db.get_sessionmaker()() as session:
            connection = Connection(provider="simplefin")
            session.add(connection)
            session.flush()
            account = session.get(Account, uuid.UUID(broker))
            assert account is not None
            account.connection_id = connection.id
            account.external_id = "SF-BROKER"
            session.commit()
        resp = admin_client.patch(f"/accounts/{broker}", json={"holdings_source": "feed"})
        assert resp.status_code == 200, resp.text

        other = admin_client.post(
            "/accounts", json={"name": "Second", "type": "brokerage", "path": "assets.second"}
        ).json()["id"]
        resp = admin_client.patch(f"/accounts/{other}", json={"holdings_source": "manual"})
        assert resp.status_code == 200, resp.text
        _hold(other, symbol, "3", days_ago=10)
        exchange.run(TODAY)
        assert exchange.requests(symbol)[-1] == (TODAY - timedelta(days=17), TODAY)
        _hold(other, symbol, "3", days_ago=100)  # backdated into the gap
        exchange.run(TODAY)
        assert exchange.requests(symbol)[-1] == (
            TODAY - timedelta(days=107),
            TODAY - timedelta(days=18),
        )
        row = _row_on(symbol, TODAY - timedelta(days=100))
        assert row.price_as_of is not None
        assert (TODAY - timedelta(days=100) - row.price_as_of).days <= 4


class TestAbsurdCloses:
    """One absurd close fails no more than itself (#390 fix round 2)."""

    def test_one_absurd_close_costs_its_row_and_is_reported_with_a_reason(
        self, broker: str, admin_client: TestClient
    ) -> None:
        _tiingo(admin_client)
        symbol = _symbol()
        _hold(broker, symbol, "2", days_ago=3)
        prices.run_fetch(today=TODAY, client=_client(_windows([])))
        bad = TODAY + timedelta(days=1)

        def handler(request: httpx.Request) -> httpx.Response:
            start = date.fromisoformat(request.url.params["startDate"])
            end = date.fromisoformat(request.url.params["endDate"])
            days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
            return httpx.Response(
                200, text=_closes(*[(d, "1e22" if d == bad else "10.5") for d in days])
            )

        later = TODAY + timedelta(days=3)
        result = prices.run_fetch(today=later, client=_client(handler))
        assert max(d for d, _c, _s in _stored(symbol)) == later  # the rest is stored
        assert bad not in {d for d, _c, _s in _stored(symbol)}
        assert result.failed == [
            {"symbol": symbol, "status": 200, "reason": "1 close(s) out of range dropped"}
        ]
        # The window moved on: the next day asks only the new tail.
        again = prices.run_fetch(today=later + timedelta(days=1), client=_client(handler))
        assert again.failed == [] and again.fetched == 1
        status = admin_client.get("/prices/status").json()
        assert status["last_failure"]["reason"] == "1 close(s) out of range dropped"
        with db.get_sessionmaker()() as session:
            audit = (
                session.execute(
                    select(AuditLog).where(AuditLog.action == "price.fetch").order_by(AuditLog.at)
                )
                .scalars()
                .all()
            )
        assert audit[1].before_after["after"]["failed"] == result.failed

    def test_a_drop_is_reported_when_another_window_failed(
        self, broker: str, admin_client: TestClient
    ) -> None:
        """The tail is refused and the head's answer carries an absurd close:
        both are reported, in failed[] and as price.fetch_failed events."""
        _tiingo(admin_client)
        symbol = _symbol()
        _hold(broker, symbol, "2", days_ago=4)
        prices.run_fetch(today=TODAY, client=_client(_windows([])))
        _hold(broker, symbol, "2", days_ago=20)  # backdated: a head window opens
        bad = TODAY - timedelta(days=15)
        tomorrow = TODAY + timedelta(days=1)

        def handler(request: httpx.Request) -> httpx.Response:
            start = date.fromisoformat(request.url.params["startDate"])
            end = date.fromisoformat(request.url.params["endDate"])
            if end == tomorrow:
                return httpx.Response(500, json={"detail": "upstream"})
            days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
            return httpx.Response(
                200, text=_closes(*[(d, "1e22" if d == bad else "10.5") for d in days])
            )

        result = prices.run_fetch(today=tomorrow, client=_client(handler))
        assert result.failed == [
            {"symbol": symbol, "status": 500, "reason": "refused"},
            {"symbol": symbol, "status": 200, "reason": "1 close(s) out of range dropped"},
        ]
        with db.get_sessionmaker()() as session:
            reasons = sorted(
                e.payload["reason"]
                for e in session.execute(
                    select(Event).where(Event.type == "price.fetch_failed")
                ).scalars()
                if e.payload["symbol"] == symbol
            )
        assert reasons == ["1 close(s) out of range dropped", "refused"]

    def test_a_value_that_does_not_fit_is_flagged_not_a_wedged_account(
        self, broker: str, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A close stored under the price limit can still overflow an amount
        for one position: that row keeps its close, has no value and says
        value_out_of_range; it is logged once; the account projects, and a
        write on another symbol still works."""
        _tiingo(admin_client)
        big, other = sorted((_symbol(), _symbol()))
        _hold(broker, big, "1000", days_ago=3)
        _hold(broker, other, "10", days_ago=3)

        def handler(request: httpx.Request) -> httpx.Response:
            start = date.fromisoformat(request.url.params["startDate"])
            end = date.fromisoformat(request.url.params["endDate"])
            days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
            close = "10000000000000" if big.lower() in request.url.path else "10.5"
            return httpx.Response(
                200, text=_closes(*[(d, close if d == TODAY else "10.5") for d in days])
            )

        from purser.holdings import projection

        # Recorded at the logger itself: structlog caches bound loggers, so
        # a capture installed mid-suite would miss them.
        logged: list[dict] = []

        class Recorder:
            def warning(self, event: str, **fields: object) -> None:
                logged.append({"event": event, **fields})

        monkeypatch.setattr(projection, "log", Recorder())
        result = prices.run_fetch(today=TODAY, client=_client(handler))
        prices.run_fetch(today=TODAY, client=_client(handler))  # nothing changes
        assert result.accounts_failed == 0
        assert [e["event"] for e in logged] == ["holdings.value_out_of_range"]
        assert logged[0]["symbols"] == [big]
        rows = {p["symbol"]: p for p in admin_client.get(f"/accounts/{broker}/positions").json()}
        assert rows[big]["market_value"] is None and rows[big]["value_out_of_range"] is True
        assert Decimal(rows[big]["price"]) == Decimal("10000000000000")
        assert Decimal(rows[other]["market_value"]) == Decimal("105")
        resp = admin_client.post(
            f"/positions/{rows[other]['id']}/events",
            json={"kind": "set_quantity", "occurred_on": TODAY.isoformat(), "quantity": "11"},
        )
        assert resp.status_code == 201, resp.text

    def test_an_absurd_feed_implied_close_is_never_used(
        self, broker: str, admin_client: TestClient
    ) -> None:
        """A feed snapshot of dust shares and a large value implies a close no
        price can hold (1e15): it never enters valuation, so the hand-tracked
        position stays unpriced and the account still projects and writes."""
        symbol = _symbol()
        feed_account = admin_client.post(
            "/accounts", json={"name": "Feed", "type": "brokerage", "path": "assets.feedb"}
        ).json()["id"]
        with db.get_sessionmaker()() as session:
            session.add(
                HoldingsSnapshot(
                    account_id=uuid.UUID(feed_account),
                    as_of=TODAY - timedelta(days=1),
                    symbol=symbol,
                    quantity=Decimal("0.000001"),
                    market_value=Decimal("1000000000.0000"),
                    source="feed",
                )
            )
            session.commit()
        _hold(broker, symbol, "1", days_ago=5)
        result = prices.run_fetch(today=TODAY)  # no provider: the projection step only
        assert result.accounts_failed == 0
        listing = admin_client.get(f"/accounts/{broker}/positions")
        assert listing.status_code == 200, listing.text
        (row,) = [p for p in listing.json() if p["symbol"] == symbol]
        assert row["price"] is None and row["market_value"] is None
        resp = admin_client.post(
            f"/positions/{row['id']}/events",
            json={"kind": "set_quantity", "occurred_on": TODAY.isoformat(), "quantity": "100"},
        )
        assert resp.status_code == 201, resp.text


def test_closes_are_stored_in_one_statement_per_batch(migrated_engine: Engine) -> None:
    """A backfill of many closes is one multi-row INSERT, not one per close."""
    from sqlalchemy import event

    symbol = _symbol()
    closes = [(TODAY - timedelta(days=n), Decimal("10")) for n in range(30)]
    statements: list[str] = []

    def count(conn, cursor, statement, parameters, context, executemany) -> None:
        if statement.lstrip().upper().startswith("INSERT INTO SECURITY_PRICES"):
            statements.append(statement)

    engine = db.get_engine()
    event.listen(engine, "before_cursor_execute", count)
    try:
        with db.get_sessionmaker()() as session:
            inserted = prices._insert_closes(session, symbol, "tiingo", closes)
            again = prices._insert_closes(session, symbol, "tiingo", closes)
            session.rollback()
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert sorted(inserted) == sorted(d for d, _c in closes)
    assert again == []  # a stored close is kept, never inserted twice
    assert len(statements) == 2


def test_a_date_answered_twice_keeps_its_first_close(migrated_engine: Engine) -> None:
    """As with one INSERT per close: the first close given for a date is the
    one stored (a close does not change once published)."""
    symbol = _symbol()
    with db.get_sessionmaker()() as session:
        inserted = prices._insert_closes(
            session, symbol, "tiingo", [(TODAY, Decimal("10")), (TODAY, Decimal("11"))]
        )
        session.commit()
    assert inserted == [TODAY]
    assert _stored(symbol) == [(TODAY, Decimal("10"), "tiingo")]


class TestStaleness:
    def test_price_stale_fires_once_per_transition(
        self, broker: str, admin_client: TestClient
    ) -> None:
        symbol = _symbol()
        _hold(broker, symbol, "1", days_ago=20)
        with db.get_sessionmaker()() as session:
            prices.record_price(
                session,
                symbol=symbol,
                price_date=TODAY - timedelta(days=10),
                close=Decimal("5"),
                token=None,
            )
            session.commit()

        def stale_events() -> list[str | None]:
            with db.get_sessionmaker()() as session:
                return [
                    e.payload["price_as_of"]
                    for e in session.execute(
                        select(Event)
                        .where(Event.type == "price.stale")
                        .order_by(Event.at, Event.id)
                    ).scalars()
                    if e.payload["symbol"] == symbol
                ]

        prices.run_fetch(today=TODAY)
        prices.run_fetch(today=TODAY)  # still stale on the same close: no repeat
        assert stale_events() == [(TODAY - timedelta(days=10)).isoformat()]

        # A fresher close ends the episode; when it too goes stale, that is a
        # new transition and alerts again.
        with db.get_sessionmaker()() as session:
            prices.record_price(
                session,
                symbol=symbol,
                price_date=TODAY - timedelta(days=2),
                close=Decimal("6"),
                token=None,
            )
            session.commit()
        prices.run_fetch(today=TODAY)
        assert len(stale_events()) == 1
        prices.run_fetch(today=TODAY + timedelta(days=6))
        assert stale_events() == [
            (TODAY - timedelta(days=10)).isoformat(),
            (TODAY - timedelta(days=2)).isoformat(),
        ]

        with db.get_sessionmaker()() as session:
            digest = whats_new(session)
        lines = [line for line in digest["stale_prices"] if line["symbol"] == symbol]
        assert lines and lines[0]["price_as_of"] == (TODAY - timedelta(days=10)).isoformat()

    def test_a_position_sold_down_to_zero_is_not_open(
        self, broker: str, admin_client: TestClient
    ) -> None:
        """Sold to zero but not closed yet (Set quantity 0 in the UI): the
        symbol is no longer held, so it is neither fetched daily, nor stale,
        nor listed in the status."""
        symbol = _symbol()
        with db.get_sessionmaker()() as session:
            recorded = service.record_position(
                session,
                account_ref=broker,
                symbol=symbol,
                quantity=Decimal("5"),
                as_of=TODAY - timedelta(days=20),
                token=None,
            )
            prices.record_price(
                session,
                symbol=symbol,
                price_date=TODAY - timedelta(days=15),
                close=Decimal("10"),
                token=None,
            )
            service.record_event(
                session,
                position_id=recorded.position.id,
                kind="set_quantity",
                occurred_on=TODAY - timedelta(days=10),
                quantity=Decimal("0"),
                token=None,
            )
            session.commit()
            assert symbol not in prices.open_symbols(session)
        result = prices.run_fetch(today=TODAY)
        assert symbol not in {s["symbol"] for s in result.stale}
        status = admin_client.get("/prices/status").json()
        assert symbol not in {s["symbol"] for s in status["symbols"]}

    def test_threshold_is_configurable(self, broker: str, admin_client: TestClient) -> None:
        assert admin_client.put("/config", json={"price_stale_days": 30}).status_code == 200
        appsettings.invalidate()
        symbol = _symbol()
        _hold(broker, symbol, "1", days_ago=20)
        with db.get_sessionmaker()() as session:
            prices.record_price(
                session,
                symbol=symbol,
                price_date=TODAY - timedelta(days=10),
                close=Decimal("5"),
                token=None,
            )
            session.commit()
        result = prices.run_fetch(today=TODAY)
        assert symbol not in {s["symbol"] for s in result.stale}


def test_a_new_provider_widens_both_source_checks(migrated_engine: Engine) -> None:
    """A provider stores its closes in security_prices AND its answered ranges
    in price_answered, in one savepoint: both CHECKs list the same
    providers, and the providers module says to widen both."""
    from purser.holdings import providers

    with migrated_engine.connect() as conn:
        checks = dict(
            conn.execute(
                text(
                    "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint"
                    " WHERE conname IN ('security_prices_source_values',"
                    " 'price_answered_source_values')"
                )
            ).all()
        )
    assert "'tiingo'" in checks["security_prices_source_values"]
    assert "'tiingo'" in checks["price_answered_source_values"]
    doc = providers.__doc__ or ""
    assert "security_prices.source" in doc and "price_answered.source" in doc
