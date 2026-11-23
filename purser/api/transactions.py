"""Transactions API — flat outside, double-entry inside (DESIGN §3.3)."""

import uuid
from datetime import date as date_type
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from purser import balances, ledger, splits
from purser.admin.auth import ReadToken, WriteToken
from purser.api.disclosure import NOTHING, Emits, disclose
from purser.core import ledger as core
from purser.db import get_session
from purser.models import Posting, Transaction, TransactionReview

DbSession = Annotated[Session, Depends(get_session)]

router = APIRouter(prefix="/transactions", tags=["transactions"])


class PostingOut(BaseModel):
    id: uuid.UUID
    account_id: uuid.UUID
    account_path: str | None = None
    amount: Decimal
    currency: str
    memo: str | None

    @classmethod
    def from_model(cls, p: Posting, *, with_path: bool = False) -> "PostingOut":
        return cls(
            id=p.id,
            account_id=p.account_id,
            account_path=p.account.path if with_path and p.account is not None else None,
            amount=p.amount,
            currency=p.currency.strip(),
            memo=p.memo,
        )


class TransactionOut(BaseModel):
    """Flat view: the real-account delta plus category path.

    With a perspective account (the register view: `GET /transactions?account=X`)
    the amount is that account's signed leg; without one, transfers present
    their outgoing side with a positive amount (the historical shape).

    A row is a transfer only when its postings are a shape Purser creates
    (DESIGN §3.3): a plain transfer, or a split loan payment — whose
    `category_path` is the interest/fee category and `remainder_amount` that
    posting's amount. Any other multi-posting row is not a transfer.

    A split transaction (decisions 87 and 118) has `category_path` null,
    `split_lines` set to its line count (category and `[Account]` lines) and
    `split_account_path` set to the account it splits — the real leg that
    keeps the bank's amount. It is never a transfer. Seen from another
    account one of its lines books to, the row shows that line's leg. The
    detail's `split` lists the lines; `PUT /transactions/{id}/split` edits
    them and `DELETE /transactions/{id}/split` removes the split.
    """

    id: uuid.UUID
    date: date_type
    payee: str | None
    description: str | None
    status: str
    origin: str
    amount: Decimal | None  # real-account delta; None for multi-leg exotics
    # Stored running balance AFTER this row from the perspective account's
    # register (decision 37); filled only for perspective queries, and only
    # for chain-head pending|posted rows — None otherwise.
    balance: Decimal | None = None
    account_path: str | None
    category_path: str | None
    transfer_account_path: str | None  # transfers: the other real leg
    is_transfer: bool
    # Split loan payment (decision 62): the interest/fee posting booked to
    # category_path (positive = expense). None on every other row.
    remainder_amount: Decimal | None = None
    split_lines: int = Field(
        default=0,
        description="Lines of a split transaction (category and [Account] lines, decisions "
        "87 and 118); 0 for every other row.",
    )
    split_account_path: str | None = Field(
        default=None,
        description="The account a split transaction splits (its real leg keeps the bank's "
        "amount); null for every other row.",
    )
    has_linked_originals: bool = False  # voided bank rows hang off this row (unlink-able)
    # 'deterministic' | 'ai' — filled only with ?with_category_provenance=true
    category_source: str | None = None
    # Review marks live in a side table, so they ride along rather than
    # being columns on the row (#175, DESIGN §4).
    reviewed_at: datetime | None = None
    escalated_at: datetime | None = None
    # Membership of the unreviewed set, computed by review.py's one predicate
    # (#248). A missing mark does NOT mean "needs review": expected schedule
    # instances, manual, agent and QIF/CSV rows carry none and are not in the
    # set — so clients render this, never `reviewed_at is null`.
    needs_review: bool = Field(
        description=(
            "true when the row is in the unreviewed set (#175): the same test "
            "as GET /transactions?reviewed=false and the review count"
        )
    )
    # Schedule instances (#231): the rule this row fills a slot of, and
    # whether a human/agent edit pinned it (exempt from rule refresh).
    schedule_id: uuid.UUID | None = None
    schedule_pinned: bool = False
    # Booked as a securities trade (#390, decision 103; #591, decision 119):
    # its category leg is the system account equity.securities.
    securities_trade: bool = Field(
        default=False,
        description="The row is a securities trade (a buy or sell: its other leg is the system "
        "account equity.securities, never spending or income). See securities_trade_locked.",
    )
    securities_trade_locked: bool = Field(
        default=False,
        description="A securities trade whose cash a recorded trade holds (a position event, "
        "decision 103): it moves only with that trade — reverse the trade to correct it. false "
        "on a row a person, an agent or a rule marked (PATCH it with a category to change it "
        "back) and on a balance adjustment.",
    )
    balance_adjustment: bool = Field(
        default=False,
        description="Purser's post-sync balance true-up of a brokerage account (decision 119): "
        "cash put in step with the broker's balance less its holdings, against "
        "equity.securities. The register labels it 'Balance adjustment (invested cash)'.",
    )
    # A change in a hand-valued asset's value (#566, decision 113): its other
    # leg is the system account equity.revaluation. Corrected by reversing it
    # (POST /value-changes/{id}/reverse), never edited.
    value_change: bool = False
    # Of those, money put into the asset (its other leg equity.capital, #567)
    # rather than a change in market value.
    money_put_in: bool = False
    # The account's starting balance from the bank (#585, decision 123): its
    # other leg is the system account equity.opening_balances. Purser derives
    # it and keeps it in step, so it is never edited; DELETE
    # /accounts/{id}/starting-balance takes it out.
    starting_balance: bool = False
    created_at: datetime

    @classmethod
    def from_model(
        cls,
        t: Transaction,
        *,
        perspective_account_id: uuid.UUID | None = None,
        has_linked_originals: bool = False,
        category_source: str | None = None,
        review: TransactionReview | None = None,
        needs_review: bool,
        balance: Decimal | None = None,
        securities_trade_locked: bool | None = None,
        balance_adjustment: bool = False,
    ) -> "TransactionOut":
        """``securities_trade_locked`` None (a caller that did not look):
        a securities trade reads as locked, the safe answer."""
        from purser import transfers

        real = [p for p in t.postings if p.account is not None and not p.account.is_virtual]
        virtual = [p for p in t.postings if p.account is not None and p.account.is_virtual]
        # Shape, not leg count: a multi-posting row that merely touches two
        # real accounts (#245) is not a transfer (decision 62).
        shape = transfers.transfer_shape(t)
        is_transfer = shape is not None
        amount: Decimal | None = None
        account_path: str | None = None
        category_path: str | None = None
        transfer_account_path: str | None = None
        mine = [p for p in real if p.account_id == perspective_account_id]
        own = next((p for p in real if p.account_id == t.split_account_id), None)
        if t.split_account_id is not None and not mine and own is not None:
            # A split is seen through its own leg unless the perspective is
            # one of its [Account] lines (decision 118).
            mine = [own]
        if len(mine) == 1:
            amount = mine[0].amount
            account_path = mine[0].account.path
            if is_transfer:
                other = next(p for p in real if p is not mine[0])
                transfer_account_path = other.account.path
        elif len(real) == 1:
            amount = real[0].amount
            account_path = real[0].account.path
        elif is_transfer:
            outgoing = min(real, key=lambda p: p.amount)
            amount = abs(outgoing.amount)
            account_path = outgoing.account.path
            incoming = max(real, key=lambda p: p.amount)
            transfer_account_path = incoming.account.path
        if len(virtual) == 1 and t.split_account_id is None:
            category_path = virtual[0].account.path
        remainder_amount = virtual[0].amount if shape is core.TransferShape.SPLIT else None
        return cls(
            id=t.id,
            date=t.date,
            payee=t.payee.name if t.payee else None,
            description=t.description,
            status=t.status,
            origin=t.origin,
            amount=amount,
            balance=balance,
            account_path=account_path,
            category_path=category_path,
            transfer_account_path=transfer_account_path,
            is_transfer=is_transfer,
            remainder_amount=remainder_amount,
            split_lines=transfers.split_lines(t),
            split_account_path=own.account.path if own is not None else None,
            # Unlink undoes a transfer; a split's backing rows come back
            # through removing the split.
            has_linked_originals=has_linked_originals and is_transfer,
            category_source=category_source,
            reviewed_at=review.reviewed_at if review else None,
            escalated_at=review.escalated_at if review else None,
            needs_review=needs_review,
            schedule_id=t.schedule_id,
            schedule_pinned=t.schedule_pinned_at is not None,
            securities_trade=(trade := ledger.is_securities_trade(t.postings)),
            securities_trade_locked=trade
            and (securities_trade_locked if securities_trade_locked is not None else True),
            balance_adjustment=balance_adjustment,
            value_change=ledger.is_value_change(t.postings),
            money_put_in=ledger.is_capital(t.postings),
            starting_balance=ledger.is_starting_balance(t.postings),
            created_at=t.created_at,
        )


class SplitLineOut(BaseModel):
    """One line of a split (decision 118): a category, or an `[Account]`
    line booking a leg on another account, which `backing_ids` — that
    account's bank rows merged into the split — may back."""

    target: str = Field(description="category path, or [account path] for an account line")
    kind: Literal["category", "account"]
    amount: Decimal = Field(description="the line's share of the row's amount (flat-form sign)")
    memo: str | None = None
    backing_ids: list[uuid.UUID] = Field(
        default_factory=list, description="bank rows backing an account line's leg"
    )


class SplitOut(BaseModel):
    account_path: str
    lines: list[SplitLineOut]


class TransactionDetail(TransactionOut):
    """Full detail: postings and provenance links; `split` lists a split's
    lines with their backing rows."""

    split: SplitOut | None = None
    postings: list[PostingOut]
    content_hash: str | None
    source_fetch_id: uuid.UUID | None
    superseded_by: uuid.UUID | None
    reverses_id: uuid.UUID | None
    created_by: uuid.UUID | None
    suggestion_id: uuid.UUID | None = Field(
        default=None,
        description="PATCH with securities_trade=true: the trade_rule suggestion the mark "
        "proposed (or refreshed) in the inbox — a rule that marks rows like this one. Null "
        "when it proposed none (no payee or description, or a rule or rejection covers it).",
    )

    @classmethod
    def from_model_detail(
        cls,
        t: Transaction,
        *,
        has_linked_originals: bool = False,
        review: TransactionReview | None = None,
        needs_review: bool,
        split_lines: list[dict[str, Any]] | None = None,
        securities_trade_locked: bool | None = None,
        balance_adjustment: bool = False,
    ) -> "TransactionDetail":
        base = TransactionOut.from_model(
            t,
            has_linked_originals=has_linked_originals,
            review=review,
            needs_review=needs_review,
            securities_trade_locked=securities_trade_locked,
            balance_adjustment=balance_adjustment,
        )
        split = None
        if split_lines is not None and base.split_account_path is not None:
            split = SplitOut(
                account_path=base.split_account_path,
                lines=[SplitLineOut(**line) for line in split_lines],
            )
        return cls(
            **base.model_dump(),
            split=split,
            postings=[PostingOut.from_model(p, with_path=True) for p in t.postings],
            content_hash=t.content_hash,
            source_fetch_id=t.source_fetch_id,
            superseded_by=t.superseded_by,
            reverses_id=t.reverses_id,
            created_by=t.created_by,
        )


class TransactionCreate(BaseModel):
    """The flat form agents and humans submit (DESIGN §3.3)."""

    account: str = Field(description="account id or ltree path")
    category: str = Field(description="category id or ltree path, e.g. expenses.dining")
    amount: Decimal = Field(decimal_places=4, max_digits=20)
    date: date_type
    payee: str | None = None
    description: str | None = None
    status: Literal["expected", "pending", "posted"] = "pending"


class TransferCreate(BaseModel):
    from_account: str
    to_account: str
    amount: Decimal = Field(gt=0, decimal_places=4, max_digits=20)
    date: date_type
    description: str | None = None
    status: Literal["pending", "posted"] = "pending"


class TransactionUpdate(BaseModel):
    date: date_type | None = None
    amount: Decimal | None = Field(default=None, decimal_places=4, max_digits=20)
    category: str | None = Field(
        default=None,
        description=(
            "category id or ltree path; '[account]' converts the row to a "
            "transfer to that account (DESIGN §3.3)"
        ),
    )
    payee: str | None = None
    description: str | None = None
    account: str | None = Field(
        default=None,
        description=(
            "perspective account (id or path) of a transfer: when recategorizing "
            "it back to a category, the side that keeps the row; with an amount "
            "on an expected/pending transfer, the side whose leg the amount is "
            "(a schedule instance defaults to its schedule's account)"
        ),
    )
    securities_trade: bool | None = Field(
        default=None,
        description=(
            "true marks a row on a brokerage account whose holdings come from the feed as a "
            "securities trade (a buy or sell: booked to the system account equity.securities, "
            "never spending or income), in place of `category`; the response's suggestion_id "
            "names the rule it proposes in the inbox. To change a marked row back, send the "
            "category it should have (false needs that category). Refused (422) on any other "
            "account, a transfer, a split, an expected row and a recorded trade's cash."
        ),
    )


class SearchPage(BaseModel):
    items: list[TransactionOut]
    total: int
    limit: int
    offset: int


def _handle(exc: ledger.LedgerError) -> HTTPException:
    code = status.HTTP_404_NOT_FOUND if isinstance(exc, ledger.NotFoundError) else 422
    return HTTPException(code, str(exc))


def _detail(session: Session, tx_id: uuid.UUID) -> TransactionDetail:
    from purser import review as review_service
    from purser import transfers

    tx = ledger.get_transaction(session, tx_id)
    locked, adjusted = _trade_flags(session, [tx])
    return TransactionDetail.from_model_detail(
        tx,
        has_linked_originals=transfers.has_linked_originals(session, tx.id),
        review=session.get(TransactionReview, tx.id),
        needs_review=tx.id in review_service.needs_review_ids(session, [tx.id]),
        split_lines=splits.describe_lines(session, tx) if tx.split_account_id else None,
        securities_trade_locked=tx.id in locked,
        balance_adjustment=tx.id in adjusted,
    )


def _trade_flags(
    session: Session, rows: list[Transaction]
) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    """(rows a recorded trade holds, balance true-ups) among ``rows``' securities
    trades (decision 119). Securities rows are few, so the per-row trade walk
    runs only for them."""
    from purser.feed import trueup

    trades = [t for t in rows if ledger.is_securities_trade(t.postings)]
    if not trades:
        return set(), set()
    locked = {t.id for t in trades if ledger.held_by_trade(session, t)}
    return locked, trueup.true_up_ids(session, [t.id for t in trades])


@router.post("", status_code=status.HTTP_201_CREATED)
def create_transaction(
    body: TransactionCreate, token: WriteToken, session: DbSession
) -> TransactionDetail:
    """Create from the flat form, with the requested status (#436)."""
    try:
        tx = ledger.create_flat_transaction(
            session,
            account_ref=body.account,
            category_ref=body.category,
            amount=body.amount,
            date=body.date,
            payee_name=body.payee,
            description=body.description,
            requested_status=core.Status(body.status),
            token=token,
        )
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    return _detail(session, tx.id)


@router.post("/transfer", status_code=status.HTTP_201_CREATED)
def create_transfer(
    body: TransferCreate, token: WriteToken, session: DbSession
) -> TransactionDetail:
    """One transaction, two real-account postings — never two mirrored rows."""
    try:
        tx = ledger.create_transfer(
            session,
            from_account_ref=body.from_account,
            to_account_ref=body.to_account,
            amount=body.amount,
            date=body.date,
            description=body.description,
            requested_status=core.Status(body.status),
            token=token,
        )
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    return _detail(session, tx.id)


@router.get(
    "",
    dependencies=[
        disclose(
            Emits(
                payee=True,
                description=("items[].description",),
                amount=(
                    "items[].amount",
                    "items[].remainder_amount",
                    "items[].value_change",
                    "items[].balance_adjustment",
                ),
                balance=("items[].balance",),
            )
        )
    ],
)
def search_transactions(
    token: ReadToken,
    session: DbSession,
    account: Annotated[str | None, Query()] = None,
    category: Annotated[
        str | None, Query(description="category subtree, e.g. expenses.dining")
    ] = None,
    payee: Annotated[str | None, Query()] = None,
    q: Annotated[
        str | None,
        Query(
            description=(
                "search: words all found in payee, description or memo "
                '("a phrase", -word excludes; payee:, desc:, memo: scope one; '
                "payee:py_... is that payee), plus account:, category:, "
                "amount:>100 (magnitude; is:debit or is:credit for direction), "
                "amount:10..20, date:2026-10, date:2026-01..2026-03, "
                "status:pending,posted, is:unreviewed. A malformed clause is a 422."
            )
        ),
    ] = None,
    start: Annotated[date_type | None, Query()] = None,
    end: Annotated[date_type | None, Query()] = None,
    status_in: Annotated[list[str] | None, Query(alias="status")] = None,
    include_collapsed: Annotated[bool, Query()] = False,
    with_category_provenance: Annotated[
        bool, Query(description="fill category_source from the audit trail (register badges)")
    ] = False,
    reviewed: Annotated[
        bool | None,
        Query(description="false = the unreviewed set (#175); true = its complement"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SearchPage:
    perspective_account_id: uuid.UUID | None = None
    if account is not None:
        try:
            perspective = ledger.resolve_account(session, account)
        except ledger.LedgerError as exc:
            raise _handle(exc) from exc
        # Categories are virtual and have no running-balance rows by
        # construction (DESIGN §3.5), so a category perspective yields a
        # blank Balance column either way — skip the lookup rather than
        # issue a query that can only come back empty.
        perspective_account_id = None if perspective.is_virtual else perspective.id
    try:
        rows, total = ledger.search_transactions(
            session,
            account_ref=account,
            category_subtree=category,
            payee=payee,
            q=q,
            start=start,
            end=end,
            statuses=status_in,
            include_collapsed=include_collapsed,
            reviewed=reviewed,
            limit=limit,
            offset=offset,
        )
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    balances_map: dict[uuid.UUID, Decimal] = {}
    if rows and perspective_account_id is not None:
        balances_map = balances.balances_for_transactions(
            session, perspective_account_id, [t.id for t in rows]
        )
    linked: set[uuid.UUID] = set()
    if rows:
        from purser import transfers

        linked = {
            row_id
            for row_id in session.execute(
                select(Transaction.superseded_by)
                .where(
                    Transaction.superseded_by.in_([t.id for t in rows]),
                    Transaction.status == "void",
                    # An expected a row settles is not a linked bank row: a
                    # matched bill payment carries nothing to unlink.
                    transfers.linked_original_clause(),
                )
                .distinct()
            ).scalars()
            if row_id is not None
        }
    reviews: dict[uuid.UUID, TransactionReview] = {}
    if rows:
        reviews = {
            r.transaction_id: r
            for r in session.execute(
                select(TransactionReview).where(
                    TransactionReview.transaction_id.in_([t.id for t in rows])
                )
            ).scalars()
        }
    # The badge's truth, from the same clause as ?reviewed=false (#248).
    from purser import review as review_service

    unreviewed = review_service.needs_review_ids(session, [t.id for t in rows])
    provenance: dict[str, str] = {}
    if with_category_provenance and rows:
        # One grouped audit read. Pending in-place categorizations audit the
        # row itself; posted corrections audit the ORIGINAL and name the
        # replacement — the register shows chain heads, so match both.
        from sqlalchemy import or_

        from purser.models import AuditLog

        ids = [t.id for t in rows]
        id_strs = [str(i) for i in ids]
        entries = session.execute(
            select(AuditLog)
            .where(
                AuditLog.action == "transaction.categorize",
                or_(
                    AuditLog.entity_id.in_(ids),
                    # before_after is encrypted (#137); the replacement id lives
                    # in its own plaintext column now.
                    AuditLog.replacement_id.in_(ids),
                ),
            )
            .order_by(AuditLog.at)  # latest wins
        ).scalars()
        for entry in entries:
            after = (entry.before_after or {}).get("after") or {}
            source = after.get("source")
            if not source:
                continue
            target = after.get("replacement_id") or (
                str(entry.entity_id) if entry.entity_id else None
            )
            if target in id_strs:
                provenance[target] = source
    locked, adjusted = _trade_flags(session, rows)
    return SearchPage(
        items=[
            TransactionOut.from_model(
                t,
                perspective_account_id=perspective_account_id,
                has_linked_originals=t.id in linked,
                category_source=provenance.get(str(t.id)),
                review=reviews.get(t.id),
                needs_review=t.id in unreviewed,
                balance=balances_map.get(t.id),
                securities_trade_locked=t.id in locked,
                balance_adjustment=t.id in adjusted,
            )
            for t in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


# NOTE: these two literal paths MUST be declared before /{tx_id} below.
# FastAPI matches routes in registration order, so /transactions/review-batch
# declared after it is swallowed by the parameterized route and 422s trying
# to parse "review-batch" as a UUID.
class MarkReviewed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transaction_ids: list[uuid.UUID]
    note: str | None = None
    # Reviewed, but handed back for a human look. The row still LEAVES the
    # unreviewed set — this is an escape hatch, not a second review tier.
    escalate: bool = False


@router.post("/mark-reviewed")
def mark_reviewed(body: MarkReviewed, token: WriteToken, session: DbSession) -> dict[str, int]:
    """Bulk, idempotent. A review mark has no draft state, like
    confirm_transfer and match (#175, decision 47)."""
    from purser import review as review_service

    marked = 0
    already = 0
    for tx_id in body.transaction_ids:
        created = review_service.mark(
            session,
            tx_id=tx_id,
            token=token,
            via=review_service.VIA_EXPLICIT,
            note=body.note,
            escalate=body.escalate,
        )
        if created:
            marked += 1
        else:
            already += 1
    session.commit()
    return {"marked": marked, "already_reviewed": already}


class ReviewAccountSummary(BaseModel):
    account_id: uuid.UUID
    path: str
    name: str
    total: int
    pending: int
    agent_pending: int


class ReviewSummary(BaseModel):
    total: int
    pending: int
    agent_pending: int
    oldest_date: date_type | None
    accounts: list[ReviewAccountSummary]


@router.get("/review-summary", dependencies=[disclose(NOTHING)])
def review_summary(token: ReadToken, session: DbSession) -> ReviewSummary:
    """Review counts and register destinations. Transfers count once globally,
    but appear on both accounts; per-account totals must not be summed."""
    from purser import review as review_service

    return ReviewSummary(**review_service.summary(session))


@router.get(
    "/review-batch",
    dependencies=[
        disclose(
            Emits(
                payee=True,
                narrative=True,
                description=("rows[].description", "escalated[].description"),
                amount=("rows[].amount", "escalated[].amount"),
            )
        )
    ],
)
def review_batch(
    token: ReadToken,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=25)] = 10,
    account: Annotated[str | None, Query()] = None,
    include_guidance: Annotated[bool, Query()] = True,
) -> dict[str, Any]:
    """The same payload the MCP tool serves — one service function, so REST
    and MCP can never disagree about what a reviewer sees."""
    from purser import review as review_service

    try:
        return review_service.batch(
            session, limit=limit, account_ref=account, include_guidance=include_guidance
        )
    except ledger.LedgerError as exc:  # unknown (404) or malformed (422, #214) account
        raise _handle(exc) from exc


SELECTION_MAX = 50


class PresentIn(BaseModel):
    transaction_ids: list[uuid.UUID] = Field(min_length=1, max_length=SELECTION_MAX)
    note: str | None = Field(default=None, max_length=200)


class PresentOut(BaseModel):
    url: str
    count: int


@router.post("/present", dependencies=[disclose(NOTHING)])
def present(body: PresentIn, token: ReadToken, session: DbSession) -> PresentOut:
    """Show the person transactions without passing their details through
    the caller (#613; the MCP `present` tool). Returns a link that opens
    those rows, in full detail and in the order given, for the signed-in
    person in Purser's web app; an integration replies with the link instead
    of repeating payees, descriptions or amounts. Up to 50 ids; the note (200
    characters or fewer) is shown above the rows. The link carries only ids
    and the note. A read: nothing changes."""
    from purser.selection import selection_link

    try:
        out = selection_link(session, [str(t) for t in body.transaction_ids], body.note)
    except ledger.LedgerError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return PresentOut(**out)


@router.get("/selection", dependencies=[disclose(NOTHING)])
def selection(
    token: ReadToken,
    session: DbSession,
    ids: Annotated[list[uuid.UUID], Query(alias="id", max_length=SELECTION_MAX)],
) -> list[TransactionDetail]:
    """The rows an agent showed the person with `present` (MCP tool or
    `POST /transactions/present`; #437, #613), in full detail and in the
    order given. A signed-in person's view: the point of `present` is that
    the values reach the person without passing through the model, so an API
    token is refused."""
    if token.kind != "session":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "a presented selection is for a signed-in person"
        )
    try:
        return [_detail(session, tx_id) for tx_id in dict.fromkeys(ids)]
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc


@router.get(
    "/{tx_id}",
    dependencies=[
        disclose(
            Emits(
                payee=True,
                description=("description", "postings[].memo", "split.lines[].memo"),
                amount=(
                    "amount",
                    "postings[].amount",
                    "split.lines[].amount",
                    "remainder_amount",
                    "value_change",
                    "balance_adjustment",
                ),
                balance=("balance",),
            )
        )
    ],
)
def get_transaction(tx_id: uuid.UUID, token: ReadToken, session: DbSession) -> TransactionDetail:
    try:
        return _detail(session, tx_id)
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc


@router.patch("/{tx_id}")
def update_transaction(
    tx_id: uuid.UUID, body: TransactionUpdate, token: WriteToken, session: DbSession
) -> TransactionDetail:
    """pending/expected edit in place; posted becomes an atomic
    reversal + replacement pair. category='[account]'
    converts the row to a transfer; a plain category on a transfer converts
    it back (send `account` = the side that keeps the row). An amount on an
    expected/pending transfer is the leg of `account` (a schedule instance
    defaults to its schedule's account) and moves both legs, never the
    direction; a posted or bank-linked transfer is refused with how to
    correct it. securities_trade=true marks a feed-holdings brokerage row a
    securities trade and proposes a rule for rows like it in the inbox
    (`suggestion_id`)."""
    from purser import suggestions

    try:
        tx = ledger.update_transaction(
            session,
            tx_id=tx_id,
            token=token,
            date=body.date,
            amount=body.amount,
            category_ref=body.category,
            payee_name=body.payee,
            description=body.description,
            account_ref=body.account,
            securities_trade=body.securities_trade,
        )
        proposed = (
            suggestions.propose_trade_rule(session, tx=tx, token=token)
            if body.securities_trade
            else None
        )
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    detail = _detail(session, tx.id)
    detail.suggestion_id = proposed.id if proposed is not None else None
    return detail


@router.post("/{tx_id}/post")
def post_transaction(tx_id: uuid.UUID, token: WriteToken, session: DbSession) -> TransactionDetail:
    try:
        tx = ledger.transition_status(
            session, tx_id=tx_id, new_status=core.Status.POSTED, token=token
        )
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    return _detail(session, tx.id)


class UnlinkResult(BaseModel):
    transfer_id: uuid.UUID
    transfer_voided: bool
    resurrected_ids: list[uuid.UUID]


@router.post("/{tx_id}/unlink-transfer")
def unlink_transfer(tx_id: uuid.UUID, token: WriteToken, session: DbSession) -> UnlinkResult:
    """Undo a transfer merge/absorb: resurrect the voided bank rows (their
    content hashes move with them, so re-imports stay no-ops) and suppress
    the automatic relink paths."""
    from purser import transfers

    try:
        result = transfers.unlink_transfer(session, transfer_id=tx_id, token=token)
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    return UnlinkResult(**result)


class SplitLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str = Field(
        description="category path or id, or '[account path or id]' for an account line"
    )
    amount: Decimal = Field(
        decimal_places=4,
        max_digits=20,
        description="the line's share of the row's amount, in the flat form's sign (negative ="
        " money out); the lines sum to the row's amount",
    )
    memo: str | None = Field(default=None, max_length=splits.MAX_MEMO_CHARS)
    backing_ids: list[uuid.UUID] = Field(
        default_factory=list,
        max_length=splits.MAX_BACKING_ROWS,
        description="account line only: bank rows on that account whose amounts sum exactly to"
        " the line's leg (its amount, negated); they are merged into the split. At most"
        f" {splits.MAX_BACKING_ROWS} across the split's lines",
    )


class SplitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lines: list[SplitLineIn] = Field(min_length=2, max_length=splits.MAX_LINES)
    dry_run: bool = False


class SplitResult(BaseModel):
    """What a split write did (or, with `dry_run`, would do)."""

    transaction_id: uuid.UUID = Field(description="the split row now (a posted row's replacement)")
    replaces_id: uuid.UUID | None = None
    dry_run: bool = False
    status: str | None = None
    account: str
    amount: Decimal
    category: str | None = None
    lines: list[SplitLineOut] = Field(default_factory=list)
    backing_added: list[uuid.UUID] = Field(default_factory=list)
    backing_restored: list[uuid.UUID] = Field(
        default_factory=list, description="rows that backed the split before and come back"
    )
    restored_ids: list[uuid.UUID] = Field(
        default_factory=list, description="the clones those rows came back as"
    )


class BackingCandidate(BaseModel):
    id: uuid.UUID
    date: date_type
    amount: Decimal
    status: str = Field(description="pending | posted, or merged for a row backing this split")
    payee: str | None
    description: str | None
    backing: bool = Field(description="already backs this split")
    exact: bool = Field(description="its amount is exactly the `amount` asked for")


class BackingCandidates(BaseModel):
    account: str
    window_days: int
    amount: Decimal | None
    candidates: list[BackingCandidate]


@router.put("/{tx_id}/split")
def split_transaction(
    tx_id: uuid.UUID, body: SplitIn, token: WriteToken, session: DbSession
) -> SplitResult:
    """Split a row across categories and `[Account]` lines, or replace the
    lines of a split (decision 118). The row keeps its real leg at the
    bank's amount; the lines must sum to it. An account line's
    `backing_ids` merge that account's bank rows into the split, so nothing
    is counted twice; rows that backed it before and are not named again
    come back. A posted row becomes a reversal + replacement; a manual
    pending row changes in place; a bank pending is refused until it posts.
    `dry_run` validates and writes nothing."""
    from purser import splits

    try:
        result = splits.split_transaction(
            session,
            tx_id=tx_id,
            lines=[
                splits.LineInput(
                    target=line.category,
                    amount=line.amount,
                    memo=line.memo,
                    backing_ids=tuple(line.backing_ids),
                )
                for line in body.lines
            ],
            token=token,
            dry_run=body.dry_run,
        )
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    if body.dry_run:
        session.rollback()
    else:
        session.commit()
    return SplitResult(**result)


@router.delete("/{tx_id}/split")
def unsplit_transaction(
    tx_id: uuid.UUID,
    token: WriteToken,
    session: DbSession,
    category: Annotated[
        str | None,
        Query(description="the row's category afterwards; default the uncategorized placeholder"),
    ] = None,
    dry_run: Annotated[bool, Query()] = False,
) -> SplitResult:
    """Remove a split: the row goes back to one category (the uncategorized
    placeholder unless `category` names one) and every backing row comes
    back exactly as it was."""
    from purser import splits

    try:
        result = splits.unsplit_transaction(
            session, tx_id=tx_id, token=token, category_ref=category, dry_run=dry_run
        )
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    if dry_run:
        session.rollback()
    else:
        session.commit()
    return SplitResult(**result)


@router.get(
    "/{tx_id}/split/candidates",
    dependencies=[
        disclose(
            Emits(
                payee=True,
                description=("candidates[].description",),
                amount=("amount", "candidates[].amount"),
            )
        )
    ],
)
def split_backing_candidates(
    tx_id: uuid.UUID,
    token: ReadToken,
    session: DbSession,
    account: Annotated[str, Query(description="the account line's account (path or id)")],
    amount: Annotated[
        Decimal | None,
        Query(description="the leg the line books on that account (its amount, negated)"),
    ] = None,
) -> BackingCandidates:
    """Bank rows that could back an account line of this row's split: live
    single rows on that account within 7 days of the row, nearest amount
    then nearest date first; the rows already backing it come first."""
    from purser import splits

    try:
        found = splits.backing_candidates(session, tx_id=tx_id, account_ref=account, amount=amount)
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    return BackingCandidates(**found)


@router.post("/{tx_id}/void")
def void_transaction(tx_id: uuid.UUID, token: WriteToken, session: DbSession) -> TransactionDetail:
    """Void an expected or pending row. A voided schedule instance keeps its
    slot filled, so the occurrence is skipped. Voiding a pending that a match
    names settles the match: a confirmed match is released and its instance
    is expected again in its slot, override kept; a proposal is withdrawn. A
    posted row cannot be voided directly — correct it or merge it."""
    try:
        tx = ledger.transition_status(
            session, tx_id=tx_id, new_status=core.Status.VOID, token=token
        )
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    return _detail(session, tx.id)


@router.post("/{tx_id}/reset-instance")
def reset_instance(tx_id: uuid.UUID, token: WriteToken, session: DbSession) -> TransactionDetail:
    """Hand a schedule instance back to its rule: unpinned, with the rule's
    date (its slot), name, payee and current estimate — both legs of a
    transfer. When the rule no longer produces the slot, or estimates
    nothing, the instance is voided and its slot released instead. Live
    instances of active schedules only."""
    from purser import schedules

    try:
        tx = schedules.reset_instance(session, tx_id=tx_id, token=token)
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    return _detail(session, tx.id)
