"""Webhook fan-out with retry + delivery log; event replay; bill.late aging.

Alert primitives live here; alert judgment lives in the agent (issue #11).
The events table is durable — a subscriber that was down catches up from it.
"""

import hashlib
import hmac
import json
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from purser import audit, crypto, events
from purser.ledger import LedgerError, NotFoundError
from purser.logging import get_logger
from purser.models import Event, WebhookDelivery, WebhookSubscription

log = get_logger(__name__)

MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 60  # 1m, 4m, 16m, 64m


def create_subscription(
    session: Session,
    *,
    url: str,
    event_types: list[str],
    secret: str | None,
    actor_token_id: uuid.UUID | None,
) -> WebhookSubscription:
    if not url.startswith(("http://", "https://")):
        raise LedgerError("url must be http(s)")
    if not event_types:
        raise LedgerError("subscribe to at least one event type ('*' for all)")
    subscription = WebhookSubscription(
        url=url,
        event_types=event_types,
        secret_enc=crypto.encrypt(secret) if secret else None,
    )
    session.add(subscription)
    session.flush()
    audit.record(
        session,
        actor_token_id=actor_token_id,
        action="webhook.create",
        entity_type="webhook_subscription",
        entity_id=subscription.id,
        after={"url": url, "event_types": event_types, "has_secret": secret is not None},
    )
    return subscription


def deactivate_subscription(
    session: Session, *, subscription_id: uuid.UUID, actor_token_id: uuid.UUID | None
) -> WebhookSubscription:
    subscription = session.get(WebhookSubscription, subscription_id)
    if subscription is None:
        raise NotFoundError(f"subscription {subscription_id} not found")
    subscription.active = False
    session.flush()
    audit.record(
        session,
        actor_token_id=actor_token_id,
        action="webhook.deactivate",
        entity_type="webhook_subscription",
        entity_id=subscription.id,
    )
    return subscription


def _matches(subscription: WebhookSubscription, event_type: str) -> bool:
    return "*" in subscription.event_types or event_type in subscription.event_types


def fan_out(session: Session) -> int:
    """Create pending deliveries for events past each active subscription's
    cursor (#219, decision 54).

    ``cursor_at`` replaces the full-events-table anti-join: each pass scans
    only ``at >= cursor_at`` and advances the cursor to the newest event
    scanned. The scan deliberately uses ``>=`` — same-timestamp ties on the
    boundary are re-scanned next pass and the ``(subscription, event)``
    unique constraint eats them via ON CONFLICT DO NOTHING, which also
    closes the IntegrityError race between concurrent passes."""
    created = 0
    subscriptions = list(
        session.execute(
            select(WebhookSubscription).where(WebhookSubscription.active.is_(True))
        ).scalars()
    )
    for subscription in subscriptions:
        events = (
            session.execute(
                select(Event).where(Event.at >= subscription.cursor_at).order_by(Event.at)
            )
            .scalars()
            .all()
        )
        for event in events:
            if not _matches(subscription, event.type):
                continue
            inserted = session.execute(
                pg_insert(WebhookDelivery)
                .values(subscription_id=subscription.id, event_id=event.id)
                .on_conflict_do_nothing(index_elements=["subscription_id", "event_id"])
                .returning(WebhookDelivery.id)
            ).first()
            if inserted is not None:
                created += 1
        if events:
            subscription.cursor_at = events[-1].at
    session.flush()
    return created


def _event_body(event: Event) -> bytes:
    return json.dumps(
        {
            "id": str(event.id),
            "at": event.at.isoformat(),
            "type": event.type,
            "entity_type": event.entity_type,
            "entity_id": str(event.entity_id) if event.entity_id else None,
            "payload": event.payload,
        },
        sort_keys=True,
    ).encode()


def _url_host(url: str) -> str:
    """Log-safe host of a subscription URL — never the full URL, never the secret."""
    try:
        return httpx.URL(url).host
    except Exception:  # malformed URL — the delivery attempt fails for the same reason
        return "<invalid-url>"


def process_due(session: Session, *, client: httpx.Client | None = None) -> dict[str, int]:
    """Attempt due deliveries; 2xx marks delivered, failures back off
    exponentially and give up (status=failed) after MAX_ATTEMPTS.

    One poison delivery (undecryptable secret, malformed URL, any unexpected
    error) must never wedge the batch: the failed attempt is recorded on that
    delivery alone and the loop moves on to the next one.
    """
    now = datetime.now(UTC)
    due = list(
        session.execute(
            select(WebhookDelivery)
            .where(WebhookDelivery.status == "pending", WebhookDelivery.next_attempt_at <= now)
            .order_by(WebhookDelivery.created_at)
            .limit(100)
        ).scalars()
    )
    stats = {"delivered": 0, "retried": 0, "failed": 0}
    for delivery in due:
        subscription = session.get(WebhookSubscription, delivery.subscription_id)
        event = session.get(Event, delivery.event_id)
        assert subscription is not None and event is not None
        if not subscription.active:
            delivery.status = "failed"
            stats["failed"] += 1
            continue
        delivery.attempts += 1
        delivery.last_attempt_at = now
        ok = False
        try:
            body = _event_body(event)
            headers = {
                "Content-Type": "application/json",
                "X-Purser-Event": event.type,
                "X-Purser-Delivery": str(delivery.id),
            }
            if subscription.secret_enc:
                secret = crypto.decrypt(subscription.secret_enc)
                headers["X-Purser-Signature"] = hmac.new(
                    secret.encode(), body, hashlib.sha256
                ).hexdigest()
            with nullcontext(client) if client is not None else httpx.Client(timeout=10) as http:
                resp = http.post(subscription.url, content=body, headers=headers)
            delivery.response_status = resp.status_code
            ok = 200 <= resp.status_code < 300
        except httpx.HTTPError:
            delivery.response_status = None
        except Exception as exc:
            delivery.response_status = None
            log.warning(
                "webhook.delivery_error",
                delivery_id=str(delivery.id),
                url_host=_url_host(subscription.url),
                error=type(exc).__name__,
            )
        # Shared backoff-or-failed decision for every non-2xx outcome.
        if ok:
            delivery.status = "delivered"
            stats["delivered"] += 1
        elif delivery.attempts >= MAX_ATTEMPTS:
            delivery.status = "failed"
            stats["failed"] += 1
        else:
            backoff = BACKOFF_BASE_SECONDS * (4 ** (delivery.attempts - 1))
            delivery.next_attempt_at = now + timedelta(seconds=backoff)
            stats["retried"] += 1
    session.flush()
    return stats


def replay_events(
    session: Session,
    *,
    since_id: uuid.UUID | None = None,
    since: datetime | None = None,
    type_: str | None = None,
    limit: int = 100,
) -> list[Event]:
    """Durable replay: a subscriber that was down catches up from here.

    Keyset pagination on (at, id): Event.at is transaction time, so
    batch-inserted events share a timestamp — a bare `at > anchor.at`
    filter would silently skip the anchor's same-timestamp siblings.
    """
    stmt = select(Event).order_by(Event.at, Event.id).limit(limit)
    if since_id is not None:
        anchor = session.get(Event, since_id)
        if anchor is None:
            raise NotFoundError(f"event {since_id} not found")
        stmt = stmt.where(tuple_(Event.at, Event.id) > (anchor.at, anchor.id))
    if since is not None:
        stmt = stmt.where(Event.at > since)
    if type_ is not None:
        stmt = stmt.where(Event.type == type_)
    return list(session.execute(stmt).scalars())


# ---------------------------------------------------------------------------
# bill.late — unmatched-expected aging, no polling required
# ---------------------------------------------------------------------------


def emit_late_bills(session: Session) -> int:
    """Daily job: any open expected transaction past its date gets ONE
    bill.late event (deduped per transaction)."""
    from purser import matching

    emitted = 0
    for row in matching.list_unmatched(session, direction="expected"):
        if row["aging_days"] < 1:
            continue
        existing = session.execute(
            select(Event.id).where(
                Event.type == "bill.late", Event.entity_id == row["transaction_id"]
            )
        ).first()
        if existing is not None:
            continue
        events.emit(
            session,
            type_="bill.late",
            entity_type="transaction",
            entity_id=row["transaction_id"],
            payload={
                "date": row["date"].isoformat(),
                "aging_days": row["aging_days"],
                "payee": row["payee"],
                "amount": str(row["amount"]) if row["amount"] is not None else None,
            },
        )
        emitted += 1
    session.flush()
    return emitted


def delivery_job() -> None:
    """Scheduled: age late bills, fan out, deliver due webhooks."""
    from purser.db import get_sessionmaker

    with get_sessionmaker()() as session:
        emit_late_bills(session)
        fan_out(session)
        process_due(session)
        session.commit()
