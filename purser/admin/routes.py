"""Identity & access routes: bootstrap, login/logout, OIDC, users, tokens."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from purser import appsettings, audit, crypto
from purser.admin import security, service
from purser.admin.auth import SESSION_COOKIE, AdminToken, CurrentToken
from purser.admin.oidc import STATE_TTL_SECONDS, LoginAttempt, OidcClient, state_cookie
from purser.admin.throttle import LoginThrottle, LoginThrottled
from purser.config import get_settings
from purser.db import get_session
from purser.models import ApiToken, EncryptionCanary, User

DbSession = Annotated[Session, Depends(get_session)]

auth_router = APIRouter(prefix="/auth", tags=["auth"])
users_router = APIRouter(prefix="/users", tags=["users"])
tokens_router = APIRouter(prefix="/tokens", tags=["tokens"])


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------


class SetupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    display_name: str | None = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str | None
    role: str
    has_password: bool
    has_oidc: bool
    # The provider the SSO link belongs to; null when unlinked, or for a
    # legacy link whose next sign-in records it (#429).
    oidc_issuer: str | None = None
    created_at: datetime

    @classmethod
    def from_model(cls, u: User) -> "UserOut":
        return cls(
            id=u.id,
            email=u.email,
            display_name=u.display_name,
            role=u.role,
            has_password=u.password_hash is not None,
            has_oidc=u.oidc_subject is not None,
            oidc_issuer=u.oidc_issuer,
            created_at=u.created_at,
        )


class UserCreate(BaseModel):
    email: EmailStr
    password: str | None = Field(default=None, min_length=8)
    display_name: str | None = None
    role: str = "member"


class TokenOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: str  # api | session — server-controlled (see service.create_token)
    scopes: list[str]
    expires_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime
    is_current: bool = False  # the token making this request (its own session)

    @classmethod
    def from_model(cls, t: ApiToken, *, current_id: uuid.UUID | None = None) -> "TokenOut":
        return cls(
            id=t.id,
            name=t.name,
            kind=t.kind,
            scopes=t.scopes,
            expires_at=t.expires_at,
            last_used_at=t.last_used_at,
            created_at=t.created_at,
            is_current=t.id == current_id,
        )


class TokenCreate(BaseModel):
    name: str
    # Recommended default for agents: read + write (DESIGN §9, #436).
    scopes: list[str] = Field(default=[security.SCOPE_READ, security.SCOPE_WRITE])
    expires_at: datetime | None = None


class TokenCreated(TokenOut):
    token: str  # the raw secret, returned exactly once


# --------------------------------------------------------------------------
# Bootstrap & sessions
# --------------------------------------------------------------------------


def _set_session_cookie(response: Response, raw: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        raw,
        httponly=True,
        secure=get_settings().cookie_secure,
        samesite="lax",
        max_age=int(service.session_ttl().total_seconds()),
    )


@auth_router.get("/setup/status")
def setup_status(session: DbSession) -> dict[str, bool]:
    """Public probe: does this instance still need first-run setup? The SPA
    routes to the wizard when true. Exposes nothing but the boolean."""
    return {"setup_needed": service.user_count(session) == 0}


@auth_router.get("/methods")
def auth_methods() -> dict[str, bool]:
    """Public probe: which sign-in methods the login page should offer.
    Password auth is always available; SSO reflects the effective OIDC state
    (env defaults + runtime Config overrides). Exposes nothing but booleans."""
    return {"password": True, "oidc": appsettings.effective().oidc_enabled}


@auth_router.post("/setup", status_code=status.HTTP_201_CREATED)
def setup_first_admin(body: SetupRequest, session: DbSession, response: Response) -> UserOut:
    """Create the first admin. Only available while no users exist (wizard step 1)."""
    # Serialize bootstrap across requests and API processes. A count-before-insert
    # alone lets two concurrent callers both become the first administrator.
    # The transaction lock is released by commit/rollback, including failures.
    session.execute(text("SELECT pg_advisory_xact_lock(1347773011, 1)"))
    if service.user_count(session) > 0:
        raise HTTPException(status.HTTP_409_CONFLICT, "setup already completed")
    user = service.create_user(
        session,
        email=body.email,
        password=body.password,
        display_name=body.display_name,
        role="admin",
        actor_token_id=None,
    )
    login = service.login_with_password(session, email=body.email, password=body.password)
    assert login is not None  # freshly created with this password
    session.commit()
    _set_session_cookie(response, login[1])
    return UserOut.from_model(user)


_LOGIN_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_429_TOO_MANY_REQUESTS: {
        "description": "Too many failed sign-ins for this email or client address. "
        "Refused without checking the password; retry after `Retry-After` seconds.",
        "headers": {
            "Retry-After": {
                "description": "Seconds until a sign-in attempt is admitted again.",
                "schema": {"type": "integer"},
            }
        },
    }
}


@auth_router.post("/login", responses=_LOGIN_RESPONSES)
def login(body: LoginRequest, request: Request, session: DbSession, response: Response) -> UserOut:
    """Password sign-in; sets the session cookie.

    Throttled per email and per client address (DESIGN decision 59): after
    repeated failures an attempt gets 429 with a `Retry-After` header (whole
    seconds) and the password is not checked. Failed and throttled attempts are
    audited as `auth.login_failed` / `auth.login_throttled`."""
    throttle: LoginThrottle = request.app.state.login_throttle
    # The ASGI peer. uvicorn rewrites it from X-Forwarded-For only for proxies
    # in its own FORWARDED_ALLOW_IPS list; the app parses no forwarding header.
    ip = request.client.host if request.client else None
    try:
        attempt = throttle.begin(body.email, ip)
    except LoginThrottled as exc:
        service.record_refused_login(
            session,
            email=body.email,
            ip=ip,
            retry_after=exc.retry_after,
            limited_by=exc.limited_by,
        )
        session.commit()
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"too many failed sign-in attempts; try again in {exc.retry_after} seconds",
            headers={"Retry-After": str(exc.retry_after)},
        ) from None
    result = service.login_with_password(session, email=body.email, password=body.password)
    if result is None:
        service.record_refused_login(session, email=body.email, ip=ip)
        session.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid email or password")
    user, raw = result
    session.commit()
    throttle.succeeded(attempt)
    _set_session_cookie(response, raw)
    return UserOut.from_model(user)


@auth_router.post("/logout")
def logout(token: CurrentToken, session: DbSession, response: Response) -> dict[str, str]:
    db_token = session.get(ApiToken, token.id)
    assert db_token is not None
    service.revoke_token(session, token=db_token, actor_token_id=token.id)
    session.commit()
    response.delete_cookie(SESSION_COOKIE)
    return {"status": "logged out"}


class EncryptionKeyOut(BaseModel):
    key: str
    fingerprint: str
    acknowledged: bool


@auth_router.get("/encryption-key")
def encryption_key(token: AdminToken, session: DbSession) -> EncryptionKeyOut:
    """Reveal the encryption key ONCE, for the wizard's record-your-key step
    (the wizard holds an admin session right after POST /auth/setup). 410 Gone
    after acknowledgment bounds the exposure window — afterwards the key lives
    only in the operator's environment."""
    canary = session.execute(select(EncryptionCanary)).scalar_one_or_none()
    if canary is not None and canary.key_acknowledged_at is not None:
        raise HTTPException(status.HTTP_410_GONE, "encryption key already acknowledged")
    key = get_settings().encryption_key
    assert key is not None  # startup fails without it
    return EncryptionKeyOut(
        key=key.split(",")[0].strip(),  # the primary (encrypting) key
        fingerprint=crypto.key_fingerprint(),
        acknowledged=False,
    )


@auth_router.post("/encryption-key/ack")
def acknowledge_encryption_key(token: AdminToken, session: DbSession) -> dict[str, str]:
    """Record that the operator stored the key; the reveal endpoint goes 410."""
    canary = session.execute(select(EncryptionCanary)).scalar_one()
    if canary.key_acknowledged_at is None:
        canary.key_acknowledged_at = datetime.now(UTC)
        audit.record(
            session,
            actor_token_id=token.id,
            action="encryption_key.acknowledge",
            entity_type="system",
        )
        session.commit()
    return {"status": "acknowledged"}


@auth_router.get("/me")
def me(token: CurrentToken, session: DbSession) -> dict[str, object]:
    user = session.get(User, token.user_id)
    assert user is not None
    return {
        "user": UserOut.from_model(user),
        "token_name": token.name,
        "scopes": token.scopes,
        "redacted_reads": security.withholds_external_ids(token.kind, token.scopes),
    }


# --------------------------------------------------------------------------
# OIDC (generic OP; Authentik config is a documented deployment step)
# --------------------------------------------------------------------------


@auth_router.get("/oidc/login")
def oidc_login() -> RedirectResponse:
    """Start an SSO sign-in: redirect to the provider with a fresh state,
    nonce and PKCE challenge, kept for the callback in a short-lived cookie."""
    attempt = LoginAttempt.new()
    url = OidcClient(appsettings.effective()).authorize_url(attempt)
    response = RedirectResponse(url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    cookie = state_cookie(get_settings().cookie_secure)
    response.set_cookie(
        cookie.name,
        attempt.cookie_value(),
        path=cookie.path,
        httponly=True,
        secure=cookie.secure,
        samesite="lax",
        max_age=STATE_TTL_SECONDS,
    )
    return response


@auth_router.get("/oidc/callback")
def oidc_callback(
    request: Request,
    session: DbSession,
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
) -> RedirectResponse:
    """The provider's redirect back. The state must match the cookie set by
    `/auth/oidc/login`; the id_token is validated before any account lookup."""
    if error is not None:
        # The provider refused or the user cancelled. Its error text is not
        # echoed back: it is attacker-controllable in a crafted link.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "the identity provider did not complete the sign-in"
        )
    cookie = state_cookie(get_settings().cookie_secure)
    attempt = LoginAttempt.from_cookie(request.cookies.get(cookie.name))
    if not code or not state or attempt is None or not attempt.state_matches(state):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "OIDC state mismatch")
    eff = appsettings.effective()
    claims = OidcClient(eff).exchange_code(code, attempt)
    subject = claims["sub"]  # validated: a non-empty string
    email = claims.get("email")
    if not isinstance(email, str) or not email:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "OIDC id_token missing email claim")
    name = claims.get("name")
    try:
        _user, raw = service.login_with_oidc(
            session,
            issuer=claims["iss"],  # validated: equal to the provider's issuer
            subject=subject,
            email=email,
            email_verified=claims.get("email_verified") is True,
            display_name=name if isinstance(name, str) else None,
            allow_provision=eff.oidc_auto_provision,
        )
    except service.OidcLoginError as exc:
        session.commit()  # a refused relink leaves its audit row (#429, D2)
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    session.commit()
    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    _set_session_cookie(response, raw)
    response.delete_cookie(
        cookie.name, path=cookie.path, secure=cookie.secure, httponly=True, samesite="lax"
    )
    return response


# --------------------------------------------------------------------------
# Users (admin scope)
# --------------------------------------------------------------------------


@users_router.get("")
def list_users(_: AdminToken, session: DbSession) -> list[UserOut]:
    users = session.execute(select(User).order_by(User.created_at)).scalars()
    return [UserOut.from_model(u) for u in users]


@users_router.post("", status_code=status.HTTP_201_CREATED)
def create_user(body: UserCreate, admin: AdminToken, session: DbSession) -> UserOut:
    if body.role not in ("member", "admin"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "role must be member|admin")
    # Emails are unique regardless of case (#429, D4).
    if service.find_user_by_email(session, body.email) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "email already exists")
    user = service.create_user(
        session,
        email=body.email,
        password=body.password,
        display_name=body.display_name,
        role=body.role,
        actor_token_id=admin.id,
    )
    session.commit()
    return UserOut.from_model(user)


@users_router.delete("/{user_id}/oidc")
def unlink_user_oidc(user_id: uuid.UUID, admin: AdminToken, session: DbSession) -> UserOut:
    """Remove a user's SSO link (admin scope; audited as `user.unlink_oidc`).

    A linked account refuses a different subject from the same provider, so
    this is how an administrator lets someone sign in with a new identity at
    that provider. The account and its password, if any, are kept; the next
    SSO sign-in that asserts the account's verified email links it again.
    404 for an unknown user, 409 when the user has no SSO link."""
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    try:
        service.unlink_oidc(session, user=user, actor_token_id=admin.id)
    except service.IdentityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    session.commit()
    return UserOut.from_model(user)


# --------------------------------------------------------------------------
# API tokens (self-service; who sees and revokes which token: decision 75)
# --------------------------------------------------------------------------


@tokens_router.get("")
def list_tokens(
    token: CurrentToken,
    session: DbSession,
    include_expired: Annotated[bool, Query()] = False,
) -> list[TokenOut]:
    """List the tokens the caller may manage (DESIGN decision 75).

    A session (a signed-in person) or an admin-scoped token sees every token of
    its own user; any other token — an agent's — sees only itself. Dead tokens
    (expired, revoked, idle-timed-out sessions) are hidden by default;
    include_expired reveals the rows retained as audit/attribution anchors
    (DESIGN §9)."""
    if service.manages_user_tokens(token):
        stmt = select(ApiToken).where(ApiToken.user_id == token.user_id)
    else:
        stmt = select(ApiToken).where(ApiToken.id == token.id)
    stmt = stmt.order_by(ApiToken.created_at)
    if not include_expired:
        now = datetime.now(UTC)
        stmt = stmt.where(
            or_(ApiToken.expires_at.is_(None), ApiToken.expires_at > now),
            or_(
                ApiToken.kind != "session",
                func.coalesce(ApiToken.last_used_at, ApiToken.created_at)
                > now - service.session_idle(),
            ),
        )
    rows = session.execute(stmt).scalars()
    return [TokenOut.from_model(t, current_id=token.id) for t in rows]


@tokens_router.post("", status_code=status.HTTP_201_CREATED)
def create_token(body: TokenCreate, token: AdminToken, session: DbSession) -> TokenCreated:
    # Structural guarantee (DESIGN §9, issue #207): only an admin principal
    # mints tokens. A scope-subset rule alone let any leaked token mint
    # non-expiring clones of itself that survive the parent's revocation.
    user = session.get(User, token.user_id)
    assert user is not None
    try:
        row, raw = service.create_token(
            session,
            user=user,
            name=body.name,
            scopes=body.scopes,
            expires_at=body.expires_at,
            actor_token_id=token.id,
        )
    except service.IdentityError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    session.commit()
    out = TokenOut.from_model(row)
    return TokenCreated(**out.model_dump(), token=raw)


@tokens_router.delete("/{token_id}")
def revoke_token(token_id: uuid.UUID, token: CurrentToken, session: DbSession) -> dict[str, str]:
    """Revoke a token (audited as `token.revoke`; DESIGN decision 75).

    Any token may revoke itself. A session (a signed-in person) may revoke
    every token of its own user, and an admin-scoped token any token. An agent
    token without admin scope can revoke only itself: any other id gets the
    same 404 as an unknown one, so the answer reveals nothing."""
    row = session.get(ApiToken, token_id)
    if row is None or not service.may_manage_token(token, row):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "token not found")
    service.revoke_token(session, token=row, actor_token_id=token.id)
    session.commit()
    return {"status": "revoked"}


# --------------------------------------------------------------------------
# Audit (admin scope) — the activity trail: change rows (mutations) plus
# read/write call rows (issue #122), grouped into virtual sessions.
# --------------------------------------------------------------------------

audit_router = APIRouter(prefix="/audit", tags=["audit"])


class AuditOut(BaseModel):
    id: uuid.UUID
    at: datetime
    actor_token_id: uuid.UUID | None
    action: str
    entity_type: str
    entity_id: uuid.UUID | None
    before_after: dict[str, object] | None
    kind: str  # read | write (call rows) | change (per-entity before/after)
    request_id: uuid.UUID | None  # groups a call row with its change rows
    detail: dict[str, object] | None  # call rows only: method/tool, status, ms…
    # MCP call rows only: fields the agent included beyond the user's privacy
    # defaults (#437); null when it disclosed nothing extra.
    disclosed: list[str] | None = None


class AuditPage(BaseModel):
    items: list[AuditOut]
    next_cursor: str | None  # keyset (at, id) — stable under constant inserts


class AuditSessionOut(BaseModel):
    actor_token_id: uuid.UUID | None  # null = System (scheduler jobs)
    token_name: str | None
    token_kind: str | None  # api | session | system
    user_email: str | None
    started_at: datetime
    ended_at: datetime
    reads: int
    writes: int
    changes: int


class AuditSessionPage(BaseModel):
    items: list[AuditSessionOut]
    next_cursor: str | None  # opaque: sessions strictly older than this


@audit_router.get("")
def list_audit(
    _: AdminToken,
    session: DbSession,
    kind: Annotated[str | None, Query(pattern="^(read|write|change)$")] = None,
    actor_token_id: uuid.UUID | None = None,
    unattributed: bool = False,
    action: str | None = None,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    request_id: uuid.UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    disclosed: Annotated[
        bool, Query(description="only calls that disclosed fields beyond the privacy defaults")
    ] = False,
    order: Annotated[str, Query(pattern="^(asc|desc)$")] = "desc",
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> AuditPage:
    from purser.models import AuditLog

    stmt = audit.audit_query(
        kind=kind,
        actor_token_id=actor_token_id,
        unattributed=unattributed,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        request_id=request_id,
        since=since,
        until=until,
        disclosed=disclosed,
    )
    if cursor is not None:
        decoded = audit.decode_cursor(cursor)
        if decoded is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid cursor")
        cur_at, cur_id = decoded
        if order == "desc":
            stmt = stmt.where(
                or_(AuditLog.at < cur_at, (AuditLog.at == cur_at) & (AuditLog.id < cur_id))
            )
        else:
            stmt = stmt.where(
                or_(AuditLog.at > cur_at, (AuditLog.at == cur_at) & (AuditLog.id > cur_id))
            )
    if order == "desc":
        stmt = stmt.order_by(AuditLog.at.desc(), AuditLog.id.desc())
    else:
        stmt = stmt.order_by(AuditLog.at.asc(), AuditLog.id.asc())
    rows = list(session.execute(stmt.limit(limit + 1)).scalars())
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = audit.encode_cursor(last.at, last.id)
    return AuditPage(
        items=[AuditOut.model_validate(r, from_attributes=True) for r in rows],
        next_cursor=next_cursor,
    )


@audit_router.get("/sessions")
def list_audit_sessions(
    _: AdminToken,
    session: DbSession,
    since: datetime | None = None,
    until: datetime | None = None,
    actor_token_id: uuid.UUID | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> AuditSessionPage:
    """Virtual sessions: per actor, split after 30 minutes of silence.
    Web session tokens naturally span their lifetime; long-lived API/MCP
    tokens split into conversation-shaped bursts; System = scheduler jobs."""
    before = None
    if cursor is not None:
        decoded = audit.decode_cursor(cursor)
        if decoded is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid cursor")
        before = decoded[0]
    if since is None:
        since = datetime.now(UTC) - timedelta(days=7)
    items = audit.list_sessions(
        session,
        since=since,
        until=until,
        actor_token_id=actor_token_id,
        before=before,
        limit=limit,
    )
    next_cursor = None
    if len(items) == limit and items:
        next_cursor = audit.encode_cursor(items[-1]["ended_at"], uuid.uuid4())
    return AuditSessionPage(
        items=[AuditSessionOut(**item) for item in items],
        next_cursor=next_cursor,
    )
