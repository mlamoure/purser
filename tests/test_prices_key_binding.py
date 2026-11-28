"""A stored price key goes only to the provider it was entered for (#698).

The two key slots are roles (the provider, the fallback). Changing or
swapping providers, through Settings, PUT/DELETE /config or the environment,
must never send one provider another's key. Every provider call goes through
an httpx MockTransport (routed by host); keys are synthetic.
"""

from datetime import date

import httpx
from fastapi.testclient import TestClient

from purser import appsettings, db
from purser.holdings import prices
from tests.test_prices import FAKE_KEY, _client, _closes
from tests.test_prices_providers import EOD_KEY, _configure, _eod_rows


def _answering(seen: list[httpx.Request]) -> httpx.Client:
    """Both providers answer one close, on the first day asked for."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "eodhd.com":
            start = date.fromisoformat(request.url.params["from"])
            return httpx.Response(200, text=_eod_rows((start, "10.00")))
        start = date.fromisoformat(request.url.params["startDate"])
        return httpx.Response(200, text=_closes((start, "10.00")))

    return _client(handler)


def _keys_sent(seen: list[httpx.Request]) -> set[tuple[str, str]]:
    return {(r.url.host, r.headers.get("Authorization", "")) for r in seen}


class TestKeyBinding:
    def test_switching_the_provider_does_not_send_the_old_key(
        self, admin_client: TestClient
    ) -> None:
        _configure(admin_client, primary="tiingo", fallback=None)
        resp = admin_client.put("/config", json={"price_provider": "eodhd"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["price_api_key_set"] is False
        assert prices.stored_key("eodhd") is None
        seen: list[httpx.Request] = []
        result = prices.probe_provider(client=_answering(seen))
        assert (result.ok, result.reason) == (False, "no API key is set")
        assert seen == []

    def test_a_swap_keeps_each_key_with_its_provider(self, admin_client: TestClient) -> None:
        _configure(admin_client)  # tiingo with FAKE_KEY, eodhd fallback with EOD_KEY
        resp = admin_client.put(
            "/config", json={"price_provider": "eodhd", "price_fallback_provider": "tiingo"}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["price_api_key_set"] is True and body["price_fallback_api_key_set"] is True
        seen: list[httpx.Request] = []
        assert prices.probe_provider("eodhd", client=_answering(seen)).ok
        assert prices.probe_provider("tiingo", client=_answering(seen)).ok
        assert _keys_sent(seen) == {
            ("eodhd.com", f"Bearer {EOD_KEY}"),
            ("api.tiingo.com", f"Token {FAKE_KEY}"),
        }

    def test_resetting_the_provider_does_not_send_the_key_elsewhere(
        self, admin_client: TestClient
    ) -> None:
        _configure(admin_client, primary="eodhd", fallback=None)  # FAKE_KEY entered for eodhd
        resp = admin_client.delete("/config/price_provider")  # back to the env default
        assert resp.status_code == 200, resp.text
        assert prices.stored_key("tiingo") is None
        assert prices.stored_key("eodhd") == FAKE_KEY

    def test_a_key_stored_before_the_binding_is_tiingos(self, admin_client: TestClient) -> None:
        # As an upgraded instance has it: the key rows, no binding rows.
        with db.get_sessionmaker()() as session:
            appsettings.set_values(
                session,
                {
                    "price_provider": "eodhd",
                    appsettings.PRICE_API_KEY: FAKE_KEY,
                    appsettings.PRICE_FALLBACK_API_KEY: EOD_KEY,
                },
                actor_token_id=None,
            )
            session.commit()
        appsettings.invalidate()
        assert prices.stored_key("eodhd") is None
        assert prices.stored_key("tiingo") == FAKE_KEY
        assert admin_client.get("/config").json()["price_api_key_set"] is False

    def test_a_key_needs_its_provider_chosen(self, admin_client: TestClient) -> None:
        resp = admin_client.put("/config", json={"price_api_key": FAKE_KEY})
        assert resp.status_code == 422
        assert "choose the price provider" in resp.json()["detail"]
        resp = admin_client.put(
            "/config", json={"price_provider": "tiingo", "price_fallback_api_key": EOD_KEY}
        )
        assert resp.status_code == 422
        assert "choose the fallback price provider" in resp.json()["detail"]
        assert FAKE_KEY not in resp.text and EOD_KEY not in resp.text

    def test_the_binding_is_not_a_config_key(self, admin_client: TestClient) -> None:
        _configure(admin_client, primary="tiingo", fallback=None)
        body = admin_client.get("/config").json()
        assert not set(body["overridden"]) & set(appsettings.PRICE_KEY_BINDINGS.values())
        assert admin_client.delete("/config/price_api_key_provider").status_code == 404
        # Removing the key removes what it was bound to.
        assert admin_client.delete("/config/price_api_key").status_code == 200
        assert "price_api_key_provider" not in appsettings.overrides()
        assert prices.stored_key("tiingo") is None
