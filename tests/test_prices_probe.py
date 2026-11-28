"""Testing a price provider key (#682): POST /prices/test and
``prices.probe_provider`` ask for a few recent closes of the provider's probe
symbol, store nothing, and report only a status and a fixed reason.

Every provider call goes through an httpx MockTransport; the key is fake.
"""

from datetime import date, timedelta

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from purser import db
from purser.holdings import prices
from purser.models import AuditLog, SecurityPrice
from tests.conftest import today
from tests.test_prices import FAKE_KEY, _client, _closes, _tiingo

OTHER_KEY = "other-test-key-not-real-fedcba9876"


def _prices_stored() -> int:
    with db.get_sessionmaker()() as session:
        return session.execute(select(func.count()).select_from(SecurityPrice)).scalar_one()


def _ok(seen: list[httpx.Request]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        start = date_param(request, "startDate")
        return httpx.Response(200, text=_closes((start, "500.25")))

    return _client(handler)


def date_param(request: httpx.Request, name: str) -> date:
    return date.fromisoformat(request.url.params[name])


class TestProbe:
    def test_a_good_key_answers_ok_and_stores_nothing(self, admin_client: TestClient) -> None:
        seen: list[httpx.Request] = []
        before = _prices_stored()
        result = prices.probe_provider("tiingo", OTHER_KEY, client=_ok(seen))
        assert result.ok and result.status is None and result.reason is None
        assert len(seen) == 1
        assert seen[0].headers["Authorization"] == f"Token {OTHER_KEY}"
        assert "/spy/" in seen[0].url.path
        assert date_param(seen[0], "startDate") == today() - timedelta(days=prices.PROBE_DAYS)
        assert _prices_stored() == before

    def test_the_stored_key_is_used_when_none_is_given(self, admin_client: TestClient) -> None:
        _tiingo(admin_client)
        seen: list[httpx.Request] = []
        assert prices.probe_provider(client=_ok(seen)).ok
        assert seen[0].headers["Authorization"] == f"Token {FAKE_KEY}"

    def test_a_refused_key_reports_the_status_only(self, admin_client: TestClient) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text=f'{{"detail": "Invalid token {OTHER_KEY}"}}')

        result = prices.probe_provider("tiingo", OTHER_KEY, client=_client(handler))
        assert (result.ok, result.status, result.reason) == (False, 401, "refused")

    def test_an_empty_answer_is_not_ok(self, admin_client: TestClient) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=_closes())

        result = prices.probe_provider("tiingo", OTHER_KEY, client=_client(handler))
        assert (result.ok, result.reason) == (False, "no closes returned")

    def test_no_provider_or_no_key_is_said_plainly(self, admin_client: TestClient) -> None:
        assert prices.probe_provider().reason == "no price provider is selected"
        assert prices.probe_provider("tiingo").reason == "no API key is set"


class TestEndpoint:
    def test_admin_only(self, admin_client: TestClient) -> None:
        minted = admin_client.post(
            "/tokens", json={"name": "member", "scopes": ["read", "write"]}
        ).json()
        headers = {"Authorization": f"Bearer {minted['token']}"}
        assert admin_client.post("/prices/test", json={}, headers=headers).status_code == 403

    def test_answers_without_a_provider_and_writes_nothing(self, admin_client: TestClient) -> None:
        with db.get_sessionmaker()() as session:
            audits = session.execute(
                select(func.count()).select_from(AuditLog).where(AuditLog.kind == "change")
            ).scalar_one()
        resp = admin_client.post("/prices/test", json={})
        assert resp.status_code == 200
        assert resp.json() == {
            "ok": False,
            "provider": "none",
            "status": None,
            "reason": "no price provider is selected",
        }
        with db.get_sessionmaker()() as session:
            assert session.execute(
                select(func.count()).select_from(AuditLog).where(AuditLog.kind == "change")
            ).scalar_one() == (audits)

    def test_a_key_that_cannot_travel_in_a_header_is_refused_unechoed(
        self, admin_client: TestClient
    ) -> None:
        bad = "spaced key-not-real"
        resp = admin_client.post("/prices/test", json={"provider": "tiingo", "api_key": bad})
        assert resp.status_code == 422
        assert bad not in resp.text
