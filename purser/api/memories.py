"""Memories API (issue #176, DESIGN §3.6, decision 48).

Reads need `read`, writes `write` (#437 retired `read:redacted`, which this
surface used to refuse wholesale).

"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from purser import memories as memories_service
from purser.admin.auth import AdminToken, ReadToken, WriteToken
from purser.db import get_session
from purser.ledger import LedgerError, NotFoundError
from purser.models import Memory

DbSession = Annotated[Session, Depends(get_session)]

router = APIRouter(prefix="/memories", tags=["memories"])


def _handle(exc: memories_service.MemoryError_ | LedgerError) -> HTTPException:
    # LedgerError comes from resolving `accounts`/`payees` links: an unknown
    # reference is a 404, a malformed path a 422 (#214) — never a 500.
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))


class MemoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    accounts: list[str] | None = None
    payees: list[str] | None = None
    pinned: bool = False
    activate: bool = False


class MemoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str | None = None
    accounts: list[str] | None = None
    payees: list[str] | None = None
    pinned: bool | None = None


class MemoryOut(BaseModel):
    id: uuid.UUID
    text: str
    state: str
    pinned: bool
    archived: bool
    links: list[str]
    stale: bool
    rules: list[uuid.UUID]


def _out(session: Session, memory: Memory, stale: set[uuid.UUID]) -> MemoryOut:
    from purser.models import Rule

    rule_ids = list(session.execute(select(Rule.id).where(Rule.memory_id == memory.id)).scalars())
    return MemoryOut(
        id=memory.id,
        text=memory.text,
        state=memory.state,
        pinned=memory.pinned,
        archived=memory.archived_at is not None,
        links=memories_service._link_paths(session, memory),
        stale=memory.id in stale,
        rules=rule_ids,
    )


@router.get("")
def list_memories(
    token: ReadToken,
    session: DbSession,
    include_drafts: bool = False,
    include_archived: bool = False,
) -> list[MemoryOut]:
    stmt = select(Memory).options(selectinload(Memory.links))
    if not include_archived:
        stmt = stmt.where(Memory.archived_at.is_(None))
    if not include_drafts:
        stmt = stmt.where(Memory.state == memories_service.STATE_ACTIVE)
    rows = list(session.execute(memories_service._ordered(stmt)).scalars())
    stale = memories_service.stale_ids(session)
    return [_out(session, m, stale) for m in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_memory(body: MemoryCreate, token: WriteToken, session: DbSession) -> MemoryOut:
    try:
        memory = memories_service.create(
            session,
            text=body.text,
            accounts=body.accounts,
            payees=body.payees,
            pinned=body.pinned,
            token=token,
            activate=body.activate,
        )
    except (memories_service.MemoryError_, LedgerError) as exc:
        raise _handle(exc) from exc
    session.commit()
    return _out(session, memory, memories_service.stale_ids(session))


@router.patch("/{memory_id}")
def update_memory(
    memory_id: uuid.UUID, body: MemoryUpdate, token: WriteToken, session: DbSession
) -> MemoryOut:
    memory = session.get(Memory, memory_id)
    if memory is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="memory not found")
    try:
        memory = memories_service.update(
            session,
            memory_id=memory_id,
            token=token,
            text=body.text,
            accounts=body.accounts,
            payees=body.payees,
            pinned=body.pinned,
        )
    except (memories_service.MemoryError_, LedgerError) as exc:
        raise _handle(exc) from exc
    session.commit()
    return _out(session, memory, memories_service.stale_ids(session))


@router.post("/{memory_id}/activate")
def activate_memory(memory_id: uuid.UUID, token: WriteToken, session: DbSession) -> MemoryOut:
    try:
        memory = memories_service.activate(session, memory_id=memory_id, token=token)
    except memories_service.MemoryError_ as exc:
        raise _handle(exc) from exc
    session.commit()
    return _out(session, memory, memories_service.stale_ids(session))


@router.post("/{memory_id}/archive")
def archive_memory(memory_id: uuid.UUID, token: WriteToken, session: DbSession) -> MemoryOut:
    try:
        memory = memories_service.archive(session, memory_id=memory_id, token=token)
    except memories_service.MemoryError_ as exc:
        raise _handle(exc) from exc
    session.commit()
    return _out(session, memory, memories_service.stale_ids(session))


@router.post("/{memory_id}/restore")
def restore_memory(memory_id: uuid.UUID, token: WriteToken, session: DbSession) -> MemoryOut:
    try:
        memory = memories_service.archive(session, memory_id=memory_id, token=token, restore=True)
    except memories_service.MemoryError_ as exc:
        raise _handle(exc) from exc
    session.commit()
    return _out(session, memory, memories_service.stale_ids(session))


@router.delete("/{memory_id}")
def delete_memory(memory_id: uuid.UUID, token: AdminToken, session: DbSession) -> dict[str, Any]:
    """Hard delete: admin only, and recoverable ONLY within
    audit_retention_days — the before-payload in the audit row is the last
    copy. Archive is the normal removal path."""
    from purser import audit

    memory = session.get(Memory, memory_id)
    if memory is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="memory not found")
    audit.record(
        session,
        actor_token_id=token.id,
        action="memory.delete",
        entity_type="memory",
        entity_id=memory.id,
        before={"text": memory.text, "state": memory.state},
    )
    session.delete(memory)
    session.commit()
    return {"deleted": str(memory_id)}
