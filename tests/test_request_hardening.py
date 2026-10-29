"""Request-level hardening from the #429 security review.

- A request body over PURSER_MAX_REQUEST_BYTES is refused with 413 before
  it is buffered: FastAPI reads a body before it resolves authentication,
  so an unauthenticated client could otherwise make the process hold any
  amount of memory before its 401.
- A session cookie is ambient, so a write a page on another site (or a
  sibling subdomain) triggers is refused; reads, bearer tokens and
  non-browser clients are unaffected.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from tests.conftest import ADMIN_CREDENTIALS


@pytest.fixture
def small_limit_client(
    clean_db: None, migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    from purser.api.app import create_app
    from purser.config import get_settings

    monkeypatch.setenv("PURSER_MAX_REQUEST_BYTES", "1024")
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as c:
            yield c
    finally:
        monkeypatch.delenv("PURSER_MAX_REQUEST_BYTES")
        get_settings.cache_clear()


class TestBodyLimit:
    def test_a_declared_oversized_body_is_refused_before_auth(
        self, small_limit_client: TestClient
    ) -> None:
        resp = small_limit_client.post(
            "/accounts", content=b'{"name": "' + b"A" * 4096 + b'"}',
            headers={"Content-Type": "application/json"},
        )  # fmt: skip
        assert resp.status_code == 413
        assert "1024 bytes" in resp.json()["detail"]

    def test_a_streamed_oversized_body_is_refused(self, small_limit_client: TestClient) -> None:
        def chunks() -> Iterator[bytes]:
            yield b'{"name": "'
            for _ in range(8):
                yield b"A" * 512
            yield b'"}'

        resp = small_limit_client.post(
            "/accounts", content=chunks(), headers={"Content-Type": "application/json"}
        )
        assert resp.status_code == 413

    def test_a_body_inside_the_limit_reaches_the_route(
        self, small_limit_client: TestClient
    ) -> None:
        resp = small_limit_client.post("/auth/setup", json=ADMIN_CREDENTIALS)
        assert resp.status_code == 201
        resp = small_limit_client.post(
            "/accounts", json={"name": "Small", "type": "checking", "path": "assets.small"}
        )
        assert resp.status_code == 201, resp.text


class TestCrossSiteSessionWrites:
    @pytest.mark.parametrize(
        "headers",
        [
            {"Sec-Fetch-Site": "cross-site"},
            {"Sec-Fetch-Site": "same-site"},  # a sibling subdomain
            {"Origin": "https://evil.example.com"},
        ],
    )
    def test_a_cookie_write_from_another_page_is_refused(
        self, admin_client: TestClient, headers: dict[str, str]
    ) -> None:
        resp = admin_client.post("/matching/run", headers=headers)
        assert resp.status_code == 403
        assert "cross-site" in resp.json()["detail"]

    @pytest.mark.parametrize(
        "headers",
        [
            {},  # not a browser page
            {"Sec-Fetch-Site": "same-origin"},
            {"Sec-Fetch-Site": "none"},
            {"Origin": "http://testserver"},
        ],
    )
    def test_a_cookie_write_from_purser_itself_passes(
        self, admin_client: TestClient, headers: dict[str, str]
    ) -> None:
        resp = admin_client.post("/matching/run", headers=headers)
        assert resp.status_code == 200, resp.text

    def test_reads_and_bearer_tokens_are_not_affected(self, admin_client: TestClient) -> None:
        cross = {"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example.com"}
        assert admin_client.get("/accounts", headers=cross).status_code == 200
        token = admin_client.post(
            "/tokens", json={"name": "agent", "scopes": ["read", "write"]}
        ).json()["token"]
        admin_client.cookies.clear()
        resp = admin_client.post(
            "/matching/run", headers={**cross, "Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200, resp.text
