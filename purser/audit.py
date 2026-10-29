"""Audit logging — every mutation writes audit_log.

Since issue #122 the log also covers every authenticated READ. Rows come in
three kinds (DESIGN §9):

- ``read`` / ``write`` — **call rows**: one per authenticated HTTP request
  (``purser.api.middleware``) or MCP tool call (``audited_tool``), carrying a
  ``detail`` payload (method/path or tool/args, status, duration, row count).
- ``change`` — the per-entity before/after rows ``record()`` has always
  written, now stamped with the surrounding call's ``request_id``.

Call rows are written AFTER the response on their own short session — a
failure to audit a read never fails the request (reads keep availability;
writes keep their in-transaction ``record()`` guarantee). Everything is
pruned after ``audit_retention_days`` (default 90) by ``prune_daily``.
"""

import base64
import binascii
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, select, text
from sqlalchemy.orm import Session

from purser.logging import get_logger
from purser.models import ApiToken, AuditLog, User

log = get_logger(__name__)

KIND_READ = "read"
KIND_WRITE = "write"
KIND_CHANGE = "change"

# One request_id per HTTP request / MCP tool call; set by the capture layer
# so change rows written anywhere downstream group under their call row.
current_request_id: ContextVar[uuid.UUID | None] = ContextVar(
    "audit_current_request_id", default=None
)

# Virtual sessions split on this much inactivity per actor (DESIGN §9).
SESSION_GAP_MINUTES = 30

# detail-payload caps: values are clipped, and the whole args/query dict is
# dropped to a marker if it still serializes too large. Raw tokens/headers
# never enter a detail payload.
_VALUE_CLIP = 200
_DICT_BYTE_CAP = 2048

_PRUNE_BATCH = 5000
_PRUNE_MAX_BATCHES = 200

# route first-segment -> entity_type for call rows (fallback: the segment).
_ENTITY_SEGMENTS = {
    "accounts": "account",
    "audit": "audit",
    "auth": "auth",
    "categories": "category",
    "categorize": "categorize",
    "config": "config",
    "connections": "connection",
    "events": "event",
    "forecast": "forecast",
    "holdings": "holding",
    "import": "import",
    "matching": "match",
    "networth": "networth",
    "position-events": "position",
    "positions": "position",
    "prices": "price",
    "schedules": "schedule",
    "tokens": "api_token",
    "memories": "memory",
    "rules": "rule",
    "transactions": "transaction",
    "users": "user",
    "valuations": "valuation",
    "webhooks": "webhook",
}


def record(
    session: Session,
    *,
    actor_token_id: uuid.UUID | None,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    """Append a change row in the caller's transaction (atomic with the change)."""
    # before_after is encrypted (#137); lift the replacement transaction id into
    # its own plaintext column so provenance-badge lookups filter without
    # decrypting every audit row. Every writer that records a reversal pair puts
    # replacement_id in `after`, so this one seam populates the column.
    replacement_id: uuid.UUID | None = None
    if after is not None:
        raw = after.get("replacement_id")
        if isinstance(raw, str):
            replacement_id = uuid.UUID(raw)
        elif isinstance(raw, uuid.UUID):
            replacement_id = raw
    session.add(
        AuditLog(
            actor_token_id=actor_token_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            before_after={"before": before, "after": after},
            replacement_id=replacement_id,
            kind=KIND_CHANGE,
            request_id=current_request_id.get(),
        )
    )


def clip_values(mapping: dict[str, Any]) -> dict[str, Any] | None:
    """Clip a query/args mapping for a detail payload: stringify + cap each
    value, cap the whole dict, never include credential material."""
    clipped: dict[str, str] = {}
    for key, value in mapping.items():
        if key.lower() in {"authorization", "token", "password"}:
            continue
        raw = value if isinstance(value, str) else repr(value)
        clipped[key] = raw[:_VALUE_CLIP]
    if not clipped:
        return None
    import json

    if len(json.dumps(clipped)) > _DICT_BYTE_CAP:
        return {"_truncated": f"{len(clipped)} values over {_DICT_BYTE_CAP}B cap"}
    return clipped


def entity_for_path(path_template: str) -> str:
    """Entity type for a call row, from the route's path template."""
    segment = path_template.strip("/").split("/", 1)[0]
    return _ENTITY_SEGMENTS.get(segment, segment or "system")


def record_call(
    *,
    actor_token_id: uuid.UUID,
    kind: str,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | None,
    request_id: uuid.UUID,
    detail: dict[str, Any],
    disclosed: list[str] | None = None,
) -> None:
    """Write one call row on its own short session, swallowing failures.

    Runs after the response has been sent (REST) or around the tool result
    (MCP) — availability beats completeness for reads, and this is the single
    seam where buffering/sampling would slot in if volume ever demands it.
    """
    from purser.db import get_sessionmaker

    try:
        with get_sessionmaker()() as session:
            session.add(
                AuditLog(
                    actor_token_id=actor_token_id,
                    action=action,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    before_after=None,
                    kind=kind,
                    request_id=request_id,
                    detail=detail,
                    disclosed=disclosed or None,
                )
            )
            session.commit()
    except Exception:
        log.warning("audit call-row write failed", action=action, exc_info=True)


# --------------------------------------------------------------------------
# Query helpers (admin /audit API)
# --------------------------------------------------------------------------


def encode_cursor(at: datetime, row_id: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(f"{at.isoformat()}|{row_id}".encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID] | None:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        at_str, id_str = raw.split("|", 1)
        return datetime.fromisoformat(at_str), uuid.UUID(id_str)
    except (ValueError, binascii.Error):
        return None


def audit_query(
    *,
    kind: str | None = None,
    actor_token_id: uuid.UUID | None = None,
    unattributed: bool = False,
    action: str | None = None,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    request_id: uuid.UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    disclosed: bool = False,
) -> Select[AuditLog]:
    stmt = select(AuditLog)
    if disclosed:
        # Calls where the agent asked for fields beyond the user's privacy
        # defaults (#437): "what did my AI choose to see".
        stmt = stmt.where(AuditLog.disclosed.is_not(None))
    if kind is not None:
        stmt = stmt.where(AuditLog.kind == kind)
    if actor_token_id is not None:
        stmt = stmt.where(AuditLog.actor_token_id == actor_token_id)
    if unattributed:
        stmt = stmt.where(AuditLog.actor_token_id.is_(None))
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    if entity_type is not None:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if entity_id is not None:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
    if request_id is not None:
        stmt = stmt.where(AuditLog.request_id == request_id)
    if since is not None:
        stmt = stmt.where(AuditLog.at >= since)
    if until is not None:
        stmt = stmt.where(AuditLog.at <= until)
    return stmt


def list_sessions(
    session: Session,
    *,
    since: datetime,
    until: datetime | None = None,
    actor_token_id: uuid.UUID | None = None,
    before: datetime | None = None,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Aggregate audit rows into virtual sessions: per actor, a new session
    starts after SESSION_GAP_MINUTES of silence. NULL actors (scheduler jobs)
    group the same way and render as "System"."""
    params: dict[str, Any] = {
        "since": since,
        "until": until,
        "actor": str(actor_token_id) if actor_token_id else None,
        "before": before,
        "limit": limit,
        "gap": f"{SESSION_GAP_MINUTES} minutes",
    }
    rows = session.execute(
        text(
            """
            WITH scoped AS (
                SELECT actor_token_id, at, kind
                FROM audit_log
                WHERE at >= :since
                  AND (CAST(:until AS timestamptz) IS NULL OR at <= :until)
                  AND (CAST(:actor AS uuid) IS NULL OR actor_token_id = :actor)
            ), marked AS (
                SELECT actor_token_id, at, kind,
                    CASE
                        WHEN LAG(at) OVER w IS NULL THEN 1
                        WHEN at - LAG(at) OVER w > CAST(:gap AS interval) THEN 1
                        ELSE 0
                    END AS new_session
                FROM scoped
                WINDOW w AS (PARTITION BY actor_token_id ORDER BY at)
            ), numbered AS (
                SELECT actor_token_id, at, kind,
                    SUM(new_session) OVER (
                        PARTITION BY actor_token_id ORDER BY at
                        ROWS UNBOUNDED PRECEDING
                    ) AS session_no
                FROM marked
            )
            SELECT actor_token_id,
                MIN(at) AS started_at,
                MAX(at) AS ended_at,
                COUNT(*) FILTER (WHERE kind = 'read') AS reads,
                COUNT(*) FILTER (WHERE kind = 'write') AS writes,
                COUNT(*) FILTER (WHERE kind = 'change') AS changes
            FROM numbered
            GROUP BY actor_token_id, session_no
            HAVING CAST(:before AS timestamptz) IS NULL OR MAX(at) < :before
            ORDER BY ended_at DESC
            LIMIT :limit
            """
        ),
        params,
    ).all()

    token_ids = {r.actor_token_id for r in rows if r.actor_token_id is not None}
    labels: dict[uuid.UUID, tuple[str, str, str | None]] = {}
    if token_ids:
        for token, owner_email in session.execute(
            select(ApiToken, User.email)
            .join(User, ApiToken.user_id == User.id)
            .where(ApiToken.id.in_(token_ids))
        ).all():
            labels[token.id] = (token.name, token.kind, owner_email)

    out: list[dict[str, Any]] = []
    for r in rows:
        name, token_kind, email = labels.get(r.actor_token_id, (None, None, None))
        out.append(
            {
                "actor_token_id": r.actor_token_id,
                "token_name": name if r.actor_token_id else "System",
                "token_kind": token_kind if r.actor_token_id else "system",
                "user_email": email,
                "started_at": r.started_at,
                "ended_at": r.ended_at,
                "reads": r.reads,
                "writes": r.writes,
                "changes": r.changes,
            }
        )
    return out


# --------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------


def prune_expired(now: datetime | None = None) -> int:
    """Delete audit rows older than the configured retention, in id-batches
    so the constant call-row insert stream never waits on a long lock."""
    from purser import appsettings
    from purser.db import get_sessionmaker

    retention = appsettings.effective().audit_retention_days
    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention)
    total = 0
    with get_sessionmaker()() as session:
        for _ in range(_PRUNE_MAX_BATCHES):
            result = session.execute(
                text(
                    "DELETE FROM audit_log WHERE id IN ("
                    "SELECT id FROM audit_log WHERE at < :cutoff "
                    "ORDER BY at LIMIT :batch)"
                ),
                {"cutoff": cutoff, "batch": _PRUNE_BATCH},
            )
            session.commit()
            # execute() is typed as Result; DELETE returns a CursorResult
            deleted = int(getattr(result, "rowcount", 0) or 0)
            total += deleted
            if deleted < _PRUNE_BATCH:
                break
    return total


def prune_job() -> None:
    deleted = prune_expired()
    if deleted:
        log.info("pruned audit rows", deleted=deleted)
