"""Transaction reads: one by id, and the register search."""

import uuid
from datetime import date as date_type

from sqlalchemy import Select, func, not_, select, text
from sqlalchemy.orm import Session, selectinload

from purser import balances, crypto
from purser.core import ledger as core
from purser.ledger.accounts import resolve_account
from purser.ledger.errors import NotFoundError, check_path
from purser.models import Account, Payee, Posting, Transaction

# ---------------------------------------------------------------------------
# Transactions — read
# ---------------------------------------------------------------------------

# What a register row needs loaded. The q path applies it to the returned page
# only: materializing these for every row of the narrowed set, not the Fernet
# decrypt, was the measured cost of search (#141).
_EAGER = (
    selectinload(Transaction.postings).selectinload(Posting.account),
    selectinload(Transaction.payee),
)
# Rows per fetch of the q path's (id, description) probe.
_PROBE_BATCH = 2000


def get_transaction(session: Session, tx_id: uuid.UUID) -> Transaction:
    tx = session.execute(
        select(Transaction)
        .options(selectinload(Transaction.postings), selectinload(Transaction.payee))
        .where(Transaction.id == tx_id)
    ).scalar_one_or_none()
    if tx is None:
        raise NotFoundError(f"transaction {tx_id} not found")
    return tx


def search_transactions(
    session: Session,
    *,
    account_ref: str | None = None,
    category_subtree: str | None = None,
    payee: str | None = None,
    q: str | None = None,
    start: date_type | None = None,
    end: date_type | None = None,
    statuses: list[str] | None = None,
    include_collapsed: bool = False,
    reviewed: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Transaction], int]:
    """Flat view by default: reversal chains collapsed, void hidden."""
    stmt: Select[Transaction] = select(Transaction).where(
        Transaction.status.in_(statuses or ["pending", "posted"])
    )

    if not include_collapsed:
        # Corrections form a chain original <- reversal <- replacement via
        # reverses_id; only the chain head (nothing points at it) is current.
        reversed_ids = select(Transaction.reverses_id).where(Transaction.reverses_id.is_not(None))
        stmt = stmt.where(Transaction.id.not_in(reversed_ids))

    if account_ref is not None:
        account = resolve_account(session, account_ref)
        stmt = stmt.where(
            Transaction.id.in_(
                select(Posting.transaction_id).where(Posting.account_id == account.id)
            )
        )
    if category_subtree is not None:
        stmt = stmt.where(
            Transaction.id.in_(
                select(Posting.transaction_id)
                .join(Account, Account.id == Posting.account_id)
                .where(
                    text("accounts.path <@ CAST(:subtree AS ltree)").bindparams(
                        subtree=check_path(category_subtree)
                    )
                )
            )
        )
    if payee is not None:
        # Equality on the blind index (payees.name is encrypted, #137).
        bidx = crypto.blind_index(core.normalize_payee(payee))
        stmt = stmt.where(Transaction.payee_id.in_(select(Payee.id).where(Payee.name_bidx == bidx)))
    # NOTE: description substring filter (`q`) is handled below, app-side —
    # ILIKE over ciphertext is impossible (#137, decision 36 / DESIGN §1e).
    if start is not None:
        stmt = stmt.where(Transaction.date >= start)
    if end is not None:
        stmt = stmt.where(Transaction.date <= end)
    if reviewed is not None:
        # ONE predicate serves the register filter, the per-row needs_review
        # flag, the count and the review batch — this is a view over the
        # registers, not a second row source (#175, #248, decision 47). Its
        # exact complement is reviewed=true, so with include_collapsed the
        # superseded links of a correction chain land there, never in false.
        from purser.review import unreviewed_clause

        unreviewed = unreviewed_clause()
        stmt = stmt.where(not_(unreviewed) if reviewed else unreviewed)

    # The balance total order (decision 37), newest first — from the same
    # place the stored running sum takes it, so page order and the Balance
    # column can never disagree. id is the final tiebreak: created_at is
    # transaction_timestamp, identical across an import batch.
    ordered = stmt.order_by(*balances.transaction_order_desc(Transaction))

    if q is not None:
        # Decrypt-and-filter in two phases (#141). The SQL above narrows by
        # account/status/date/payee; phase one streams only (id, description)
        # of that already-ordered set and substring-matches the decrypted
        # description, so `total` stays exact and the order is the SQL order.
        # Phase two loads full rows, with their postings and payee, for the
        # returned page alone. q=None keeps the pure-SQL path.
        needle = q.casefold()
        probe = ordered.with_only_columns(Transaction.id, Transaction.description)
        hits = [
            tx_id
            for tx_id, description in session.execute(
                probe.execution_options(yield_per=_PROBE_BATCH)
            )
            if description is not None and needle in description.casefold()
        ]
        page_ids = hits[offset : offset + limit]
        if not page_ids:
            return [], len(hits)
        loaded = session.execute(
            select(Transaction).options(*_EAGER).where(Transaction.id.in_(page_ids))
        ).scalars()
        by_id = {t.id: t for t in loaded}
        return [by_id[i] for i in page_ids], len(hits)

    total = session.execute(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    ).scalar_one()
    rows = session.execute(ordered.options(*_EAGER).limit(limit).offset(offset)).scalars().all()
    return list(rows), total
