"""Accounts & categories API (hybrid ledger: categories are virtual accounts)."""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from purser import balances, ledger, networth, reconciliation
from purser.admin import security
from purser.admin.auth import ReadToken, WriteToken
from purser.db import get_session
from purser.models import Account, ApiToken

DbSession = Annotated[Session, Depends(get_session)]

router = APIRouter(prefix="/accounts", tags=["accounts"])
categories_router = APIRouter(prefix="/categories", tags=["categories"])


class ReconciliationOut(BaseModel):
    """A recorded reconciliation (issue #268), re-checked against the ledger
    when it is read — so a backdated import, edit or replay that moved the
    balance on ``as_of`` shows up here as a nonzero ``difference`` instead of
    leaving the reconciled-through marker silently wrong.

    ``statement_balance`` is the figure as the statement shows it (positive =
    money held; for a credit card, the amount owed). Every other amount is in
    ledger sign (a card is negative when owed)."""

    id: uuid.UUID
    as_of: date
    basis: Literal["posted", "pending_inclusive"]
    statement_balance: Decimal
    target_balance: Decimal  # the statement figure in ledger sign
    adjustment: Decimal  # what Purser posted to reach the target; 0 = it already matched
    adjustment_tx_id: uuid.UUID | None
    created_at: datetime
    # Live check, computed on read:
    ledger_balance: Decimal  # the balance on as_of in this basis NOW
    difference: Decimal  # ledger_balance - target_balance; 0 = still reconciled
    in_balance: bool

    @classmethod
    def from_check(cls, c: reconciliation.Check) -> "ReconciliationOut":
        row = c.reconciliation
        return cls(
            id=row.id,
            as_of=row.as_of,
            basis=row.basis,
            statement_balance=c.statement_balance,
            target_balance=row.target_balance,
            adjustment=row.adjustment,
            adjustment_tx_id=row.adjustment_tx_id,
            created_at=row.created_at,
            ledger_balance=c.ledger_balance,
            difference=c.difference,
            in_balance=c.in_balance,
        )


DriftBasis = Literal["pending_inclusive", "posted_only"]


def _drift_basis(value: str | None) -> DriftBasis | None:
    """The stored pin as the response Literal (the DB CHECK admits only these
    two values; anything else reads as no pin)."""
    if value == "pending_inclusive":
        return "pending_inclusive"
    if value == "posted_only":
        return "posted_only"
    return None


class AccountOut(BaseModel):
    id: uuid.UUID
    parent_id: uuid.UUID | None
    name: str
    type: str
    is_virtual: bool
    path: str
    description: str | None  # classifier-oriented, for categories (DESIGN §3.2)
    currency: str
    # None for API tokens — external account identifiers never leave the
    # server for agents (DESIGN §9, #437).
    external_id: str | None
    connection_id: uuid.UUID | None
    min_balance: Decimal | None
    # Lifecycle (DESIGN §3.4): hidden_at drops it from the default listing;
    # closed_at makes it read-only. Both are always null for categories.
    hidden_at: datetime | None
    closed_at: datetime | None
    # Feed sync-from boundary (§7.1, decision 40) — a date, not an external
    # identifier, so it is not redacted.
    feed_start: date | None = None
    # Feed sign inversion (§7.1, decision 43) — feed configuration, admin-set.
    feed_invert: bool = False
    # Drift-basis pin (§7.3, decision 83) — feed configuration, admin-set; not
    # an external identifier, so not redacted. Null = the basis is learned
    # from the account's sync history (decision 64).
    drift_basis_override: DriftBasis | None = None
    # Where a brokerage account's holdings come from (§8, decision 88): the
    # feed's snapshots, or positions tracked by hand. Admin-set feed
    # configuration; always "feed" off brokerage.
    holdings_source: Literal["feed", "manual"] = "feed"
    balance: Decimal | None = None
    # Valuation overlay (DESIGN §8, decision 42): open loan/property accounts
    # report their latest valuation as `balance` (method "valuation",
    # `balance_as_of` = the valuation date); everything else reports the
    # postings sum (method "postings"). `postings_balance` carries the
    # postings sum alongside a valuation so flows that operate on postings
    # (the close panel's balancing adjustment) keep seeing it.
    balance_method: str | None = None
    balance_as_of: date | None = None
    postings_balance: Decimal | None = None
    # Reconciliation (issue #268): the latest reconciled date, and that
    # reconciliation re-checked against the ledger now. Never one without the
    # other — a marker shown without its check could be silently stale.
    reconciled_through: date | None = None
    reconciliation: ReconciliationOut | None = None
    created_at: datetime

    @classmethod
    def from_model(
        cls,
        a: Account,
        *,
        redacted: bool,
        balance: Decimal | None = None,
        balance_method: str | None = None,
        balance_as_of: date | None = None,
        postings_balance: Decimal | None = None,
        reconciliation: ReconciliationOut | None = None,
    ) -> "AccountOut":
        return cls(
            id=a.id,
            parent_id=a.parent_id,
            name=a.name,
            type=a.type,
            is_virtual=a.is_virtual,
            path=a.path,
            description=a.description,
            currency=a.currency.strip(),
            external_id=None if redacted else a.external_id,
            connection_id=None if redacted else a.connection_id,
            min_balance=a.min_balance,
            hidden_at=a.hidden_at,
            closed_at=a.closed_at,
            feed_start=a.feed_start,
            feed_invert=a.feed_invert,
            drift_basis_override=_drift_basis(a.drift_basis_override),
            holdings_source="manual" if a.holdings_source == "manual" else "feed",
            balance=balance,
            balance_method=balance_method,
            balance_as_of=balance_as_of,
            postings_balance=postings_balance,
            reconciled_through=a.reconciled_through,
            reconciliation=reconciliation,
            created_at=a.created_at,
        )


_MIN_BALANCE_HELP = (
    "The account's floor, in ledger sign: the nightly forecast scan emits "
    "`forecast.shortfall` when the 30-day projection drops below it (e.g. `500.00` "
    "to be warned before checking goes under $500; a credit card, negative when "
    "owed, takes a negative floor such as `-4500.00`). Checking, savings, cash and "
    "credit-card accounts only (422 otherwise); at most 4 decimal places."
)


class AccountCreate(BaseModel):
    name: str
    type: str
    parent_id: uuid.UUID | None = None
    path: str | None = None
    currency: str = Field(default="USD", min_length=3, max_length=3)
    external_id: str | None = None
    min_balance: Decimal | None = Field(
        default=None,
        decimal_places=4,
        max_digits=20,
        description=_MIN_BALANCE_HELP,
    )
    description: str | None = None


class CategoryUpdate(BaseModel):
    """Name + description only — no path/re-parenting (ltree cascade and
    schedule templates make that a separate feature)."""

    name: str | None = Field(default=None, min_length=1)
    description: str | None = None


class AccountUpdate(BaseModel):
    """Rename / re-describe / hide / move a real account (DESIGN §3.4).
    ``hidden`` toggles the display-only ``hidden_at`` timestamp; ``path``
    moves the account and everything beneath it (decision 72).

    ``feed_start`` (§7.1, decision 40) is admin-only: explicit null clears,
    absent leaves unchanged (model_fields_set distinguishes the two).
    ``feed_invert`` (§7.1, decision 43) is admin-only feed configuration
    too; a plain bool, so absent/None simply leaves it unchanged.
    ``drift_basis_override`` (§7.3, decision 83) is admin-only as well, with
    ``feed_start``'s explicit-null-clears rule. ``holdings_source`` (§8,
    decision 88) is admin-only too; absent/None leaves it unchanged.
    ``min_balance`` (§3.4, #466) is write scope, with the explicit-null-clears
    rule."""

    name: str | None = Field(default=None, min_length=1)
    description: str | None = None
    hidden: bool | None = None
    feed_start: date | None = None
    feed_invert: bool | None = None
    drift_basis_override: DriftBasis | None = Field(
        default=None,
        description="Pin the ledger basis the post-sync balance-drift check compares this "
        "account's bank balance on: `posted_only` (the bank leaves pending charges out of "
        "its balance) or `pending_inclusive`. An explicit null clears the pin and the basis "
        "is learned from the account's sync history again; absent leaves it unchanged. "
        "Admin scope; checking, savings, cash and credit-card accounts only (422 otherwise).",
    )
    holdings_source: Literal["feed", "manual"] | None = Field(
        default=None,
        description="Where this brokerage account's holdings come from: `feed` (the "
        "SimpleFIN snapshots) or `manual` (positions recorded by hand, priced daily, and "
        "projected into the same snapshots). Switching to `manual` keeps every feed "
        "snapshot as history; the first manual snapshot is dated the day after the last "
        "feed one. Switching back to `feed` needs a feed mapping (422 without one) and changes "
        "no snapshot: the hand-tracked holdings stay in effect until the first sync after the "
        "switch, which ends them from its own date (a position the feed does not report ends "
        "there); positions stay, read-only. Admin scope; brokerage accounts only (422 "
        "otherwise).",
    )
    min_balance: Decimal | None = Field(
        default=None,
        decimal_places=4,
        max_digits=20,
        description=_MIN_BALANCE_HELP + " An explicit null clears the floor, and the "
        "account leaves the shortfall scan; absent leaves it unchanged. Write scope.",
    )
    path: str | None = Field(
        default=None,
        description="Move the account to this ltree path, e.g. `liabilities.citicard`. "
        "Accounts beneath it move along and keep their relative paths. Refused with 409 "
        "when an account already sits at or under the new path, and with 422 for a "
        "malformed path or a destination under a category root such as `expenses`. "
        "Categories keep their paths.",
    )


class CloseAccount(BaseModel):
    # Optional: only needed when the account carries a nonzero live balance.
    # Path or UUID of a category; the default path is auto-created if absent.
    adjustment_category: str | None = None


class CloseResult(BaseModel):
    closed_at: datetime
    adjustment_tx_id: uuid.UUID | None = None


class RollupOut(BaseModel):
    path: str
    total: Decimal
    transaction_count: int
    start: date | None
    end: date | None


def _handle(exc: ledger.LedgerError) -> HTTPException:
    if isinstance(exc, ledger.NotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, ledger.PathConflictError):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return HTTPException(422, str(exc))


def _reconciliation_fields(
    session: Session, accounts: list[Account]
) -> dict[uuid.UUID, dict[str, object]]:
    """`reconciliation` kwargs for reconciled accounts: the latest one, re-checked
    against the ledger (one batched query for any number of accounts)."""
    return {
        account_id: {"reconciliation": ReconciliationOut.from_check(c)}
        for account_id, c in reconciliation.latest_checks(session, accounts).items()
    }


def _balance_fields(
    session: Session, accounts: list[Account], *, all_accounts: bool = False
) -> dict[uuid.UUID, dict[str, object]]:
    """Per-account `from_model` balance kwargs: postings sum everywhere,
    replaced by the latest valuation for open loan/property (decision 42),
    plus the latest holdings market value for open brokerage (decision 50).
    `balance_as_of`/`postings_balance` ride along only when an overlay
    actually produced the answer."""
    ids = None if all_accounts else [a.id for a in accounts]
    reported = networth.reported_balances(session, ids)
    fields: dict[uuid.UUID, dict[str, object]] = {}
    for a in accounts:
        row = reported.get(a.id)
        if row is None:
            fields[a.id] = {"balance": Decimal("0"), "balance_method": "postings"}
            continue
        fields[a.id] = {"balance": row.balance, "balance_method": row.method}
        if row.as_of is not None:
            fields[a.id]["balance_as_of"] = row.as_of
            fields[a.id]["postings_balance"] = row.postings
    return fields


def _account_out(session: Session, account: Account, token: ApiToken) -> AccountOut:
    """One real account with its balance fields and reconciliation check."""
    fields = _balance_fields(session, [account])
    reconciled = _reconciliation_fields(session, [account])
    return AccountOut.from_model(
        account,
        redacted=security.withholds_external_ids(token.kind, token.scopes),
        **fields[account.id],  # type: ignore[arg-type]
        **reconciled.get(account.id, {}),  # type: ignore[arg-type]
    )


@router.get("")
def list_accounts(
    token: ReadToken,
    session: DbSession,
    include_balances: Annotated[bool, Query()] = False,
    include_virtual: Annotated[bool, Query()] = False,
    include_hidden: Annotated[bool, Query()] = False,
    include_closed: Annotated[bool, Query()] = False,
) -> list[AccountOut]:
    # Hidden/closed filtering applies ONLY to this default listing (DESIGN
    # §3.4): every other surface — net worth, sync, single-account GET — still
    # sees hidden and closed accounts.
    redacted = security.withholds_external_ids(token.kind, token.scopes)
    stmt = select(Account).order_by(Account.path)
    if not include_virtual:
        stmt = stmt.where(Account.is_virtual.is_(False))
    if not include_hidden:
        stmt = stmt.where(Account.hidden_at.is_(None))
    if not include_closed:
        stmt = stmt.where(Account.closed_at.is_(None))
    accounts = list(session.execute(stmt).scalars())
    fields = _balance_fields(session, accounts, all_accounts=True) if include_balances else {}
    reconciled = _reconciliation_fields(session, accounts)
    return [
        AccountOut.from_model(
            a,
            redacted=redacted,
            **fields.get(a.id, {}),  # type: ignore[arg-type]
            **reconciled.get(a.id, {}),  # type: ignore[arg-type]
        )
        for a in accounts
    ]


@router.get("/{account_id}")
def get_account(account_id: uuid.UUID, token: ReadToken, session: DbSession) -> AccountOut:
    try:
        account = ledger.get_account(session, account_id)
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    return _account_out(session, account, token)


class BalanceAsOf(BaseModel):
    """Point-in-time balance from the stored running-balance table
    (decision 37: chain-head pending+posted register semantics), or the
    latest valuation for open loan/property accounts (decision 42)."""

    account_id: uuid.UUID
    path: str
    as_of: date
    balance: Decimal
    method: str  # "postings" | "valuation" | "postings+holdings"
    currency: str


@router.get("/{account_id}/balance")
def balance_as_of(
    account_id: uuid.UUID,
    token: ReadToken,
    session: DbSession,
    as_of: Annotated[date | None, Query(description="defaults to today (UTC)")] = None,
) -> BalanceAsOf:
    """Balance at end of ``as_of``. Open loan/property accounts answer with
    their latest valuation at or before that date (decision 42); open
    brokerage accounts add their latest holdings market value on top of the
    postings basis (decision 50); everything else answers from the stored
    running balances (decision 37), never recomputed. 0 before first
    activity; closed accounts answer for their whole history."""
    try:
        account = ledger.get_account(session, account_id)
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    if account.is_virtual:
        raise HTTPException(422, f"{account.path} is a category; balances apply to real accounts")
    effective = as_of or datetime.now(UTC).date()
    balance, method, _overlay_as_of = networth.account_balance_at(session, account, effective)
    return BalanceAsOf(
        account_id=account.id,
        path=account.path,
        as_of=effective,
        balance=balance,
        method=method,
        currency=account.currency.strip(),
    )


class BalancePoint(BaseModel):
    date: date
    balance: Decimal
    delta: Decimal
    transaction_id: uuid.UUID


class BalanceSeries(BaseModel):
    """Full-resolution balance history: one point per stored change, which is
    the grain the running-balance store keeps and the register shows."""

    account_id: uuid.UUID
    path: str
    start: date
    end: date
    currency: str
    method: str  # "postings" | "valuation" | "postings+holdings"
    opening: Decimal
    points: list[BalancePoint]
    truncated: bool


_SERIES_MAX_POINTS = 5000


@router.get("/{account_id}/balance/series")
def balance_series(
    account_id: uuid.UUID,
    token: ReadToken,
    session: DbSession,
    start: Annotated[date | None, Query(description="defaults to end - 90d")] = None,
    end: Annotated[date | None, Query(description="defaults to today (UTC)")] = None,
) -> BalanceSeries:
    """Balance history for one account over ``[start, end]``, one point per
    transaction that moved it, plus the ``opening`` balance carried into the
    window.

    REST is not token-budgeted, so this is the full per-change series — the
    downsampled, narrated form is MCP ``balance_history``. Both compose on
    the same service functions, so they can never disagree. ``truncated`` is
    set rather than silently dropping the tail past 5000 points."""
    try:
        account = ledger.get_account(session, account_id)
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    if account.is_virtual:
        raise HTTPException(422, f"{account.path} is a category; balances apply to real accounts")
    window_end = end or datetime.now(UTC).date()
    window_start = start or window_end - timedelta(days=90)
    if window_end < window_start:
        raise HTTPException(422, "end must be >= start")

    rows, truncated = balances.change_points(
        session, account.id, start=window_start, end=window_end, limit=_SERIES_MAX_POINTS
    )
    # The opening balance is the day BEFORE the window: `start` itself is the
    # first point a caller can see move, so carrying its own activity into
    # the opening would double-count it.
    opening_map, method = networth.account_balance_series(
        session, account, dates=[window_start - timedelta(days=1)]
    )
    return BalanceSeries(
        account_id=account.id,
        path=account.path,
        start=window_start,
        end=window_end,
        currency=account.currency.strip(),
        method=method,
        opening=next(iter(opening_map.values()), Decimal("0")),
        points=[
            BalancePoint(
                date=row.date,
                balance=Decimal(row.balance),
                delta=Decimal(row.delta),
                transaction_id=row.transaction_id,
            )
            for row in rows
        ],
        truncated=truncated,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
def create_account(body: AccountCreate, token: WriteToken, session: DbSession) -> AccountOut:
    try:
        account = ledger.create_account(
            session,
            name=body.name,
            type_=body.type,
            parent_id=body.parent_id,
            path=body.path,
            currency=body.currency,
            external_id=body.external_id,
            min_balance=body.min_balance,
            description=body.description,
            actor_token_id=token.id,
        )
        # A new card/loan is a strong signal its payments already sit in the
        # other accounts' history: suggest them, never activate (#234).
        from purser import schedules as schedules_service

        schedules_service.detect_for_new_liability(session, account, actor_token_id=token.id)
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    return AccountOut.from_model(
        account, redacted=security.withholds_external_ids(token.kind, token.scopes)
    )


_PATH_CONFLICT = {
    "description": "The new `path` is taken: an account already sits at it or beneath it."
}


@router.patch("/{account_id}", responses={409: _PATH_CONFLICT})
def update_account(
    account_id: uuid.UUID,
    body: AccountUpdate,
    token: WriteToken,
    session: DbSession,
    background_tasks: BackgroundTasks,
) -> AccountOut:
    """Rename / re-describe / hide / move a real account, or set its floor.

    ``min_balance`` sets (or, with null, clears) the floor the nightly
    forecast scan checks; the next scan uses it. A new ``path`` moves the
    account and every account beneath it, keeping their relative paths; the
    move is audited (`account.move`) and emits `account.moved`. Balances and
    history are unaffected: transactions point at accounts by id."""
    # feed_start/feed_invert/drift_basis_override/holdings_source are feed
    # configuration — admin, like connect/discovered/map (§7.1); rename/
    # describe/hide stay write-scope. Field-conditional check because they
    # ride the existing account-update surface (#154). holdings_source changes
    # what the importer writes (decision 88), exactly like feed_start.
    feed_start_set = "feed_start" in body.model_fields_set
    feed_invert_set = "feed_invert" in body.model_fields_set
    drift_basis_set = "drift_basis_override" in body.model_fields_set
    holdings_source_set = "holdings_source" in body.model_fields_set
    min_balance_set = "min_balance" in body.model_fields_set
    if (
        feed_start_set or feed_invert_set or drift_basis_set or holdings_source_set
    ) and not security.is_admin(token.scopes):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "requires scope: admin")
    try:
        account = ledger.update_account(
            session,
            account_id=account_id,
            name=body.name,
            description=body.description,
            hidden=body.hidden,
            feed_start=body.feed_start,
            feed_start_set=feed_start_set,
            feed_invert=body.feed_invert,
            drift_basis_override=body.drift_basis_override,
            drift_basis_override_set=drift_basis_set,
            holdings_source=body.holdings_source,
            min_balance=body.min_balance,
            min_balance_set=min_balance_set,
            path=body.path,
            actor_token_id=token.id,
        )
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    if holdings_source_set:
        # A switch can rewrite snapshot rows; heal net worth now, like the
        # other balance-moving writes (decision 38).
        background_tasks.add_task(networth.consume_watermark)
    return _account_out(session, account, token)


@router.post("/{account_id}/close")
def close_account(
    account_id: uuid.UUID,
    body: CloseAccount,
    token: WriteToken,
    session: DbSession,
    background_tasks: BackgroundTasks,
) -> CloseResult:
    """Close a real account: void expected instances, post a guided balancing
    adjustment (when nonzero), unmap any feed link, then make it read-only."""
    try:
        result = ledger.close_account(
            session,
            account_id=account_id,
            adjustment_category=body.adjustment_category,
            token=token,
        )
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    background_tasks.add_task(networth.consume_watermark)
    return CloseResult(closed_at=result["closed_at"], adjustment_tx_id=result["adjustment_tx_id"])


@router.post("/{account_id}/reopen")
def reopen_account(
    account_id: uuid.UUID,
    token: WriteToken,
    session: DbSession,
    background_tasks: BackgroundTasks,
) -> AccountOut:
    """Reopen a closed account so it accepts writes (and corrections) again."""
    try:
        account = ledger.reopen_account(session, account_id=account_id, token=token)
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    background_tasks.add_task(networth.consume_watermark)
    return _account_out(session, account, token)


class ReconcileRequest(BaseModel):
    as_of: date = Field(
        description="The statement date: the balance is the one at the end of this day. "
        "Today (UTC) or earlier; a future date is rejected."
    )
    statement_balance: Decimal = Field(
        decimal_places=4,
        max_digits=20,
        description="The balance AS THE STATEMENT SHOWS IT. Checking, savings and cash: "
        "positive = money in the account. Credit card: positive = the amount owed "
        "(a credit balance is negative). Purser converts it to ledger sign, where a card "
        "is negative when owed.",
    )
    basis: Literal["posted", "pending_inclusive"] = Field(
        default="posted",
        description="Which balance the statement reports: `posted` (cleared rows only, "
        "the usual statement balance) or `pending_inclusive` (posted plus pending, the "
        "'current balance' many banks show).",
    )
    dry_run: bool = Field(
        default=False,
        description="Return exactly what the call would do — the same computation, "
        "conflicts included — without writing anything.",
    )


class ReconcileResult(BaseModel):
    """What the reconcile did (or, with ``dry_run``, would do). Amounts are in
    ledger sign except ``statement_balance``, which echoes the input."""

    reconciliation_id: uuid.UUID | None  # null on a dry run of a new reconciliation
    account_id: uuid.UUID
    as_of: date
    basis: Literal["posted", "pending_inclusive"]
    statement_balance: Decimal
    target_balance: Decimal
    ledger_balance: Decimal  # the balance on as_of in this basis before the adjustment
    adjustment: Decimal  # target_balance - ledger_balance; 0 = already in balance
    adjustment_tx_id: uuid.UUID | None  # null when nothing was (or would be) posted
    category: str | None  # where the adjustment posts; null when it is 0
    reconciled_through: date | None
    created: bool  # this call recorded a new reconciliation
    dry_run: bool


_RECONCILE_CONFLICT = {
    "description": "The request contradicts a recorded reconciliation. `detail.code` is "
    "`reconciliation_moved` (this exact reconciliation is recorded, but the ledger balance "
    "on that date has changed since — `detail.difference` = ledger minus target), "
    "`reconciliation_conflict` (a different balance or basis is recorded for that date), or "
    "`reconciliation_out_of_order` (the date is before the account's latest reconciliation)."
}


@router.post("/{account_id}/reconcile", responses={409: _RECONCILE_CONFLICT})
def reconcile_account(
    account_id: uuid.UUID, body: ReconcileRequest, token: WriteToken, session: DbSession
) -> ReconcileResult:
    """Reconcile a checking, savings, cash or credit-card account to a statement
    balance (issue #268).

    Purser compares the statement figure with its own balance on ``as_of`` in
    the chosen ``basis`` and posts ONE adjustment for the difference, dated
    ``as_of``, to the system category ``expenses.reconciliation_adjustments``.
    The adjustment lands in the review set so a person sees it. When the ledger
    already matches, nothing is posted and the clean reconcile is still
    recorded. Either way ``reconciled_through`` advances and an
    ``account.reconciled`` event is emitted.

    Safe to retry: the same date, balance and basis returns ``created: false``
    while the ledger still matches, and 409 ``reconciliation_moved`` with the
    difference once a backdated change has moved it. ``GET /accounts/{id}``
    and ``GET /accounts/{id}/reconciliations`` re-check recorded
    reconciliations the same way on every read."""
    try:
        outcome = reconciliation.reconcile(
            session,
            account_id=account_id,
            as_of=body.as_of,
            statement_balance=body.statement_balance,
            basis=body.basis,
            dry_run=body.dry_run,
            token=token,
        )
    except reconciliation.ReconciliationConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=exc.detail) from exc
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    if not outcome.dry_run:
        session.commit()
    return ReconcileResult(
        reconciliation_id=outcome.reconciliation_id,
        account_id=outcome.account_id,
        as_of=outcome.as_of,
        basis=outcome.basis,
        statement_balance=outcome.statement_balance,
        target_balance=outcome.target_balance,
        ledger_balance=outcome.ledger_balance,
        adjustment=outcome.adjustment,
        adjustment_tx_id=outcome.adjustment_tx_id,
        category=outcome.category_path,
        reconciled_through=outcome.reconciled_through,
        created=outcome.created,
        dry_run=outcome.dry_run,
    )


@router.get("/{account_id}/reconciliations")
def list_reconciliations(
    account_id: uuid.UUID,
    token: ReadToken,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> list[ReconciliationOut]:
    """An account's reconciliations, newest first, each re-checked against the
    ledger now: ``in_balance`` is false (and ``difference`` nonzero) when a
    later write changed the balance on that date."""
    try:
        account = ledger.get_account(session, account_id)
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    return [
        ReconciliationOut.from_check(c)
        for c in reconciliation.history(session, account.id, limit=limit)
    ]


@categories_router.get("")
def list_categories(token: ReadToken, session: DbSession) -> list[AccountOut]:
    accounts = session.execute(
        select(Account).where(Account.is_virtual.is_(True)).order_by(Account.path)
    ).scalars()
    redacted = security.withholds_external_ids(token.kind, token.scopes)
    return [AccountOut.from_model(a, redacted=redacted) for a in accounts]


@categories_router.post("", status_code=status.HTTP_201_CREATED)
def create_category(body: AccountCreate, token: WriteToken, session: DbSession) -> AccountOut:
    if body.type not in ledger.CATEGORY_TYPES:
        raise HTTPException(422, "type must be category_expense | category_income")
    try:
        account = ledger.create_account(
            session,
            name=body.name,
            type_=body.type,
            parent_id=body.parent_id,
            path=body.path,
            currency=body.currency,
            description=body.description,
            actor_token_id=token.id,
        )
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    return AccountOut.from_model(account, redacted=False)


@categories_router.patch("/{category_id}")
def update_category(
    category_id: uuid.UUID, body: CategoryUpdate, token: WriteToken, session: DbSession
) -> AccountOut:
    """Rename or re-describe a category (descriptions steer the classifier)."""
    try:
        account = ledger.update_category(
            session,
            category_id=category_id,
            name=body.name,
            description=body.description,
            actor_token_id=token.id,
        )
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    return AccountOut.from_model(account, redacted=False)


@categories_router.delete("/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_category(category_id: uuid.UUID, token: WriteToken, session: DbSession) -> None:
    """Delete an UNUSED category (no children, postings, or schedule
    templates). Used categories are ledger history — rename instead."""
    try:
        ledger.delete_category(session, category_id=category_id, actor_token_id=token.id)
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()


@categories_router.get("/{subtree}/rollup")
def category_rollup(
    subtree: str,
    token: ReadToken,
    session: DbSession,
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
) -> RollupOut:
    """Spending rollup over a category subtree — a single indexed ltree query."""
    try:
        result = ledger.category_rollup(session, subtree=subtree, start=start, end=end)
    except ledger.LedgerError as exc:  # a malformed subtree path (#214)
        raise _handle(exc) from exc
    return RollupOut(
        path=result["path"],
        total=result["total"],
        transaction_count=result["transaction_count"],
        start=start,
        end=end,
    )
