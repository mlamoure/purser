"""The MCP Host/Origin guard (DNS-rebinding protection, purser/mcp/mount.py).

The allowlist admits the instance's public host — PURSER_PUBLIC_URL, which is
what a reverse proxy forwards — plus the local dev hosts. FastMCP answers 421
for any other Host and 403 for an Origin outside the list.

Two facts shape the integration tests:
- FastMCP also admits the server's own host, and TestClient's default Host
  is that host (``testserver``), so every request here names its Host.
- Bearer authentication runs before the guard, so a request without a valid
  token is a 401 whatever its Host.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

import purser.config
from purser.mcp.mount import _host_origin_allowlist

PUBLIC_URL = "https://purser.example.com"
PUBLIC_HOST = "purser.example.com"
FOREIGN_HOST = "rebind.example.net"

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "allowlist-test", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


@contextmanager
def _public_url(url: str) -> Iterator[None]:
    """PURSER_PUBLIC_URL set and the cached Settings dropped on both sides, so
    neither this value nor a stale one leaks into neighbouring tests."""
    previous = os.environ.get("PURSER_PUBLIC_URL")
    os.environ["PURSER_PUBLIC_URL"] = url
    purser.config.get_settings.cache_clear()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("PURSER_PUBLIC_URL", None)
        else:
            os.environ["PURSER_PUBLIC_URL"] = previous
        purser.config.get_settings.cache_clear()


def test_allowlist_admits_the_public_host_and_local_dev_hosts() -> None:
    with _public_url(PUBLIC_URL):
        hosts, origins = _host_origin_allowlist()
    assert {PUBLIC_HOST, f"{PUBLIC_HOST}:*", "localhost", "localhost:*"} <= set(hosts)
    assert PUBLIC_URL in origins
    assert "*" not in hosts
    assert "*" not in origins


@pytest.fixture
def read_token(admin_client: TestClient) -> str:
    """Minted on the default app: an https public URL makes the session cookie
    Secure, which TestClient's plain-http requests would never send back."""
    resp = admin_client.post("/tokens", json={"name": "allowlist-reader", "scopes": ["read"]})
    assert resp.status_code == 201, resp.text
    return resp.json()["token"]


@pytest.fixture
def mcp_client(read_token: str) -> Iterator[TestClient]:
    """An app built while PURSER_PUBLIC_URL is set: the MCP wrapper reads the
    allowlist once, at create_app()."""
    from purser.api.app import create_app

    with _public_url(PUBLIC_URL), TestClient(create_app()) as c:
        yield c


def _initialize(client: TestClient, *, host: str, token: str | None, origin: str | None = None):
    headers = {**MCP_HEADERS, "Host": host}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if origin is not None:
        headers["Origin"] = origin
    return client.post("/mcp/", json=INITIALIZE, headers=headers)


class TestHostOriginGuard:
    def test_public_host_needs_the_public_url(
        self, admin_client: TestClient, read_token: str
    ) -> None:
        # Control: the default app (PURSER_PUBLIC_URL unset) refuses the same
        # request, so the 200 below is the allowlist's doing.
        resp = _initialize(admin_client, host=PUBLIC_HOST, token=read_token)
        assert resp.status_code == 421

    def test_foreign_host_is_misdirected(self, mcp_client: TestClient, read_token: str) -> None:
        resp = _initialize(mcp_client, host=FOREIGN_HOST, token=read_token)
        assert resp.status_code == 421

    def test_public_host_is_served(self, mcp_client: TestClient, read_token: str) -> None:
        resp = _initialize(mcp_client, host=PUBLIC_HOST, token=read_token)
        assert resp.status_code == 200, resp.text
        assert '"serverInfo"' in resp.text

    def test_public_host_with_public_origin_is_served(
        self, mcp_client: TestClient, read_token: str
    ) -> None:
        resp = _initialize(mcp_client, host=PUBLIC_HOST, token=read_token, origin=PUBLIC_URL)
        assert resp.status_code == 200, resp.text

    def test_public_host_with_foreign_origin_is_forbidden(
        self, mcp_client: TestClient, read_token: str
    ) -> None:
        resp = _initialize(
            mcp_client, host=PUBLIC_HOST, token=read_token, origin=f"https://{FOREIGN_HOST}"
        )
        assert resp.status_code == 403

    def test_missing_bearer_is_unauthorized_before_the_host_check(
        self, mcp_client: TestClient
    ) -> None:
        resp = _initialize(mcp_client, host=FOREIGN_HOST, token=None)
        assert resp.status_code == 401
        assert resp.json() == {"error": "invalid or missing bearer token"}
