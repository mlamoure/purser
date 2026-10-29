"""Request authentication and scope enforcement.

Every route declares its required scope explicitly via these dependencies —
no default-open routes. Bearer tokens (agents) and
session cookies (browsers) both resolve to api_tokens rows, so audit has one
actor mechanism.
"""

from collections.abc import Callable
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from purser.admin import security, service
from purser.config import get_settings
from purser.db import get_session
from purser.models import ApiToken

SESSION_COOKIE = "purser_session"

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def cross_site(request: Request) -> bool:
    """Whether a browser sent this request from another site's page (#429).

    A session cookie travels with any request the browser makes to Purser,
    including one a page on a sibling subdomain triggers (SameSite=Lax only
    stops other SITES). Browsers say where a request came from:
    ``Sec-Fetch-Site`` (``same-origin``, or ``none`` for a typed URL) and,
    on unsafe methods, ``Origin``. A request carrying neither is not from a
    browser page (a script, the test client) and is not cross-site."""
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None:
        return fetch_site not in ("same-origin", "none")
    origin = request.headers.get("origin")
    if origin is None:
        return False
    own = {_origin_of(str(request.base_url)), _origin_of(get_settings().public_url)}
    return origin.lower() not in own


def get_current_token(
    request: Request, session: Annotated[Session, Depends(get_session)]
) -> ApiToken:
    raw: str | None = None
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        raw = auth_header.removeprefix("Bearer ").strip()
    elif SESSION_COOKIE in request.cookies:
        raw = request.cookies[SESSION_COOKIE]
        # A cookie is ambient: refuse a write a page elsewhere triggered
        # (#429). A bearer token is never sent by the browser on its own.
        if request.method not in _SAFE_METHODS and cross_site(request):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "cross-site request refused: a signed-in session accepts writes only from "
                "Purser's own pages",
            )
    if not raw:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "missing credentials (Bearer token or session cookie)"
        )
    token = service.authenticate_token(session, raw)
    if token is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired credentials")
    session.commit()  # persist last_used_at
    # request.state is backed by the ASGI scope, so the audit middleware sees
    # the resolved actor after the response (issue #122). Its absence is the
    # "nothing to audit" signal for public/unauthenticated requests.
    request.state.audit_actor = token.id
    return token


CurrentToken = Annotated[ApiToken, Depends(get_current_token)]


def _forbid(needed: str) -> HTTPException:
    return HTTPException(status.HTTP_403_FORBIDDEN, f"requires scope: {needed}")


def require_read(token: CurrentToken) -> ApiToken:
    if not security.can_read(token.scopes):
        raise _forbid("read")
    return token


def require_write(token: CurrentToken) -> ApiToken:
    if not security.can_write(token.scopes):
        raise _forbid("write")
    return token


def require_admin(token: CurrentToken) -> ApiToken:
    if not security.is_admin(token.scopes):
        raise _forbid("admin")
    return token


ReadToken = Annotated[ApiToken, Depends(require_read)]
WriteToken = Annotated[ApiToken, Depends(require_write)]
AdminToken = Annotated[ApiToken, Depends(require_admin)]


# Documented factory form for less common combinations.
def require_scopes(check: Callable[[list[str]], bool], needed: str) -> Callable[..., ApiToken]:
    def dependency(token: CurrentToken) -> ApiToken:
        if not check(token.scopes):
            raise _forbid(needed)
        return token

    return dependency
