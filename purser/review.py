"""New-transaction review flags (issue #175, DESIGN §4, decision 47).

The unreviewed set is a FILTER over the registers, not a second row source —
0.5.0 replaced the review queue with per-account registers deliberately, and
this must not quietly rebuild it.

Membership is exactly: live chain heads with ``origin IN ('system','rule')``,
or ``origin='import'`` with a non-NULL ``source_fetch_id``. That last clause
reuses decision 45's feed-row definition, because QIF and CSV imports are also
``origin='import'`` and a 6,081-row Quicken import must not become 6,081
review items — the user chose those rows. "Live" is ``pending``/``posted``:
an ``expected`` row — a schedule instance, though its origin is ``rule`` — is
a forecast, never a member.

That membership test exists ONCE, as ``unreviewed_clause()``. The count, the
batch, the aged sweep, ``GET /transactions?reviewed=`` and the per-row
``needs_review`` flag the register badge renders all evaluate it (#248): a
client re-deriving it from ``reviewed_at`` flags every row that simply never
needed a mark.

A row LEAVES the set when anything token-bearing writes to it, or on an
explicit mark. Automation that merely *touches* a row never marks it;
automation that *reviews* it does, via ``via='ai_review'``, and only when the
operator enabled the backend reviewer. ``via`` carries that difference into
the audit trail permanently, so a row the backend cleared is never confusable
with one a human looked at.

A mark FOLLOWS its bank line (decision 82): a pending's mark is copied to its
posted successor's live head when that head has no mark and its category legs
equal the pending's (``carry_to_successor``), a system correction's
replacement takes the original's mark (``inherit``), and an unlinked bank
line's clone takes its void row's (``copy_to_clone``). Each copy is whole —
actor, ``via``, time, note, escalation — with ``carried_from`` naming the
source, through one copier, ``_copy_mark``. A merge or an absorb never
carries a mark: pairing rows into a transfer is a judgement of its own.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.orm import Session

from purser import audit
from purser.logging import get_logger
from purser.models import Account, ApiToken, Posting, Transaction, TransactionReview

log = get_logger(__name__)

VIA_EXPLICIT = "explicit"
VIA_UPDATE = "update"
VIA_POST = "post"
VIA_CONFIRM_TRANSFER = "confirm_transfer"
VIA_MATCH = "match"
VIA_UNLINK = "unlink"
VIA_AI_REVIEW = "ai_review"
VIA_AGED = "aged"
VIA_BACKFILL = "backfill"

# ~6 short lines. The reviewer needs a checklist, not an essay: this rides in
# every review_batch payload and counts against the token budget.
DEFAULT_GUIDANCE: tuple[str, ...] = (
    "Is this a transfer between your own accounts? If so set the category to "
    "[account path]. Escalate instead when more than one row could be its pair.",
    "Does the category match this payee's history? The provenance says who "
    "chose it — 'rule' and 'deterministic' are past decisions, 'ai' is a guess.",
    "Is the payee the bank's raw descriptor rather than a friendly name?",
    "Do the amount and date look ordinary for this account?",
    "A fix you find yourself making repeatedly should graduate into a rule — "
    "propose_rule. These flags are the record of what rules failed to catch.",
    "If a surface you needed was unavailable (see `context`), escalate rather "
    "than clearing the row confidently.",
)


def scope_clause() -> Any:
    """Rows that can EVER be unreviewed. Mirrors the partial index in 0021."""
    return and_(
        Transaction.superseded_by.is_(None),
        Transaction.status.in_(["pending", "posted"]),
        or_(
            Transaction.origin.in_(["system", "rule"]),
            and_(
                Transaction.origin == "import",
                Transaction.source_fetch_id.is_not(None),
            ),
        ),
    )


def unreviewed_clause() -> Any:
    """THE unreviewed predicate: in scope, a chain head, and never marked.

    Every surface that answers "does this row need review?" evaluates this
    one clause rather than restating it, so they cannot drift apart (#248).
    ``scope_clause`` mirrors the 0021 partial index, which cannot hold the
    chain-head subquery, so that test joins here: a corrected row's review
    state rides on its replacement (``inherit``), and the original and its
    reversal are never asked about.
    """
    reviewed = select(TransactionReview.transaction_id)
    reversed_ids = select(Transaction.reverses_id).where(Transaction.reverses_id.is_not(None))
    return and_(
        scope_clause(),
        Transaction.id.not_in(reversed_ids),
        Transaction.id.not_in(reviewed),
    )


def _unreviewed_ids() -> Select[uuid.UUID]:
    return select(Transaction.id).where(unreviewed_clause())


def needs_review_ids(session: Session, tx_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """Which of these rows are in the unreviewed set — the per-row flag the
    register's "needs review" badge renders. One query per page, by the same
    clause as the count and ``?reviewed=false``, so the badge can never claim
    a row the count does not (#248)."""
    if not tx_ids:
        return set()
    return set(
        session.execute(
            select(Transaction.id).where(Transaction.id.in_(tx_ids)).where(unreviewed_clause())
        ).scalars()
    )


def unreviewed_count(session: Session) -> int:
    return len(list(session.execute(_unreviewed_ids()).scalars()))


def oldest_unreviewed_days(session: Session) -> int | None:
    row = session.execute(
        select(Transaction.date)
        .where(Transaction.id.in_(_unreviewed_ids()))
        .order_by(Transaction.date)
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    return (datetime.now(UTC).date() - row).days


def is_reviewed(session: Session, tx_id: uuid.UUID) -> bool:
    return session.get(TransactionReview, tx_id) is not None


def mark(
    session: Session,
    *,
    tx_id: uuid.UUID,
    token: ApiToken | None,
    via: str,
    note: str | None = None,
    escalate: bool = False,
) -> bool:
    """Idempotent and monotonic. Returns True when this call created the mark.

    There is no un-review in 0.14: an "unreview" is an invitation to churn,
    and re-surfacing is better expressed by the row's content changing.
    """
    existing = session.get(TransactionReview, tx_id)
    if existing is not None:
        if escalate and existing.escalated_at is None:
            existing.escalated_at = datetime.now(UTC)
            session.flush()
        return False
    row = TransactionReview(
        transaction_id=tx_id,
        reviewed_by=token.id if token else None,
        via=via,
        note=note,
        escalated_at=datetime.now(UTC) if escalate else None,
    )
    session.add(row)
    session.flush()
    audit.record(
        session,
        actor_token_id=token.id if token else None,
        action="transaction.reviewed",
        entity_type="transaction",
        entity_id=tx_id,
        after={"via": via, "note": note, "escalated": escalate},
    )
    return True


def mark_if_in_scope(
    session: Session, *, tx: Transaction | None, token: ApiToken | None, via: str
) -> None:
    """Service-layer helper for the implicit marks.

    Cheap and total: callers do not have to know whether a row is in scope,
    and marking something outside it would be harmless but noisy in the audit
    log, so filter here rather than at each of the five call sites.
    """
    if tx is None or token is None:
        return
    in_scope = session.execute(
        select(Transaction.id).where(Transaction.id == tx.id).where(scope_clause())
    ).scalar_one_or_none()
    if in_scope is None:
        return
    mark(session, tx_id=tx.id, token=token, via=via)


CARRY_PENDING_POSTED = "pending_posted"
CARRY_CORRECTION = "correction"
CARRY_UNLINK = "unlink"


def _copy_mark(
    session: Session, *, source: TransactionReview, target_id: uuid.UUID, reason: str
) -> bool:
    """Copy ``source`` onto ``target_id`` WHOLE — actor, ``via``, time, note,
    escalation — with ``carried_from`` naming the source row (decision 82).
    Marks are only ever added: a target that already has one keeps it and
    nothing is written. The copy is automation (NULL audit actor); the mark
    still names who made it. Returns True when it copied."""
    if session.get(TransactionReview, target_id) is not None:
        return False
    session.add(
        TransactionReview(
            transaction_id=target_id,
            reviewed_at=source.reviewed_at,
            reviewed_by=source.reviewed_by,
            via=source.via,
            note=source.note,
            escalated_at=source.escalated_at,
            carried_from=source.transaction_id,
        )
    )
    session.flush()
    audit.record(
        session,
        actor_token_id=None,
        action="transaction.reviewed",
        entity_type="transaction",
        entity_id=target_id,
        after={
            "via": source.via,
            "carried_from": str(source.transaction_id),
            "reason": reason,
            "reviewed_by": str(source.reviewed_by) if source.reviewed_by else None,
            "reviewed_at": source.reviewed_at.isoformat(),
            "escalated": source.escalated_at is not None,
        },
    )
    return True


def _category_legs(session: Session, tx_id: uuid.UUID) -> set[uuid.UUID]:
    """The category (virtual-account) legs of a row, as account ids."""
    return set(
        session.execute(
            select(Posting.account_id)
            .join(Account, Account.id == Posting.account_id)
            .where(Posting.transaction_id == tx_id, Account.is_virtual.is_(True))
        ).scalars()
    )


def carry_to_successor(session: Session, *, pending: Transaction, successor: Transaction) -> bool:
    """A pending superseded by its posted ``successor`` hands its review mark
    on (decision 82, #342): the bank posting the row a person already looked
    at is the same bank line (decision 29's rule), so it must not come back
    for review.

    The mark goes to the successor's live HEAD — post-sync categorization may
    already have corrected the posted row, and a reversed row is never asked
    about — and only when that head is in review scope, has no mark of its
    own (marks are only added), and carries the pending's category legs. A
    different category means the posted row says something the person never
    saw (its category came from rules or payee memory, not from the
    pending), so it comes back for review: re-surfacing follows content.
    Returns True when it copied."""
    source = session.get(TransactionReview, pending.id)
    if source is None:
        return False
    from purser import schedules  # function-local: a service far above this one

    head_id = schedules.chain_heads(session, [successor.id])[successor.id]
    in_scope = session.execute(
        select(Transaction.id).where(Transaction.id == head_id).where(scope_clause())
    ).scalar_one_or_none()
    if in_scope is None:
        return False
    if _category_legs(session, pending.id) != _category_legs(session, head_id):
        return False
    return _copy_mark(session, source=source, target_id=head_id, reason=CARRY_PENDING_POSTED)


def inherit(
    session: Session, *, original_id: uuid.UUID, replacement_id: uuid.UUID, token: ApiToken | None
) -> None:
    """Correction-chain inheritance (DESIGN §4).

    A token-bearing actor's replacement is REVIEWED — the correction *is* the
    review. A system/rule actor's replacement carries the original's state, so
    automation neither grants nor revokes it: a rule recategorizing an
    unreviewed import row leaves it needing review, and a rule touching an
    already-reviewed row does not nag again — its mark is copied whole, so
    who reviewed it and when survive the post-sync correction (decision 82).
    """
    if token is not None:
        mark(session, tx_id=replacement_id, token=token, via=VIA_UPDATE)
        return
    original = session.get(TransactionReview, original_id)
    if original is not None:
        _copy_mark(session, source=original, target_id=replacement_id, reason=CARRY_CORRECTION)


def copy_to_clone(session: Session, *, void_id: uuid.UUID, clone_id: uuid.UUID) -> None:
    """Unlink carries review state onto the resurrected clone, exactly as the
    content_hash moves (decision 29): the clone IS that bank line, so its
    review state is that bank line's state — the whole mark, escalation
    included (decision 82)."""
    source = session.get(TransactionReview, void_id)
    if source is None:
        return
    _copy_mark(session, source=source, target_id=clone_id, reason=CARRY_UNLINK)


def aged_sweep(session: Session, *, max_age_days: int) -> int:
    """Auto-mark rows older than the threshold as ``via='aged'``.

    The set must not grow without bound. But the sweep firing is ITSELF the
    signal that review has lapsed, so the count is surfaced in whats_new
    rather than clearing silently — papering over the problem the sweep exists
    to bound would defeat it.
    """
    cutoff = datetime.now(UTC).date() - timedelta(days=max_age_days)
    stale = list(
        session.execute(
            select(Transaction.id)
            .where(Transaction.id.in_(_unreviewed_ids()))
            .where(Transaction.date < cutoff)
        ).scalars()
    )
    for tx_id in stale:
        mark(session, tx_id=tx_id, token=None, via=VIA_AGED)
    if stale:
        log.info("review.aged_sweep", marked=len(stale), max_age_days=max_age_days)
    return len(stale)


def aged_sweep_job() -> None:
    from purser import appsettings
    from purser.db import get_sessionmaker

    with get_sessionmaker()() as session:
        marked = aged_sweep(session, max_age_days=appsettings.effective().review_max_age_days)
        session.commit()
    if marked:
        log.warning("review.aged_out", count=marked)


BATCH_DEFAULT = 10
BATCH_CAP = 25


def _context_map(session: Session) -> dict[str, bool]:
    """What context this batch could actually offer.

    The requirement is not that a HUMAN reviewed the row — it is that the row
    was reviewed WITH the relevant context present. So a batch that silently
    ships `memories: []` because that feature is absent would be a row
    reviewed blind, clearing indistinguishably from one reviewed properly.
    Declaring the degradation is what lets the reviewer escalate instead.
    """
    from sqlalchemy import inspect as sa_inspect

    tables = set(sa_inspect(session.get_bind()).get_table_names())
    return {
        "rules": "rules" in tables,
        "memories": "memories" in tables,
        "provenance": True,
        "transfer_candidates": True,
    }


def batch(
    session: Session,
    *,
    limit: int = BATCH_DEFAULT,
    account_ref: str | None = None,
    include_guidance: bool = True,
) -> dict[str, Any]:
    """The review loop in one round-trip: rows WITH their signals inline.

    Everything a reviewer needs to judge a row travels with it — category and
    how it was assigned, any open transfer candidate, raw-vs-friendly payee,
    what automation did. Zero per-row round-trips is the point; a reviewer
    that has to fetch context per row will skip it.

    Escalated rows sort FIRST when a backend pass has run, so the connected
    agent picks up exactly what the server handed back rather than competing
    with it for the same set.
    """
    from purser import appsettings
    from purser import ledger as ledger_service
    from purser.models import Posting
    from purser.models import TransactionReview as _Review

    limit = max(1, min(limit, BATCH_CAP))
    stmt = (
        select(Transaction)
        .where(Transaction.id.in_(_unreviewed_ids()))
        .order_by(Transaction.date.desc(), Transaction.id.desc())
    )
    if account_ref:
        account = ledger_service.resolve_account(session, account_ref)
        stmt = stmt.where(
            Transaction.id.in_(
                select(Posting.transaction_id).where(Posting.account_id == account.id)
            )
        )
    rows = list(session.execute(stmt.limit(limit)).scalars())
    total = unreviewed_count(session)

    escalated = list(
        session.execute(
            select(Transaction)
            .join(_Review, _Review.transaction_id == Transaction.id)
            .where(_Review.escalated_at.is_not(None))
            .order_by(_Review.escalated_at.desc())
            .limit(limit)
        ).scalars()
    )

    payload: dict[str, Any] = {
        "unreviewed_total": total,
        "oldest_days": oldest_unreviewed_days(session),
        "context": _context_map(session),
        "escalated": [_row_payload(session, tx, flagged=True) for tx in escalated],
        "rows": [_row_payload(session, tx) for tx in rows],
        "remaining": max(0, total - len(rows)),
    }
    if include_guidance:
        payload["guidance"] = appsettings.review_guidance()
    return payload


def _row_payload(session: Session, tx: Transaction, *, flagged: bool = False) -> dict[str, Any]:
    real = next((p for p in tx.postings if not p.account.is_virtual), None)
    virtual = next((p for p in tx.postings if p.account.is_virtual), None)
    other_real = next((p for p in tx.postings if not p.account.is_virtual and p is not real), None)
    flags: list[str] = []
    if virtual is not None and virtual.account.path.endswith("uncategorized"):
        flags.append("uncategorized")
    if tx.payee is None:
        flags.append("no_payee")
    if other_real is not None:
        flags.append("transfer")

    automation: list[dict[str, Any]] = []
    source = _category_source(session, tx.id)
    if source:
        automation.append({"what": "categorized", "source": source})

    return {
        "id": str(tx.id),
        "date": tx.date.isoformat(),
        "account": real.account.path if real else None,
        "payee": tx.payee.name if tx.payee else None,
        "description": tx.description,
        "amount": str(real.amount) if real else None,
        "status": tx.status,
        "origin": tx.origin,
        "category": {
            "path": virtual.account.path if virtual else None,
            "source": source,
        },
        "transfer_to": other_real.account.path if other_real else None,
        "flags": flags,
        "automation": automation,
        "memories": _relevant_memories(session, real, tx),
        **({"escalated": True} if flagged else {}),
    }


def _relevant_memories(session: Session, real: Any, tx: Transaction) -> list[dict[str, Any]]:
    """Exact-FK matches only (#176) — a reviewer never receives a memory about
    a different account. Empty here means "none relevant"; whether the SURFACE
    exists at all is what the batch's `context` map answers."""
    from sqlalchemy import inspect as sa_inspect

    if "memories" not in set(sa_inspect(session.get_bind()).get_table_names()):
        return []
    from purser import memories as memories_service

    return memories_service.relevant_for(
        session,
        account_ids=[real.account_id] if real is not None else [],
        payee_ids=[tx.payee_id] if tx.payee_id else [],
    )


def _category_source(session: Session, tx_id: uuid.UUID) -> str | None:
    """Who chose this row's category — the same audit read the register's
    provenance badge uses (posted corrections audit the ORIGINAL and name the
    replacement, so both are matched)."""
    from purser.models import AuditLog

    entries = list(
        session.execute(
            select(AuditLog)
            .where(
                AuditLog.action == "transaction.categorize",
                or_(AuditLog.entity_id == tx_id, AuditLog.replacement_id == tx_id),
            )
            .order_by(AuditLog.at)
        ).scalars()
    )
    for entry in reversed(entries):
        after = (entry.before_after or {}).get("after") or {}
        if after.get("source"):
            return str(after["source"])
    return None


# --------------------------------------------------------------------------
# Backend reviewer (DESIGN §11.2) — off unless a provider is configured AND
# the operator turned it on.
# --------------------------------------------------------------------------

# One sync must not fan out into hundreds of provider calls.
AI_REVIEW_PER_RUN_CAP = 25


def ai_review_pass(session: Session, *, client: Any = None) -> dict[str, int]:
    """Review server-side instead of waiting for a connected agent.

    A no-op unless ``ai_provider != "none"`` AND ``ai_review`` is on — gated
    exactly like the categorizer, plus its own switch, because configuring a
    provider must grant nothing by itself.

    This is the ONE place automation may mark a row reviewed, and it does so
    as ``via='ai_review'`` so a row the backend cleared is never confusable
    with one a human looked at. It clears what the context supports and
    ESCALATES the rest — the user's attention lands on the residue rather
    than the whole set, which is the actual point of running it server-side.

    A provider failure degrades exactly as ``categorize.run`` does: the latch
    stops further calls, the sync completes, rows simply stay unreviewed.
    """
    from purser import ai_providers, appsettings

    stats = {"reviewed": 0, "escalated": 0, "skipped": 0}
    settings = appsettings.effective()
    if settings.ai_provider == "none" or not settings.ai_review:
        return stats

    payload = batch(session, limit=AI_REVIEW_PER_RUN_CAP, include_guidance=True)
    rows = payload.get("rows") or []
    if not rows:
        return stats

    provider_down = False
    for row in rows:
        if provider_down:
            stats["skipped"] += 1
            continue
        try:
            verdict = ai_providers.review_row(
                settings,
                row=row,
                guidance=payload.get("guidance") or list(DEFAULT_GUIDANCE),
                client=client,
            )
        except ai_providers.AiProviderError as exc:
            provider_down = True
            stats["skipped"] += 1
            log.warning("review.provider_error", error=str(exc))
            continue
        if verdict is None:
            stats["skipped"] += 1
            continue
        tx_id = uuid.UUID(str(row["id"]))
        if verdict.escalate:
            mark(
                session,
                tx_id=tx_id,
                token=None,
                via=VIA_AI_REVIEW,
                note=verdict.note,
                escalate=True,
            )
            stats["escalated"] += 1
            continue
        mark(session, tx_id=tx_id, token=None, via=VIA_AI_REVIEW, note=verdict.note)
        stats["reviewed"] += 1
    if stats["reviewed"] or stats["escalated"]:
        log.info("review.ai_pass", **stats)
    return stats
