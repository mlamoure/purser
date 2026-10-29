"""Connections API: SimpleFIN claim, sync, discovery/mapping, health."""

import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from purser import networth
from purser.admin.auth import AdminToken, ReadToken, WriteToken
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


class HealthOut(ConnectionOut):
    last_errors: list[str]
    fetches_today: int
    # Historical backfill progress (issue #126): how far back we've fetched and
    # whether the institution's history is exhausted.
    backfill_cursor: date | None
    backfill_complete: bool


class DiscoveredOut(BaseModel):
    external_id: str
    name: str | None
    org: str | None
    currency: str
    balance: str | None
    balance_date: date | None = None
    mapped_account_id: uuid.UUID | None
    mapped_account_path: str | None
    # Live reported-vs-ledger drift (DESIGN §7.3), mapped cash/card accounts
    # only. Tri-state: both None = not applicable (unmapped / valuation- or
    # holdings-driven type); ledger_balance set + drift None = clean (either
    # basis matched); both set = drifting, drift is the signed delta string.
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


class MapRequest(BaseModel):
    external_id: str
    account_id: uuid.UUID | None = None
    create: dict[str, str] | None = None


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
    errors: list[str]
    # Long-idle catch-up (#218): backfill-style windows fetched ahead of the
    # final ≤max-range window, with their summed import counts.
    gap_windows: int = 0
    gap_stats: dict[str, int] | None = None


class BackfillRequest(BaseModel):
    # windows: how many ≤90-day pages to walk this run (bridge budget capped).
    windows: int = Field(4, ge=1, le=6)
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


@router.get("")
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


@router.get("/{connection_id}/discovered")
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
            actor_token_id=token.id,
        )
    except service.FeedError as exc:
        raise _wrap(exc) from exc
    except Exception as exc:  # ledger errors (bad type/path)
        raise HTTPException(422, str(exc)) from exc
    session.commit()
    return {"account_id": str(account.id), "path": account.path}
