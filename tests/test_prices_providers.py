"""A second provider, a fallback, and per-symbol price settings (#686,
decision 139): EODHD, the fallback asked for what the provider cannot
answer, ticker aliases and symbols priced by hand only.

Every provider call goes through an httpx MockTransport (routed by host);
keys, tickers and closes are synthetic.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from purser import db
from purser.holdings import prices
from purser.holdings.providers import EodhdProvider, PriceFetchError
from purser.models import AuditLog
from tests.conftest import today
from tests.test_prices import FAKE_KEY, _client, _closes, _hold, _stored, broker  # noqa: F401

EOD_KEY = "eodhd-test-key-not-real-0123456789"


def _sym() -> str:
    return f"E{uuid.uuid4().hex[:6].upper()}"


def _eod_rows(*rows: tuple[date, str]) -> str:
    """An EODHD-shaped body: numbers as JSON numbers, a separate
    adjusted_close the parser must ignore."""
    body = ",".join(
        f'{{"date": "{d.isoformat()}", "open": 1.0, "close": {c}, "adjusted_close": 1.25,'
        f' "volume": 0}}'
        for d, c in rows
    )
    return f"[{body}]"


def _configure(
    admin_client: TestClient, primary: str = "tiingo", fallback: str | None = "eodhd"
) -> None:
    body: dict[str, str] = {"price_provider": primary, "price_api_key": FAKE_KEY}
    if fallback is not None:
        body |= {"price_fallback_provider": fallback, "price_fallback_api_key": EOD_KEY}
    resp = admin_client.put("/config", json=body)
    assert resp.status_code == 200, resp.text
    assert FAKE_KEY not in resp.text and EOD_KEY not in resp.text


class Routed:
    """A MockTransport handler routing by host, recording each request."""

    def __init__(self, tiingo: int | str, eodhd: int | str) -> None:
        self.answers = {"api.tiingo.com": tiingo, "eodhd.com": eodhd}
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        answer = self.answers[request.url.host]
        if isinstance(answer, int):
            return httpx.Response(answer, text='{"detail": "nope"}')
        if request.url.host == "eodhd.com":
            start = date.fromisoformat(request.url.params["from"])
            end = date.fromisoformat(request.url.params["to"])
        else:
            start = date.fromisoformat(request.url.params["startDate"])
            end = date.fromisoformat(request.url.params["endDate"])
        days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
        rows = [(d, answer) for d in days]
        return httpx.Response(
            200, text=_eod_rows(*rows) if request.url.host == "eodhd.com" else _closes(*rows)
        )

    def hosts(self) -> list[str]:
        return [r.url.host for r in self.seen]


class TestEodhdProvider:
    def test_ticker_spelling(self) -> None:
        assert EodhdProvider.ticker("AAPL") == "AAPL.US"
        assert EodhdProvider.ticker("BRK.B") == "BRK-B.US"
        assert EodhdProvider.ticker("VOD.LSE") == "VOD.LSE"
        assert EodhdProvider.ticker("shop.to") == "SHOP.TO"

    def test_reads_the_unadjusted_close_with_the_key_in_the_header(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, text=_eod_rows((today(), "199.99"), (today(), "1e30")))

        closes = EodhdProvider(EOD_KEY, client=_client(handler)).closes(
            "BRK.B", today() - timedelta(days=3), today()
        )
        assert list(closes) == [(today(), Decimal("199.990000"))]
        assert closes.dropped == 1
        assert seen[0].url.path == "/api/eod/BRK-B.US"
        assert seen[0].url.params["fmt"] == "json"
        assert seen[0].headers["Authorization"] == f"Bearer {EOD_KEY}"
        assert EOD_KEY not in str(seen[0].url)

    @pytest.mark.parametrize(
        ("status", "reason"),
        [(401, "refused"), (403, "refused"), (404, "not found"), (402, "daily limit reached")],
    )
    def test_failures_name_the_symbol_status_and_reason(self, status: int, reason: str) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, text=f"Unauthenticated {EOD_KEY}")

        with pytest.raises(PriceFetchError) as raised:
            EodhdProvider(EOD_KEY, client=_client(handler)).closes("AAPL", today(), today())
        assert (raised.value.symbol, raised.value.status, raised.value.reason) == (
            "AAPL",
            status,
            reason,
        )
        assert EOD_KEY not in str(raised.value)

    def test_an_alias_is_sent_as_given(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, text="[]")

        EodhdProvider(EOD_KEY, client=_client(handler)).closes(
            "VOD", today(), today(), ticker="VOD.LSE"
        )
        assert seen[0].url.path == "/api/eod/VOD.LSE"


class TestConfig:
    def test_the_fallback_must_differ_and_its_key_is_write_only(
        self, admin_client: TestClient
    ) -> None:
        _configure(admin_client)
        body = admin_client.get("/config").json()
        assert body["values"]["price_fallback_provider"] == "eodhd"
        assert body["price_fallback_api_key_set"] is True
        resp = admin_client.put("/config", json={"price_fallback_provider": "tiingo"})
        assert resp.status_code == 422
        resp = admin_client.put("/config", json={"price_fallback_api_key": "has space"})
        assert resp.status_code == 422 and "has space" not in resp.text


class TestFallback:
    def test_a_window_the_provider_does_not_know_is_asked_of_the_fallback(
        self,
        admin_client: TestClient,
        broker: str,  # noqa: F811  (the fixture imported above)
    ) -> None:
        _configure(admin_client)
        symbol = _sym()
        _hold(broker, symbol, "2", days_ago=10)
        routed = Routed(tiingo=404, eodhd="42.5")

        result = prices.run_fetch(today=today(), client=_client(routed))
        assert result.failed == [] and result.fallback == "eodhd"
        assert {source for _d, _c, source in _stored(symbol)} == {"eodhd"}
        assert routed.hosts() == ["api.tiingo.com", "eodhd.com"]

        # Answered by the fallback: the next run asks neither for those dates.
        again = Routed(tiingo=404, eodhd="42.5")
        prices.run_fetch(today=today(), client=_client(again))
        assert again.seen == []

    def test_a_refused_provider_key_is_still_reported(
        self,
        admin_client: TestClient,
        broker: str,  # noqa: F811
    ) -> None:
        _configure(admin_client)
        symbol = _sym()
        _hold(broker, symbol, "1", days_ago=5)
        result = prices.run_fetch(today=today(), client=_client(Routed(tiingo=401, eodhd="9")))
        assert {source for _d, _c, source in _stored(symbol)} == {"eodhd"}
        assert result.failed == [{"symbol": symbol, "status": 401, "reason": "refused"}]

    def test_unknown_to_both_is_one_not_found(
        self,
        admin_client: TestClient,
        broker: str,  # noqa: F811
    ) -> None:
        _configure(admin_client)
        symbol = _sym()
        _hold(broker, symbol, "1", days_ago=10)
        result = prices.run_fetch(today=today(), client=_client(Routed(tiingo=404, eodhd=404)))
        assert result.failed == [{"symbol": symbol, "status": 404, "reason": "not found"}]

    def test_a_fallback_without_a_key_is_skipped_and_said(
        self,
        admin_client: TestClient,
        broker: str,  # noqa: F811
    ) -> None:
        admin_client.put(
            "/config",
            json={
                "price_provider": "tiingo",
                "price_api_key": FAKE_KEY,
                "price_fallback_provider": "eodhd",
            },
        )
        symbol = _sym()
        _hold(broker, symbol, "1", days_ago=5)
        routed = Routed(tiingo="3", eodhd="4")
        result = prices.run_fetch(today=today(), client=_client(routed))
        assert (
            result.fallback_skipped
            == "the eodhd fallback provider has no API key (Settings, Market data)"
        )
        assert set(routed.hosts()) == {"api.tiingo.com"}


class TestSymbolSettings:
    def test_an_alias_is_asked_and_a_change_asks_again(
        self,
        admin_client: TestClient,
        broker: str,  # noqa: F811
    ) -> None:
        _configure(admin_client, primary="eodhd", fallback=None)
        admin_client.put("/config", json={"price_api_key": EOD_KEY})
        symbol = _sym()
        _hold(broker, symbol, "1", days_ago=10)
        routed = Routed(tiingo=500, eodhd=404)
        prices.run_fetch(today=today(), client=_client(routed))
        assert routed.seen[0].url.path == f"/api/eod/{symbol}.US"

        resp = admin_client.put(f"/prices/symbols/{symbol}", json={"aliases": {"eodhd": "ZZZ.LSE"}})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "symbol": symbol,
            "skip_providers": False,
            "aliases": {"eodhd": "ZZZ.LSE"},
        }
        fixed = Routed(tiingo=500, eodhd="7.25")
        prices.run_fetch(today=today(), client=_client(fixed))
        assert fixed.seen and all(r.url.path == "/api/eod/ZZZ.LSE" for r in fixed.seen)
        assert {source for _d, _c, source in _stored(symbol)} == {"eodhd"}
        assert admin_client.get("/prices/symbols").json() == [resp.json()]

    def test_a_symbol_that_skips_the_providers_is_never_sent(
        self,
        admin_client: TestClient,
        broker: str,  # noqa: F811
    ) -> None:
        _configure(admin_client)
        symbol = _sym()
        _hold(broker, symbol, "1", days_ago=5)
        assert (
            admin_client.put(f"/prices/symbols/{symbol}", json={"skip_providers": True}).status_code
            == 200
        )
        routed = Routed(tiingo="1", eodhd="1")
        result = prices.run_fetch(today=today(), client=_client(routed))
        assert routed.seen == [] and result.symbols == 0

        assert admin_client.delete(f"/prices/symbols/{symbol}").status_code == 200
        assert admin_client.get("/prices/symbols").json() == []
        prices.run_fetch(today=today(), client=_client(routed))
        assert routed.seen  # asked again once the setting is gone

    def test_settings_are_audited_and_validated(self, admin_client: TestClient) -> None:
        symbol = _sym()
        bad = admin_client.put(
            f"/prices/symbols/{symbol}", json={"aliases": {"eodhd": "has space"}}
        )
        assert bad.status_code == 422
        assert (
            admin_client.put(
                f"/prices/symbols/{symbol}", json={"aliases": {"other": "X"}}
            ).status_code
            == 422
        )
        assert (
            admin_client.put(f"/prices/symbols/{symbol}", json={"skip_providers": True}).status_code
            == 200
        )
        with db.get_sessionmaker()() as session:
            rows = list(
                session.execute(
                    select(AuditLog).where(AuditLog.action == "price.symbol_settings")
                ).scalars()
            )
        assert rows and rows[-1].before_after["after"]["skip_providers"] is True

    def test_read_scope_cannot_change_settings(self, admin_client: TestClient) -> None:
        minted = admin_client.post("/tokens", json={"name": "reader", "scopes": ["read"]}).json()
        headers = {"Authorization": f"Bearer {minted['token']}"}
        assert admin_client.get("/prices/symbols", headers=headers).status_code == 200
        resp = admin_client.put(
            f"/prices/symbols/{_sym()}", json={"skip_providers": True}, headers=headers
        )
        assert resp.status_code == 403


def test_both_source_checks_accept_eodhd(migrated_engine: object) -> None:
    with db.get_sessionmaker()() as session:
        defs = dict(
            session.execute(
                text(
                    "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint"
                    " WHERE conname IN ('security_prices_source_values',"
                    " 'price_answered_source_values')"
                )
            ).all()
        )
    assert all("'eodhd'" in d for d in defs.values()) and len(defs) == 2
