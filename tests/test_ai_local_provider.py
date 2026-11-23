"""The explicit local-model provider (#640, DESIGN decision 133): provider
selection, the connection test and model list against a fake
OpenAI-compatible server, the configurable timeout, clearing a stored key,
the privacy wording pinned to the prompt builders, and the upgrade path of
an existing ``openai`` + base URL configuration.

No test reaches the network: the fake server is an ``httpx.MockTransport``
handler, and address classification gets literal IPs or an injected
resolver.
"""

import inspect
import json
from collections.abc import Callable, Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

import purser.config
from purser import ai_providers, appsettings, crypto
from purser.ai_providers import AiProviderError, Settings

LOCAL_URL = "http://127.0.0.1:11434/v1"
# Derived, never pasted (CLAUDE.md: no provider-shaped secret literals).
STORED_KEY = "test-" + "stored-key-" + "0" * 8
WRONG_KEY = "test-" + "wrong-key-" + "1" * 8
ROW_DATE = date(2026, 1, 15)
PRIVACY_PAGE = Path(__file__).parents[1] / "docs/src/content/docs/explanation/privacy-and-agents.md"


@pytest.fixture(autouse=True)
def fresh_settings_cache() -> Iterator[None]:
    yield
    purser.config.get_settings.cache_clear()
    appsettings.invalidate()


class FakeOpenAIServer:
    """An in-process OpenAI-compatible server: ``GET /v1/models`` and
    ``POST /v1/chat/completions``, optionally behind a key. A refusal echoes
    the Authorization header in its body, the way a careless server might, so
    the tests prove Purser never surfaces a provider's response body."""

    def __init__(
        self,
        *,
        models: tuple[str, ...] = ("llama3.1:8b", "qwen2.5:7b"),
        key: str | None = None,
        lists_models: bool = True,
        reply: str = '{"ok": true}',
    ) -> None:
        self.models = models
        self.key = key
        self.lists_models = lists_models
        self.reply = reply
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        auth = request.headers.get("authorization")
        if self.key is not None and auth != f"Bearer {self.key}":
            return httpx.Response(401, json={"error": {"message": f"invalid key: {auth}"}})
        if request.method == "GET" and request.url.path == "/v1/models":
            if not self.lists_models:
                return httpx.Response(404, text="not found")
            data = [{"id": m, "object": "model"} for m in self.models]
            return httpx.Response(200, json={"object": "list", "data": data})
        if request.method == "POST" and request.url.path == "/v1/chat/completions":
            body = json.loads(request.content)
            if body["model"] not in self.models:
                return httpx.Response(404, json={"error": {"message": "model not found"}})
            message = {"role": "assistant", "content": self.reply}
            return httpx.Response(200, json={"choices": [{"message": message}]})
        return httpx.Response(404, text="not found")

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    @property
    def completions(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/chat/completions")]


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "ai_provider": "local",
        "ai_base_url": LOCAL_URL,
        "ai_model": "llama3.1:8b",
        "ai_api_key": None,
    }
    return Settings(**(values | overrides))


def _patch_http(monkeypatch: pytest.MonkeyPatch, handler: Callable[..., httpx.Response]) -> None:
    """Route every httpx.Client Purser opens to ``handler`` (the API paths
    open their own client)."""
    real_client = httpx.Client

    def patched(*args: object, **kwargs: object) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "Client", patched)


# --------------------------------------------------------------------------
# Provider selection and the request path
# --------------------------------------------------------------------------


class TestLocalProvider:
    def test_local_uses_the_openai_compatible_path_without_a_key(self) -> None:
        server = FakeOpenAIServer(reply='{"category": "expenses.dining", "confidence": 0.9}')
        suggestion = ai_providers.suggest(
            _settings(),
            payee="Corner Cafe",
            description=None,
            amount=Decimal("-12.50"),
            categories=["expenses.dining"],
            client=server.client(),
        )
        assert suggestion is not None and suggestion.category_path == "expenses.dining"
        (request,) = server.completions
        assert str(request.url) == f"{LOCAL_URL}/chat/completions"
        assert "authorization" not in request.headers

    def test_local_never_falls_back_to_the_cloud_default(self) -> None:
        server = FakeOpenAIServer()
        for url in (ai_providers.CLOUD_DEFAULT_BASE_URL, "", "https://api.openai.com/v1/"):
            with pytest.raises(AiProviderError) as exc:
                ai_providers.suggest(
                    _settings(ai_base_url=url),
                    payee="x",
                    description=None,
                    amount=Decimal("-1.00"),
                    categories=["expenses.dining"],
                    client=server.client(),
                )
            assert exc.value.kind == "config"
        assert server.requests == []

    def test_openai_keeps_the_cloud_default(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

        ai_providers.suggest(
            Settings(ai_provider="openai", ai_model="m", ai_api_key=STORED_KEY),
            payee="x",
            description=None,
            amount=Decimal("-1.00"),
            categories=["expenses.dining"],
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        assert str(seen[0].url) == "https://api.openai.com/v1/chat/completions"
        assert seen[0].headers["authorization"] == f"Bearer {STORED_KEY}"


class TestTimeout:
    def test_the_timeout_is_configurable_and_holds_per_request(self) -> None:
        server = FakeOpenAIServer()
        ai_providers.review_row(
            _settings(ai_timeout_seconds=90),
            row={"id": "r1"},
            guidance=["check it"],
            client=server.client(),
        )
        assert server.requests[0].extensions["timeout"] == {
            "connect": 90.0,
            "read": 90.0,
            "write": 90.0,
            "pool": 90.0,
        }

    def test_the_default_stays_thirty_seconds(self) -> None:
        assert Settings().ai_timeout_seconds == 30

    def test_a_timeout_is_a_sanitized_provider_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        with pytest.raises(AiProviderError) as exc:
            ai_providers.suggest(
                _settings(ai_timeout_seconds=45, ai_api_key=STORED_KEY),
                payee="x",
                description=None,
                amount=Decimal("-1.00"),
                categories=["expenses.dining"],
                client=httpx.Client(transport=httpx.MockTransport(handler)),
            )
        assert exc.value.kind == "timeout"
        assert str(exc.value) == "provider did not answer within 45 s"


# --------------------------------------------------------------------------
# Model list and connection test (unit level)
# --------------------------------------------------------------------------


class TestModelList:
    def test_lists_sorted_unique_ids(self) -> None:
        server = FakeOpenAIServer(models=("b-model", "a-model", "b-model"))
        listing = ai_providers.list_models(_settings(), client=server.client())
        assert listing.listed and listing.models == ["a-model", "b-model"]
        assert str(server.requests[0].url) == f"{LOCAL_URL}/models"

    def test_a_server_without_a_list_says_so(self) -> None:
        server = FakeOpenAIServer(lists_models=False)
        listing = ai_providers.list_models(_settings(), client=server.client())
        assert listing == ai_providers.ModelList(listed=False, models=[])

    def test_a_refused_key_raises_with_the_status_only(self) -> None:
        server = FakeOpenAIServer(key=STORED_KEY)
        with pytest.raises(AiProviderError) as exc:
            ai_providers.list_models(_settings(ai_api_key=WRONG_KEY), client=server.client())
        assert exc.value.status == 401
        assert WRONG_KEY not in str(exc.value)


class TestConnectionTest:
    def test_succeeds_against_a_keyless_local_server(self) -> None:
        report = ai_providers.check_connection(_settings(), client=FakeOpenAIServer().client())
        assert report.ok and report.reachable and report.answered
        assert report.authenticated is True
        assert report.model_listed is True
        assert report.key_sent is False
        assert report.network == "this_machine"
        assert report.warning is None
        assert report.message.startswith("Connected: llama3.1:8b answered in ")

    def test_a_wrong_url_fails_cleanly(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        report = ai_providers.check_connection(
            _settings(
                ai_base_url="http://user:pa55word@192.168.1.20:11434/v1", ai_api_key=WRONG_KEY
            ),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        assert not report.ok and not report.reachable
        assert report.message.startswith("Could not reach 192.168.1.20:11434 (ConnectError).")
        dumped = json.dumps(report.__dict__)
        assert WRONG_KEY not in dumped
        assert "pa55word" not in dumped

    def test_localhost_gets_the_container_hint(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        report = ai_providers.check_connection(
            _settings(ai_base_url="http://localhost:11434/v1"),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        assert "host.docker.internal" in report.message

    def test_a_bad_key_fails_cleanly_without_echoing_it(self) -> None:
        server = FakeOpenAIServer(key=STORED_KEY)
        report = ai_providers.check_connection(
            _settings(ai_api_key=WRONG_KEY), client=server.client()
        )
        assert not report.ok and report.reachable
        assert report.authenticated is False
        assert report.key_sent is True
        assert report.message == "The server refused the API key (HTTP 401). Check the key."
        assert WRONG_KEY not in json.dumps(report.__dict__)

    def test_a_missing_key_is_named(self) -> None:
        report = ai_providers.check_connection(
            _settings(), client=FakeOpenAIServer(key=STORED_KEY).client()
        )
        assert report.authenticated is False
        assert "needs an API key" in report.message

    def test_a_server_without_a_model_list_still_passes(self) -> None:
        server = FakeOpenAIServer(lists_models=False)
        report = ai_providers.check_connection(_settings(), client=server.client())
        assert report.ok
        assert report.model_listed is None
        assert len(server.completions) == 1

    def test_a_reply_that_is_not_json_fails(self) -> None:
        server = FakeOpenAIServer(reply="Sure! Here you go: ok")
        report = ai_providers.check_connection(_settings(), client=server.client())
        assert not report.ok and report.answered is False
        assert "not with the JSON" in report.message

    def test_an_unknown_model_is_reported(self) -> None:
        server = FakeOpenAIServer()
        report = ai_providers.check_connection(
            _settings(ai_model="missing:1b"), client=server.client()
        )
        assert not report.ok
        assert report.model_listed is False
        assert "HTTP 404" in report.message and "'missing:1b'" in report.message

    def test_no_model_yet_stops_after_reaching_the_server(self) -> None:
        server = FakeOpenAIServer()
        report = ai_providers.check_connection(_settings(ai_model=None), client=server.client())
        assert not report.ok and report.reachable
        assert (
            report.message
            == "Reached 127.0.0.1:11434. It lists 2 models. Choose a model to finish the test."
        )
        assert server.completions == []

    def test_a_slow_model_is_told_to_raise_the_timeout(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/models"):
                return httpx.Response(200, json={"data": [{"id": "llama3.1:8b"}]})
            raise httpx.ReadTimeout("timed out", request=request)

        report = ai_providers.check_connection(
            _settings(ai_timeout_seconds=20),
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        assert not report.ok and report.reachable
        assert "did not answer within 20 s" in report.message
        assert "raise the timeout" in report.message

    def test_local_on_the_public_internet_warns(self) -> None:
        report = ai_providers.check_connection(
            _settings(ai_base_url="http://models.example.com/v1"),
            client=FakeOpenAIServer().client(),
            resolve=lambda host: ["203.0.113.7", "8.8.8.8"],
        )
        assert report.ok
        assert report.network == "public_internet"
        assert report.warning is not None and "leaves your network" in report.warning

    def test_hosted_choice_on_the_public_internet_does_not_warn(self) -> None:
        report = ai_providers.check_connection(
            _settings(ai_provider="openai", ai_base_url="http://models.example.com/v1"),
            client=FakeOpenAIServer().client(),
            resolve=lambda host: ["8.8.8.8"],
        )
        assert report.network == "public_internet" and report.warning is None

    def test_local_without_a_base_url_makes_no_request(self) -> None:
        server = FakeOpenAIServer()
        report = ai_providers.check_connection(
            _settings(ai_base_url=ai_providers.CLOUD_DEFAULT_BASE_URL), client=server.client()
        )
        assert not report.ok and not report.reachable
        assert "needs its server's base URL" in report.message
        assert server.requests == []


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:11434/v1", "this_machine"),
        ("http://[::1]:8080/v1", "this_machine"),
        ("http://localhost:1234/v1", "this_machine"),
        ("http://192.168.1.20:11434/v1", "private_network"),
        ("http://10.0.0.5/v1", "private_network"),
        ("http://172.17.0.1:11434/v1", "private_network"),
        ("http://100.101.102.103:11434/v1", "private_network"),  # 100.64/10, e.g. Tailscale
        ("http://[fd12:3456::1]/v1", "private_network"),
        ("http://8.8.8.8/v1", "public_internet"),
    ],
)
def test_network_of_literal_addresses(url: str, expected: str) -> None:
    def no_dns(host: str) -> list[str]:
        raise AssertionError(f"resolved {host}")

    assert ai_providers.network_of(url, resolve=no_dns) == expected


def test_network_of_names_uses_the_resolver() -> None:
    assert (
        ai_providers.network_of(
            "http://host.docker.internal:11434/v1", resolve=lambda h: ["172.17.0.1"]
        )
        == "private_network"
    )

    def fails(host: str) -> list[str]:
        raise OSError("no such host")

    assert ai_providers.network_of("http://nowhere.invalid/v1", resolve=fails) == "unknown"


# --------------------------------------------------------------------------
# The privacy wording, pinned to the prompt builders
# --------------------------------------------------------------------------


def _disclosure(capability: str) -> ai_providers.Disclosure:
    return next(d for d in ai_providers.DISCLOSURES if d.capability == capability)


def _fields(capability: str) -> set[str]:
    return {field for sent in _disclosure(capability).sends for field in sent.fields}


class TestDisclosure:
    def test_categorize_wording_covers_every_prompt_input(self) -> None:
        """A new input to the categorize prompt fails here until the wording
        on the card and in the docs names it."""
        assert _fields("categorize") == set(inspect.signature(ai_providers._prompt).parameters)

    def test_categorize_prompt_carries_what_the_wording_says(self) -> None:
        prompt = ai_providers._prompt(
            payee="PAYEE-SENTINEL",
            description="DESC-SENTINEL",
            amount=Decimal("-41.27"),
            categories=["expenses.sentinel", "[assets.sentinel]"],
            category_descriptions={
                "expenses.sentinel": "CATEGORY-DESC-SENTINEL",
                "[assets.sentinel]": "transfer to/from the ACCOUNT-NAME-SENTINEL account",
            },
            memories=["MEMORY-SENTINEL"],
        )
        for sentinel in (
            "PAYEE-SENTINEL",
            "DESC-SENTINEL",
            "-41.27",
            "expenses.sentinel",
            "CATEGORY-DESC-SENTINEL",
            "[assets.sentinel]",
            "ACCOUNT-NAME-SENTINEL",
            "MEMORY-SENTINEL",
        ):
            assert sentinel in prompt

    def test_review_wording_covers_the_row_and_the_checklist(
        self, admin_client: TestClient, db_session: Session
    ) -> None:
        from purser import review
        from purser.models import Transaction

        assert set(inspect.signature(ai_providers._review_prompt).parameters) == {"row", "guidance"}
        admin_client.post(
            "/accounts", json={"name": "Checking", "type": "checking", "path": "assets.checking"}
        )
        admin_client.post(
            "/categories",
            json={
                "name": "Uncategorized",
                "type": "category_expense",
                "path": "expenses.uncategorized",
            },
        )
        resp = admin_client.post(
            "/transactions",
            json={
                "account": "assets.checking",
                "category": "expenses.uncategorized",
                "amount": "-25.00",
                "date": ROW_DATE.isoformat(),
                "payee": "Corner Market",
                "status": "pending",
            },
        )
        assert resp.status_code == 201, resp.text
        tx = db_session.execute(select(Transaction)).scalar_one()
        plain = set(review._row_payload(db_session, tx))
        flagged = set(review._row_payload(db_session, tx, flagged=True))
        declared = _fields("review")
        # Everything a row can carry is named...
        assert plain | flagged | {"guidance"} <= declared
        # ...and nothing is named that a row cannot carry (`locked` appears
        # only on a row a recorded trade holds).
        assert declared <= plain | flagged | {"guidance", "locked"}

    def test_the_docs_state_the_same_wording(self) -> None:
        page = PRIVACY_PAGE.read_text()
        for disclosure in ai_providers.DISCLOSURES:
            for sent in disclosure.sends:
                assert sent.text in page, sent.text

    def test_config_serves_the_wording(self, admin_client: TestClient) -> None:
        body = admin_client.get("/config").json()
        assert body["ai_disclosure"] == [
            {"capability": d.capability, "when": d.when, "sends": [s.text for s in d.sends]}
            for d in ai_providers.DISCLOSURES
        ]


# --------------------------------------------------------------------------
# /config: selection, base URL rule, timeout, clearing the key
# --------------------------------------------------------------------------


class TestConfigApi:
    def test_local_needs_its_own_base_url(self, admin_client: TestClient) -> None:
        resp = admin_client.put("/config", json={"ai_provider": "local"})
        assert resp.status_code == 422
        assert "needs ai_base_url" in resp.json()["detail"]
        resp = admin_client.put(
            "/config",
            json={"ai_provider": "local", "ai_base_url": ai_providers.CLOUD_DEFAULT_BASE_URL},
        )
        assert resp.status_code == 422

        resp = admin_client.put(
            "/config",
            json={"ai_provider": "local", "ai_base_url": LOCAL_URL, "ai_model": "llama3.1:8b"},
        )
        assert resp.status_code == 200, resp.text
        values = resp.json()["values"]
        assert (values["ai_provider"], values["ai_base_url"]) == ("local", LOCAL_URL)
        assert admin_client.get("/categorize/status").json()["provider"] == "local"

        # Resetting the URL would leave `local` on the cloud default.
        assert admin_client.delete("/config/ai_base_url").status_code == 422
        # Switching away first is fine.
        assert admin_client.put("/config", json={"ai_provider": "none"}).status_code == 200
        assert admin_client.delete("/config/ai_base_url").status_code == 200

    def test_timeout_round_trip_and_bounds(self, admin_client: TestClient) -> None:
        assert admin_client.get("/config").json()["values"]["ai_timeout_seconds"] == 30
        resp = admin_client.put("/config", json={"ai_timeout_seconds": 120})
        assert resp.status_code == 200
        assert resp.json()["values"]["ai_timeout_seconds"] == 120
        assert appsettings.effective().ai_timeout_seconds == 120
        for bad in (4, 601, None):
            assert admin_client.put("/config", json={"ai_timeout_seconds": bad}).status_code == 422
        assert (
            admin_client.delete("/config/ai_timeout_seconds").json()["values"]["ai_timeout_seconds"]
            == 30
        )

    def test_env_timeout_is_the_default(
        self, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PURSER_AI_TIMEOUT_SECONDS", "75")
        purser.config.get_settings.cache_clear()
        assert admin_client.get("/config").json()["values"]["ai_timeout_seconds"] == 75

    def test_clear_key_stores_no_key_even_over_the_env(
        self,
        admin_client: TestClient,
        migrated_engine: Engine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("PURSER_AI_API_KEY", WRONG_KEY)
        purser.config.get_settings.cache_clear()
        assert admin_client.get("/config").json()["ai_api_key_set"] is True

        resp = admin_client.put("/config", json={"ai_api_key": STORED_KEY})
        assert resp.json()["ai_api_key_set"] is True

        resp = admin_client.put("/config", json={"ai_api_key": None})
        assert resp.status_code == 200
        assert resp.json()["ai_api_key_set"] is False
        assert "ai_api_key" in resp.json()["overridden"]
        assert appsettings.effective().ai_api_key is None

        with migrated_engine.connect() as conn:
            row = conn.execute(
                text("SELECT value, value_enc FROM app_settings WHERE key = 'ai_api_key'")
            ).one()
            audits = conn.execute(
                text(
                    "SELECT before_after_enc FROM audit_log"
                    " WHERE entity_type = 'config' AND kind = 'change' ORDER BY at"
                )
            ).scalars()
            blobs = [crypto.decrypt(bytes(enc)) for enc in audits]
        assert row.value is None and json.loads(crypto.decrypt(bytes(row.value_enc))) is None
        assert json.loads(blobs[-1]) == {
            "before": {"ai_api_key": "set"},
            "after": {"ai_api_key": "unset"},
        }
        assert not any(STORED_KEY in blob or WRONG_KEY in blob for blob in blobs)

        # DELETE returns to the environment's key.
        assert admin_client.delete("/config/ai_api_key").json()["ai_api_key_set"] is True

    def test_a_key_http_cannot_carry_is_refused_without_echoing_it(
        self, admin_client: TestClient
    ) -> None:
        resp = admin_client.put("/config", json={"ai_api_key": "test-key-with space"})
        assert resp.status_code == 422
        assert "with space" not in resp.text


# --------------------------------------------------------------------------
# /config/ai/models and /config/ai/test
# --------------------------------------------------------------------------


class TestCheckEndpoints:
    def test_admin_only(self, admin_client: TestClient) -> None:
        token = admin_client.post("/tokens", json={"name": "agent", "scopes": ["read", "write"]})
        headers = {"Authorization": f"Bearer {token.json()['token']}"}
        for path in ("/config/ai/test", "/config/ai/models"):
            assert admin_client.post(path, json={}, headers=headers).status_code == 403

    def test_needs_a_provider(self, admin_client: TestClient) -> None:
        resp = admin_client.post("/config/ai/test", json={})
        assert resp.status_code == 422
        assert resp.json()["detail"] == "choose an AI provider to check"

    def test_checks_the_form_before_it_is_saved(
        self, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        server = FakeOpenAIServer()
        _patch_http(monkeypatch, server)
        form = {"provider": "local", "base_url": LOCAL_URL, "model": "qwen2.5:7b"}

        models = admin_client.post("/config/ai/models", json=form).json()
        assert models == {
            "listed": True,
            "models": ["llama3.1:8b", "qwen2.5:7b"],
            "key_sent": False,
            "message": "Found 2 models.",
        }
        report = admin_client.post("/config/ai/test", json=form | {"timeout_seconds": 60}).json()
        assert report["ok"] is True
        assert report["key_sent"] is False
        assert report["network"] == "this_machine"
        assert server.completions[-1].extensions["timeout"]["read"] == 60.0
        # Nothing was saved.
        assert admin_client.get("/config").json()["values"]["ai_provider"] == "none"

    def test_the_stored_key_is_sent_unless_the_form_says_not_to(
        self, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        admin_client.put(
            "/config",
            json={
                "ai_provider": "local",
                "ai_base_url": LOCAL_URL,
                "ai_model": "llama3.1:8b",
                "ai_api_key": STORED_KEY,
            },
        )
        server = FakeOpenAIServer(key=STORED_KEY)
        _patch_http(monkeypatch, server)

        resp = admin_client.post("/config/ai/test", json={})
        assert resp.json()["ok"] is True and resp.json()["key_sent"] is True
        assert STORED_KEY not in resp.text

        resp = admin_client.post("/config/ai/test", json={"use_stored_key": False})
        assert resp.json()["authenticated"] is False
        assert "needs an API key" in resp.json()["message"]

        resp = admin_client.post("/config/ai/test", json={"api_key": WRONG_KEY})
        assert resp.json()["message"] == "The server refused the API key (HTTP 401). Check the key."
        assert WRONG_KEY not in resp.text
        # The stored key is untouched by any check.
        assert appsettings.effective().ai_api_key == STORED_KEY

    def test_a_server_without_a_model_list(
        self, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_http(monkeypatch, FakeOpenAIServer(lists_models=False))
        resp = admin_client.post(
            "/config/ai/models", json={"provider": "local", "base_url": LOCAL_URL}
        )
        assert resp.json() == {
            "listed": False,
            "models": [],
            "key_sent": False,
            "message": "This server does not list its models: type the model name.",
        }

    def test_models_failure_is_sanitized(
        self, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_http(monkeypatch, FakeOpenAIServer(key=STORED_KEY))
        resp = admin_client.post(
            "/config/ai/models",
            json={"provider": "local", "base_url": LOCAL_URL, "api_key": WRONG_KEY},
        )
        assert resp.json()["listed"] is False
        assert resp.json()["message"] == "The server refused the API key (HTTP 401). Check the key."
        assert WRONG_KEY not in resp.text


class TestStoredKeyDestination:
    """The stored key goes only where categorization would send it: the
    saved provider at the saved base URL (#667). A check of any other
    destination sends the request's key or none, says so, and the call row
    records the host it reached."""

    LAN_URL = "http://192.168.1.20:11434/v1"

    def _save_hosted(self, client: TestClient) -> None:
        resp = client.put(
            "/config",
            json={"ai_provider": "openai", "ai_model": "llama3.1:8b", "ai_api_key": STORED_KEY},
        )
        assert resp.status_code == 200, resp.text

    def _auth(self, server: FakeOpenAIServer) -> set[str | None]:
        return {r.headers.get("authorization") for r in server.requests}

    def test_an_unsaved_address_gets_no_stored_key(
        self, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._save_hosted(admin_client)
        server = FakeOpenAIServer()
        _patch_http(monkeypatch, server)
        for path in ("/config/ai/test", "/config/ai/models"):
            for form in (
                {"base_url": self.LAN_URL},  # same provider, another address
                {"provider": "local", "base_url": self.LAN_URL},
                # The saved address under another provider is not the saved
                # destination either.
                {"provider": "local", "base_url": ai_providers.CLOUD_DEFAULT_BASE_URL + "/"},
            ):
                server.requests.clear()
                resp = admin_client.post(path, json=form)
                assert resp.status_code == 200, resp.text
                body = resp.json()
                assert body["key_sent"] is False, (path, form)
                assert body["message"].endswith(
                    "The stored key was not sent: Purser sends it only to the saved provider "
                    "and address. Enter this server's key to check with one."
                ), body
                assert STORED_KEY not in resp.text
                assert self._auth(server) <= {None}, (path, form)
        # The request's own key still goes where the request says.
        server.requests.clear()
        resp = admin_client.post(
            "/config/ai/models", json={"base_url": self.LAN_URL, "api_key": WRONG_KEY}
        )
        assert resp.json()["key_sent"] is True
        assert "stored key" not in resp.json()["message"]
        assert self._auth(server) == {f"Bearer {WRONG_KEY}"}
        assert appsettings.effective().ai_api_key == STORED_KEY

    def test_the_saved_destination_gets_the_stored_key(
        self, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._save_hosted(admin_client)
        server = FakeOpenAIServer(key=STORED_KEY)
        _patch_http(monkeypatch, server)
        # Nothing in the form, or the saved values restated (a trailing slash
        # is the same address).
        for form in ({}, {"provider": "openai", "base_url": "https://api.openai.com/v1/"}):
            server.requests.clear()
            resp = admin_client.post("/config/ai/test", json=form)
            assert resp.json()["ok"] is True and resp.json()["key_sent"] is True, resp.text
            assert "stored key" not in resp.json()["message"]
            assert self._auth(server) == {f"Bearer {STORED_KEY}"}
        resp = admin_client.post("/config/ai/test", json={"use_stored_key": False})
        assert resp.json()["key_sent"] is False
        assert "stored key" not in resp.json()["message"]

    def test_the_call_row_records_the_host_checked(
        self, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._save_hosted(admin_client)
        _patch_http(monkeypatch, FakeOpenAIServer())
        url = "http://someone:url-secret@192.168.1.20:11434/v1/private-path?token=q"
        admin_client.post("/config/ai/test", json={"provider": "local", "base_url": url})
        admin_client.post("/config/ai/models", json={})
        rows = admin_client.get("/audit", params={"limit": 20}).json()["items"]
        test_row = next(r for r in rows if r["action"] == "POST /config/ai/test")
        assert test_row["detail"]["ai_check"] == {
            "provider": "local",
            "host": "192.168.1.20:11434",
            "key_sent": False,
        }
        models_row = next(r for r in rows if r["action"] == "POST /config/ai/models")
        assert models_row["detail"]["ai_check"] == {
            "provider": "openai",
            "host": "api.openai.com",
            "key_sent": True,
        }
        for secret in ("url-secret", "someone", "private-path", "token=q", STORED_KEY):
            assert secret not in json.dumps(test_row) + json.dumps(models_row)


def test_host_of_keeps_only_host_and_port() -> None:
    assert ai_providers.host_of("http://u:p@example.test:8080/v1?x=1") == "example.test:8080"
    assert ai_providers.host_of("https://api.example.test/v1") == "api.example.test"
    assert ai_providers.host_of("http://[::1]:11434/v1") == "[::1]:11434"
    assert ai_providers.host_of("http://h:99999/v1") == "h"
    assert ai_providers.host_of("not a url") is None


# --------------------------------------------------------------------------
# Categorization through a local model, and the upgrade path
# --------------------------------------------------------------------------


def _seed_ledger(client: TestClient) -> str:
    client.post(
        "/accounts", json={"name": "Checking", "type": "checking", "path": "assets.checking"}
    )
    client.post(
        "/accounts", json={"name": "Rainy Day Fund", "type": "savings", "path": "assets.savings"}
    )
    for name, path in [("Uncategorized", "expenses.uncategorized"), ("Dining", "expenses.dining")]:
        client.post("/categories", json={"name": name, "type": "category_expense", "path": path})
    resp = client.post(
        "/transactions",
        json={
            "account": "assets.checking",
            "category": "expenses.uncategorized",
            "amount": "-18.40",
            "date": ROW_DATE.isoformat(),
            "payee": "Corner Cafe",
            "status": "pending",
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


class TestCategorizeThroughLocal:
    def test_a_local_model_categorizes_with_provenance(
        self,
        admin_client: TestClient,
        migrated_engine: Engine,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tx_id = _seed_ledger(admin_client)
        admin_client.put(
            "/config",
            json={"ai_provider": "local", "ai_base_url": LOCAL_URL, "ai_model": "llama3.1:8b"},
        )
        server = FakeOpenAIServer(reply='{"category": "expenses.dining", "confidence": 0.8}')
        _patch_http(monkeypatch, server)

        stats = admin_client.post("/categorize/run").json()
        assert stats["ai"] == 1
        (request,) = server.completions
        assert str(request.url) == f"{LOCAL_URL}/chat/completions"
        assert "authorization" not in request.headers
        prompt = json.loads(request.content)["messages"][0]["content"]
        # What the card says is sent is what was sent.
        for expected in ("Corner Cafe", "-18.40", "expenses.dining", "Rainy Day Fund"):
            assert expected in prompt

        with migrated_engine.connect() as conn:
            enc = conn.execute(
                text(
                    "SELECT before_after_enc FROM audit_log "
                    "WHERE action = 'transaction.categorize' AND entity_id = :id"
                ),
                {"id": tx_id},
            ).scalar_one()
        assert json.loads(crypto.decrypt(bytes(enc)))["after"]["provider"] == "local"


class TestUpgradePath:
    """An install configured before #640 (``openai`` pointed at a server on
    the user's network, with or without a key) behaves exactly as before."""

    @pytest.mark.parametrize("with_key", [True, False])
    def test_existing_openai_with_base_url_is_unchanged(
        self,
        admin_client: TestClient,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
        with_key: bool,
    ) -> None:
        _seed_ledger(admin_client)
        before = {
            "ai_provider": "openai",
            "ai_base_url": "http://192.168.1.20:11434/v1/",
            "ai_model": "llama3.1:8b",
        }
        if with_key:
            before["ai_api_key"] = STORED_KEY
        # The rows a pre-#640 install holds, written as that version did.
        appsettings.set_values(db_session, before, actor_token_id=None)
        db_session.commit()
        appsettings.invalidate()

        body = admin_client.get("/config").json()
        assert body["values"]["ai_provider"] == "openai"
        assert body["values"]["ai_base_url"] == "http://192.168.1.20:11434/v1/"
        assert body["values"]["ai_model"] == "llama3.1:8b"
        assert body["values"]["ai_timeout_seconds"] == 30
        assert body["ai_api_key_set"] is with_key
        assert sorted(body["overridden"]) == sorted(before)

        server = FakeOpenAIServer(reply='{"category": "expenses.dining", "confidence": 0.8}')
        _patch_http(monkeypatch, server)
        assert admin_client.post("/categorize/run").json()["ai"] == 1
        (request,) = server.completions
        assert str(request.url) == "http://192.168.1.20:11434/v1/chat/completions"
        assert request.headers.get("authorization") == (
            f"Bearer {STORED_KEY}" if with_key else None
        )
        sent = json.loads(request.content)
        assert set(sent) == {"model", "messages", "temperature"}
        assert (sent["model"], sent["temperature"]) == ("llama3.1:8b", 0)
        assert request.extensions["timeout"]["read"] == 30.0
