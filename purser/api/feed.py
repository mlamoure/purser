"""Connections API: SimpleFIN claim, sync, discovery/mapping, health."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from purser import networth
from purser.admin import security
from purser.admin.auth import AdminToken, ReadToken, WriteToken
from purser.api.disclosure import NOTHING, disclose
from purser.crypto import EncryptionKeyMissing
from purser.db import get_session
from purser.feed import service
from purser.feed.simplefin import SimpleFinError

DbSession = Annotated[Session, Depends(get_session)]

router = APIRouter(prefix="/connections", tags=["connections"])


class ConnectionCreate(BaseModel):
    setup_token: str  # one-time SimpleFIN setup token (base64 claim URL)


class ConnectionOut(BaseModel):
    id: uuid.UUID
    provider: str
    status: str
    created_at: datetime
    last_fetch_at: datetime | None


class IssueOut(BaseModel):
    """One error the server reported with a fetch (SimpleFIN v2 ``errlist``,
    #479). ``code`` is ``gen.*`` (general), ``con.*`` (one bank login: e.g.
    ``con.auth``, log in again) or ``act.*`` (one account: ``act.failed``,
    ``act.missingdata``, try again later). A v1 message has no code and reads
    as a naked ``gen.``."""

    code: str
    msg: str
    conn_id: str | None = None
    account_id: str | None = None


class HealthOut(ConnectionOut):
    # Messages of the latest fetch's issues (the shape before #479).
    last_errors: list[str]
    last_issues: list[IssueOut] = []
    # SimpleFIN protocol version of the latest fetch (None before the first).
    protocol_version: int | None = None
    fetches_today: int
    # Historical backfill progress (issue #126): how far back we've fetched and
    # whether the institution's history is exhausted.
    backfill_cursor: date | None
    backfill_complete: bool


class ProposalOut(BaseModel):
    """What Purser proposes for a delivered bank account nothing takes yet
    (decision 126), from what the feed states about it. The feed carries no
    account type, so ``type_basis`` says what the type rests on: `name` (the
    bank's own words), `holdings` (the name says nothing and the feed lists
    holdings), `balance` (a debt, with or without credit left) or `default`
    (nothing says; offered as checking). ``confident`` is false when the type
    deserves a second look, and ``note`` says why when the name names a cash
    product but the feed lists holdings for it too."""

    action: Literal["map", "ignore", "blocked"]
    name: str
    type: str
    type_basis: str
    confident: bool
    path: str
    starting_balance: bool
    # A ledger account path, or another delivered account's ref: the same
    # account arriving through a second login at the same bank.
    duplicate_of: str | None = None
    note: str | None = None


class DiscoveredOut(BaseModel):
    external_id: str
    name: str | None
    # The bank login's name (v2 connection name; v1 org name).
    org: str | None
    # The bank login's id (SimpleFIN v2 conn_id; None for a v1 fetch).
    conn_id: str | None = None
    # True when another account in the same fetch has this external id (two
    # logins): it is neither imported nor drift-checked until the ids differ.
    ambiguous: bool = False
    # An admin chose not to track this unmapped account (#560).
    ignored: bool = False
    # Purser's proposal for an unmapped account that is not ignored; the map
    # form starts from it. Null once mapped, ignored or ambiguous.
    proposal: ProposalOut | None = None
    currency: str
    balance: str | None
    # The bank's available balance (#483), on cash and card accounts only and
    # only when it differs from balance: spendable cash, or a card's
    # remaining credit. Null otherwise.
    available_balance: str | None = None
    balance_date: date | None = None
    mapped_account_id: uuid.UUID | None
    mapped_account_path: str | None
    # The mapped Purser account's name, what the screens show (#552).
    mapped_account_name: str | None = None
    # Live reported-vs-ledger drift (DESIGN §7.3), mapped cash/card accounts
    # only, plus a hand-tracked brokerage account whose feed_balance_is_cash
    # is on (decision 103). Tri-state: both None = not applicable (unmapped /
    # valuation- or holdings-driven type); ledger_balance set + drift None =
    # clean (either basis matched); both set = drifting, drift is the signed
    # delta string.
    ledger_balance: str | None = None
    drift: str | None = None
    # Set whenever ledger_balance is: the ledger basis this account's bank
    # balance is compared on, chosen from its sync history (issue #283,
    # decision 64). posted_only = the bank leaves pending charges out of its
    # balance, so drift is measured against posted transactions only;
    # ledger_balance itself stays pending-inclusive.
    drift_basis: Literal["pending_inclusive", "posted_only"] | None = None
    # Issue #307 (decision 83): true when drift_basis is the account's admin-set
    # pin (accounts.drift_basis_override) rather than the learned basis.
    drift_basis_pinned: bool = False
    # Set whenever drift_basis is: the basis the sync history selects, pinned or
    # not (equal to drift_basis when unpinned) — what clearing the pin returns to.
    drift_basis_learned: Literal["pending_inclusive", "posted_only"] | None = None


class InventoryIssueOut(BaseModel):
    code: str
    message: str


class InventoryAccountOut(BaseModel):
    # The bank's name for the account; for tokens that withhold external
    # identifiers (#437) the masked account-number suffix is removed.
    name: str | None
    # SimpleFIN account id; null for tokens that withhold external ids.
    external_id: str | None = None
    # The ledger account it feeds, or null when it is not connected.
    account: str | None
    connected: bool
    # Mapped to a ledger account that is closed (so not syncing).
    closed: bool = False
    balance_date: date | None
    days_since_balance: int | None
    # balance_date older than feed_stale_days.
    stale: bool
    # Rows the latest sync's fetch carried for the account.
    holdings: int
    transactions: int
    issue: InventoryIssueOut | None = None
    ambiguous: bool = False
    # Not connected by choice (#560): an admin ignored it.
    ignored: bool = False


class InventoryLoginOut(BaseModel):
    institution: str | None
    # SimpleFIN v2 conn_id; null for v1 fetches and for tokens that withhold
    # external ids.
    conn_id: str | None = None
    url: str | None = None
    issue: InventoryIssueOut | None = None
    accounts: list[InventoryAccountOut]


class InventoryConnectionOut(BaseModel):
    # Null for tokens that withhold external ids.
    connection_id: uuid.UUID | None = None
    status: str
    last_sync_at: datetime | None
    protocol_version: int | None = None
    stale_days: int
    logins: list[InventoryLoginOut]


class MapRequest(BaseModel):
    external_id: str
    account_id: uuid.UUID | None = None
    create: dict[str, str] | None = None
    # Start the account from the bank's reported balance once its rows are
    # in (decision 123). The mapping form sends true unless it is unticked.
    starting_balance: bool = False


class IgnoreRequest(BaseModel):
    external_id: str
    # False tracks the account again (it shows as unmapped).
    ignored: bool = True


class IgnoreOut(BaseModel):
    external_id: str
    ignored: bool


class GapRepairOut(BaseModel):
    """The re-fetch a sync ran (or owes) for accounts back from an outage."""

    accounts: list[str]  # ledger account paths
    start: date
    end: date
    # Set when the outage began before the Bridge's reach (90 days): rows
    # before this date need a manual import.
    unreachable_before: date | None = None
    # Set when the repair could not run on this sync (rate_budget,
    # fetch_error); it stays owed and the next sync tries again.
    deferred: Literal["rate_budget", "fetch_error"] | None = None
    # The last page's archived fetch, and how many ≤45-day pages it took.
    raw_fetch_id: uuid.UUID | None = None
    pages: int = 0
    imported: int = 0
    skipped: int = 0
    skipped_before_feed_start: int = 0
    skipped_masked_duplicate: int = 0
    skipped_redescribed: int = 0
    # Rows that took over a hand-tracked trade's cash (#408, decision 103).
    linked_trade_cash: int = 0


class TrueUpOut(BaseModel):
    """A balance true-up this sync posted (#591, decision 119)."""

    account_id: uuid.UUID
    amount: Decimal = Field(
        description="Signed, in ledger sign: the cash the account's postings were short (+) or "
        "over (-) against the broker's balance less its holdings."
    )
    transaction_id: uuid.UUID


class SyncOut(BaseModel):
    raw_fetch_id: uuid.UUID
    imported: int
    skipped: int
    # Zero-amount feed rows (investment-account markers) filtered before the
    # ledger's nonzero invariant — the feed-side twin of the CSV planner's
    # skipped_zero_amount (#144/#147). Issue #151.
    skipped_zero_amount: int = 0
    # Rows dated before the account's feed_start boundary — manual-import
    # territory the feed must not re-import (issue #154, decision 40).
    skipped_before_feed_start: int = 0
    # Rows re-delivered with masked reference digits — same identity under
    # masking, skipped deterministically (issue #171, decision 44).
    skipped_masked_duplicate: int = 0
    # Posted rows a widened fetch re-delivered under a new description, beside
    # the original the ledger already holds (issue #331, decision 74).
    skipped_redescribed: int = 0
    unmapped_accounts: int
    voided_pendings: int
    holdings_snapshots: int = 0
    # Feed holdings rows of accounts tracked by hand (holdings_source='manual',
    # #390, decision 88): archived with the fetch, never stored as snapshots.
    holdings_skipped_manual: int = 0
    # The first fetch after an account went back to the feed ends its manual
    # period (decision 88): the hand-tracked rows it deleted from its date on
    # and the manual zero rows (tombstones) it wrote for the positions the
    # feed does not report. Audited as account.manual_period_end.
    manual_period_rows_deleted: int = 0
    manual_period_tombstones: int = 0
    # Accounts skipped because another account in the fetch has the same id
    # (two bank logins, #479): never imported onto one ledger account.
    skipped_ambiguous_accounts: int = 0
    # The broker's own copy of a trade's cash Purser had already posted on a
    # hand-tracked account (#408, decision 103): imported as the trade's cash,
    # Purser's row superseded by it (counted in imported too) — or, while
    # pending, not stored, so the posted row takes over when the bank books it.
    linked_trade_cash: int = 0
    skipped_trade_cash_pending: int = 0
    errors: list[str]
    issues: list[IssueOut] = []
    # Long-idle catch-up (#218): backfill-style windows fetched ahead of the
    # final ≤max-range window, with their summed import counts.
    gap_windows: int = 0
    # The gap windows' share of the counts above (which include them, #490).
    gap_stats: dict[str, int] | None = None
    # Outage repair (#481): set when accounts came back from an outage whose
    # rows this sync's window did not reach; see GapRepairOut.
    gap_repair: GapRepairOut | None = None
    # Feed-holdings brokerage accounts whose cash this sync put in step with
    # the broker's balance (#591, decision 119). Skips are on the audit row.
    balance_true_ups: list[TrueUpOut] = []


class RefetchRequest(BaseModel):
    start: date
    # Inclusive; default today. At most 90 days from start.
    end: date | None = None
    # Ledger account paths or ids mapped to this connection; default all.
    accounts: list[str] | None = None


class RefetchOut(BaseModel):
    # The last page's archived fetch; pages = ≤45-day requests made (#490).
    raw_fetch_id: uuid.UUID
    pages: int = 1
    start: date
    end: date
    accounts: list[str]
    imported: int
    skipped: int
    skipped_before_feed_start: int = 0
    skipped_masked_duplicate: int = 0
    skipped_redescribed: int = 0
    # Rows that took over a hand-tracked trade's cash (#408, decision 103).
    linked_trade_cash: int = 0


class BackfillRequest(BaseModel):
    # windows: how many ≤45-day pages to walk this run (bridge budget capped; #490).
    windows: int = Field(8, ge=1, le=12)
    # optional floor: stop once a window reaches this date (don't page past it).
    target_date: date | None = None
    # Optional focus (issue #154): SimpleFIN external ids, sent as repeatable
    # `account` query params. A bridge that ignores the param returns every
    # account — feed_start still guards, so the mechanisms compose.
    accounts: list[str] | None = None


class BackfillOut(BaseModel):
    windows_fetched: int
    coverage_start: date | None
    coverage_end: date | None
    imported: int
    skipped: int
    # feed_start guard hits across the re-paged windows — the latch-reset
    # re-page is exactly the flow this stat exists to make visible (#154).
    skipped_before_feed_start: int = 0
    # Masked-reference dedup hits (issue #171, decision 44).
    skipped_masked_duplicate: int = 0
    # Re-described copies of rows the ledger already holds (issue #331,
    # decision 74).
    skipped_redescribed: int = 0
    # Rows that took over a hand-tracked trade's cash (#408, decision 103).
    linked_trade_cash: int = 0
    empty_windows: int
    backfill_complete: bool
    stopped_because: str
    # Manual-QIF rows overlapping the backfilled range, by account path — a
    # duplicate-risk warning surface, never a skip.
    qif_overlap: dict[str, int]


def _wrap(exc: Exception) -> HTTPException:
    if isinstance(exc, EncryptionKeyMissing):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))


@router.post("", status_code=status.HTTP_201_CREATED)
def create_connection(
    body: ConnectionCreate, token: AdminToken, session: DbSession
) -> ConnectionOut:
    """Exchange a one-time SimpleFIN setup token; the access URL is stored
    encrypted and never returned."""
    try:
        connection = service.create_connection(
            session, setup_token=body.setup_token, actor_token_id=token.id
        )
    except (service.FeedError, SimpleFinError, EncryptionKeyMissing) as exc:
        raise _wrap(exc) from exc
    session.commit()
    return ConnectionOut(
        id=connection.id,
        provider=connection.provider,
        status=connection.status,
        created_at=connection.created_at,
        last_fetch_at=connection.last_fetch_at,
    )


@router.get("/inventory", dependencies=[disclose(NOTHING)])
def feed_inventory(token: ReadToken, session: DbSession) -> list[InventoryConnectionOut]:
    """Every account SimpleFIN delivers, connected or not, grouped by bank
    login: the login's issue, and per account whether it feeds a ledger
    account, how old its bank balance is, whether holdings and transactions
    arrive, and any account-level issue. Read scope; account numbers and
    SimpleFIN ids are withheld from non-admin API tokens (#482)."""
    from purser.admin import security
    from purser.feed import inventory

    redact = security.withholds_external_ids(token.kind, token.scopes)
    return [
        InventoryConnectionOut(**row) for row in inventory.feed_inventory(session, redact=redact)
    ]


@router.get("", dependencies=[disclose(NOTHING)])
def list_connections(token: ReadToken, session: DbSession) -> list[HealthOut]:
    return [HealthOut(**h) for h in service.connection_health(session)]


@router.post("/{connection_id}/sync")
def trigger_sync(
    connection_id: uuid.UUID,
    token: WriteToken,
    session: DbSession,
    background_tasks: BackgroundTasks,
) -> SyncOut:
    """Fetch now (archive-first, then import). Agents may trigger syncs."""
    try:
        result = service.sync_connection(
            session, connection_id=connection_id, actor_token_id=token.id
        )
    except (service.FeedError, EncryptionKeyMissing) as exc:
        session.commit()  # keep the sync.failed event + status change
        raise _wrap(exc) from exc
    session.commit()
    background_tasks.add_task(networth.consume_watermark)
    return SyncOut(**result)


@router.post("/{connection_id}/backfill")
def trigger_backfill(
    connection_id: uuid.UUID,
    body: BackfillRequest,
    token: WriteToken,
    session: DbSession,
    background_tasks: BackgroundTasks,
) -> BackfillOut:
    """Fetch older history now (archive-first, then import). Agents may trigger
    backfills — same write scope as sync."""
    try:
        result = service.backfill_connection(
            session,
            connection_id=connection_id,
            actor_token_id=token.id,
            windows=body.windows,
            target_date=body.target_date,
            accounts=body.accounts,
        )
    except (service.FeedError, EncryptionKeyMissing) as exc:
        raise _wrap(exc) from exc
    session.commit()
    background_tasks.add_task(networth.consume_watermark)
    return BackfillOut(**result)


@router.post("/{connection_id}/refetch")
def trigger_refetch(
    connection_id: uuid.UUID,
    body: RefetchRequest,
    token: WriteToken,
    session: DbSession,
    background_tasks: BackgroundTasks,
) -> RefetchOut:
    """Re-fetch a date range (at most 90 days) for some or all mapped
    accounts and import what is missing — for a gap a bank outage left that
    the automatic repair did not cover. Archive-first, idempotent; shares
    the daily fetch budget. Agents may run it (write scope)."""
    try:
        result = service.refetch_connection(
            session,
            connection_id=connection_id,
            start=body.start,
            end=body.end,
            accounts=body.accounts,
            actor_token_id=token.id,
        )
    except (service.FeedError, EncryptionKeyMissing) as exc:
        raise _wrap(exc) from exc
    session.commit()
    background_tasks.add_task(networth.consume_watermark)
    return RefetchOut(**result)


@router.get("/{connection_id}/discovered", dependencies=[disclose(NOTHING)])
def discovered_accounts(
    connection_id: uuid.UUID, token: AdminToken, session: DbSession
) -> list[DiscoveredOut]:
    try:
        rows: list[dict[str, Any]] = service.discover_accounts(session, connection_id)
    except service.FeedError as exc:
        raise _wrap(exc) from exc
    return [DiscoveredOut(**r) for r in rows]


@router.post("/{connection_id}/map")
def map_account(
    connection_id: uuid.UUID, body: MapRequest, token: AdminToken, session: DbSession
) -> dict[str, str]:
    try:
        account = service.map_account(
            session,
            connection_id=connection_id,
            external_id=body.external_id,
            account_id=body.account_id,
            create=body.create,
            starting_balance=body.starting_balance,
            actor_token_id=token.id,
        )
    except service.FeedError as exc:
        raise _wrap(exc) from exc
    except Exception as exc:  # ledger errors (bad type/path)
        raise HTTPException(422, str(exc)) from exc
    from purser import starting_balance

    started = starting_balance.get(session, account.id)
    session.commit()
    return {
        "account_id": str(account.id),
        "path": account.path,
        # Present when the account now starts from the bank's balance.
        **({"starting_balance": str(started.amount)} if started is not None else {}),
    }


class MapAccountItem(BaseModel):
    """One line of a mapping batch. Everything but ``ref`` is optional: what
    is left out takes the plan's proposal."""

    ref: str = Field(description="The delivered account's `ref` from the plan.")
    action: Literal["map", "ignore"] | None = Field(
        default=None, description="Default: the plan's own action (map, or ignore a duplicate)."
    )
    name: str | None = None
    type: str | None = Field(
        default=None,
        description="checking, savings, credit_card, cash, brokerage, loan or property. "
        "It cannot be changed after mapping.",
    )
    path: str | None = None
    account: str | None = Field(
        default=None,
        description="Map onto this existing open account (path or id) instead of a new one.",
    )
    starting_balance: bool | None = Field(
        default=None,
        description="Start the account from the bank's balance (decision 123). Default: yes "
        "for a new checking, savings, cash or credit-card account, no for an existing one.",
    )
    replace_feed: bool | None = Field(
        default=None,
        description="With `account`: map onto it although it already takes another bank "
        "account's feed, which then waits to be mapped again (decision 134). Admin scope only; "
        "without it a line onto an account that has a feed is refused, naming that feed.",
    )


class MapAccountsRequest(BaseModel):
    accounts: list[MapAccountItem] | None = Field(
        default=None,
        description="The lines to apply. Omit to get the plan and write nothing.",
    )
    dry_run: bool = Field(
        default=False, description="Check the lines and answer what they would do, unwritten."
    )


class MapAccountsOut(BaseModel):
    # plan: Purser's proposal for every bank account waiting to be mapped
    # (no `accounts` sent). results: what the sent lines did, or would do.
    plan: list[dict[str, Any]] | None = None
    results: list[dict[str, Any]] | None = None
    dry_run: bool = False


@router.post("/map-accounts")
def map_accounts(body: MapAccountsRequest, token: WriteToken, session: DbSession) -> MapAccountsOut:
    """Map the bank accounts SimpleFIN delivers onto ledger accounts, in one
    batch (decision 126; write scope).

    Without ``accounts`` it answers the **plan** and writes nothing: for each
    delivered account no ledger account takes, a proposed name, type and
    ledger path, derived from what the feed states (its name, its bank, its
    balance and whether it lists holdings; the feed carries no type). A
    duplicate, the same account through a second login at the same bank, is
    proposed as ``ignore``.

    With ``accounts`` it applies those lines: each names a delivered account
    by its ``ref`` and takes the proposal unless it overrides a field. The
    whole batch is checked first and lands whole or not at all; ``dry_run``
    answers the same without writing. Mapping imports what was already
    fetched and, by default, starts a cash or card account from the bank's
    balance. **An account's type cannot be changed afterwards**, so confirm
    the plan with the person before applying it.

    A line onto an existing account that already takes a feed is refused,
    naming that feed, unless it says ``replace_feed`` and the token is an
    admin's (decision 134); such a line names the feed it replaces in
    ``replaces``, the dry run included.

    The bank's account ids and balances are never part of the answer."""
    from purser.feed import proposals

    try:
        if body.accounts is None:
            return MapAccountsOut(plan=[p.as_dict() for p in proposals.plan(session)])
        results = proposals.apply(
            session,
            items=[item.model_dump(exclude_none=True) for item in body.accounts],
            actor_token_id=token.id,
            dry_run=body.dry_run,
            may_replace=security.is_admin(token.scopes),
        )
    except service.FeedError as exc:
        raise _wrap(exc) from exc
    except Exception as exc:  # ledger errors (bad type/path), mapping errors
        raise HTTPException(422, str(exc)) from exc
    if not body.dry_run:
        session.commit()
    return MapAccountsOut(results=results, dry_run=body.dry_run)


@router.post("/{connection_id}/ignore")
def ignore_account(
    connection_id: uuid.UUID, body: IgnoreRequest, token: AdminToken, session: DbSession
) -> IgnoreOut:
    """Ignore an unmapped feed account (the same account delivered through a
    second bank login, or one that does not belong in the ledger), or track
    it again with ``ignored: false``. An ignored account is not reported as
    waiting to be mapped; nothing is imported or removed."""
    try:
        now = service.set_ignored(
            session,
            connection_id=connection_id,
            external_id=body.external_id,
            ignored=body.ignored,
            actor_token_id=token.id,
        )
    except service.FeedError as exc:
        raise _wrap(exc) from exc
    session.commit()
    return IgnoreOut(external_id=body.external_id, ignored=now)
