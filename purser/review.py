"""Transactions that need review (issue #694, DESIGN §4, decision 141).

A row needs review only for a reason, and both reasons are read from the row
itself — nothing is stored about a row, so nothing is carried, inherited or
copied when it is corrected, posted, split or unlinked:

- ``uncategorized`` — a bank-sync row whose category, or a split line of it,
  is ``expenses.uncategorized`` or ``income.uncategorized``. Gated by the
  ``review_uncategorized`` setting (on by default). It clears the moment
  anything categorizes the row: a rule, payee memory, the AI classifier, a
  person or an agent.
- ``reconciliation_adjustment`` — the adjustment a reconcile posted
  (``account_reconciliations.adjustment_tx_id``) while its live row still
  books to ``expenses.reconciliation_adjustments`` and the reconciliation's
  ``adjustment_accepted_at`` is NULL. Always on. Recategorizing or splitting
  it away clears it; otherwise the user accepts it (``reconciliation.
  accept_adjustment``). An asset's value change (``equity.revaluation``,
  decision 113) never books to that category, so it is never flagged.

Only a live chain head is asked: ``pending``/``posted``, unsuperseded, and no
reversal points at it. A schedule instance (``expected``), a corrected row and
its reversal, a merged-away bank row and a void row never need review.

"Bank-sync row" is decision 45's feed-row definition — ``origin='import' AND
source_fetch_id IS NOT NULL`` — applied to the row's LINEAGE, not the head
alone: a person who edits a posted bank row's description writes a correction
whose replacement is ``origin='human'`` with no fetch, and an unlinked
replacement's clone carries neither, yet each is still that bank line with no
category. So the predicate walks back from the head exactly as
``lineage.ancestry`` does — through each correction to the row it replaced,
and from a one-sided row to the void one-sided row it carries on (a pending's
posted successor, an unlinked original's clone; never a merge, an absorb or a
match) — and asks whether any row on the way is a feed row. Rows typed by a
person or written by an agent, QIF/CSV imports and Purser's own entries
(starting balances, true-ups, value changes, merged transfers) have no feed
row in their lineage and are never flagged for a missing category. The same
walk finds a reconciliation adjustment's original row after it was corrected.

That test exists ONCE, as ``flagged_rows()`` (#248's single-predicate rule):
the register's per-row ``needs_review``/``review_reason``,
``GET /transactions?needs_review=``, the search term ``is:needs-review``,
``GET /transactions/review-summary``, ``review_batch``, ``describe``,
``whats_new`` and the digest all evaluate it rather than restating it.
"""

import uuid
from collections.abc import Collection
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, case, func, literal, select
from sqlalchemy.orm import Session, aliased

from purser.feed.importer import UNCATEGORIZED_EXPENSE, UNCATEGORIZED_INCOME
from purser.lineage import chain_head_clause
from purser.models import Account, AccountReconciliation, Match, Posting, Transaction
from purser.reconciliation import CATEGORY_PATH as ADJUSTMENT_PATH

REASON_UNCATEGORIZED = "uncategorized"
REASON_ADJUSTMENT = "reconciliation_adjustment"
REASONS = (REASON_UNCATEGORIZED, REASON_ADJUSTMENT)
UNCATEGORIZED_PATHS = (UNCATEGORIZED_EXPENSE, UNCATEGORIZED_INCOME)

LIVE_STATUSES = ("pending", "posted")
# A lineage is a handful of rows long; the walk stops here even if a cycle
# could ever be built by hand (lineage._MAX_HOPS).
_MAX_HOPS = 64


def _uncategorized_enabled() -> bool:
    from purser import appsettings

    return bool(appsettings.effective().review_uncategorized)


def _one_real_leg(tx_id: Any) -> ColumnElement[bool]:
    """SQL: the transaction ``tx_id`` names posts to exactly one real account
    (lineage._one_real_leg): a bank line's row, never a transfer."""
    real = (
        select(func.count(Posting.id))
        .join(Account, Account.id == Posting.account_id)
        .where(Posting.transaction_id == tx_id, Account.is_virtual.is_(False))
        .scalar_subquery()
    )
    return real == 1


def flagged_rows(
    *, ids: Collection[uuid.UUID] | None = None, uncategorized: bool | None = None
) -> Select[uuid.UUID, str]:
    """THE predicate: ``(transaction id, reason)`` for every row that needs
    review, or for those of ``ids`` that do. ``uncategorized`` overrides the
    ``review_uncategorized`` setting (None reads it).

    Self-contained SQL (its CTEs nest inside whatever statement uses it), so
    a caller can filter, count or group by it in one statement."""
    if uncategorized is None:
        uncategorized = _uncategorized_enabled()
    paths = [ADJUSTMENT_PATH, *(UNCATEGORIZED_PATHS if uncategorized else ())]
    is_adjustment = Account.path == ADJUSTMENT_PATH
    candidates = (
        select(
            Transaction.id.label("id"),
            func.bool_or(is_adjustment).label("adjustment"),
            func.bool_or(~is_adjustment).label("uncategorized"),
        )
        .join(Posting, Posting.transaction_id == Transaction.id)
        .join(Account, Account.id == Posting.account_id)
        .where(
            Transaction.status.in_(LIVE_STATUSES),
            Transaction.superseded_by.is_(None),
            chain_head_clause(),
            Account.path.in_(paths),
        )
        .group_by(Transaction.id)
    )
    if ids is not None:
        candidates = candidates.where(Transaction.id.in_(list(ids)))
    cand = candidates.cte("review_candidates", nesting=True)

    # The lineage walk, back from each candidate (lineage.ancestry in SQL):
    # a replacement to the row its correction replaced, else a one-sided row
    # to the void one-sided row it carries on — not a matched expected.
    walk = select(cand.c.id.label("start"), cand.c.id.label("cur"), literal(0).label("depth")).cte(
        "review_lineage", recursive=True, nesting=True
    )
    row, reversal, earlier = aliased(Transaction), aliased(Transaction), aliased(Transaction)
    settled = select(Match.id).where(
        Match.expected_id == earlier.id, Match.posted_id == earlier.superseded_by
    )
    previous = func.coalesce(reversal.reverses_id, earlier.id)
    walk = walk.union_all(
        select(walk.c.start, previous, walk.c.depth + 1)
        .select_from(walk)
        .join(row, row.id == walk.c.cur)
        .outerjoin(reversal, reversal.id == row.reverses_id)
        .outerjoin(
            earlier,
            and_(
                reversal.reverses_id.is_(None),
                earlier.superseded_by == row.id,
                earlier.status == "void",
                _one_real_leg(earlier.id),
                _one_real_leg(row.id),
                ~settled.exists(),
            ),
        )
        .where(previous.is_not(None), walk.c.depth < _MAX_HOPS)
    )

    # Per candidate: is any row of its lineage a feed row, and is any the
    # adjustment of a reconciliation nobody accepted?
    feed = aliased(Transaction)
    roots = (
        select(
            walk.c.start.label("id"),
            func.bool_or(and_(feed.origin == "import", feed.source_fetch_id.is_not(None))).label(
                "from_feed"
            ),
            func.bool_or(
                and_(
                    AccountReconciliation.id.is_not(None),
                    AccountReconciliation.adjustment_accepted_at.is_(None),
                )
            ).label("unaccepted"),
        )
        .select_from(walk)
        .join(feed, feed.id == walk.c.cur)
        .outerjoin(AccountReconciliation, AccountReconciliation.adjustment_tx_id == walk.c.cur)
        .group_by(walk.c.start)
        .cte("review_roots", nesting=True)
    )
    whens = [(and_(cand.c.adjustment, roots.c.unaccepted), literal(REASON_ADJUSTMENT))]
    if uncategorized:
        whens.append((and_(cand.c.uncategorized, roots.c.from_feed), literal(REASON_UNCATEGORIZED)))
    reason = case(*whens, else_=None)
    flagged = (
        select(cand.c.id.label("id"), reason.label("reason"))
        .join(roots, roots.c.id == cand.c.id)
        .cte("review_flagged", nesting=True)
    )
    return select(flagged.c.id, flagged.c.reason).where(flagged.c.reason.is_not(None))


def needs_review_clause(*, uncategorized: bool | None = None) -> ColumnElement[bool]:
    """SQL: the enclosing query's ``transactions`` row needs review. Its
    exact complement (``~``) is every other row, so ``?needs_review=false``
    with ``include_collapsed`` puts a corrected row and its reversal there."""
    flagged = flagged_rows(uncategorized=uncategorized).subquery("needs_review")
    return Transaction.id.in_(select(flagged.c.id))


def review_reasons(session: Session, tx_ids: Collection[uuid.UUID]) -> dict[uuid.UUID, str]:
    """``{row: reason}`` for the rows of ``tx_ids`` that need review: what
    the register's badge renders, one query per page, from the same predicate
    as the count and ``?needs_review=true`` (#248)."""
    if not tx_ids:
        return {}
    return {
        tx_id: reason
        for tx_id, reason in session.execute(flagged_rows(ids=list(dict.fromkeys(tx_ids)))).all()
    }


def counts(session: Session) -> dict[str, int]:
    """How many rows need review, and for which reason."""
    flagged = flagged_rows().subquery("flagged")
    by_reason: dict[str, int] = {
        reason: n
        for reason, n in session.execute(
            select(flagged.c.reason, func.count()).group_by(flagged.c.reason)
        ).all()
    }
    return {
        "total": sum(by_reason.values()),
        REASON_UNCATEGORIZED: by_reason.get(REASON_UNCATEGORIZED, 0),
        REASON_ADJUSTMENT: by_reason.get(REASON_ADJUSTMENT, 0),
    }


def headline(block: dict[str, int]) -> str | None:
    """'N transactions need review (X with no category, Y reconciliation
    adjustments)' — the one sentence describe, whats_new and the digest use;
    None when nothing does."""
    total = block.get("total", 0)
    if not total:
        return None
    parts = []
    if block.get(REASON_UNCATEGORIZED):
        parts.append(f"{block[REASON_UNCATEGORIZED]} with no category")
    if block.get(REASON_ADJUSTMENT):
        n = block[REASON_ADJUSTMENT]
        parts.append(f"{n} reconciliation adjustment{'' if n == 1 else 's'}")
    verb = "needs" if total == 1 else "need"
    noun = "transaction" if total == 1 else "transactions"
    return f"{total} {noun} {verb} review ({', '.join(parts)})"


def summary(session: Session) -> dict[str, Any]:
    """Counts and register destinations for the attention panel: global
    totals count each row once; per-account totals count a row on each real
    account it posts to, so they must not be summed. No monetary or merchant
    data — only where to go. Agent drafts (``agent_pending``: an agent's
    pending rows) are a separate count, unrelated to the reasons."""
    flagged = flagged_rows().cte("flagged")
    agent_draft = and_(
        Transaction.origin == "agent",
        Transaction.status == "pending",
        Transaction.superseded_by.is_(None),
    )
    is_flagged = flagged.c.id.is_not(None)
    totals = session.execute(
        select(
            func.count(),
            func.count().filter(flagged.c.reason == REASON_UNCATEGORIZED),
            func.count().filter(flagged.c.reason == REASON_ADJUSTMENT),
            func.count().filter(Transaction.status == "pending"),
            func.min(Transaction.date),
        )
        .select_from(flagged)
        .join(Transaction, Transaction.id == flagged.c.id)
    ).one()
    accounts = session.execute(
        select(
            Account.id,
            Account.path,
            Account.name,
            func.count(func.distinct(Transaction.id)).filter(is_flagged),
            func.count(func.distinct(Transaction.id)).filter(
                flagged.c.reason == REASON_UNCATEGORIZED
            ),
            func.count(func.distinct(Transaction.id)).filter(flagged.c.reason == REASON_ADJUSTMENT),
            func.count(func.distinct(Transaction.id)).filter(
                and_(is_flagged, Transaction.status == "pending")
            ),
            func.count(func.distinct(Transaction.id)).filter(agent_draft),
        )
        .select_from(Transaction)
        .join(Posting, Posting.transaction_id == Transaction.id)
        .join(Account, Account.id == Posting.account_id)
        .outerjoin(flagged, flagged.c.id == Transaction.id)
        .where(Account.is_virtual.is_(False), is_flagged | agent_draft)
        .group_by(Account.id)
        .order_by(Account.name, Account.id)
    ).all()
    return {
        "total": totals[0],
        "uncategorized": totals[1],
        "reconciliation_adjustments": totals[2],
        "pending": totals[3],
        "oldest_date": totals[4],
        "agent_pending": session.scalar(select(func.count()).where(agent_draft)),
        "accounts": [
            dict(
                zip(
                    (
                        "account_id",
                        "path",
                        "name",
                        "total",
                        "uncategorized",
                        "reconciliation_adjustments",
                        "pending",
                        "agent_pending",
                    ),
                    row,
                    strict=True,
                )
            )
            for row in accounts
        ],
    }


BATCH_DEFAULT = 10
BATCH_CAP = 25


def batch(
    session: Session, *, limit: int = BATCH_DEFAULT, account_ref: str | None = None
) -> dict[str, Any]:
    """The rows that need review, newest first, each with what it takes to
    fix it in one round-trip: its reason, account, payee, description,
    amount, status, category and the financial notes linked to its account
    or payee. A row with no category is fixed by categorizing it
    (update_transaction, or a rule for the payee); a reconciliation
    adjustment by recategorizing or splitting it, or accepting it."""
    from purser import ledger as ledger_service

    limit = max(1, min(limit, BATCH_CAP))
    flagged = flagged_rows().subquery("flagged")
    stmt = (
        select(Transaction, flagged.c.reason)
        .join(flagged, flagged.c.id == Transaction.id)
        .order_by(Transaction.date.desc(), Transaction.id.desc())
    )
    total_stmt = select(func.count()).select_from(flagged)
    if account_ref:
        account = ledger_service.resolve_account(session, account_ref)
        on_account = Transaction.id.in_(
            select(Posting.transaction_id).where(Posting.account_id == account.id)
        )
        stmt = stmt.where(on_account)
        total_stmt = (
            select(func.count())
            .select_from(flagged)
            .join(Transaction, Transaction.id == flagged.c.id)
            .where(on_account)
        )
    rows = session.execute(stmt.limit(limit)).all()
    total = int(session.scalar(total_stmt) or 0)
    return {
        "total": total,
        "rows": [_row_payload(session, tx, reason) for tx, reason in rows],
        "remaining": max(0, total - len(rows)),
    }


def _row_payload(session: Session, tx: Transaction, reason: str) -> dict[str, Any]:
    from purser import memories as memories_service

    real = next((p for p in tx.postings if not p.account.is_virtual), None)
    if tx.split_account_id is not None:
        real = next((p for p in tx.postings if p.account_id == tx.split_account_id), real)
    categories = [p for p in tx.postings if p.account.is_virtual]
    return {
        "id": str(tx.id),
        "date": tx.date.isoformat(),
        "account": real.account.path if real else None,
        "payee": tx.payee.name if tx.payee else None,
        "description": tx.description,
        "amount": str(real.amount) if real else None,
        "status": tx.status,
        "reason": reason,
        # A split names no single category; its lines are on get_transaction.
        "category": categories[0].account.path if len(categories) == 1 else None,
        "memories": memories_service.relevant_for(
            session,
            account_ids=[real.account_id] if real is not None else [],
            payee_ids=[tx.payee_id] if tx.payee_id else [],
        ),
    }
