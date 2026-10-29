"""Durable event emission (DESIGN §12 decision: events table, replayable).

Emitters call this inside the same DB transaction as the change; webhook
delivery consumes the table asynchronously (issue #11).
"""

import uuid
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from purser.models import Event


def emit(
    session: Session,
    *,
    type_: str,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
    dedup_key: str | None = None,
) -> None:
    """Append an event. ``payload`` is encrypted at rest (#137); ``dedup_key``
    is a plaintext structural id (transfer pair key, forecast shortfall key)
    that the once-only emit guards filter on — a JSONB path over the encrypted
    payload can no longer serve that lookup."""
    session.add(
        Event(
            type=type_,
            entity_type=entity_type,
            entity_id=entity_id,
            payload=payload or {},
            dedup_key=dedup_key,
        )
    )


def emit_once(
    session: Session,
    *,
    type_: str,
    dedup_key: str,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> bool:
    """Emit unless an event of ``type_`` with ``dedup_key`` already exists;
    returns whether it emitted. The check and the insert run under a
    transaction-scoped advisory lock of the pair, so two runs that overlap
    (the daily job and an admin's fetch-now) cannot both emit (#405): the
    second waits for the first to commit, then sees its row."""
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"{type_}\x1f{dedup_key}"},
    )
    seen = session.execute(
        select(Event.id).where(Event.type == type_, Event.dedup_key == dedup_key).limit(1)
    ).first()
    if seen is not None:
        return False
    emit(
        session,
        type_=type_,
        entity_type=entity_type,
        entity_id=entity_id,
        payload=payload,
        dedup_key=dedup_key,
    )
    session.flush()
    return True
