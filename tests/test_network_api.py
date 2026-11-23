"""GET /config/network: how this Purser is reached (#632).

The app cannot see the host's port publishing from inside its container, so
the mode comes from what the deployment reports (Compose passes
PURSER_BIND_ADDRESS) and is "unknown" when it reports nothing. The response
also says whether the address the admin UI is open at is one agents can use.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from purser.config import get_settings


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """Settings re-read per request: set variables, then call the route."""
    for name in ("PURSER_PUBLIC_URL", "PURSER_BIND_ADDRESS", "PURSER_ALLOWED_HOSTS"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield monkeypatch
    monkeypatch.undo()
    get_settings.cache_clear()


def _set(env: pytest.MonkeyPatch, **variables: str) -> None:
    for key, value in variables.items():
        env.setenv(f"PURSER_{key.upper()}", value)
    get_settings.cache_clear()


def _network(client: TestClient, host: str | None = None) -> dict[str, object]:
    resp = client.get("/config/network", headers={"Host": host} if host else {})
    assert resp.status_code == 200, resp.text
    body: dict[str, object] = resp.json()
    return body


class TestNetworkReport:
    def test_compose_default_is_this_machine(
        self, admin_client: TestClient, env: pytest.MonkeyPatch
    ) -> None:
        _set(env, bind_address="127.0.0.1", public_url="http://localhost:8000")
        body = _network(admin_client, "localhost:8000")
        assert body["reach"] == "this_machine"
        assert body["mcp_url"] == "http://localhost:8000/mcp"
        assert body["bind_address"] == "127.0.0.1"
        assert body["public_url_is_local"] is True
        assert body["request_host_accepted"] is True
        assert str(body["guide_url"]).endswith(
            "/tutorials/ai-setup/#use-purser-from-another-machine-on-the-home-network"
        )

    def test_home_network_reports_the_address_and_the_names(
        self, admin_client: TestClient, env: pytest.MonkeyPatch
    ) -> None:
        _set(
            env,
            bind_address="0.0.0.0",
            public_url="http://192.0.2.10:8000/",
            allowed_hosts="192.0.2.10,homeserver,homeserver.local",
        )
        body = _network(admin_client, "homeserver.local:8000")
        assert body["reach"] == "home_network"
        assert body["public_url"] == "http://192.0.2.10:8000"
        assert body["mcp_url"] == "http://192.0.2.10:8000/mcp"
        assert body["allowed_hosts"] == ["192.0.2.10", "homeserver", "homeserver.local"]
        assert body["public_url_is_local"] is False
        assert body["request_host"] == "homeserver.local:8000"
        assert body["request_host_accepted"] is True

    def test_an_unreported_deployment_is_unknown(
        self, admin_client: TestClient, env: pytest.MonkeyPatch
    ) -> None:
        _set(env, public_url="http://192.0.2.10:8000")
        body = _network(admin_client, "192.0.2.10:8000")
        assert body["reach"] == "unknown"
        assert body["bind_address"] is None
        assert body["mcp_url"] == "http://192.0.2.10:8000/mcp"

    def test_an_https_public_url_is_a_reverse_proxy(
        self, admin_client: TestClient, env: pytest.MonkeyPatch
    ) -> None:
        _set(env, bind_address="127.0.0.1", public_url="https://purser.example.com")
        body = _network(admin_client, "purser.example.com")
        assert body["reach"] == "reverse_proxy"
        assert body["request_host_accepted"] is True

    @pytest.mark.parametrize("host", ["otherbox:8000", "192.0.2.99:8000"])
    def test_an_address_agents_cannot_use_is_flagged(
        self, admin_client: TestClient, env: pytest.MonkeyPatch, host: str
    ) -> None:
        """The mismatch hint's source: the UI is open at a name the MCP Host
        guard refuses, so connection steps copied from it would get 421."""
        _set(
            env,
            bind_address="0.0.0.0",
            public_url="http://192.0.2.10:8000",
            allowed_hosts="homeserver",
        )
        body = _network(admin_client, host)
        assert body["request_host"] == host
        assert body["request_host_accepted"] is False


class TestNetworkScope:
    def test_unauthenticated_gets_401(self, clean_db: None, client: TestClient) -> None:
        assert client.get("/config/network").status_code == 401

    def test_agent_tokens_get_403(self, admin_client: TestClient) -> None:
        token = admin_client.post(
            "/tokens", json={"name": "agent", "scopes": ["read", "write"]}
        ).json()["token"]
        resp = admin_client.get("/config/network", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403
