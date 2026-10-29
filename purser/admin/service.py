"""Identity services: users, API tokens, sessions."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, and_, delete, func, or_, select, update
from sqlalchemy.orm import Session

from purser import appsettings, audit
from purser.admin import oidc, security
from purser.logging import get_logger
from purser.models import ApiToken, AuditLog, Match, PositionEvent, Transaction, User

log = get_logger(__name__)

# Scopes a browser session carries; sessions resolve to token identities so
# there is exactly one audit mechanism (DESIGN §9).
MEMBER_SESSION_SCOPES = [security.SCOPE_READ, security.SCOPE_WRITE]
ADMIN_SESSION_SCOPES = [*MEMBER_SESSION_SCOPES, security.SCOPE_ADMIN]


def session_ttl() -> timedelta:
    """Absolute session lifetime; single source for token expiry AND cookie max-age."""
    return timedelta(days=appsettings.effective().session_ttl_days)


def session_idle() -> timedelta:
    """Sliding inactivity window: a session unused for this long stops authenticating."""
    return timedelta(hours=appsettings.effective().session_idle_hours)


def _last_seen(token: ApiToken) -> datetime:
    return token.last_used_at or token.created_at


class IdentityError(Exception):
    pass


def user_count(session: Session) -> int:
    return session.execute(select(func.count()).select_from(User)).scalar_one()


def create_user(
    session: Session,
    *,
    email: str,
    password: str | None,
    display_name: str | None = None,
    role: str = "member",
    oidc_issuer: str | None = None,
    oidc_subject: str | None = None,
    actor_token_id: uuid.UUID | None,
) -> User:
    user = User(
        email=email,
        display_name=display_name,
        role=role,
        oidc_issuer=oidc_issuer,
        oidc_subject=oidc_subject,
        password_hash=security.hash_password(password) if password else None,
    )
    session.add(user)
    session.flush()
    audit.record(
        session,
        actor_token_id=actor_token_id,
        action="user.create",
        entity_type="user",
        entity_id=user.id,
        after={"email": email, "role": role, "oidc": oidc_subject is not None},
    )
    return user


def create_token(
    session: Session,
    *,
    user: User,
    name: str,
    scopes: list[str],
    expires_at: datetime | None = None,
    kind: str = "api",
    actor_token_id: uuid.UUID | None,
) -> tuple[ApiToken, str]:
    """Create an API token; returns (row, raw_token). Raw token is shown once.

    ``kind`` is server-controlled: only the login paths pass 'session'. It is
    deliberately NOT exposed on the create endpoint — ledger origin attribution
    (human vs agent) keys off it.
    """
    retired = sorted(set(scopes) & set(security.RETIRED_SCOPES))
    if retired:
        use = sorted({security.RETIRED_SCOPES[s] for s in retired})
        raise IdentityError(
            f"retired scopes: {retired}; use {' and '.join(repr(u) for u in use)} (#436, #437)"
        )
    unknown = set(scopes) - set(security.ALL_SCOPES)
    if unknown:
        raise IdentityError(f"unknown scopes: {sorted(unknown)}")
    raw, token_hash = security.generate_token()
    token = ApiToken(
        user_id=user.id,
        name=name,
        kind=kind,
        token_hash=token_hash,
        scopes=scopes,
        expires_at=expires_at,
    )
    session.add(token)
    session.flush()
    audit.record(
        session,
        actor_token_id=actor_token_id,
        action="token.create",
        entity_type="api_token",
        entity_id=token.id,
        after={"name": name, "kind": kind, "scopes": scopes, "user_id": str(user.id)},
    )
    return token, raw


def manages_user_tokens(actor: ApiToken) -> bool:
    """True when ``actor`` may see every token of its own user (decision 75):
    a session token (a human sign-in) or an admin-scoped token. Any other
    token — an agent's — sees only itself."""
    return actor.kind == "session" or security.is_admin(actor.scopes)


def may_manage_token(actor: ApiToken, target: ApiToken) -> bool:
    """Whether ``actor`` may see and revoke ``target`` (decision 75, #355).

    A token always manages itself; an admin-scoped token manages any token; a
    session token manages every token of its own user. An agent token without
    admin scope manages only itself, so a leaked read-only token cannot sign
    the admin out or revoke its sibling agents."""
    if target.id == actor.id or security.is_admin(actor.scopes):
        return True
    return actor.kind == "session" and target.user_id == actor.user_id


def revoke_token(session: Session, *, token: ApiToken, actor_token_id: uuid.UUID | None) -> None:
    before = {"expires_at": token.expires_at.isoformat() if token.expires_at else None}
    token.expires_at = datetime.now(UTC)
    audit.record(
        session,
        actor_token_id=actor_token_id,
        action="token.revoke",
        entity_type="api_token",
        entity_id=token.id,
        before=before,
        after={"expires_at": token.expires_at.isoformat()},
    )


def authenticate_token(session: Session, raw: str) -> ApiToken | None:
    """Resolve a raw bearer/session token to a live ApiToken row."""
    token = session.execute(
        select(ApiToken).where(ApiToken.token_hash == security.hash_token(raw))
    ).scalar_one_or_none()
    if token is None:
        return None
    now = datetime.now(UTC)
    if token.expires_at is not None and token.expires_at <= now:
        return None
    # Sessions also die after an inactivity window (sliding — every
    # authenticated request refreshes last_used_at below).
    if token.kind == "session" and _last_seen(token) <= now - session_idle():
        return None
    token.last_used_at = now
    return token


def login_with_password(session: Session, *, email: str, password: str) -> tuple[User, str] | None:
    """Verify credentials and mint a session token (browser cookie value).

    Every call runs exactly one argon2 verification (issue #304): an unknown
    email, or an account with no password, is checked against
    ``security.dummy_password_hash()`` and refused whatever it returns. A
    shortcut there would answer measurably faster than a wrong password and
    tell a caller which emails have accounts."""
    user = find_user_by_email(session, email)
    if user is None or not user.password_hash:
        security.verify_password(security.dummy_password_hash(), password)
        return None
    if not security.verify_password(user.password_hash, password):
        return None
    return user, _mint_session(session, user, "password")


def record_refused_login(
    session: Session,
    *,
    email: str,
    ip: str | None,
    retry_after: int | None = None,
    limited_by: list[str] | None = None,
) -> None:
    """Audit a refused password sign-in (issue #212, decision 59).

    No principal resolved, so the row is unattributed (it groups under System).
    It carries the email as entered and the client address — never the
    password. ``retry_after`` marks an attempt the throttle refused before any
    password check (``auth.login_throttled``); without it the password was
    checked and wrong, or the account has none (``auth.login_failed``)."""
    after: dict[str, object] = {"email": email, "ip": ip}
    action = "auth.login_failed"
    if retry_after is not None:
        action = "auth.login_throttled"
        after["retry_after"] = retry_after
        after["limited_by"] = limited_by or []
    audit.record(session, actor_token_id=None, action=action, entity_type="user", after=after)


def email_matches(email: str) -> ColumnElement[bool]:
    """``User.email`` equals ``email`` regardless of case (#429, D4). Both
    sides are lowered in the database, so the comparison is the one the
    unique index on ``lower(email)`` enforces."""
    return func.lower(User.email) == func.lower(email)


def find_user_by_email(session: Session, email: str) -> User | None:
    return session.execute(select(User).where(email_matches(email))).scalar_one_or_none()


class OidcLoginError(Exception):
    """The OP authenticated the subject, but Purser refuses the login (#211)."""


LINKED_ELSEWHERE = (
    "this account is linked to a different SSO identity; ask an administrator to unlink it"
)


def login_with_oidc(
    session: Session,
    *,
    issuer: str,
    subject: str,
    email: str,
    email_verified: bool,
    display_name: str | None,
    allow_provision: bool,
) -> tuple[User, str]:
    """Resolve the user for an OIDC identity and mint a session.

    An identity is the pair (issuer, subject): a subject is unique only at
    its provider (#429, D1). ``issuer`` is the validated ``iss`` of a sign-in,
    so it is always the provider this instance is configured with. In order:

    1. The pair is linked: sign in.
    2. A legacy link (NULL issuer, migration 0041) with this subject: the
       sign-in comes from the configured provider, which is the only one that
       could have made it, so it claims the link and records the issuer.
    3. An account with this email, compared regardless of case (D4), when the
       OP asserts ``email_verified`` — an OP that issues tokens carrying
       unverified, subject-chosen emails must not take over a local account:
       - not linked: link it;
       - linked through another provider: relink (a provider migration);
       - linked to another subject of this provider, or a legacy link: refuse
         and audit (D2). Only an administrator's unlink frees it.
    4. Otherwise provision a member, only when the admin enabled
       ``oidc_auto_provision`` (default off) and the email is verified.

    A refusal raises ``OidcLoginError``; it may leave an audit row that the
    caller commits."""
    issuer = oidc.issuer_key(issuer)
    user = session.execute(
        select(User).where(User.oidc_issuer == issuer, User.oidc_subject == subject)
    ).scalar_one_or_none()
    if user is not None:
        return user, _mint_session(session, user, "oidc")

    legacy = session.execute(
        select(User).where(User.oidc_issuer.is_(None), User.oidc_subject == subject)
    ).scalar_one_or_none()
    if legacy is not None:
        legacy.oidc_issuer = issuer
        audit.record(
            session,
            actor_token_id=None,
            action="user.oidc_issuer_recorded",
            entity_type="user",
            entity_id=legacy.id,
            before={"oidc_issuer": None},
            after={"oidc_issuer": issuer},
        )
        return legacy, _mint_session(session, legacy, "oidc")

    existing = find_user_by_email(session, email)
    if existing is not None:
        if not email_verified:
            raise OidcLoginError(
                "the identity provider did not assert email_verified;"
                " refusing to link this email to an existing account"
            )
        previous = existing.oidc_issuer
        if existing.oidc_subject is not None and previous in (issuer, None):
            audit.record(
                session,
                actor_token_id=None,
                action="user.link_oidc_refused",
                entity_type="user",
                entity_id=existing.id,
                after={"email": email, "issuer": issuer, "linked_issuer": previous},
            )
            raise OidcLoginError(LINKED_ELSEWHERE)
        relink = existing.oidc_subject is not None
        existing.oidc_issuer = issuer
        existing.oidc_subject = subject
        after: dict[str, object] = {"email": email, "issuer": issuer}
        if relink:
            after["previous_issuer"] = previous
        audit.record(
            session,
            actor_token_id=None,
            action="user.link_oidc",
            entity_type="user",
            entity_id=existing.id,
            after=after,
        )
        return existing, _mint_session(session, existing, "oidc")

    if not allow_provision:
        raise OidcLoginError(
            "account not provisioned; ask the admin to create your user"
            " or enable OIDC auto-provisioning"
        )
    if not email_verified:
        raise OidcLoginError(
            "the identity provider did not assert email_verified; refusing to auto-provision"
        )
    user = create_user(
        session,
        email=email,
        password=None,
        display_name=display_name,
        oidc_issuer=issuer,
        oidc_subject=subject,
        actor_token_id=None,
    )
    return user, _mint_session(session, user, "oidc")


def unlink_oidc(session: Session, *, user: User, actor_token_id: uuid.UUID | None) -> None:
    """Remove a user's SSO link (admin only; #429, D2). The account keeps its
    password, if any; its next SSO sign-in with a verified email links again,
    from whichever provider is configured then."""
    if user.oidc_subject is None:
        raise IdentityError("user has no SSO link")
    before = {"oidc_issuer": user.oidc_issuer}
    user.oidc_issuer = None
    user.oidc_subject = None
    audit.record(
        session,
        actor_token_id=actor_token_id,
        action="user.unlink_oidc",
        entity_type="user",
        entity_id=user.id,
        before=before,
        after={"oidc_issuer": None},
    )


def cleanup_sessions(session: Session) -> tuple[int, int]:
    """Hygiene for kind='session' rows (DESIGN §9). Returns (deleted, stamped).

    Hard-deletes dead sessions (absolute-expired, revoked, or idle-timed-out)
    that nothing references — sessions whose only trace is their own
    login/logout bookkeeping, which is deleted with them. Dead sessions
    referenced by the ledger (transactions.created_by, matches.decided_by,
    position_events.actor_token_id) or by any other audit row are
    attribution anchors: those are stamped expired and retained forever.
    """
    now = datetime.now(UTC)
    dead = and_(
        ApiToken.kind == "session",
        or_(
            ApiToken.expires_at <= now,
            func.coalesce(ApiToken.last_used_at, ApiToken.created_at) <= now - session_idle(),
        ),
    )
    # Bookkeeping = audit rows the session wrote about its own login/logout.
    # Anything else it acted on (a transaction, a token it minted, another
    # token it revoked) makes it an attribution anchor.
    bookkeeping = or_(
        AuditLog.action == "auth.login",
        and_(AuditLog.action == "token.revoke", AuditLog.entity_id == ApiToken.id),
    )
    anchored = or_(
        select(Transaction.id).where(Transaction.created_by == ApiToken.id).exists(),
        select(Match.id).where(Match.decided_by == ApiToken.id).exists(),
        # A recorded position event names its author by foreign key (#390).
        select(PositionEvent.id).where(PositionEvent.actor_token_id == ApiToken.id).exists(),
        select(AuditLog.id).where(AuditLog.actor_token_id == ApiToken.id, ~bookkeeping).exists(),
    )
    doomed = session.execute(select(ApiToken.id).where(dead, ~anchored)).scalars().all()
    if doomed:
        session.execute(delete(AuditLog).where(AuditLog.actor_token_id.in_(doomed)))
        session.execute(delete(ApiToken).where(ApiToken.id.in_(doomed)))
    # Retained dead sessions get their death stamped so idle-timeout config
    # changes can never resurrect them and listing stays a pure expiry check.
    unstamped = (
        session.execute(
            select(ApiToken.id).where(
                dead, or_(ApiToken.expires_at.is_(None), ApiToken.expires_at > now)
            )
        )
        .scalars()
        .all()
    )
    if unstamped:
        session.execute(update(ApiToken).where(ApiToken.id.in_(unstamped)).values(expires_at=now))
    stamped = len(unstamped)
    if doomed or stamped:
        audit.record(
            session,
            actor_token_id=None,
            action="session.cleanup",
            entity_type="api_token",
            after={"deleted": len(doomed), "stamped_expired": stamped},
        )
    return len(doomed), stamped


def cleanup_sessions_job() -> None:
    """Hourly scheduler job wrapping cleanup_sessions in its own DB session."""
    from purser.db import get_sessionmaker

    with get_sessionmaker()() as db:
        deleted, stamped = cleanup_sessions(db)
        db.commit()
    if deleted or stamped:
        log.info("session cleanup", deleted=deleted, stamped_expired=stamped)


def _mint_session(session: Session, user: User, method: str) -> str:
    scopes = ADMIN_SESSION_SCOPES if user.role == "admin" else MEMBER_SESSION_SCOPES
    token, raw = create_token(
        session,
        user=user,
        name=f"session:{method}:{user.email}",
        scopes=list(scopes),
        expires_at=datetime.now(UTC) + session_ttl(),
        kind="session",
        actor_token_id=None,
    )
    audit.record(
        session,
        actor_token_id=token.id,
        action="auth.login",
        entity_type="user",
        entity_id=user.id,
        after={"method": method},
    )
    return raw
