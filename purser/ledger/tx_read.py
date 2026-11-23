"""Transaction reads: one by id, and the register search."""

import uuid
from datetime import date as date_type
from typing import Any

from sqlalchemy import (
    LargeBinary,
    Select,
    String,
    any_,
    bindparam,
    cast,
    func,
    not_,
    or_,
    select,
    text,
    true,
    type_coerce,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session, aliased, selectinload

from purser import balances, crypto
from purser.core import ledger as core
from purser.core import search_query
from purser.ledger.accounts import CATEGORY_TYPES, resolve_account
from purser.ledger.errors import LedgerError, NotFoundError, check_path
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
# Rows per fetch of the q path's (id, text) probe.
_PROBE_BATCH = 2000
_UUID_ARRAY = ARRAY(PG_UUID(as_uuid=True))


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
    """Flat view by default: reversal chains collapsed, void hidden.

    ``q`` is the search grammar (``core.search_query``, decision 135): its
    structured clauses AND with the explicit parameters in SQL, and its text
    clauses are matched against the decrypted payee, description and memos —
    in memory when the search text cache is ready, row by row otherwise."""
    query = None
    if q is not None:
        try:
            query = search_query.parse(q)
        except search_query.QueryError as exc:
            raise LedgerError(str(exc)) from None
        if query.never:
            return [], 0
        if query.statuses is not None:
            wanted = set(statuses) & query.statuses if statuses else query.statuses
            if not wanted:
                return [], 0
            statuses = sorted(wanted)
        start = _later(start, query.start)
        end = _earlier(end, query.end)
        if query.reviewed is not None:
            if reviewed is not None and reviewed != query.reviewed:
                return [], 0
            reviewed = query.reviewed

    stmt: Select[Transaction] = select(Transaction).where(
        Transaction.status.in_(statuses or ["pending", "posted"])
    )

    if not include_collapsed:
        # Corrections form a chain original <- reversal <- replacement via
        # reverses_id; only the chain head (nothing points at it) is current.
        reversed_ids = select(Transaction.reverses_id).where(Transaction.reverses_id.is_not(None))
        stmt = stmt.where(Transaction.id.not_in(reversed_ids))

    perspective: Account | None = None
    if account_ref is not None:
        perspective = resolve_account(session, account_ref)
        stmt = stmt.where(
            Transaction.id.in_(
                select(Posting.transaction_id).where(Posting.account_id == perspective.id)
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

    store = None
    if query is not None:
        from purser.search import textcache

        store = textcache.ready()
        stmt = _structured(session, stmt, query, perspective, store)

    # The balance total order (decision 37), newest first — from the same
    # place the stored running sum takes it, so page order and the Balance
    # column can never disagree. id is the final tiebreak: created_at is
    # transaction_timestamp, identical across an import batch.
    ordered = stmt.order_by(*balances.transaction_order_desc(Transaction))

    if query is not None and query.text:
        # Descriptions, memos and payee names are encrypted (decision 36):
        # SQL cannot match them.
        if store is not None:
            # Cache first: the in-memory text gives the matching ids (or,
            # with only exclusions, the ids to leave out); SQL keeps every
            # other filter, the order, the exact total and the page.
            ids, excluded = store.match(session, query.text)
            if ids is not None:
                return _page_of(session, stmt, ids, limit, offset)
            stmt = _without(stmt, excluded)
            ordered = stmt.order_by(*balances.transaction_order_desc(Transaction))
        else:
            # Cold path, until the cache is built (and in processes that
            # never build one), in two phases (#141): stream the narrowed,
            # ordered set's text, match it, then load the page's rows.
            return _cold_page(session, ordered, query.text, limit, offset)

    total = session.execute(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    ).scalar_one()
    rows = session.execute(ordered.options(*_EAGER).limit(limit).offset(offset)).scalars().all()
    return list(rows), total


def _later(a: date_type | None, b: date_type | None) -> date_type | None:
    return b if a is None else a if b is None else max(a, b)


def _earlier(a: date_type | None, b: date_type | None) -> date_type | None:
    return b if a is None else a if b is None else min(a, b)


def _contains(column: Any, value: str) -> Any:
    """A case-insensitive substring test on a plaintext column."""
    escaped = value.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return func.lower(column).like(f"%{escaped}%", escape="\\")


def _structured(
    session: Session,
    stmt: Select[Transaction],
    query: search_query.Query,
    perspective: Account | None,
    store: Any,
) -> Select[Transaction]:
    """The query's clauses SQL can answer: payee references, account and
    category names, amounts and direction."""
    for ref, negated in query.payee_refs:
        payee_id = _payee_for_ref(session, ref, store)
        if negated:
            stmt = stmt.where(or_(Transaction.payee_id.is_(None), Transaction.payee_id != payee_id))
        else:
            stmt = stmt.where(Transaction.payee_id == payee_id)
    for name, negated in query.accounts:
        named = select(Account.id).where(
            ~Account.is_virtual,
            or_(_contains(Account.name, name), _contains(cast(Account.path, String), name)),
        )
        rows = Transaction.id.in_(
            select(Posting.transaction_id).where(Posting.account_id.in_(named))
        )
        stmt = stmt.where(not_(rows) if negated else rows)
    for name, negated in query.categories:
        # A category whose name or path contains it, and its subtree.
        root = aliased(Account)
        roots = select(root.path).where(
            root.type.in_(CATEGORY_TYPES),
            or_(_contains(root.name, name), _contains(cast(root.path, String), name)),
        )
        member = aliased(Account)
        under = select(member.id).where(
            member.type.in_(CATEGORY_TYPES),
            member.path.op("<@")(func.any(func.array(roots.scalar_subquery()))),
        )
        rows = Transaction.id.in_(
            select(Posting.transaction_id).where(Posting.account_id.in_(under))
        )
        stmt = stmt.where(not_(rows) if negated else rows)
    if query.amounts or query.direction is not None:
        # One leg must satisfy every amount clause and the direction: the
        # perspective account's leg, a split's own leg, else any account leg
        # (a transfer seen from nowhere has one each way).
        leg, leg_account, owner = aliased(Posting), aliased(Account), aliased(Transaction)
        conditions: list[Any] = [
            ~leg_account.is_virtual,
            or_(owner.split_account_id.is_(None), leg.account_id == owner.split_account_id),
        ]
        if perspective is not None and not perspective.is_virtual:
            conditions.append(leg.account_id == perspective.id)
        magnitude = func.abs(leg.amount)
        for bounds in query.amounts:
            if bounds.minimum is not None:
                conditions.append(
                    magnitude > bounds.minimum
                    if bounds.min_exclusive
                    else magnitude >= bounds.minimum
                )
            if bounds.maximum is not None:
                conditions.append(
                    magnitude < bounds.maximum
                    if bounds.max_exclusive
                    else magnitude <= bounds.maximum
                )
        if query.direction == "debit":
            conditions.append(leg.amount < 0)
        elif query.direction == "credit":
            conditions.append(leg.amount > 0)
        stmt = stmt.where(
            Transaction.id.in_(
                select(leg.transaction_id)
                .join(leg_account, leg_account.id == leg.account_id)
                .join(owner, owner.id == leg.transaction_id)
                .where(*conditions)
            )
        )
    return stmt


def _payee_for_ref(session: Session, ref: str, store: Any) -> uuid.UUID:
    """The payee a ``py_`` reference names (decision 91): from the search
    cache when it is ready, else by computing every payee's reference."""
    found: uuid.UUID | None = store.payee_for_ref(session, ref) if store is not None else None
    if found is None and store is None:
        for (payee_id,) in session.execute(select(Payee.id)):
            if crypto.payee_ref(payee_id) == ref:
                found = payee_id
                break
    if found is None:
        raise LedgerError(f"unknown payee reference {ref}")
    return found


def _without(stmt: Select[Transaction], excluded: list[uuid.UUID]) -> Select[Transaction]:
    if not excluded:
        return stmt
    return stmt.where(
        ~(Transaction.id == any_(bindparam("search_excluded", excluded, type_=_UUID_ARRAY)))
    )


def _cold_page(
    session: Session,
    ordered: Select[Transaction],
    clauses: tuple[search_query.Text, ...],
    limit: int,
    offset: int,
) -> tuple[list[Transaction], int]:
    """Decrypt-and-filter in two phases: stream (id, description, payee,
    memos) of the narrowed, ordered set, keep the rows whose folded text
    satisfies every clause (``search_query.text_matches``, the semantics the
    cache's scan equals), then load full rows for the page alone. Fields no
    clause reads are not fetched."""
    fields = {c.field for c in clauses}
    want_payee = bool(fields & {"any", "payee"})
    want_memo = bool(fields & {"any", "memo"})
    columns: list[Any] = [Transaction.id, Transaction.description, Transaction.payee_id]
    if want_memo:
        # Raw ciphertext (the column's own type would decrypt every one):
        # decrypted below, for the rows the narrowing kept.
        memo_enc = type_coerce(Posting.__table__.c.memo_enc, LargeBinary)
        columns.append(
            select(func.array_agg(memo_enc))
            .where(Posting.__table__.c.transaction_id == Transaction.id, memo_enc.is_not(None))
            .scalar_subquery()
        )
    payee_enc: dict[uuid.UUID, bytes] = {}
    payee_names: dict[uuid.UUID, str] = {}
    if want_payee:
        payee_enc = {
            row[0]: row[1]
            for row in session.execute(
                select(Payee.id, type_coerce(Payee.__table__.c.name_enc, LargeBinary))
            )
        }

    def payee_name(payee_id: uuid.UUID | None) -> str:
        if payee_id is None or payee_id not in payee_enc:
            return ""
        if payee_id not in payee_names:
            enc = payee_enc[payee_id]
            payee_names[payee_id] = search_query.fold(crypto.decrypt(enc)) if enc else ""
        return payee_names[payee_id]

    hits = []
    probe = ordered.with_only_columns(*columns).execution_options(yield_per=_PROBE_BATCH)
    for row in session.execute(probe):
        memos = [search_query.fold(crypto.decrypt(m)) for m in row[3] or ()] if want_memo else []
        if search_query.text_matches(
            clauses,
            payee=payee_name(row[2]) if want_payee else "",
            description=search_query.fold(row[1]) if row[1] else "",
            memos=memos,
        ):
            hits.append(row[0])
    page_ids = hits[offset : offset + limit]
    if not page_ids:
        return [], len(hits)
    loaded = session.execute(
        select(Transaction).options(*_EAGER).where(Transaction.id.in_(page_ids))
    ).scalars()
    by_id = {t.id: t for t in loaded}
    return [by_id[i] for i in page_ids], len(hits)


def _page_of(
    session: Session,
    stmt: Select[Transaction],
    ids: list[uuid.UUID],
    limit: int,
    offset: int,
) -> tuple[list[Transaction], int]:
    """One page of ``stmt`` restricted to ``ids``, in the register order, with
    the exact total: one id query with a window count, then full rows for
    the page alone (as the two-phase path loads them)."""
    if not ids:
        return [], 0
    # Each matched id is looked up by primary key (a LATERAL with LIMIT 1,
    # which the planner cannot flatten into a join it would answer by
    # hashing the whole table): "id = ANY(ids)" walked the date index
    # backwards hoping the LIMIT stops early, and a plain join to
    # unnest(ids) hashed every transaction; both cost 100-200 ms at 200k
    # rows for a few thousand matches, this costs about 10.
    matched_ids = (
        func.unnest(bindparam("search_ids", ids, type_=_UUID_ARRAY))
        .table_valued("id")
        .render_derived(name="search_ids")
    )
    hit = (
        stmt.where(Transaction.id == matched_ids.c.id)
        .with_only_columns(Transaction.id, Transaction.date, Transaction.created_at)
        .limit(1)
        .lateral("hit")
    )
    matched = select(hit.c.id).select_from(matched_ids).join(hit, true())
    page = session.execute(
        matched.add_columns(func.count().over().label("total"))
        .order_by(*balances.transaction_order_desc(hit.c))
        .limit(limit)
        .offset(offset)
    ).all()
    if not page:
        total = 0
        if offset:
            total = session.execute(
                select(func.count()).select_from(matched.subquery())
            ).scalar_one()
        return [], total
    page_ids = [row.id for row in page]
    loaded = session.execute(
        select(Transaction).options(*_EAGER).where(Transaction.id.in_(page_ids))
    ).scalars()
    by_id = {t.id: t for t in loaded}
    return [by_id[i] for i in page_ids], page[0].total
