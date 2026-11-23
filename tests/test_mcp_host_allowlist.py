"""The MCP Host/Origin guard (DNS-rebinding protection, purser/mcp/mount.py).

The allowlist admits the instance's public host — PURSER_PUBLIC_URL, which is
what a reverse proxy forwards — the exact names in PURSER_ALLOWED_HOSTS (#633)
and the local dev hosts. FastMCP answers 421 for any other Host and 403 for an
Origin outside the list.

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


# A home server (decision 117): its address is the public URL, and its other
# names are listed (#633). RFC 5737 documentation addresses only.
HOME_URL = "http://192.0.2.10:8000"
HOME_NAMES = "homeserver,homeserver.local"


@contextmanager
def _env(**variables: str) -> Iterator[None]:
    """PURSER_* variables set and the cached Settings dropped on both sides,
    so neither these values nor stale ones leak into neighbouring tests."""
    names = {f"PURSER_{key.upper()}": value for key, value in variables.items()}
    previous = {name: os.environ.get(name) for name in names}
    os.environ.update(names)
    purser.config.get_settings.cache_clear()
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        purser.config.get_settings.cache_clear()


def test_allowlist_admits_the_public_host_and_local_dev_hosts() -> None:
    with _env(public_url=PUBLIC_URL):
        hosts, origins = _host_origin_allowlist()
    assert {PUBLIC_HOST, f"{PUBLIC_HOST}:*", "localhost", "localhost:*"} <= set(hosts)
    assert PUBLIC_URL in origins
    assert "*" not in hosts
    assert "*" not in origins


def test_allowlist_adds_the_allowed_names_exactly() -> None:
    """Each allowed name on any port, or on the one port written with it; its
    origins on the public URL's port. No entry is a pattern (#633)."""
    with _env(public_url=HOME_URL, allowed_hosts=f"{HOME_NAMES},192.0.2.11:8010"):
        hosts, origins = _host_origin_allowlist()
    assert {"homeserver", "homeserver:*", "homeserver.local", "homeserver.local:*"} <= set(hosts)
    assert "192.0.2.11:8010" in hosts
    assert "192.0.2.11" not in hosts and "192.0.2.11:*" not in hosts
    assert {"http://homeserver:8000", "http://homeserver.local:8000"} <= set(origins)
    assert "http://192.0.2.11:8010" in origins
    assert not [h for h in hosts if any(c in h.removesuffix(":*") for c in "*?[")]


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

    with _env(public_url=PUBLIC_URL), TestClient(create_app()) as c:
        yield c


@pytest.fixture
def home_client(read_token: str) -> Iterator[TestClient]:
    """A home server reached by its address and its listed names (#633)."""
    from purser.api.app import create_app

    with _env(public_url=HOME_URL, allowed_hosts=HOME_NAMES), TestClient(create_app()) as c:
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

    def test_misdirected_answer_names_the_address_and_the_setting(
        self, mcp_client: TestClient, read_token: str
    ) -> None:
        """An agent that connects by the wrong name is told which one works
        and which setting changes it (#581), instead of a bare status."""
        resp = _initialize(mcp_client, host="192.0.2.10:8000", token=read_token)
        assert resp.status_code == 421
        detail = resp.json()["detail"]
        assert f"{PUBLIC_URL}/mcp" in detail
        assert "'192.0.2.10:8000'" in detail
        assert "PURSER_PUBLIC_URL" in detail

    def test_misdirected_answer_is_not_given_without_a_token(self, mcp_client: TestClient) -> None:
        resp = _initialize(mcp_client, host=FOREIGN_HOST, token=None)
        assert resp.status_code == 401
        assert PUBLIC_URL not in resp.text

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


class TestAllowedHosts:
    """#633: the machine's other names reach the MCP endpoint; any other name
    still gets 421, so DNS rebinding stays blocked."""

    @pytest.mark.parametrize(
        "host", ["homeserver:8000", "homeserver.local:8000", "HomeServer.Local:8000"]
    )
    def test_an_allowed_name_is_served(
        self, home_client: TestClient, read_token: str, host: str
    ) -> None:
        resp = _initialize(home_client, host=host, token=read_token)
        assert resp.status_code == 200, resp.text

    @pytest.mark.parametrize("host", ["192.0.2.10:8000", "localhost:8000", "127.0.0.1:8000"])
    def test_the_public_url_and_localhost_keep_working(
        self, home_client: TestClient, read_token: str, host: str
    ) -> None:
        resp = _initialize(home_client, host=host, token=read_token)
        assert resp.status_code == 200, resp.text

    def test_an_allowed_name_as_origin_is_served(
        self, home_client: TestClient, read_token: str
    ) -> None:
        resp = _initialize(
            home_client,
            host="192.0.2.10:8000",
            token=read_token,
            origin="http://homeserver.local:8000",
        )
        assert resp.status_code == 200, resp.text

    @pytest.mark.parametrize(
        "host", ["otherbox:8000", "homeserver.example.net:8000", "evil-homeserver.local:8000"]
    )
    def test_an_unlisted_name_is_still_misdirected(
        self, home_client: TestClient, read_token: str, host: str
    ) -> None:
        resp = _initialize(home_client, host=host, token=read_token)
        assert resp.status_code == 421

    def test_the_421_says_how_to_allow_another_name(
        self, home_client: TestClient, read_token: str
    ) -> None:
        resp = _initialize(home_client, host="otherbox:8000", token=read_token)
        assert resp.status_code == 421
        detail = resp.json()["detail"]
        assert f"{HOME_URL}/mcp" in detail
        assert "homeserver, homeserver.local" in detail
        assert "add it to PURSER_ALLOWED_HOSTS" in detail
        assert "'otherbox'" in detail
