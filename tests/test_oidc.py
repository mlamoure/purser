"""OIDC round-trip against a real (in-process, over-HTTP) mock OpenID Provider."""

import hashlib
import hmac
import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from joserfc.jwk import ECKey, RSAKey
from sqlalchemy import Engine, text

import purser.config
from purser.admin import oidc as oidc_module
from purser.admin.auth import SESSION_COOKIE
from purser.admin.oidc import STATE_COOKIE, LoginAttempt, OidcClient
from tests import mock_oidc
from tests.conftest import ADMIN_CREDENTIALS


@pytest.fixture(scope="module")
def op() -> Iterator[mock_oidc.MockOP]:
    server = mock_oidc.MockOP()
    server.start()
    yield server
    server.stop()


@contextmanager
def _oidc_env(
    op: mock_oidc.MockOP, *, auto_provision: bool, public_url: str | None = None
) -> Iterator[None]:
    os.environ["PURSER_OIDC_ISSUER"] = op.issuer
    os.environ["PURSER_OIDC_CLIENT_ID"] = mock_oidc.CLIENT_ID
    os.environ["PURSER_OIDC_CLIENT_SECRET"] = mock_oidc.CLIENT_SECRET
    if auto_provision:
        os.environ["PURSER_OIDC_AUTO_PROVISION"] = "true"
    if public_url is not None:
        os.environ["PURSER_PUBLIC_URL"] = public_url
    purser.config.get_settings.cache_clear()
    try:
        yield
    finally:
        for var in (
            "PURSER_OIDC_ISSUER",
            "PURSER_OIDC_CLIENT_ID",
            "PURSER_OIDC_CLIENT_SECRET",
            "PURSER_OIDC_AUTO_PROVISION",
            "PURSER_PUBLIC_URL",
        ):
            os.environ.pop(var, None)
        purser.config.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_op_claims(op: mock_oidc.MockOP) -> Iterator[None]:
    yield
    op.reset()


@pytest.fixture
def oidc_client(
    op: mock_oidc.MockOP, clean_db: None, migrated_engine: Engine
) -> Iterator[TestClient]:
    """Client with auto-provisioning ON — the pre-#211 open behavior."""
    with _oidc_env(op, auto_provision=True):
        from purser.api.app import create_app

        with TestClient(create_app()) as client:
            yield client


@pytest.fixture
def oidc_client_no_provision(
    op: mock_oidc.MockOP, clean_db: None, migrated_engine: Engine
) -> Iterator[TestClient]:
    """Client with the #211 default: unknown subjects are refused."""
    with _oidc_env(op, auto_provision=False):
        from purser.api.app import create_app

        with TestClient(create_app()) as client:
            yield client


def _through_op(authorize_url: str) -> str:
    """Follow the authorize redirect at the mock OP (real HTTP), as a browser
    would, and return the app callback path it sends the user back to."""
    resp = httpx.get(authorize_url, follow_redirects=False)
    assert resp.status_code == 302, resp.text
    back = urlparse(resp.headers["location"])
    assert back.path == "/auth/oidc/callback"
    return f"{back.path}?{back.query}"


def _start(client: TestClient) -> str:
    """Begin a sign-in and return the callback path the OP redirects to."""
    resp = client.get("/auth/oidc/login", follow_redirects=False)
    assert resp.status_code == 307, resp.text
    return _through_op(resp.headers["location"])


def _callback(client: TestClient) -> "httpx.Response":
    return client.get(_start(client), follow_redirects=False)


def test_oidc_round_trip(oidc_client: TestClient, op: mock_oidc.MockOP) -> None:
    # 1. Login redirects to the OP's authorize endpoint with state.
    resp = oidc_client.get("/auth/oidc/login", follow_redirects=False)
    assert resp.status_code == 307
    authorize = urlparse(resp.headers["location"])
    assert f"{authorize.scheme}://{authorize.netloc}" == op.issuer
    params = parse_qs(authorize.query)
    state = params["state"][0]
    assert params["response_type"] == ["code"]
    assert params["client_id"] == [mock_oidc.CLIENT_ID]

    # 2. The OP redirects back with a code; the app exchanges it (real HTTP),
    #    validates the id_token against the OP's JWKS, and creates a session.
    callback = _through_op(resp.headers["location"])
    assert parse_qs(urlparse(callback).query)["state"] == [state]
    resp = oidc_client.get(callback, follow_redirects=False)
    assert resp.status_code == 303, resp.text

    # 3. The session works and the user was provisioned from the id_token.
    me = oidc_client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["email"] == mock_oidc.EMAIL
    assert me.json()["user"]["has_oidc"] is True


def test_oidc_state_mismatch_rejected(oidc_client: TestClient, op: mock_oidc.MockOP) -> None:
    resp = oidc_client.get("/auth/oidc/login", follow_redirects=False)
    assert resp.status_code == 307
    resp = oidc_client.get(
        f"/auth/oidc/callback?code={mock_oidc.VALID_CODE}&state=forged-state",
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_oidc_disabled_returns_404(clean_db: None, client: TestClient) -> None:
    resp = client.get("/auth/oidc/login", follow_redirects=False)
    assert resp.status_code == 404


class TestProvisioningGate:
    """#211: unknown subjects are refused unless oidc_auto_provision is on."""

    def test_unknown_subject_refused_by_default(self, oidc_client_no_provision: TestClient) -> None:
        resp = _callback(oidc_client_no_provision)
        assert resp.status_code == 403
        assert "not provisioned" in resp.text
        assert oidc_client_no_provision.get("/auth/me").status_code == 401

    def test_pre_created_user_still_logs_in_without_provisioning(
        self, oidc_client_no_provision: TestClient, migrated_engine: Engine
    ) -> None:
        # The admin pre-creates the user; the verified email links on first login.
        oidc_client_no_provision.post(
            "/auth/setup", json={"email": mock_oidc.EMAIL, "password": "correct-horse-battery"}
        )
        oidc_client_no_provision.cookies.clear()
        resp = _callback(oidc_client_no_provision)
        assert resp.status_code == 303, resp.text
        me = oidc_client_no_provision.get("/auth/me")
        assert me.json()["user"]["has_oidc"] is True
        with migrated_engine.connect() as conn:
            linked = conn.execute(
                text("SELECT COUNT(*) FROM audit_log WHERE action = 'user.link_oidc'")
            ).scalar_one()
        assert linked == 1


class TestEmailVerification:
    """#211: linking and provisioning both require the OP to assert
    email_verified — an unverified email must never take over an account."""

    def test_unverified_email_does_not_link(
        self, oidc_client: TestClient, op: mock_oidc.MockOP
    ) -> None:
        oidc_client.post(
            "/auth/setup", json={"email": mock_oidc.EMAIL, "password": "correct-horse-battery"}
        )
        oidc_client.cookies.clear()
        op.extra_claims = {"email_verified": False}
        resp = _callback(oidc_client)
        assert resp.status_code == 403
        assert "email_verified" in resp.text
        # The local account was not linked.
        oidc_client.post(
            "/auth/login",
            json={"email": mock_oidc.EMAIL, "password": "correct-horse-battery"},
        )
        assert oidc_client.get("/auth/me").json()["user"]["has_oidc"] is False

    def test_missing_email_verified_claim_does_not_provision(
        self, oidc_client: TestClient, op: mock_oidc.MockOP
    ) -> None:
        # Auto-provisioning is ON here, but the claim is absent entirely.
        op.extra_claims = {}
        resp = _callback(oidc_client)
        assert resp.status_code == 403
        assert oidc_client.get("/auth/me").status_code == 401


def test_oidc_round_trip_configured_via_config_api(
    op: mock_oidc.MockOP, clean_db: None, client: TestClient
) -> None:
    """The full login flow works when OIDC is configured entirely through
    /config (DB overrides, no PURSER_OIDC_* env) — issue #105. The client
    is built per request, so the change applies live, no restart."""
    from purser import appsettings

    assert client.post("/auth/setup", json=ADMIN_CREDENTIALS).status_code == 201
    assert client.get("/auth/oidc/login", follow_redirects=False).status_code == 404

    resp = client.put(
        "/config",
        json={
            "oidc_issuer": op.issuer,
            "oidc_client_id": mock_oidc.CLIENT_ID,
            "oidc_client_secret": mock_oidc.CLIENT_SECRET,
            # The subject below is unknown, so this flow also needs the
            # (runtime-editable) #211 provisioning gate opened.
            "oidc_auto_provision": True,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["values"]["oidc_enabled"] is True
    assert resp.json()["values"]["oidc_auto_provision"] is True

    login = client.get("/auth/oidc/login", follow_redirects=False)
    assert login.status_code == 307
    authorize = urlparse(login.headers["location"])
    assert f"{authorize.scheme}://{authorize.netloc}" == op.issuer
    callback = client.get(_through_op(login.headers["location"]), follow_redirects=False)
    assert callback.status_code == 303, callback.text

    me = client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["email"] == mock_oidc.EMAIL
    assert me.json()["user"]["has_oidc"] is True

    appsettings.invalidate()  # don't leak this module's overrides cache


def test_auth_methods_public_default(clean_db: None, client: TestClient) -> None:
    """The login page's pre-auth probe: password always on, SSO off until
    OIDC is configured. No session required."""
    resp = client.get("/auth/methods")
    assert resp.status_code == 200
    assert resp.json() == {"password": True, "oidc": False}


def test_auth_methods_reflects_runtime_config(
    op: mock_oidc.MockOP, admin_client: TestClient
) -> None:
    """Flipping OIDC on through /config shows up on the public probe live,
    and the probe needs no session cookie."""
    from purser import appsettings

    resp = admin_client.put(
        "/config",
        json={
            "oidc_issuer": op.issuer,
            "oidc_client_id": mock_oidc.CLIENT_ID,
            "oidc_client_secret": mock_oidc.CLIENT_SECRET,
        },
    )
    assert resp.status_code == 200, resp.text

    fresh = TestClient(admin_client.app)  # no session cookie — the probe is public
    assert fresh.get("/auth/methods").json() == {"password": True, "oidc": True}

    appsettings.invalidate()  # don't leak this module's overrides cache


# --------------------------------------------------------------------------
# #429 / #416: OIDC validation in depth. Each check below is a regression
# test against the mock OP; tokens are crafted with joserfc or by hand.
# --------------------------------------------------------------------------

DROP = mock_oidc.DROP


def _signed_in(client: TestClient) -> bool:
    return client.get("/auth/me").status_code == 200


def _segment(obj: Any) -> str:
    return mock_oidc.b64url(json.dumps(obj).encode())


def _hs256(claims: dict[str, Any], secret: bytes) -> str:
    """An HS256 token, signed by hand: joserfc refuses to import a PEM as an
    HMAC secret, which is exactly the key-confusion attempt under test."""
    signing_input = f"{_segment({'alg': 'HS256', 'kid': mock_oidc.KID})}.{_segment(claims)}"
    sig = hmac.new(secret, signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{mock_oidc.b64url(sig)}"


def _manual_rs256(op: mock_oidc.MockOP, payload: Any, extra_header: dict[str, Any]) -> str:
    """RS256 over an arbitrary header and payload (joserfc's encoder checks
    headers and wants a dict payload; these tests need neither)."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    header = {"alg": "RS256", "kid": mock_oidc.KID, **extra_header}
    signing_input = f"{_segment(header)}.{_segment(payload)}".encode()
    private = serialization.load_pem_private_key(op.key.as_pem(private=True), password=None)
    assert isinstance(private, rsa.RSAPrivateKey)
    sig = private.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input.decode()}.{mock_oidc.b64url(sig)}"


def _with(**changes: Any) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Claims mutation: set each change; DROP removes the claim."""

    def mutate(claims: dict[str, Any]) -> dict[str, Any]:
        out = {**claims, **changes}
        return {k: v for k, v in out.items() if v is not DROP}

    return mutate


# Claim sets the app must refuse (OIDC Core 3.1.3.7), keyed by test id.
REJECTED_CLAIMS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    # 5 s ago is inside the iat/nbf skew allowance: exp gets no leeway.
    "expired": lambda c: {**c, "exp": c["iat"] - 5},
    "exp_missing": _with(exp=DROP),
    "exp_not_numeric": _with(exp="tomorrow"),
    "iat_missing": _with(iat=DROP),
    "iat_far_future": lambda c: {**c, "iat": c["iat"] + 600, "exp": c["exp"] + 600},
    "nbf_future": lambda c: {**c, "nbf": c["iat"] + 600},
    "aud_other_client": _with(aud="another-client"),
    "aud_missing": _with(aud=DROP),
    "aud_multi_without_azp": _with(aud=[mock_oidc.CLIENT_ID, "another-client"]),
    "azp_other_client": _with(azp="another-client"),
    "iss_other": _with(iss="https://op.example.com"),
    "iss_missing": _with(iss=DROP),
    "sub_missing": _with(sub=DROP),
    "sub_empty": _with(sub=""),
    "sub_not_string": _with(sub=1234),
    "nonce_missing": _with(nonce=DROP),
    "nonce_other": _with(nonce="nonce-from-another-login"),
}

# Variations a compliant provider may produce, which the app must accept.
ACCEPTED_CLAIMS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "iat_within_clock_skew": lambda c: {**c, "iat": c["iat"] + 30},
    "aud_multi_with_azp": _with(
        aud=[mock_oidc.CLIENT_ID, "another-client"], azp=mock_oidc.CLIENT_ID
    ),
    "aud_list_single": _with(aud=[mock_oidc.CLIENT_ID]),
    "azp_self": _with(azp=mock_oidc.CLIENT_ID),
    "nbf_past": lambda c: {**c, "nbf": c["iat"] - 10},
}


class TestIdTokenClaims:
    @pytest.mark.parametrize("case", sorted(REJECTED_CLAIMS))
    def test_rejected(self, case: str, oidc_client: TestClient, op: mock_oidc.MockOP) -> None:
        mutate = REJECTED_CLAIMS[case]
        op.id_token_factory = lambda c: op.sign(mutate(c))
        resp = _callback(oidc_client)
        assert resp.status_code == 401, resp.text
        assert resp.json()["detail"] == "OIDC id_token validation failed"
        assert not _signed_in(oidc_client)

    @pytest.mark.parametrize("case", sorted(ACCEPTED_CLAIMS))
    def test_accepted(self, case: str, oidc_client: TestClient, op: mock_oidc.MockOP) -> None:
        mutate = ACCEPTED_CLAIMS[case]
        op.id_token_factory = lambda c: op.sign(mutate(c))
        resp = _callback(oidc_client)
        assert resp.status_code == 303, resp.text
        assert _signed_in(oidc_client)


class TestIdTokenSignature:
    """Algorithm allow-list (RS256, ES256), key selection by kid, key type."""

    def _rejects(self, client: TestClient) -> None:
        resp = _callback(client)
        assert resp.status_code == 401, resp.text
        assert not _signed_in(client)

    def test_alg_none_rejected(self, oidc_client: TestClient, op: mock_oidc.MockOP) -> None:
        op.id_token_factory = lambda c: f"{_segment({'alg': 'none'})}.{_segment(c)}."
        self._rejects(oidc_client)

    def test_hs256_keyed_with_the_rsa_public_key_rejected(
        self, oidc_client: TestClient, op: mock_oidc.MockOP
    ) -> None:
        pem = op.key.as_pem(private=False)
        op.id_token_factory = lambda c: _hs256(c, pem)
        self._rejects(oidc_client)

    def test_hs256_keyed_with_the_client_secret_rejected(
        self, oidc_client: TestClient, op: mock_oidc.MockOP
    ) -> None:
        op.id_token_factory = lambda c: _hs256(c, mock_oidc.CLIENT_SECRET.encode())
        self._rejects(oidc_client)

    @pytest.mark.parametrize("alg", ["RS384", "RS512", "PS256"])
    def test_rsa_alg_outside_allow_list_rejected(
        self, alg: str, oidc_client: TestClient, op: mock_oidc.MockOP
    ) -> None:
        op.id_token_factory = lambda c: op.sign(c, alg=alg)
        self._rejects(oidc_client)

    def test_es256_with_ec_key_accepted(
        self, oidc_client: TestClient, op: mock_oidc.MockOP
    ) -> None:
        ec = ECKey.generate_key("P-256", parameters={"kid": "ec-key"})
        op.published_keys = [op.key_dict, ec.as_dict(private=False)]
        op.id_token_factory = lambda c: op.sign(c, key=ec, alg="ES256")
        assert _callback(oidc_client).status_code == 303
        assert _signed_in(oidc_client)

    def test_es256_header_naming_the_rsa_key_rejected(
        self, oidc_client: TestClient, op: mock_oidc.MockOP
    ) -> None:
        ec = ECKey.generate_key("P-256")
        op.id_token_factory = lambda c: op.sign(
            c, key=ec, alg="ES256", header={"kid": mock_oidc.KID}
        )
        self._rejects(oidc_client)

    def test_unknown_kid_rejected(self, oidc_client: TestClient, op: mock_oidc.MockOP) -> None:
        op.id_token_factory = lambda c: op.sign(c, header={"kid": "not-published"})
        self._rejects(oidc_client)

    def test_other_key_reusing_the_kid_rejected(
        self, oidc_client: TestClient, op: mock_oidc.MockOP
    ) -> None:
        impostor = RSAKey.generate_key(2048, parameters={"kid": mock_oidc.KID})
        op.id_token_factory = lambda c: op.sign(c, key=impostor)
        self._rejects(oidc_client)

    def test_tampered_payload_rejected(self, oidc_client: TestClient, op: mock_oidc.MockOP) -> None:
        def tamper(c: dict[str, Any]) -> str:
            header, _payload, sig = op.sign(c).split(".")
            return f"{header}.{_segment({**c, 'sub': 'someone-else'})}.{sig}"

        op.id_token_factory = tamper
        self._rejects(oidc_client)

    def test_missing_kid_with_one_published_key_accepted(
        self, oidc_client: TestClient, op: mock_oidc.MockOP
    ) -> None:
        op.id_token_factory = lambda c: op.sign(c, header={"kid": DROP})
        assert _callback(oidc_client).status_code == 303

    def test_missing_kid_with_several_published_keys_rejected(
        self, oidc_client: TestClient, op: mock_oidc.MockOP
    ) -> None:
        other = RSAKey.generate_key(2048, parameters={"kid": "second-key"})
        op.published_keys = [op.key_dict, other.as_dict(private=False)]
        op.id_token_factory = lambda c: op.sign(c, header={"kid": DROP})
        self._rejects(oidc_client)

    def test_key_published_for_encryption_only_rejected(
        self, oidc_client: TestClient, op: mock_oidc.MockOP
    ) -> None:
        op.published_keys = [{**op.key_dict, "use": "enc"}]
        self._rejects(oidc_client)

    def test_unknown_crit_header_rejected(
        self, oidc_client: TestClient, op: mock_oidc.MockOP
    ) -> None:
        op.id_token_factory = lambda c: _manual_rs256(op, c, {"crit": ["x-ext"], "x-ext": 1})
        self._rejects(oidc_client)

    def test_unknown_non_critical_header_ignored(
        self, oidc_client: TestClient, op: mock_oidc.MockOP
    ) -> None:
        op.id_token_factory = lambda c: _manual_rs256(op, c, {"x-ext": 1})
        assert _callback(oidc_client).status_code == 303

    @pytest.mark.parametrize("token", ["not-a-jwt", "a.b.c", "a.b.c.d.e"])
    def test_malformed_token_rejected(
        self, token: str, oidc_client: TestClient, op: mock_oidc.MockOP
    ) -> None:
        # "a.b.c.d.e" is the JWE compact shape: never decrypted, refused.
        op.id_token_factory = lambda c: token
        self._rejects(oidc_client)

    def test_payload_not_a_json_object_rejected(
        self, oidc_client: TestClient, op: mock_oidc.MockOP
    ) -> None:
        op.id_token_factory = lambda c: _manual_rs256(op, [c], {})
        self._rejects(oidc_client)


class TestKeyRotation:
    """Discovery and the JWKS are fetched per sign-in: no cache to go stale."""

    def test_rotated_key_accepted_on_next_login(
        self, oidc_client: TestClient, op: mock_oidc.MockOP
    ) -> None:
        assert _callback(oidc_client).status_code == 303
        oidc_client.cookies.clear()
        op.key = RSAKey.generate_key(2048, parameters={"kid": "rotated-key"})
        assert _callback(oidc_client).status_code == 303
        assert _signed_in(oidc_client)

    def test_retired_key_rejected_after_rotation(
        self, oidc_client: TestClient, op: mock_oidc.MockOP
    ) -> None:
        retired = op.key
        op.key = RSAKey.generate_key(2048, parameters={"kid": "rotated-key"})
        op.id_token_factory = lambda c: op.sign(c, key=retired)
        assert _callback(oidc_client).status_code == 401


class TestProviderFailures:
    """The OP unreachable or misbehaving: a fixed 502, never a 500."""

    def _login_status(self, client: TestClient) -> int:
        return client.get("/auth/oidc/login", follow_redirects=False).status_code

    def test_discovery_issuer_mismatch(self, oidc_client: TestClient, op: mock_oidc.MockOP) -> None:
        op.discovery_overrides = {"issuer": "https://op.example.com"}
        assert self._login_status(oidc_client) == 502

    def test_discovery_issuer_trailing_slash_tolerated(
        self, oidc_client: TestClient, op: mock_oidc.MockOP
    ) -> None:
        # The provider's issuer carries a slash the configured value lacks;
        # the id_token's iss must then equal the provider's exact value.
        op.discovery_overrides = {"issuer": f"{op.issuer}/"}
        op.extra_claims = {"email_verified": True, "iss": f"{op.issuer}/"}
        assert _callback(oidc_client).status_code == 303

    @pytest.mark.parametrize("field", ["authorization_endpoint", "token_endpoint", "jwks_uri"])
    def test_discovery_missing_endpoint(
        self, field: str, oidc_client: TestClient, op: mock_oidc.MockOP
    ) -> None:
        op.discovery_overrides = {field: None}
        assert self._login_status(oidc_client) == 502

    def test_discovery_endpoint_with_non_http_scheme(
        self, oidc_client: TestClient, op: mock_oidc.MockOP
    ) -> None:
        op.discovery_overrides = {"jwks_uri": "file:///etc/hostname"}
        assert self._login_status(oidc_client) == 502

    def test_discovery_redirect_not_followed(
        self, oidc_client: TestClient, op: mock_oidc.MockOP
    ) -> None:
        op.discovery_redirect = f"{op.issuer}/redirected-discovery"
        assert self._login_status(oidc_client) == 502

    def test_jwks_unavailable(self, oidc_client: TestClient, op: mock_oidc.MockOP) -> None:
        op.jwks_status = 500
        resp = _callback(oidc_client)
        assert resp.status_code == 502
        assert not _signed_in(oidc_client)

    @pytest.mark.parametrize(
        "keys",
        [[], [{"kty": "unknown-kty", "kid": "x"}], [{"kty": "RSA", "kid": "x"}]],
        ids=["empty", "unknown-kty", "rsa-without-params"],
    )
    def test_jwks_without_usable_keys(
        self, keys: list[dict[str, Any]], oidc_client: TestClient, op: mock_oidc.MockOP
    ) -> None:
        op.published_keys = keys
        resp = _callback(oidc_client)
        assert resp.status_code == 502, resp.text
        assert not _signed_in(oidc_client)

    @pytest.mark.parametrize(
        "body", [{"access_token": "x"}, {"id_token": 123}], ids=["absent", "not-a-string"]
    )
    def test_token_response_without_id_token(
        self, body: dict[str, Any], oidc_client: TestClient, op: mock_oidc.MockOP
    ) -> None:
        op.token_response = body
        resp = _callback(oidc_client)
        assert resp.status_code == 502
        assert not _signed_in(oidc_client)


class TestLoginRequestBinding:
    """state (login CSRF), nonce (id_token replay) and PKCE (code interception)."""

    def _cookie(self, client: TestClient) -> str:
        value = client.cookies.get(STATE_COOKIE)
        assert value is not None
        return value

    def _with_cookie(self, client: TestClient, path: str, cookie: str) -> httpx.Response:
        client.cookies.clear()
        return client.get(
            path, headers={"Cookie": f"{STATE_COOKIE}={cookie}"}, follow_redirects=False
        )

    def test_authorize_request_carries_nonce_and_s256_challenge(
        self, oidc_client: TestClient
    ) -> None:
        resp = oidc_client.get("/auth/oidc/login", follow_redirects=False)
        params = parse_qs(urlparse(resp.headers["location"]).query)
        state, nonce, verifier = self._cookie(oidc_client).split(".")
        assert params["state"] == [state]
        assert params["nonce"] == [nonce]
        assert params["code_challenge_method"] == ["S256"]
        digest = hashlib.sha256(verifier.encode()).digest()
        assert params["code_challenge"] == [mock_oidc.b64url(digest)]
        assert params["redirect_uri"] == ["http://localhost:8000/auth/oidc/callback"]
        assert params["scope"] == ["openid email profile"]
        # Fresh secrets on every attempt.
        again = oidc_client.get("/auth/oidc/login", follow_redirects=False)
        assert parse_qs(urlparse(again.headers["location"]).query)["nonce"] != [nonce]

    def test_token_request_sends_the_code_verifier(
        self, oidc_client: TestClient, op: mock_oidc.MockOP
    ) -> None:
        callback = _start(oidc_client)
        verifier = self._cookie(oidc_client).split(".")[2]
        assert oidc_client.get(callback, follow_redirects=False).status_code == 303
        assert op.token_requests[-1]["code_verifier"] == verifier

    def test_state_cookie_attributes(self, oidc_client: TestClient) -> None:
        resp = oidc_client.get("/auth/oidc/login", follow_redirects=False)
        cookie = resp.headers["set-cookie"].lower()
        assert cookie.startswith(f"{STATE_COOKIE}=")
        for attr in ("httponly", "samesite=lax", "path=/auth/oidc", "max-age=600"):
            assert attr in cookie

    def test_state_cookie_cleared_after_sign_in(self, oidc_client: TestClient) -> None:
        resp = _callback(oidc_client)
        assert resp.status_code == 303
        cleared = [h for h in resp.headers.get_list("set-cookie") if h.startswith(STATE_COOKIE)]
        assert cleared
        assert "max-age=0" in cleared[0].lower()

    def test_missing_state_cookie_rejected(self, oidc_client: TestClient) -> None:
        callback = _start(oidc_client)
        oidc_client.cookies.clear()
        assert oidc_client.get(callback, follow_redirects=False).status_code == 400
        assert not _signed_in(oidc_client)

    @pytest.mark.parametrize("cookie", ["only-state", "a.b", "a..c", "a.b.c.d"])
    def test_malformed_state_cookie_rejected(self, cookie: str, oidc_client: TestClient) -> None:
        callback = _start(oidc_client)
        assert self._with_cookie(oidc_client, callback, cookie).status_code == 400

    def test_non_ascii_state_rejected(self, oidc_client: TestClient) -> None:
        oidc_client.get("/auth/oidc/login", follow_redirects=False)
        resp = oidc_client.get(
            "/auth/oidc/callback?code=x&state=%C3%A9t%C3%A9", follow_redirects=False
        )
        assert resp.status_code == 400

    def test_tampered_code_verifier_fails_the_exchange(self, oidc_client: TestClient) -> None:
        callback = _start(oidc_client)
        state, nonce, _verifier = self._cookie(oidc_client).split(".")
        resp = self._with_cookie(oidc_client, callback, f"{state}.{nonce}.{'x' * 64}")
        assert resp.status_code == 502  # the provider refuses the PKCE check
        assert not _signed_in(oidc_client)

    def test_tampered_nonce_rejects_the_id_token(self, oidc_client: TestClient) -> None:
        callback = _start(oidc_client)
        state, _nonce, verifier = self._cookie(oidc_client).split(".")
        resp = self._with_cookie(oidc_client, callback, f"{state}.other-nonce.{verifier}")
        assert resp.status_code == 401
        assert not _signed_in(oidc_client)

    def test_callback_from_another_browser_rejected(self, oidc_client: TestClient) -> None:
        # Login CSRF: an attacker's own callback URL, opened in a victim's
        # browser that holds a different sign-in attempt.
        attacker_callback = _start(oidc_client)
        oidc_client.cookies.clear()
        oidc_client.get("/auth/oidc/login", follow_redirects=False)  # victim's attempt
        resp = oidc_client.get(attacker_callback, follow_redirects=False)
        assert resp.status_code == 400
        assert not _signed_in(oidc_client)

    def test_provider_error_redirect(self, oidc_client: TestClient) -> None:
        oidc_client.get("/auth/oidc/login", follow_redirects=False)
        resp = oidc_client.get(
            "/auth/oidc/callback?error=access_denied&error_description=%3Cscript%3E",
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "script" not in resp.text
        assert not _signed_in(oidc_client)

    def test_missing_code_rejected(self, oidc_client: TestClient) -> None:
        resp = oidc_client.get("/auth/oidc/login", follow_redirects=False)
        state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]
        resp = oidc_client.get(f"/auth/oidc/callback?state={state}", follow_redirects=False)
        assert resp.status_code == 400

    def test_session_fixation(self, oidc_client: TestClient) -> None:
        """A session id planted before sign-in is never adopted: login mints a
        new one and only that one authenticates."""
        oidc_client.cookies.set(SESSION_COOKIE, "planted-session-value")
        resp = _callback(oidc_client)
        assert resp.status_code == 303
        minted = [h for h in resp.headers.get_list("set-cookie") if h.startswith(SESSION_COOKIE)]
        assert minted
        assert "planted-session-value" not in minted[0]
        fresh = TestClient(oidc_client.app)
        fresh.cookies.set(SESSION_COOKIE, "planted-session-value")
        assert fresh.get("/auth/me").status_code == 401


def test_rejection_logs_the_check_not_the_token(
    oidc_client: TestClient, op: mock_oidc.MockOP, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, dict[str, Any]]] = []

    class Recorder:
        def warning(self, event: str, **kw: Any) -> None:
            events.append((event, kw))

    monkeypatch.setattr(oidc_module, "log", Recorder())
    issued: list[str] = []

    def factory(c: dict[str, Any]) -> str:
        issued.append(op.sign({**c, "aud": "another-client"}))
        return issued[-1]

    op.id_token_factory = factory
    callback = _start(oidc_client)
    secrets_in_flight = [
        issued_part
        for issued_part in (oidc_client.cookies.get(STATE_COOKIE) or "").split(".")
        if issued_part
    ]
    resp = oidc_client.get(callback, follow_redirects=False)
    assert resp.status_code == 401
    secrets_in_flight += [issued[0], parse_qs(urlparse(callback).query)["code"][0]]
    assert [e for e, _ in events] == ["oidc.id_token_rejected"]
    assert events[0][1]["reason"] == "InvalidClaimError"
    for secret in secrets_in_flight:
        assert secret not in repr(events)
        assert secret not in resp.text


class TestClaimsToAccount:
    def test_email_verified_must_be_boolean_true(
        self, oidc_client: TestClient, op: mock_oidc.MockOP
    ) -> None:
        op.extra_claims = {"email_verified": "true"}
        assert _callback(oidc_client).status_code == 403
        assert not _signed_in(oidc_client)

    def test_email_must_be_a_string(self, oidc_client: TestClient, op: mock_oidc.MockOP) -> None:
        op.extra_claims = {"email_verified": True, "email": ["oidc-user@example.com"]}
        assert _callback(oidc_client).status_code == 502
        assert not _signed_in(oidc_client)

    def test_non_string_name_is_ignored(
        self, oidc_client: TestClient, op: mock_oidc.MockOP
    ) -> None:
        op.extra_claims = {"email_verified": True, "name": {"given": "OIDC"}}
        assert _callback(oidc_client).status_code == 303
        assert oidc_client.get("/auth/me").json()["user"]["display_name"] is None

    def test_auto_provisioned_user_is_a_member(self, oidc_client: TestClient) -> None:
        assert _callback(oidc_client).status_code == 303
        assert oidc_client.get("/auth/me").json()["user"]["role"] == "member"

    def test_linked_subject_keeps_its_account_when_the_email_changes(
        self, oidc_client: TestClient, op: mock_oidc.MockOP
    ) -> None:
        assert _callback(oidc_client).status_code == 303
        oidc_client.cookies.clear()
        op.extra_claims = {"email_verified": False, "email": "renamed@example.com"}
        assert _callback(oidc_client).status_code == 303
        assert oidc_client.get("/auth/me").json()["user"]["email"] == mock_oidc.EMAIL

    def test_new_subject_from_the_same_provider_is_refused_audited(
        self, oidc_client: TestClient, op: mock_oidc.MockOP, migrated_engine: Engine
    ) -> None:
        """#429 D2, changed deliberately (it used to relink): a second subject
        of the linked provider asserting the same verified email is refused,
        the refusal is audited, and the link stays as it was."""
        assert _callback(oidc_client).status_code == 303
        oidc_client.cookies.clear()
        op.extra_claims = {"email_verified": True, "sub": "oidc-user-5678"}
        resp = _callback(oidc_client)
        assert resp.status_code == 403
        assert resp.json()["detail"] == (
            "this account is linked to a different SSO identity; ask an administrator to unlink it"
        )
        assert not _signed_in(oidc_client)
        assert _link(migrated_engine) == (oidc_module.issuer_key(op.issuer), mock_oidc.SUBJECT)
        assert _actions(migrated_engine, "user.link_oidc") == 0
        assert _actions(migrated_engine, "user.link_oidc_refused") == 1


def test_oidc_settings_are_admin_only(admin_client: TestClient, op: mock_oidc.MockOP) -> None:
    resp = admin_client.post("/tokens", json={"name": "agent", "scopes": ["read", "write"]})
    headers = {"Authorization": f"Bearer {resp.json()['token']}"}
    for body in (
        {"oidc_issuer": op.issuer},
        {"oidc_client_id": "x"},
        {"oidc_client_secret": "x"},
        {"oidc_auto_provision": True},
    ):
        assert admin_client.put("/config", json=body, headers=headers).status_code == 403
    assert admin_client.delete("/config/oidc_issuer", headers=headers).status_code == 403


# Unit level: the outbound HTTP client, through httpx.MockTransport.


def _settings(issuer: str | None = "https://op.example.com") -> purser.config.Settings:
    return purser.config.Settings(
        oidc_issuer=issuer, oidc_client_id="purser-test", oidc_client_secret="s3cret"
    )


def _discovery(issuer: str, **overrides: Any) -> dict[str, Any]:
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/authorize",
        "token_endpoint": f"{issuer}/token",
        "jwks_uri": f"{issuer}/jwks",
        **overrides,
    }


def _client_for(handler: Callable[[httpx.Request], httpx.Response]) -> OidcClient:
    return OidcClient(_settings(), transport=httpx.MockTransport(handler))


def _status(fn: Callable[[], object]) -> int:
    with pytest.raises(HTTPException) as exc:
        fn()
    return exc.value.status_code


class TestOutboundRequests:
    def test_https_issuer_refuses_plain_http_endpoints(self) -> None:
        doc = _discovery("https://op.example.com", token_endpoint="http://op.example.com/token")
        client = _client_for(lambda r: httpx.Response(200, json=doc))
        assert _status(client.metadata) == 502

    def test_timeout_maps_to_502(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow provider", request=request)

        assert _status(_client_for(handler).metadata) == 502

    @pytest.mark.parametrize(
        "response",
        [
            httpx.Response(200, text="<html>not json</html>"),
            httpx.Response(200, json=["not", "an", "object"]),
            httpx.Response(404, json={}),
            httpx.Response(301, headers={"Location": "https://elsewhere.example.com/"}),
        ],
        ids=["html", "array", "404", "redirect"],
    )
    def test_bad_discovery_responses_map_to_502(self, response: httpx.Response) -> None:
        assert _status(_client_for(lambda r: response).metadata) == 502

    def test_requests_carry_the_timeout(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=_discovery("https://op.example.com"))

        _client_for(handler).metadata()
        assert seen[0].extensions["timeout"]["read"] == oidc_module.HTTP_TIMEOUT_SECONDS
        assert str(seen[0].url) == "https://op.example.com/.well-known/openid-configuration"

    def test_authorize_url_keeps_the_endpoint_query(self) -> None:
        doc = _discovery(
            "https://op.example.com",
            authorization_endpoint="https://op.example.com/authorize?tenant=t1",
        )
        client = _client_for(lambda r: httpx.Response(200, json=doc))
        attempt = LoginAttempt.new()
        params = parse_qs(urlparse(client.authorize_url(attempt)).query)
        assert params["tenant"] == ["t1"]
        assert params["nonce"] == [attempt.nonce]
        assert params["code_challenge"] == [attempt.code_challenge]

    def test_disabled_settings_404(self) -> None:
        assert _status(lambda: OidcClient(_settings(issuer=None))) == 404


# --------------------------------------------------------------------------
# #429 D1-D4: links scoped by issuer, relinking only across providers, admin
# unlink, the __Host- state cookie, case-insensitive email.
# --------------------------------------------------------------------------


def _link(engine: Engine, email: str = mock_oidc.EMAIL) -> tuple[str | None, str | None]:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT oidc_issuer, oidc_subject FROM users WHERE lower(email) = lower(:e)"),
            {"e": email},
        ).one()
    return row.oidc_issuer, row.oidc_subject


def _actions(engine: Engine, action: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT COUNT(*) FROM audit_log WHERE action = :a"), {"a": action}
            ).scalar_one()
        )


def _audit_payload(engine: Engine, action: str) -> dict[str, Any]:
    from purser import crypto

    with engine.connect() as conn:
        enc = conn.execute(
            text("SELECT before_after_enc FROM audit_log WHERE action = :a"), {"a": action}
        ).scalar_one()
    payload = crypto.decrypt_json(enc)
    assert isinstance(payload, dict)
    return payload


def _user_count(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text("SELECT COUNT(*) FROM users")).scalar_one())


@pytest.fixture(scope="module")
def other_op() -> Iterator[mock_oidc.MockOP]:
    """A second provider, for moves between providers."""
    server = mock_oidc.MockOP()
    server.start()
    yield server
    server.stop()


@contextmanager
def _switch_provider(to: mock_oidc.MockOP) -> Iterator[None]:
    """Point the running instance at another provider, as an admin would."""
    previous = os.environ["PURSER_OIDC_ISSUER"]
    os.environ["PURSER_OIDC_ISSUER"] = to.issuer
    purser.config.get_settings.cache_clear()
    try:
        yield
    finally:
        os.environ["PURSER_OIDC_ISSUER"] = previous
        purser.config.get_settings.cache_clear()
        to.reset()


class TestLinksScopedByIssuer:
    """D1: an SSO link is the pair (issuer, subject)."""

    def test_sign_in_records_the_issuer(
        self, oidc_client: TestClient, op: mock_oidc.MockOP, migrated_engine: Engine
    ) -> None:
        assert _callback(oidc_client).status_code == 303
        assert _link(migrated_engine) == (op.issuer, mock_oidc.SUBJECT)
        assert oidc_client.get("/auth/me").json()["user"]["oidc_issuer"] == op.issuer

    def test_issuer_is_stored_without_its_trailing_slash(
        self, oidc_client: TestClient, op: mock_oidc.MockOP, migrated_engine: Engine
    ) -> None:
        op.discovery_overrides = {"issuer": f"{op.issuer}/"}
        op.extra_claims = {"email_verified": True, "iss": f"{op.issuer}/"}
        assert _callback(oidc_client).status_code == 303
        assert _link(migrated_engine)[0] == op.issuer

    def test_same_subject_at_another_provider_is_another_person(
        self,
        oidc_client: TestClient,
        op: mock_oidc.MockOP,
        other_op: mock_oidc.MockOP,
        migrated_engine: Engine,
    ) -> None:
        assert _callback(oidc_client).status_code == 303
        oidc_client.cookies.clear()
        with _switch_provider(other_op):
            # The same `sub` string, a different person at the new provider.
            other_op.extra_claims = {"email_verified": True, "email": "someone@example.com"}
            assert _callback(oidc_client).status_code == 303
            me = oidc_client.get("/auth/me").json()["user"]
        assert me["email"] == "someone@example.com"  # provisioned, not a takeover
        assert _user_count(migrated_engine) == 2
        assert _link(migrated_engine) == (op.issuer, mock_oidc.SUBJECT)

    def test_legacy_link_is_claimed_by_the_configured_provider(
        self, oidc_client: TestClient, op: mock_oidc.MockOP, migrated_engine: Engine
    ) -> None:
        """A link migration 0041 could not attribute keeps working: the next
        sign-in from the configured provider records its issuer."""
        assert _callback(oidc_client).status_code == 303
        with migrated_engine.begin() as conn:
            conn.execute(text("UPDATE users SET oidc_issuer = NULL"))
        oidc_client.cookies.clear()
        assert _callback(oidc_client).status_code == 303
        assert oidc_client.get("/auth/me").json()["user"]["email"] == mock_oidc.EMAIL
        assert _link(migrated_engine) == (op.issuer, mock_oidc.SUBJECT)
        assert _audit_payload(migrated_engine, "user.oidc_issuer_recorded") == {
            "before": {"oidc_issuer": None},
            "after": {"oidc_issuer": op.issuer},
        }

    def test_legacy_link_refuses_another_subject(
        self, oidc_client: TestClient, op: mock_oidc.MockOP, migrated_engine: Engine
    ) -> None:
        assert _callback(oidc_client).status_code == 303
        with migrated_engine.begin() as conn:
            conn.execute(text("UPDATE users SET oidc_issuer = NULL"))
        oidc_client.cookies.clear()
        op.extra_claims = {"email_verified": True, "sub": "oidc-user-5678"}
        assert _callback(oidc_client).status_code == 403
        assert _link(migrated_engine) == (None, mock_oidc.SUBJECT)
        assert _actions(migrated_engine, "user.link_oidc_refused") == 1

    def test_identity_pair_is_unique(
        self, oidc_client: TestClient, migrated_engine: Engine
    ) -> None:
        from sqlalchemy.exc import IntegrityError

        assert _callback(oidc_client).status_code == 303
        with pytest.raises(IntegrityError), migrated_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (email, oidc_issuer, oidc_subject) "
                    "SELECT 'twin@example.com', oidc_issuer, oidc_subject FROM users"
                )
            )

    def test_legacy_subject_stays_unique(self, clean_db: None, migrated_engine: Engine) -> None:
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError), migrated_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (email, oidc_subject) "
                    "VALUES ('a@example.com', 'sub-1'), ('b@example.com', 'sub-1')"
                )
            )

    def test_an_issuer_needs_a_subject(self, clean_db: None, migrated_engine: Engine) -> None:
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError), migrated_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (email, oidc_issuer) "
                    "VALUES ('half@example.com', 'https://sso.example.com')"
                )
            )


class TestRelinking:
    """D2: a verified email relinks only across providers."""

    def test_unlinked_account_links_by_verified_email(
        self, oidc_client_no_provision: TestClient, op: mock_oidc.MockOP, migrated_engine: Engine
    ) -> None:
        setup = {**ADMIN_CREDENTIALS, "email": mock_oidc.EMAIL}
        assert oidc_client_no_provision.post("/auth/setup", json=setup).status_code == 201
        oidc_client_no_provision.cookies.clear()
        assert _callback(oidc_client_no_provision).status_code == 303
        assert _link(migrated_engine) == (op.issuer, mock_oidc.SUBJECT)
        assert _audit_payload(migrated_engine, "user.link_oidc")["after"] == {
            "email": mock_oidc.EMAIL,
            "issuer": op.issuer,
        }

    def test_move_to_another_provider_relinks_audited(
        self,
        oidc_client: TestClient,
        op: mock_oidc.MockOP,
        other_op: mock_oidc.MockOP,
        migrated_engine: Engine,
    ) -> None:
        assert _callback(oidc_client).status_code == 303
        oidc_client.cookies.clear()
        with _switch_provider(other_op):
            other_op.extra_claims = {"email_verified": True, "sub": "new-provider-sub"}
            assert _callback(oidc_client).status_code == 303
            me = oidc_client.get("/auth/me").json()["user"]
        assert me["email"] == mock_oidc.EMAIL
        assert _user_count(migrated_engine) == 1
        assert _link(migrated_engine) == (other_op.issuer, "new-provider-sub")
        assert _audit_payload(migrated_engine, "user.link_oidc")["after"] == {
            "email": mock_oidc.EMAIL,
            "issuer": other_op.issuer,
            "previous_issuer": op.issuer,
        }

    def test_move_to_another_provider_needs_a_verified_email(
        self,
        oidc_client: TestClient,
        op: mock_oidc.MockOP,
        other_op: mock_oidc.MockOP,
        migrated_engine: Engine,
    ) -> None:
        assert _callback(oidc_client).status_code == 303
        oidc_client.cookies.clear()
        with _switch_provider(other_op):
            other_op.extra_claims = {"email_verified": False, "sub": "new-provider-sub"}
            assert _callback(oidc_client).status_code == 403
        assert _link(migrated_engine) == (op.issuer, mock_oidc.SUBJECT)

    def test_refusal_is_audited_and_the_linked_identity_still_signs_in(
        self, oidc_client: TestClient, op: mock_oidc.MockOP, migrated_engine: Engine
    ) -> None:
        assert _callback(oidc_client).status_code == 303
        oidc_client.cookies.clear()
        op.extra_claims = {"email_verified": True, "sub": "oidc-user-5678"}
        assert _callback(oidc_client).status_code == 403
        assert _audit_payload(migrated_engine, "user.link_oidc_refused")["after"] == {
            "email": mock_oidc.EMAIL,
            "issuer": op.issuer,
            "linked_issuer": op.issuer,
        }
        op.reset()
        assert _callback(oidc_client).status_code == 303
        assert _signed_in(oidc_client)

    def test_after_an_admin_unlink_the_new_subject_links(
        self, oidc_client: TestClient, op: mock_oidc.MockOP, migrated_engine: Engine
    ) -> None:
        assert oidc_client.post("/auth/setup", json=ADMIN_CREDENTIALS).status_code == 201
        admin_cookie = oidc_client.cookies.get(SESSION_COOKIE)
        assert admin_cookie is not None
        oidc_client.cookies.clear()
        assert _callback(oidc_client).status_code == 303  # provisions + links
        user_id = oidc_client.get("/auth/me").json()["user"]["id"]
        oidc_client.cookies.clear()

        oidc_client.cookies.set(SESSION_COOKIE, admin_cookie)
        resp = oidc_client.delete(f"/users/{user_id}/oidc")
        assert resp.status_code == 200, resp.text
        assert resp.json()["has_oidc"] is False
        assert resp.json()["oidc_issuer"] is None
        oidc_client.cookies.clear()

        op.extra_claims = {"email_verified": True, "sub": "oidc-user-5678"}
        assert _callback(oidc_client).status_code == 303
        assert oidc_client.get("/auth/me").json()["user"]["id"] == user_id
        assert _link(migrated_engine) == (op.issuer, "oidc-user-5678")


class TestUnlinkRoute:
    """The admin-only "Unlink SSO" action (D2)."""

    def _linked_member(self, admin_client: TestClient, engine: Engine) -> str:
        resp = admin_client.post("/users", json={"email": "linked@example.com"})
        assert resp.status_code == 201
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE users SET oidc_issuer = 'https://sso.example.com', "
                    "oidc_subject = 'sub-1' WHERE email = 'linked@example.com'"
                )
            )
        return str(resp.json()["id"])

    def test_unlink_is_audited_with_the_admin_as_actor(
        self, admin_client: TestClient, migrated_engine: Engine
    ) -> None:
        user_id = self._linked_member(admin_client, migrated_engine)
        resp = admin_client.delete(f"/users/{user_id}/oidc")
        assert resp.status_code == 200, resp.text
        assert resp.json()["has_oidc"] is False
        assert _link(migrated_engine, "linked@example.com") == (None, None)
        listed = {u["id"]: u for u in admin_client.get("/users").json()}
        assert listed[user_id]["has_oidc"] is False
        with migrated_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT actor_token_id, entity_id FROM audit_log "
                    "WHERE action = 'user.unlink_oidc'"
                )
            ).one()
        assert str(row.entity_id) == user_id
        assert row.actor_token_id is not None
        assert _audit_payload(migrated_engine, "user.unlink_oidc") == {
            "before": {"oidc_issuer": "https://sso.example.com"},
            "after": {"oidc_issuer": None},
        }

    def test_unlinking_an_unlinked_user_is_a_conflict(
        self, admin_client: TestClient, migrated_engine: Engine
    ) -> None:
        user_id = self._linked_member(admin_client, migrated_engine)
        assert admin_client.delete(f"/users/{user_id}/oidc").status_code == 200
        assert admin_client.delete(f"/users/{user_id}/oidc").status_code == 409
        assert _actions(migrated_engine, "user.unlink_oidc") == 1

    def test_unknown_user_is_404(self, admin_client: TestClient) -> None:
        resp = admin_client.delete("/users/00000000-0000-0000-0000-000000000000/oidc")
        assert resp.status_code == 404

    def test_unlink_needs_admin_scope(
        self, admin_client: TestClient, migrated_engine: Engine
    ) -> None:
        user_id = self._linked_member(admin_client, migrated_engine)
        resp = admin_client.post("/tokens", json={"name": "agent", "scopes": ["read", "write"]})
        headers = {"Authorization": f"Bearer {resp.json()['token']}"}
        fresh = TestClient(admin_client.app)
        assert fresh.delete(f"/users/{user_id}/oidc", headers=headers).status_code == 403
        assert fresh.delete(f"/users/{user_id}/oidc").status_code == 401
        assert _link(migrated_engine, "linked@example.com") == ("https://sso.example.com", "sub-1")


@pytest.fixture
def https_oidc_client(
    op: mock_oidc.MockOP, clean_db: None, migrated_engine: Engine
) -> Iterator[TestClient]:
    """An instance served over https: cookies are secure."""
    with _oidc_env(op, auto_provision=True, public_url="https://purser.example.com"):
        from purser.api.app import create_app

        with TestClient(create_app(), base_url="https://purser.example.com") as client:
            yield client


def _set_cookie(resp: httpx.Response, name: str) -> str:
    headers = [h for h in resp.headers.get_list("set-cookie") if h.startswith(f"{name}=")]
    assert len(headers) == 1, resp.headers.get_list("set-cookie")
    return headers[0].lower() + ";"


class TestSecureStateCookie:
    """D3: a __Host- prefixed state cookie on https; plain HTTP unchanged."""

    def test_https_login_sets_a_host_prefixed_cookie(self, https_oidc_client: TestClient) -> None:
        resp = https_oidc_client.get("/auth/oidc/login", follow_redirects=False)
        assert resp.status_code == 307
        cookie = _set_cookie(resp, oidc_module.SECURE_STATE_COOKIE)
        for attr in ("httponly;", "secure;", "samesite=lax;", "path=/;", "max-age=600;"):
            assert attr in cookie
        assert "domain=" not in cookie
        unprefixed = [
            h for h in resp.headers.get_list("set-cookie") if h.startswith(f"{STATE_COOKIE}=")
        ]
        assert not unprefixed

    def test_https_round_trip_reads_and_clears_the_host_cookie(
        self, https_oidc_client: TestClient
    ) -> None:
        resp = _callback(https_oidc_client)
        assert resp.status_code == 303, resp.text
        cleared = _set_cookie(resp, oidc_module.SECURE_STATE_COOKIE)
        for attr in ("max-age=0;", "secure;", "path=/;"):
            assert attr in cleared
        assert _signed_in(https_oidc_client)

    def test_https_callback_ignores_the_unprefixed_cookie(
        self, https_oidc_client: TestClient
    ) -> None:
        """A cookie without the prefix, which a sibling host could plant, is
        not read on https."""
        callback = _start(https_oidc_client)
        value = https_oidc_client.cookies.get(oidc_module.SECURE_STATE_COOKIE)
        assert value is not None
        https_oidc_client.cookies.clear()
        resp = https_oidc_client.get(
            callback, headers={"Cookie": f"{STATE_COOKIE}={value}"}, follow_redirects=False
        )
        assert resp.status_code == 400
        assert not _signed_in(https_oidc_client)

    def test_http_keeps_the_path_scoped_cookie(self, oidc_client: TestClient) -> None:
        resp = oidc_client.get("/auth/oidc/login", follow_redirects=False)
        cookie = _set_cookie(resp, STATE_COOKIE)
        assert "path=/auth/oidc;" in cookie
        assert "secure" not in cookie
        assert not [h for h in resp.headers.get_list("set-cookie") if h.startswith("__Host-")]

    def test_http_callback_ignores_a_host_prefixed_cookie(self, oidc_client: TestClient) -> None:
        callback = _start(oidc_client)
        value = oidc_client.cookies.get(STATE_COOKIE)
        assert value is not None
        oidc_client.cookies.clear()
        resp = oidc_client.get(
            callback,
            headers={"Cookie": f"{oidc_module.SECURE_STATE_COOKIE}={value}"},
            follow_redirects=False,
        )
        assert resp.status_code == 400


class TestEmailCase:
    """D4: email comparison and uniqueness ignore case; emails stay as entered."""

    def test_sso_links_an_account_whose_email_differs_in_case(
        self, oidc_client_no_provision: TestClient, migrated_engine: Engine
    ) -> None:
        setup = {**ADMIN_CREDENTIALS, "email": "OIDC-User@example.com"}
        assert oidc_client_no_provision.post("/auth/setup", json=setup).status_code == 201
        oidc_client_no_provision.cookies.clear()
        assert _callback(oidc_client_no_provision).status_code == 303
        me = oidc_client_no_provision.get("/auth/me").json()["user"]
        assert me["email"] == "OIDC-User@example.com"  # stored as entered
        assert me["has_oidc"] is True
        assert _user_count(migrated_engine) == 1

    def test_auto_provisioning_does_not_duplicate_a_case_variant(
        self, oidc_client: TestClient, op: mock_oidc.MockOP, migrated_engine: Engine
    ) -> None:
        setup = {**ADMIN_CREDENTIALS, "email": "Oidc-User@example.com"}
        assert oidc_client.post("/auth/setup", json=setup).status_code == 201
        oidc_client.cookies.clear()
        op.extra_claims = {"email_verified": True, "email": "OIDC-USER@example.com"}
        assert _callback(oidc_client).status_code == 303
        assert _user_count(migrated_engine) == 1
        assert oidc_client.get("/auth/me").json()["user"]["role"] == "admin"

    def test_provisioned_email_is_stored_as_the_provider_sent_it(
        self, oidc_client: TestClient, op: mock_oidc.MockOP
    ) -> None:
        op.extra_claims = {"email_verified": True, "email": "New.Person@example.com"}
        assert _callback(oidc_client).status_code == 303
        assert oidc_client.get("/auth/me").json()["user"]["email"] == "New.Person@example.com"
