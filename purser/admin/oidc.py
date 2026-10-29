"""OIDC login against a generic OpenID Provider (authorization code flow).

Discovery -> authorize redirect (state, nonce, PKCE S256) -> back-channel code
exchange -> id_token validation with joserfc against the OP's JWKS. Tested
against a mock OP (tests/mock_oidc.py); pointing it at a real provider is a
deployment step (docs: how-to/oidc), not code.

Every outbound request has a timeout and never follows redirects. Discovery
and the JWKS are fetched per sign-in, so a key rotation at the OP takes effect
on the next login with no cache to go stale. Failures map to fixed messages:
502 when the OP cannot be reached or answers nonsense, 401 when the id_token
fails validation. Logs name the failing check, never a code or token.
"""

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import HTTPException, status
from joserfc import jwt
from joserfc.errors import ExpiredTokenError, InvalidClaimError, JoseError, MissingClaimError
from joserfc.jwk import KeySet
from joserfc.jws import JWSRegistry

from purser.config import Settings
from purser.logging import get_logger

log = get_logger(__name__)

STATE_COOKIE = "purser_oidc_state"
# On plain HTTP the cookie is scoped to the OIDC routes. On HTTPS it carries
# the __Host- prefix instead (#429, D3), which the browser accepts only with
# Secure, Path=/ and no Domain: a sibling subdomain cannot plant or overwrite
# it, so it cannot slip its own sign-in attempt into this browser.
STATE_COOKIE_PATH = "/auth/oidc"
SECURE_STATE_COOKIE = f"__Host-{STATE_COOKIE}"
STATE_TTL_SECONDS = 600
HTTP_TIMEOUT_SECONDS = 10
# Tolerated clock difference with the OP for `iat` and `nbf` (a token stamped
# slightly in the future). `exp` gets none: an expired token is refused.
CLOCK_SKEW_SECONDS = 60

# The id_token signature algorithms Purser accepts. The registry rejects every
# other `alg`, `none` and the HMAC family included, and a key whose type does
# not fit the algorithm. Header parameters it does not know are ignored unless
# listed in `crit` (RFC 7515 section 4).
ID_TOKEN_ALGORITHMS = ("RS256", "ES256")
_ID_TOKEN_JWS = JWSRegistry(algorithms=list(ID_TOKEN_ALGORITHMS), strict_check_header=False)

_ENDPOINTS = ("authorization_endpoint", "token_endpoint", "jwks_uri")


@dataclass(frozen=True)
class StateCookie:
    name: str
    path: str
    secure: bool


def state_cookie(secure: bool) -> StateCookie:
    """Where this instance keeps the sign-in attempt: ``__Host-`` + name at
    ``/`` when cookies are secure (an https ``PURSER_PUBLIC_URL``), else the
    plain name scoped to ``/auth/oidc``. Login sets it and the callback reads
    it by the same rule."""
    if secure:
        return StateCookie(SECURE_STATE_COOKIE, "/", True)
    return StateCookie(STATE_COOKIE, STATE_COOKIE_PATH, False)


def issuer_key(issuer: str) -> str:
    """The form an issuer is stored and compared in: without the trailing
    slash, the one difference discovery tolerates between the configured and
    the provider's issuer. Migration 0041 backfills with the same rule."""
    return issuer.rstrip("/")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class LoginAttempt:
    """The per-sign-in secrets the browser carries in the state cookie.

    ``state`` binds the callback to the browser that started the login,
    ``nonce`` binds the id_token to it, and ``code_verifier`` (PKCE, RFC 7636)
    makes an intercepted authorization code useless without the cookie.
    All three are urlsafe tokens, which never contain a ".".
    """

    state: str
    nonce: str
    code_verifier: str

    @classmethod
    def new(cls) -> "LoginAttempt":
        return cls(
            state=secrets.token_urlsafe(24),
            nonce=secrets.token_urlsafe(24),
            code_verifier=secrets.token_urlsafe(48),
        )

    @classmethod
    def from_cookie(cls, value: str | None) -> "LoginAttempt | None":
        parts = value.split(".") if value else []
        if len(parts) != 3 or not all(parts):
            return None
        return cls(*parts)

    def cookie_value(self) -> str:
        return f"{self.state}.{self.nonce}.{self.code_verifier}"

    @property
    def code_challenge(self) -> str:
        return _b64url(hashlib.sha256(self.code_verifier.encode("ascii")).digest())

    def state_matches(self, state: str) -> bool:
        return hmac.compare_digest(self.state.encode(), state.encode())


class _IdTokenClaims(jwt.JWTClaimsRegistry):
    """joserfc's registry with no leeway on ``exp``.

    The registry applies one leeway to ``exp``, ``nbf`` and ``iat``. Purser
    wants skew tolerance only for a token stamped slightly in the future, so
    ``exp`` is checked on its own: the token is refused at or after it."""

    def validate_exp(self, value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise InvalidClaimError("exp")
        if value <= self.now:
            raise ExpiredTokenError("exp")


def _op_unavailable(what: str, reason: str) -> HTTPException:
    log.warning("oidc.provider_error", step=what, reason=reason)
    return HTTPException(status.HTTP_502_BAD_GATEWAY, f"OIDC {what} failed")


class OidcClient:
    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None) -> None:
        if not settings.oidc_enabled:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "OIDC is not configured")
        self._settings = settings
        self._transport = transport  # tests inject httpx.MockTransport
        self._metadata: dict[str, Any] | None = None

    @property
    def redirect_uri(self) -> str:
        return f"{self._settings.public_url.rstrip('/')}/auth/oidc/callback"

    @property
    def _client_id(self) -> str:
        return str(self._settings.oidc_client_id)

    def _request(self, method: str, url: str, what: str, **kwargs: Any) -> httpx.Response:
        try:
            with httpx.Client(
                timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False, transport=self._transport
            ) as http:
                return http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise _op_unavailable(what, type(exc).__name__) from None

    def _json(self, resp: httpx.Response, what: str) -> dict[str, Any]:
        if resp.status_code != 200:
            raise _op_unavailable(what, f"HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            raise _op_unavailable(what, "response is not JSON") from None
        if not isinstance(body, dict):
            raise _op_unavailable(what, "response is not a JSON object")
        return body

    def metadata(self) -> dict[str, Any]:
        """The OP's discovery document, checked before any of it is used."""
        if self._metadata is not None:
            return self._metadata
        what = "discovery"
        issuer = str(self._settings.oidc_issuer).rstrip("/")
        meta = self._json(
            self._request("GET", f"{issuer}/.well-known/openid-configuration", what), what
        )
        for field in ("issuer", *_ENDPOINTS):
            if not isinstance(meta.get(field), str) or not meta[field]:
                raise _op_unavailable(what, f"{field} missing")
        # OIDC Discovery 4.3: the document must name the issuer it was fetched
        # from. A trailing slash is the one difference tolerated, because the
        # configured value is often entered without the provider's slash.
        if meta["issuer"].rstrip("/") != issuer:
            raise _op_unavailable(what, "issuer does not match the configured issuer")
        schemes = ("https://",) if issuer.startswith("https://") else ("https://", "http://")
        for field in _ENDPOINTS:
            if not meta[field].startswith(schemes):
                raise _op_unavailable(what, f"{field} is not an allowed URL")
        self._metadata = meta
        return meta

    def authorize_url(self, attempt: LoginAttempt) -> str:
        """The provider URL that starts ``attempt``; the caller stores the
        attempt in the state cookie."""
        url = httpx.URL(self.metadata()["authorization_endpoint"])
        return str(
            url.copy_merge_params(
                {
                    "response_type": "code",
                    "client_id": self._client_id,
                    "redirect_uri": self.redirect_uri,
                    "scope": "openid email profile",
                    "state": attempt.state,
                    "nonce": attempt.nonce,
                    "code_challenge": attempt.code_challenge,
                    "code_challenge_method": "S256",
                }
            )
        )

    def exchange_code(self, code: str, attempt: LoginAttempt) -> dict[str, Any]:
        """Exchange the code and return validated id_token claims."""
        meta = self.metadata()
        what = "token exchange"
        resp = self._request(
            "POST",
            meta["token_endpoint"],
            what,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "code_verifier": attempt.code_verifier,
            },
            auth=(self._client_id, str(self._settings.oidc_client_secret)),
        )
        id_token = self._json(resp, what).get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "OIDC response missing id_token")
        return self.validate_id_token(id_token, nonce=attempt.nonce)

    def signing_keys(self) -> KeySet:
        what = "JWKS fetch"
        jwks = self._json(self._request("GET", self.metadata()["jwks_uri"], what), what)
        try:
            return KeySet.import_key_set(jwks)  # type: ignore[arg-type]
        except (JoseError, ValueError, KeyError, TypeError):
            raise _op_unavailable(what, "no usable keys") from None

    def validate_id_token(self, id_token: str, *, nonce: str) -> dict[str, Any]:
        """OIDC Core 3.1.3.7: signature, iss, aud/azp, exp, iat, nbf, nonce."""
        meta = self.metadata()
        keys = self.signing_keys()
        try:
            token = jwt.decode(id_token, keys, registry=_ID_TOKEN_JWS)
            claims = token.claims
            if not isinstance(claims, dict):
                raise InvalidClaimError("payload")
            _IdTokenClaims(
                leeway=CLOCK_SKEW_SECONDS,
                iss={"essential": True, "value": meta["issuer"]},
                aud={"essential": True, "value": self._client_id},
                sub={"essential": True},
                exp={"essential": True},
                iat={"essential": True},
                nonce={"essential": True, "value": nonce},
            ).validate(claims)
            self._check_azp(claims)
        except (JoseError, ValueError, TypeError, KeyError) as exc:
            log.warning(
                "oidc.id_token_rejected",
                reason=type(exc).__name__,
                detail=exc.description if isinstance(exc, JoseError) else None,
            )
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "OIDC id_token validation failed"
            ) from None
        return claims

    def _check_azp(self, claims: dict[str, Any]) -> None:
        """A token for several audiences must name Purser as the authorized
        party; an azp, when present, must be Purser's client id."""
        aud = claims["aud"]
        audiences = [aud] if isinstance(aud, str) else list(aud)
        azp = claims.get("azp")
        if azp is not None and azp != self._client_id:
            raise InvalidClaimError("azp")
        if len(audiences) > 1 and azp is None:
            raise MissingClaimError("azp")
