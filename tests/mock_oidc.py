"""A minimal in-process OpenID Provider for round-trip tests.

Serves discovery, an authorize endpoint, JWKS and a token endpoint over real
HTTP (uvicorn in a thread) so the app's OIDC client exercises its actual
network path. The authorize endpoint records each request's nonce and PKCE
challenge against the code it issues; the token endpoint enforces the PKCE
verifier and puts the nonce into the id_token, like a real provider.

Tests misbehave the provider through the attributes ``reset`` restores:
``extra_claims`` (merged into the id_token; ``DROP`` removes a claim),
``id_token_factory`` (craft the whole id_token from the default claims),
``published_keys`` (the JWKS), ``discovery_overrides``, ``discovery_redirect``,
``jwks_status`` and ``token_response``.
"""

import base64
import hashlib
import secrets
import socket
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from joserfc import jwt
from joserfc.jwk import ECKey, RSAKey

SUBJECT = "oidc-user-1234"
EMAIL = "oidc-user@example.com"
NAME = "OIDC User"
CLIENT_ID = "purser-test"
CLIENT_SECRET = "purser-test-secret"
VALID_CODE = "test-auth-code"
KID = "test-key"

# Value for extra_claims that removes the claim from the id_token.
DROP: Any = object()

SigningKey = RSAKey | ECKey


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class MockOP:
    def __init__(self) -> None:
        self.port = self._free_port()
        self.issuer = f"http://127.0.0.1:{self.port}"
        self.original_key = RSAKey.generate_key(2048, parameters={"kid": KID})
        # code -> the authorize request's nonce and PKCE challenge
        self.authorizations: dict[str, dict[str, str | None]] = {}
        self.token_requests: list[dict[str, Any]] = []
        self.reset()
        self.app = self._build_app()
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def reset(self) -> None:
        # Merged into the id_token payload; tests mutate this to simulate an
        # OP that does not assert email_verified (#211).
        self.extra_claims: dict[str, Any] = {"email_verified": True}
        self.key: SigningKey = self.original_key
        # None publishes the current signing key alone.
        self.published_keys: list[dict[str, Any]] | None = None
        self.id_token_factory: Callable[[dict[str, Any]], str] | None = None
        self.discovery_overrides: dict[str, Any] = {}
        self.discovery_redirect: str | None = None
        self.jwks_status = 200
        self.token_response: dict[str, Any] | None = None
        self.token_requests.clear()

    @property
    def key_dict(self) -> dict[str, Any]:
        return self.key.as_dict(private=False)

    def sign(
        self,
        claims: dict[str, Any],
        *,
        key: SigningKey | None = None,
        alg: str = "RS256",
        header: dict[str, Any] | None = None,
    ) -> str:
        """An id_token over ``claims``; the kid defaults to the key's own."""
        key = key or self.key
        hdr: dict[str, Any] = {"alg": alg}
        if key.kid:
            hdr["kid"] = key.kid
        hdr.update(header or {})
        hdr = {k: v for k, v in hdr.items() if v is not DROP}
        return jwt.encode(hdr, claims, key, algorithms=[alg])

    def discovery_document(self) -> dict[str, Any]:
        return {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/authorize",
            "token_endpoint": f"{self.issuer}/token",
            "jwks_uri": f"{self.issuer}/jwks",
            "response_types_supported": ["code"],
            "id_token_signing_alg_values_supported": ["RS256", "ES256"],
            "code_challenge_methods_supported": ["S256"],
            **self.discovery_overrides,
        }

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return int(s.getsockname()[1])

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.get("/.well-known/openid-configuration", response_model=None)
        def discovery() -> dict[str, Any] | RedirectResponse:
            if self.discovery_redirect is not None:
                return RedirectResponse(self.discovery_redirect, status_code=302)
            return self.discovery_document()

        @app.get("/redirected-discovery")
        def redirected_discovery() -> dict[str, Any]:
            return self.discovery_document()

        @app.get("/authorize")
        def authorize(request: Request) -> RedirectResponse:
            q = request.query_params
            if q.get("client_id") != CLIENT_ID or q.get("response_type") != "code":
                raise HTTPException(400, "bad authorize request")
            code = f"{VALID_CODE}-{secrets.token_urlsafe(8)}"
            self.authorizations[code] = {
                "nonce": q.get("nonce"),
                "code_challenge": q.get("code_challenge"),
                "code_challenge_method": q.get("code_challenge_method"),
                "redirect_uri": q.get("redirect_uri"),
            }
            back = urlencode({"code": code, "state": q.get("state", "")})
            return RedirectResponse(f"{q['redirect_uri']}?{back}", status_code=302)

        @app.get("/jwks")
        def jwks() -> dict[str, Any]:
            if self.jwks_status != 200:
                raise HTTPException(self.jwks_status, "jwks unavailable")
            keys = self.published_keys if self.published_keys is not None else [self.key_dict]
            return {"keys": keys}

        @app.post("/token")
        async def token(request: Request) -> dict[str, Any]:
            form = await request.form()
            self.token_requests.append(dict(form))
            auth = request.headers.get("Authorization", "")
            expected = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
            if auth != f"Basic {expected}":
                raise HTTPException(401, "bad client credentials")
            grant = self.authorizations.pop(str(form.get("code")), None)  # single use
            if grant is None or form.get("redirect_uri") != grant["redirect_uri"]:
                raise HTTPException(400, "invalid_grant")
            if grant["code_challenge"] is not None:
                verifier = str(form.get("code_verifier", ""))
                digest = b64url(hashlib.sha256(verifier.encode()).digest())
                if grant["code_challenge_method"] != "S256" or digest != grant["code_challenge"]:
                    raise HTTPException(400, "invalid_grant: PKCE verification failed")
            if self.token_response is not None:
                return self.token_response
            now = int(time.time())
            claims = {
                "iss": self.issuer,
                "aud": CLIENT_ID,
                "sub": SUBJECT,
                "email": EMAIL,
                "name": NAME,
                "iat": now,
                "exp": now + 300,
                "nonce": grant["nonce"],
                **self.extra_claims,
            }
            claims = {k: v for k, v in claims.items() if v is not DROP}
            if self.id_token_factory is not None:
                id_token = self.id_token_factory(claims)
            else:
                id_token = self.sign(claims)
            return {"access_token": "at-test", "token_type": "Bearer", "id_token": id_token}

        return app

    def start(self) -> None:
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.time() + 10
        while not self._server.started:
            if time.time() > deadline:
                raise RuntimeError("mock OP failed to start")
            time.sleep(0.05)

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)
