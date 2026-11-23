"""Human-readable audit activity, complete filtering and bounded-page exports."""

import csv
import io
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import Select, case, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from purser import appsettings, audit, sharing
from purser.admin.auth import AdminToken
from purser.db import get_session, get_sessionmaker
from purser.models import Account, ApiToken, AuditLog, AuditReceipt

router = APIRouter(prefix="/audit", tags=["audit"])
DbSession = Annotated[Session, Depends(get_session)]


class ActivityFilter(BaseModel):
    view: Literal["sharing", "changes", "access", "technical"] = "sharing"
    integration_id: uuid.UUID | None = None
    actor_token_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    data_type: str | None = None
    outcome: (
        Literal["shared", "empty", "denied", "failed", "uncertain", "legacy", "changed"] | None
    ) = None
    extra: bool = False
    # requested: calls that asked to withhold fields (privacy used);
    # identifiable: calls whose answer returned payees, descriptions or amounts.
    privacy: Literal["requested", "identifiable"] | None = None
    since: datetime | None = None
    until: datetime | None = None
    request_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def valid_window(self) -> "ActivityFilter":
        for value in (self.since, self.until):
            if value is not None and value.tzinfo is None:
                raise ValueError("audit timestamps must include a time zone")
        if self.since and self.until and self.until < self.since:
            raise ValueError("end must not precede start")
        return self


class SharingSummary(BaseModel):
    version: int = 1
    fields: list[str] = Field(default_factory=list)
    data_types: list[str] = Field(default_factory=list)
    withheld: list[str] = Field(default_factory=list)
    record_count: int | None = None
    count_unit: str = "records"
    accounts: list[str] = Field(default_factory=list)
    requested_accounts: list[str] = Field(default_factory=list)
    requested_coverage: dict[str, str] = Field(default_factory=dict)
    returned_dates: dict[str, str] | None = None
    response_has_more: bool = False
    partial: bool = False
    empty: bool = False


class PrivacyRecord(BaseModel):
    """What a call row says about privacy (#613): the fields the call asked to
    withhold, the fields its answer left out, and the payee, description and
    amount fields it returned with real values."""

    requested: list[str]
    withheld: list[str]
    identifiable: list[str]


class AuditIdentity(BaseModel):
    token_id: str | None = None
    name: str = "System"
    kind: str = "system"
    integration_id: str | None = None
    user_id: str | None = None
    user_name: str | None = None
    scopes: list[str] = Field(default_factory=list)
    created_at: str | None = None
    authorization: dict[str, Any] | None = None
    historical: bool = True


class ActivityOut(BaseModel):
    id: uuid.UUID
    at: datetime
    kind: str
    action: str
    entity_type: str
    entity_id: uuid.UUID | None
    request_id: uuid.UUID | None
    channel: str | None
    outcome: str
    identity: AuditIdentity
    sharing: SharingSummary | None
    # Null on rows written before #613 and on rows that are not calls.
    privacy: PrivacyRecord | None
    extra_fields: list[str]
    requested_extra: list[str]
    before_after: dict[str, Any] | None
    technical: dict[str, Any]
    completion_point: str | None


class ActivityPage(BaseModel):
    items: list[ActivityOut]
    next_cursor: str | None
    until: datetime


def _outcome() -> ColumnElement[str]:
    return case(
        (AuditLog.outcome.is_(None), "legacy"),
        (
            (AuditLog.outcome.in_(["shared", "empty"]))
            & ((AuditReceipt.event_id.is_(None)) | (AuditReceipt.state == "interrupted")),
            "uncertain",
        ),
        else_=AuditLog.outcome,
    )


def _query(filters: ActivityFilter) -> Select[AuditLog, ApiToken, str]:
    outcome = _outcome()
    stmt = (
        select(AuditLog, ApiToken, outcome)
        .outerjoin(ApiToken, ApiToken.id == AuditLog.actor_token_id)
        .outerjoin(AuditReceipt, AuditReceipt.event_id == AuditLog.id)
    )
    access = AuditLog.entity_type.in_(["auth", "api_token", "user", "config"])
    if filters.view == "sharing":
        stmt = stmt.where(
            func.coalesce(AuditLog.actor_kind, ApiToken.kind) == "api",
            AuditLog.kind.in_(["read", "write"]),
            ~access,
            AuditLog.entity_type != "audit",
        )
    elif filters.view == "changes":
        stmt = stmt.where(AuditLog.kind == "change", ~access)
    elif filters.view == "access":
        stmt = stmt.where(access)
    if filters.integration_id:
        stmt = stmt.where(
            or_(
                func.coalesce(AuditLog.integration_id, ApiToken.integration_id)
                == filters.integration_id,
                (AuditLog.entity_type == "api_token")
                & AuditLog.entity_id.in_(
                    select(ApiToken.id)
                    .where(ApiToken.integration_id == filters.integration_id)
                    .correlate(None)
                ),
            )
        )
    if filters.actor_token_id:
        stmt = stmt.where(AuditLog.actor_token_id == filters.actor_token_id)
    if filters.account_id:
        stmt = stmt.where(AuditLog.account_ids.contains([filters.account_id]))
    if filters.data_type:
        stmt = stmt.where(AuditLog.data_types.contains([filters.data_type]))
    if filters.outcome:
        stmt = stmt.where(outcome == filters.outcome)
    if filters.extra:
        stmt = stmt.where(AuditLog.disclosed.is_not(None), AuditLog.channel.is_not(None))
    if filters.privacy == "requested":
        stmt = stmt.where(func.cardinality(AuditLog.privacy_requested) > 0)
    elif filters.privacy == "identifiable":
        stmt = stmt.where(func.cardinality(AuditLog.identifiable) > 0)
    if filters.since:
        stmt = stmt.where(AuditLog.at >= filters.since)
    if filters.until:
        stmt = stmt.where(AuditLog.at <= filters.until)
    if filters.request_id:
        stmt = stmt.where(AuditLog.request_id == filters.request_id)
    return stmt


def _entry(session: Session, row: AuditLog, token: ApiToken | None, outcome: str) -> ActivityOut:
    detail = row.detail or {}
    who = detail.get("identity")
    if not who:
        who = (
            {
                **sharing.identity(session, token),
                "historical": False,
                "scopes": [],
                "authorization": None,
            }
            if token
            else {
                "name": "System" if row.actor_token_id is None else "Unknown credential",
                "kind": "system" if row.actor_token_id is None else "unknown",
                "historical": False,
            }
        )
    return ActivityOut(
        id=row.id,
        at=row.at,
        kind=row.kind,
        action=row.action,
        entity_type=row.entity_type,
        entity_id=row.entity_id,
        request_id=row.request_id,
        channel=row.channel,
        outcome=outcome,
        identity=AuditIdentity.model_validate(who),
        sharing=SharingSummary.model_validate(detail["sharing"]) if detail.get("sharing") else None,
        privacy=PrivacyRecord.model_validate(detail["privacy"]) if detail.get("privacy") else None,
        extra_fields=(row.disclosed or []) if row.channel else [],
        requested_extra=detail.get("requested_extra")
        or (row.disclosed or [] if not row.channel else []),
        before_after=row.before_after,
        technical={k: v for k, v in detail.items() if k not in {"identity", "sharing", "privacy"}},
        completion_point=detail.get("completion_point")
        or ("http_send" if row.channel == "rest" else None),
    )


def _page(
    session: Session, filters: ActivityFilter, cursor: str | None, limit: int
) -> ActivityPage:
    filters = filters.model_copy(update={"until": filters.until or datetime.now(UTC)})
    stmt = _query(filters)
    if cursor:
        decoded = audit.decode_cursor(cursor)
        if decoded is None or decoded[0].tzinfo is None:
            raise HTTPException(422, "invalid cursor")
        at, row_id = decoded
        stmt = stmt.where(or_(AuditLog.at < at, (AuditLog.at == at) & (AuditLog.id < row_id)))
    rows = session.execute(
        stmt.order_by(AuditLog.at.desc(), AuditLog.id.desc()).limit(limit + 1)
    ).all()
    more = len(rows) > limit
    rows = rows[:limit]
    return ActivityPage(
        items=[_entry(session, row, token, outcome) for row, token, outcome in rows],
        next_cursor=audit.encode_cursor(rows[-1][0].at, rows[-1][0].id) if more else None,
        until=filters.until,
    )


@router.get("/activity")
def activity(
    _: AdminToken,
    session: DbSession,
    filters: Annotated[ActivityFilter, Query()],
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> ActivityPage:
    return _page(session, filters, cursor, limit)


class AuditOption(BaseModel):
    id: str
    name: str


class AuditOptions(BaseModel):
    integrations: list[AuditOption]
    accounts: list[AuditOption]
    data_types: list[str]
    retention_days: int
    retained_since: datetime | None
    retention_cutoff: datetime
    capture_health: dict[str, Any]


@router.get("/options")
def options(_: AdminToken, session: DbSession) -> AuditOptions:
    integrations: dict[str, str] = {}
    for token in session.scalars(
        select(ApiToken).where(ApiToken.kind == "api").order_by(ApiToken.created_at)
    ):
        integrations[str(token.integration_id or token.id)] = token.name
    retention = appsettings.effective().audit_retention_days
    return AuditOptions(
        integrations=[AuditOption(id=k, name=v) for k, v in integrations.items()],
        accounts=[
            AuditOption(id=str(a.id), name=a.name)
            for a in session.scalars(select(Account).where(Account.type.not_like("category%")))
        ],
        data_types=list(session.scalars(select(func.unnest(AuditLog.data_types)).distinct())),
        retention_days=retention,
        retained_since=session.scalar(select(func.min(AuditLog.at))),
        retention_cutoff=datetime.now(UTC) - timedelta(days=retention),
        capture_health=audit.capture_health(),
    )


@router.get("/export")
def export_activity(
    _: AdminToken,
    filters: Annotated[ActivityFilter, Query()],
    format: Literal["json", "csv"] = "json",
) -> StreamingResponse:
    frozen = filters.model_copy(update={"until": filters.until or datetime.now(UTC)})

    def generate() -> Iterator[str]:
        cursor = None
        first = True
        output = io.StringIO()
        writer = csv.writer(output)
        if format == "json":
            yield '{"filters":' + frozen.model_dump_json() + ',"items":['
        else:
            writer.writerow(
                [
                    "event_id",
                    "at_utc",
                    "integrator",
                    "integration_id",
                    "outcome",
                    "action",
                    "privacy_requested",
                    "identifiable",
                    "evidence_json",
                ]
            )
            yield output.getvalue()
            output.seek(0)
            output.truncate()
        while True:
            with get_sessionmaker()() as session:
                page = _page(session, frozen, cursor, 250)
            for entry in page.items:
                if format == "json":
                    yield ("" if first else ",") + entry.model_dump_json()
                else:
                    # Spreadsheet formula injection is possible even in a token name.
                    def safe(value: str) -> str:
                        return (
                            "'" + value
                            if value.lstrip().startswith(("=", "+", "-", "@"))
                            else value
                        )

                    writer.writerow(
                        [
                            str(entry.id),
                            entry.at.isoformat(),
                            safe(entry.identity.name),
                            entry.identity.integration_id or "",
                            entry.outcome,
                            safe(entry.action),
                            " ".join(entry.privacy.requested) if entry.privacy else "",
                            " ".join(entry.privacy.identifiable) if entry.privacy else "",
                            entry.model_dump_json(),
                        ]
                    )
                    yield output.getvalue()
                    output.seek(0)
                    output.truncate()
                first = False
            cursor = page.next_cursor
            if cursor is None:
                break
        if format == "json":
            yield "]}"

    return StreamingResponse(
        generate(),
        media_type="application/json" if format == "json" else "text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="purser-audit.{format}"',
            "Cache-Control": "no-store",
        },
    )
