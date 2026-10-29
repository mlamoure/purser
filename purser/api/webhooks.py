"""Events replay + webhook subscription API."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from purser import webhooks as service
from purser.admin.auth import AdminToken, ReadToken
from purser.db import get_session
from purser.ledger import LedgerError, NotFoundError
from purser.models import WebhookDelivery, WebhookSubscription

DbSession = Annotated[Session, Depends(get_session)]

events_router = APIRouter(prefix="/events", tags=["events"])
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


class EventOut(BaseModel):
    id: uuid.UUID
    at: datetime
    type: str
    entity_type: str | None
    entity_id: uuid.UUID | None
    payload: dict[str, Any]


class SubscriptionCreate(BaseModel):
    url: str
    event_types: list[str] = ["*"]
    secret: str | None = None  # stored encrypted; used for HMAC signatures


class SubscriptionOut(BaseModel):
    id: uuid.UUID
    url: str
    event_types: list[str]
    active: bool
    has_secret: bool


class DeliveryOut(BaseModel):
    id: uuid.UUID
    event_id: uuid.UUID
    status: str
    attempts: int
    response_status: int | None
    last_attempt_at: datetime | None
    next_attempt_at: datetime


def _handle(exc: LedgerError) -> HTTPException:
    code = status.HTTP_404_NOT_FOUND if isinstance(exc, NotFoundError) else 422
    return HTTPException(code, str(exc))


@events_router.get("")
def replay_events(
    token: ReadToken,
    session: DbSession,
    since_id: Annotated[uuid.UUID | None, Query()] = None,
    since: Annotated[datetime | None, Query()] = None,
    type: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[EventOut]:
    """Durable event replay — a subscriber that was down for a day catches
    up by paging from its last seen event id or timestamp."""
    try:
        rows = service.replay_events(
            session, since_id=since_id, since=since, type_=type, limit=limit
        )
    except LedgerError as exc:
        raise _handle(exc) from exc
    return [EventOut.model_validate(e, from_attributes=True) for e in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_subscription(
    body: SubscriptionCreate, token: AdminToken, session: DbSession
) -> SubscriptionOut:
    try:
        subscription = service.create_subscription(
            session,
            url=body.url,
            event_types=body.event_types,
            secret=body.secret,
            actor_token_id=token.id,
        )
    except LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    return SubscriptionOut(
        id=subscription.id,
        url=subscription.url,
        event_types=subscription.event_types,
        active=subscription.active,
        has_secret=subscription.secret_enc is not None,
    )


@router.get("")
def list_subscriptions(token: AdminToken, session: DbSession) -> list[SubscriptionOut]:
    rows = session.execute(select(WebhookSubscription)).scalars()
    return [
        SubscriptionOut(
            id=s.id,
            url=s.url,
            event_types=s.event_types,
            active=s.active,
            has_secret=s.secret_enc is not None,
        )
        for s in rows
    ]


@router.delete("/{subscription_id}")
def deactivate(subscription_id: uuid.UUID, token: AdminToken, session: DbSession) -> dict[str, str]:
    try:
        service.deactivate_subscription(
            session, subscription_id=subscription_id, actor_token_id=token.id
        )
    except LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    return {"status": "deactivated"}


@router.get("/{subscription_id}/deliveries")
def list_deliveries(
    subscription_id: uuid.UUID, token: AdminToken, session: DbSession
) -> list[DeliveryOut]:
    rows = session.execute(
        select(WebhookDelivery)
        .where(WebhookDelivery.subscription_id == subscription_id)
        .order_by(WebhookDelivery.created_at.desc())
        .limit(100)
    ).scalars()
    return [DeliveryOut.model_validate(d, from_attributes=True) for d in rows]


@router.post("/deliver")
def run_delivery(token: AdminToken, session: DbSession) -> dict[str, int]:
    """Run one late-bill + fan-out + delivery cycle now (also scheduled)."""
    late = service.emit_late_bills(session)
    fanned = service.fan_out(session)
    stats = service.process_due(session)
    session.commit()
    return {"late_bills_emitted": late, "deliveries_created": fanned, **stats}
