"""Memories — user-owned financial context served to agents (issue #176,
DESIGN §3.6, decision 48).

**Rules act, review flags ask, memories inform.** A memory that would change
what lands in the ledger is a rule in the wrong table, and this module is the
only place that decides what an agent sees.

Two properties are structural rather than conventional:

- **Non-executable.** Nothing here parses memory text. There is no evaluator,
  no import hook, no scheduler entry that reads one. That absence IS the
  feature.
- **A draft is never served.** ``_serving_filter`` is applied by every read
  path, so an agent — or Purser's own optional authoring tiers — cannot write
  its own future context. For AI-written text to influence a later pass, a
  human must activate it.

Serving is the real risk: 100 memories is roughly 12k tokens, so every read is
budgeted and truncation-accounted (guardrail 6).
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from purser import audit
from purser.logging import get_logger
from purser.models import Account, ApiToken, Memory, MemoryLink, Payee

log = get_logger(__name__)

STATE_DRAFT = "draft"
STATE_ACTIVE = "active"

# Caps (DESIGN §3.6, test-pinned). The draft cap is the anti-flood valve: an
# agent proposing drafts cannot fill the table, and neither can an authoring
# pass.
MAX_TEXT_CHARS = 500
MAX_ACTIVE = 100
MAX_PINNED = 8
MAX_DRAFTS = 25
MAX_LINKS = 8

# Serving budgets. describe() carries a COUNT plus the pinned few; everything
# wider is on demand and reports exact truncation.
DESCRIBE_MAX_ROWS = 8
DESCRIBE_CHAR_BUDGET = 1000
LIST_DEFAULT_ROWS = 10
LIST_MAX_ROWS = 25
LIST_CHAR_BUDGET = 2500
# Per-prompt cap for the classifier consumer: this is a per-ROW cost against
# what may be a small local model.
PROMPT_MAX_MEMORIES = 5
PROMPT_CHAR_BUDGET = 500


class MemoryError_(Exception):
    """A memory request the service refuses (cap hit, bad state)."""


def _serving_filter(stmt: Any) -> Any:
    """Active, unarchived. Applied by EVERY read path — this is what makes
    "a draft is never served" true by construction rather than by review."""
    return stmt.where(Memory.state == STATE_ACTIVE, Memory.archived_at.is_(None))


def _ordered(stmt: Any) -> Any:
    """Deterministic everywhere, so a truncated payload is stable between
    calls rather than reshuffling under the reader."""
    return stmt.order_by(Memory.pinned.desc(), Memory.updated_at.desc(), Memory.id)


def _count(session: Session, *, state: str, archived: bool = False) -> int:
    stmt = select(Memory.id).where(Memory.state == state)
    stmt = stmt.where(Memory.archived_at.is_not(None) if archived else Memory.archived_at.is_(None))
    return len(list(session.execute(stmt).scalars()))


def _check_caps(session: Session, *, state: str, pinned: bool) -> None:
    if state == STATE_DRAFT:
        if _count(session, state=STATE_DRAFT) >= MAX_DRAFTS:
            raise MemoryError_(f"draft cap reached ({MAX_DRAFTS}); approve or discard some first")
        return
    if _count(session, state=STATE_ACTIVE) >= MAX_ACTIVE:
        raise MemoryError_(f"active memory cap reached ({MAX_ACTIVE}); archive some first")
    if pinned:
        pinned_count = len(
            list(
                session.execute(
                    _serving_filter(select(Memory.id)).where(Memory.pinned.is_(True))
                ).scalars()
            )
        )
        if pinned_count >= MAX_PINNED:
            raise MemoryError_(f"pinned cap reached ({MAX_PINNED})")


def _resolve_links(
    session: Session, *, accounts: list[str] | None, payees: list[str] | None
) -> list[MemoryLink]:
    from purser import ledger as ledger_service

    refs = (accounts or []) + (payees or [])
    if len(refs) > MAX_LINKS:
        raise MemoryError_(f"a memory may carry at most {MAX_LINKS} links")
    links: list[MemoryLink] = []
    for ref in accounts or []:
        account = ledger_service.resolve_account(session, ref.strip("[]"))
        links.append(MemoryLink(account_id=account.id))
    for ref in payees or []:
        payee = ledger_service.resolve_payee(session, ref)
        links.append(MemoryLink(payee_id=payee.id))
    return links


def create(
    session: Session,
    *,
    text: str,
    accounts: list[str] | None = None,
    payees: list[str] | None = None,
    pinned: bool = False,
    token: ApiToken | None,
    activate: bool,
) -> Memory:
    """A draft unless the caller asks for ``activate`` (#436). The caller
    checks the ``write`` scope — not this function."""
    body = (text or "").strip()
    if not body:
        raise MemoryError_("a memory needs text")
    if len(body) > MAX_TEXT_CHARS:
        raise MemoryError_(f"a memory is at most {MAX_TEXT_CHARS} characters")
    state = STATE_ACTIVE if activate else STATE_DRAFT
    _check_caps(session, state=state, pinned=pinned)
    memory = Memory(
        text=body,
        state=state,
        pinned=pinned and state == STATE_ACTIVE,
        created_by=token.id if token else None,
        updated_by=token.id if token else None,
    )
    memory.links = _resolve_links(session, accounts=accounts, payees=payees)
    session.add(memory)
    session.flush()
    audit.record(
        session,
        actor_token_id=token.id if token else None,
        action="memory.create",
        entity_type="memory",
        entity_id=memory.id,
        after={"text": body, "state": state, "pinned": memory.pinned},
    )
    return memory


def update(
    session: Session,
    *,
    memory_id: uuid.UUID,
    token: ApiToken | None,
    text: str | None = None,
    accounts: list[str] | None = None,
    payees: list[str] | None = None,
    pinned: bool | None = None,
) -> Memory:
    memory = session.get(Memory, memory_id)
    if memory is None:
        raise MemoryError_("memory not found")
    before = {"text": memory.text, "pinned": memory.pinned, "state": memory.state}
    if text is not None:
        body = text.strip()
        if not body:
            raise MemoryError_("a memory needs text")
        if len(body) > MAX_TEXT_CHARS:
            raise MemoryError_(f"a memory is at most {MAX_TEXT_CHARS} characters")
        memory.text = body
    if pinned is not None:
        if pinned and not memory.pinned:
            _check_caps(session, state=STATE_ACTIVE, pinned=True)
        memory.pinned = pinned and memory.state == STATE_ACTIVE
    if accounts is not None or payees is not None:
        memory.links = _resolve_links(session, accounts=accounts, payees=payees)
    memory.updated_by = token.id if token else None
    memory.updated_at = datetime.now(UTC)
    session.flush()
    audit.record(
        session,
        actor_token_id=token.id if token else None,
        action="memory.update",
        entity_type="memory",
        entity_id=memory.id,
        before=before,
        after={"text": memory.text, "pinned": memory.pinned, "state": memory.state},
    )
    return memory


def activate(session: Session, *, memory_id: uuid.UUID, token: ApiToken | None) -> Memory:
    """Draft -> active. THE moment the containment loop closes, which is why
    it is the user's explicit choice (or the agent's, when the user asked it
    to remember something) and why the drafts inbox badges AI-authored rows."""
    memory = session.get(Memory, memory_id)
    if memory is None:
        raise MemoryError_("memory not found")
    if memory.state == STATE_ACTIVE:
        return memory
    _check_caps(session, state=STATE_ACTIVE, pinned=False)
    memory.state = STATE_ACTIVE
    memory.updated_by = token.id if token else None
    memory.updated_at = datetime.now(UTC)
    session.flush()
    audit.record(
        session,
        actor_token_id=token.id if token else None,
        action="memory.activate",
        entity_type="memory",
        entity_id=memory.id,
        after={"state": STATE_ACTIVE},
    )
    return memory


def archive(
    session: Session, *, memory_id: uuid.UUID, token: ApiToken | None, restore: bool = False
) -> Memory:
    """Archive is the normal removal path and is reversible — user prose is
    not derivable from anything else in the system."""
    memory = session.get(Memory, memory_id)
    if memory is None:
        raise MemoryError_("memory not found")
    memory.archived_at = None if restore else datetime.now(UTC)
    if restore:
        _check_caps(session, state=STATE_ACTIVE, pinned=False)
    else:
        memory.pinned = False
    memory.updated_by = token.id if token else None
    memory.updated_at = datetime.now(UTC)
    session.flush()
    audit.record(
        session,
        actor_token_id=token.id if token else None,
        action="memory.restore" if restore else "memory.archive",
        entity_type="memory",
        entity_id=memory.id,
        after={"archived": not restore},
    )
    return memory


# --------------------------------------------------------------------------
# Serving
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Served:
    rows: list[dict[str, Any]]
    truncated: int
    matched: int


def _link_paths(session: Session, memory: Memory) -> list[str]:
    out: list[str] = []
    for link in memory.links:
        if link.account_id is not None:
            account = session.get(Account, link.account_id)
            if account is not None:
                out.append(account.path)
        elif link.payee_id is not None:
            payee = session.get(Payee, link.payee_id)
            if payee is not None:
                out.append(payee.name)
    return out


def _budgeted(session: Session, rows: list[Memory], *, max_rows: int, char_budget: int) -> Served:
    served: list[dict[str, Any]] = []
    used = 0
    for memory in rows:
        if len(served) >= max_rows or used + len(memory.text) > char_budget:
            break
        served.append(
            {
                "id": str(memory.id),
                "text": memory.text,
                "links": _link_paths(session, memory),
                "pinned": memory.pinned,
            }
        )
        used += len(memory.text)
    return Served(rows=served, truncated=len(rows) - len(served), matched=len(rows))


def serving_block(session: Session) -> dict[str, Any]:
    """What ``describe()`` carries: a COUNT plus the pinned few, never the
    table. The count tells the agent the rest exist; `tool` says how to get
    them."""
    rows = list(
        session.execute(
            _ordered(_serving_filter(select(Memory).options(selectinload(Memory.links))))
        ).scalars()
    )
    pinned = [m for m in rows if m.pinned]
    served = _budgeted(
        session, pinned, max_rows=DESCRIBE_MAX_ROWS, char_budget=DESCRIBE_CHAR_BUDGET
    )
    return {
        "active": len(rows),
        "pinned": served.rows,
        "pinned_truncated": served.truncated,
        "tool": "list_memories",
    }


def list_memories(
    session: Session,
    *,
    account: str | None = None,
    payee: str | None = None,
    include_drafts: bool = False,
    limit: int = LIST_DEFAULT_ROWS,
) -> dict[str, Any]:
    from purser import ledger as ledger_service

    limit = max(1, min(limit, LIST_MAX_ROWS))
    stmt = select(Memory).options(selectinload(Memory.links))
    # include_drafts is the Config UI's drafts inbox — the ONLY read path that
    # sees a draft, and it never feeds an agent.
    stmt = stmt.where(Memory.archived_at.is_(None)) if include_drafts else _serving_filter(stmt)
    if account:
        target = ledger_service.resolve_account(session, account.strip("[]"))
        stmt = stmt.where(
            Memory.id.in_(select(MemoryLink.memory_id).where(MemoryLink.account_id == target.id))
        )
    if payee:
        target_payee = ledger_service.resolve_payee(session, payee)
        stmt = stmt.where(
            Memory.id.in_(
                select(MemoryLink.memory_id).where(MemoryLink.payee_id == target_payee.id)
            )
        )
    rows = list(session.execute(_ordered(stmt)).scalars())
    served = _budgeted(session, rows, max_rows=limit, char_budget=LIST_CHAR_BUDGET)
    return {
        "memories": served.rows,
        "truncated": served.truncated,
        "matched": served.matched,
        **(
            {"advice": "narrow with account= or payee= to see the rest"} if served.truncated else {}
        ),
    }


def relevant_for(
    session: Session,
    *,
    account_ids: list[uuid.UUID] | None = None,
    payee_ids: list[uuid.UUID] | None = None,
    max_rows: int = PROMPT_MAX_MEMORIES,
    char_budget: int = PROMPT_CHAR_BUDGET,
) -> list[dict[str, Any]]:
    """Memories relevant to a row, by EXACT FK match.

    No substring, no name similarity, no embeddings (decisions 35/40). Pinned
    and global memories come first, then exact matches.

    Note what is absent: this takes account and payee ids only. At
    categorization time the CATEGORY is the unknown being decided, so a
    category-linked memory cannot reach the classifier — stated in §3.6
    because it surprises people who link one and wonder why.
    """
    ids = list(account_ids or []) + list(payee_ids or [])
    stmt = _serving_filter(select(Memory).options(selectinload(Memory.links)))
    if ids:
        linked = select(MemoryLink.memory_id).where(
            or_(
                MemoryLink.account_id.in_(account_ids or []),
                MemoryLink.payee_id.in_(payee_ids or []),
            )
        )
        # Global (unlinked) memories apply to everything; pinned ones lead.
        unlinked = select(MemoryLink.memory_id)
        stmt = stmt.where(or_(Memory.id.in_(linked), Memory.id.not_in(unlinked)))
    rows = list(session.execute(_ordered(stmt)).scalars())
    return _budgeted(session, rows, max_rows=max_rows, char_budget=char_budget).rows


def stale_ids(session: Session) -> set[uuid.UUID]:
    """Memories whose links all point at closed or hidden accounts.

    A LINT, never an action: Purser never edits or archives the user's prose
    on its own.
    """
    rows = list(
        session.execute(
            _serving_filter(select(Memory).options(selectinload(Memory.links)))
        ).scalars()
    )
    out: set[uuid.UUID] = set()
    for memory in rows:
        account_links = [link for link in memory.links if link.account_id is not None]
        if not account_links:
            continue
        dead = 0
        for link in account_links:
            account = session.get(Account, link.account_id)
            if account is not None and (
                account.closed_at is not None or account.hidden_at is not None
            ):
                dead += 1
        if dead == len(account_links):
            out.add(memory.id)
    return out
