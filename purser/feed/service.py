"""Feed service: connections, scheduled sync, discovery/mapping.

Raw-first: every payload lands verbatim in raw_fetches (append-only) before
the importer touches it. The ledger is a projection of this archive and is
re-derivable from it.
"""

import hashlib
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from purser import audit, crypto, events
from purser import ledger as ledger_service
from purser.feed import drift, importer, reconcile, simplefin
from purser.logging import get_logger
from purser.models import Account, Connection, Posting, RawFetch, Transaction

log = get_logger(__name__)

FIRST_SYNC_DAYS = 90  # Bridge range cap
# Re-fetch overlap so pending->posted transitions land. A sync reaches further
# back while an older pending is still open (#282, _open_pendings_start); the
# importer checks what only that reach fetched for re-described rows (#331).
RESYNC_OVERLAP_DAYS = importer.RESYNC_OVERLAP_DAYS
# Cap on backfill-style gap windows one sync will issue for a long-idle
# connection (#218) — bounds a pathological multi-year gap to a handful of
# fetches; anything older is logged as uncovered rather than silently lost.
SYNC_GAP_WINDOWS_MAX = 4

# Historical backfill (issue #126): page ≤90-day windows backward from the
# first sync's start, one RawFetch per window (shares the daily fetch budget).
BACKFILL_RESERVE_FETCHES = 2  # headroom left for the scheduled sync + one manual
BACKFILL_MAX_WINDOWS_PER_RUN = 6  # cap the work (and the budget bite) per call
BACKFILL_EMPTY_STREAK_STOP = 2  # this many empty windows in a row => history exhausted
BACKFILL_WINDOW_OVERLAP_DAYS = 1  # 1-day overlap between windows; idempotent import eats it


class FeedError(Exception):
    pass


def create_connection(
    session: Session,
    *,
    setup_token: str,
    actor_token_id: uuid.UUID | None,
    client: httpx.Client | None = None,
) -> Connection:
    """Claim the one-time setup token and store the access URL encrypted."""
    crypto.ensure_key()  # before the claim: claiming consumes the token
    access_url = simplefin.claim_access_url(setup_token, client=client)
    connection = Connection(access_url_enc=crypto.encrypt(access_url))
    session.add(connection)
    session.flush()
    audit.record(
        session,
        actor_token_id=actor_token_id,
        action="connection.create",
        entity_type="connection",
        entity_id=connection.id,
        after={"provider": "simplefin"},  # never the URL or token
    )
    return connection


def get_connection(session: Session, connection_id: uuid.UUID) -> Connection:
    connection = session.get(Connection, connection_id)
    if connection is None:
        raise FeedError(f"connection {connection_id} not found")
    return connection


def _fetches_today(session: Session, connection_id: uuid.UUID) -> int:
    since = datetime.now(UTC) - timedelta(days=1)
    return session.execute(
        select(func.count())
        .select_from(RawFetch)
        .where(RawFetch.connection_id == connection_id, RawFetch.fetched_at >= since)
    ).scalar_one()


def _archive_payload(
    session: Session, connection_id: uuid.UUID, payload: dict[str, Any]
) -> RawFetch:
    """Archive a fetched payload verbatim in raw_fetches (append-only) before
    anything interprets it. content_hash is SHA-256 over the canonical
    (sort_keys) plaintext; payload_enc is Fernet over that same string, so
    idempotency is independent of the key. Shared by sync and backfill."""
    canonical = json.dumps(payload, sort_keys=True)
    raw = RawFetch(
        connection_id=connection_id,
        content_hash=hashlib.sha256(canonical.encode()).hexdigest(),
        payload_enc=crypto.encrypt(canonical),
    )
    session.add(raw)
    session.flush()
    return raw


def _open_pendings_start(session: Session, connection_id: uuid.UUID, today: date) -> date | None:
    """Where a sync's fetch must start so that every open pending on the
    connection's cash and card accounts is re-checked (#282, decision 70), not
    only those inside the resync overlap: the oldest one's date, less the
    successor tolerance, so a posted successor dated a few days before its
    pending is fetched too.

    The reconciler voids import-origin pendings on all of the connection's
    accounts, but only pendings on accounts whose balance IS the postings sum
    widen the fetch: checking, savings, cash and card, the set the drift check
    compares (#331, decision 74). A brokerage, loan or property account is
    valued by holdings or valuations, so its pendings move no balance, and some
    feeds list every fund purchase as pending forever. One such account would
    keep every sync at the maximal fetch. Its pendings are still re-checked
    whenever the window covers them. Bounded by what ONE live fetch can serve:
    a pending older than ``BRIDGE_MAX_RANGE_DAYS`` is beyond the feed's reach
    and widens nothing, otherwise every sync would pay for a maximal fetch
    that could never settle it. ``None`` when no pending is in reach."""
    floor = today - timedelta(days=simplefin.BRIDGE_MAX_RANGE_DAYS)
    oldest = session.execute(
        select(func.min(Transaction.date))
        .join(Posting, Posting.transaction_id == Transaction.id)
        .join(Account, Account.id == Posting.account_id)
        .where(
            Account.connection_id == connection_id,
            Account.type.in_(drift.ELIGIBLE_TYPES),
            Transaction.status == "pending",
            Transaction.origin == "import",
            Transaction.date >= floor,
        )
    ).scalar_one()
    if oldest is None:
        return None
    return max(floor, oldest - timedelta(days=reconcile.PENDING_MATCH_WINDOW_DAYS))


def sync_connection(
    session: Session,
    *,
    connection_id: uuid.UUID,
    actor_token_id: uuid.UUID | None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Fetch -> archive verbatim -> import. Respects Bridge limits.

    The window starts at the resync overlap behind ``last_fetch_at``, or
    further back while an older pending on a cash or card account is still
    open, so every such pending is re-checked against the bank on every sync
    (#282). Rows dated before the overlap start were fetched only for that
    reach: every import this sync makes passes it as ``tail_before``, and the
    importer skips a re-described copy of a row the ledger holds there (#331).

    On failure the error status and sync.failed event are flushed but NOT
    committed — callers own the commit (and must commit to persist them).
    """
    connection = get_connection(session, connection_id)
    if _fetches_today(session, connection.id) >= simplefin.BRIDGE_MAX_FETCHES_PER_DAY:
        raise FeedError("Bridge rate limit reached (24 fetches/day); try later")

    today = datetime.now(UTC).date()
    if connection.last_fetch_at is not None:
        start = (connection.last_fetch_at - timedelta(days=RESYNC_OVERLAP_DAYS)).date()
    else:
        start = today - timedelta(days=FIRST_SYNC_DAYS)
    overlap_start = start  # where this sync would start with no pending to re-check
    pending_start = _open_pendings_start(session, connection.id, today)
    if pending_start is not None and pending_start < start:
        start = pending_start

    if connection.access_url_enc is None:
        raise FeedError("connection has no access URL to sync")
    access_url = crypto.decrypt(connection.access_url_enc)

    def _fetch(window_start: date, window_end: date | None) -> dict[str, Any]:
        try:
            return simplefin.fetch_accounts(
                access_url, start=window_start, end=window_end, client=client
            )
        except simplefin.SimpleFinError as exc:
            connection.status = "error"
            events.emit(
                session,
                type_="sync.failed",
                entity_type="connection",
                entity_id=connection.id,
                payload={"error": str(exc)},
            )
            session.flush()
            raise FeedError(f"sync failed: {exc}") from exc

    # A long-idle connection's range can exceed what a bridge serves in one
    # fetch (BRIDGE_MAX_RANGE_DAYS). Degrade like backfill (#218): fetch the
    # older portion as capped gap windows imported with window_start=None —
    # a partial slice must never reconcile live pendings — then the final
    # ≤max-range window normally so pending transitions land as usual.
    span = timedelta(days=simplefin.BRIDGE_MAX_RANGE_DAYS)
    gap_windows: list[tuple[date, date]] = []
    while today - start > span:
        window_end = start + span
        gap_windows.append((start, window_end))
        start = window_end - timedelta(days=BACKFILL_WINDOW_OVERLAP_DAYS)
    if len(gap_windows) > SYNC_GAP_WINDOWS_MAX:
        uncovered = gap_windows[:-SYNC_GAP_WINDOWS_MAX]
        gap_windows = gap_windows[-SYNC_GAP_WINDOWS_MAX:]
        log.warning(
            "sync.gap_truncated",
            connection_id=str(connection.id),
            uncovered_start=uncovered[0][0].isoformat(),
            uncovered_end=uncovered[-1][1].isoformat(),
        )
    gap_stats: dict[str, int] = {}
    for gap_start, gap_end in gap_windows:
        if _fetches_today(session, connection.id) >= simplefin.BRIDGE_MAX_FETCHES_PER_DAY:
            raise FeedError("Bridge rate limit reached (24 fetches/day); try later")
        gap_payload = _fetch(gap_start, gap_end)
        gap_raw = _archive_payload(session, connection.id, gap_payload)
        # A gap window is overlap territory, never widened: tail_before keeps
        # the importer's re-description check off it (#331).
        for key, value in importer.import_fetch(
            session, gap_raw, window_start=None, tail_before=overlap_start
        ).items():
            if isinstance(value, int):
                gap_stats[key] = gap_stats.get(key, 0) + value

    payload = _fetch(start, None)

    raw = _archive_payload(session, connection.id, payload)

    stats = importer.import_fetch(session, raw, window_start=start, tail_before=overlap_start)
    redescribed = stats.pop("redescribed")
    # Post-import, pre-audit: the check compares against the just-imported
    # ledger, and the audit lookup inside it must still see the PREVIOUS
    # sync's row (its observation block is the consecutive-sync gate memory).
    drift_obs = drift.check_after_sync(
        session, connection=connection, payload=payload, raw_fetch=raw
    )
    connection.last_fetch_at = datetime.now(UTC)
    connection.status = "active" if not payload.get("errors") else "warning"
    session.flush()
    audit.record(
        session,
        actor_token_id=actor_token_id,
        action="connection.sync",
        entity_type="connection",
        entity_id=connection.id,
        after={
            "raw_fetch_id": str(raw.id),
            # The window the live fetch covered: wider than the resync
            # overlap while an older pending is still open (#282).
            "window_start": start.isoformat(),
            # Where the overlap starts: rows before it were reachable only
            # through the widening, and re-described copies there were
            # skipped (#331), one entry each in "redescribed".
            "overlap_start": overlap_start.isoformat(),
            **stats,
            **({"redescribed": redescribed} if redescribed else {}),
            **({"gap_windows": len(gap_windows), "gap_stats": gap_stats} if gap_windows else {}),
            "errors": payload.get("errors", []),
            "balance_drift": drift_obs,
        },
    )
    result: dict[str, Any] = {"raw_fetch_id": raw.id, **stats, "errors": payload.get("errors", [])}
    if gap_windows:
        result["gap_windows"] = len(gap_windows)
        result["gap_stats"] = gap_stats
    return result


def _first_fetch_window_start(session: Session, connection_id: uuid.UUID) -> date | None:
    """The oldest date the first sync already covered: min(fetched_at) minus the
    first-sync window (FIRST_SYNC_DAYS). ``None`` when no fetch exists yet — the
    caller can't backfill a connection that has never synced. This is the seed
    for ``backfill_cursor`` (the archive doesn't record request params)."""
    earliest = session.execute(
        select(func.min(RawFetch.fetched_at)).where(RawFetch.connection_id == connection_id)
    ).scalar_one()
    if earliest is None:
        return None
    return earliest.date() - timedelta(days=FIRST_SYNC_DAYS)


def _qif_overlap_counts(
    session: Session, connection_id: uuid.UUID, start: date, end: date
) -> dict[str, int]:
    """Manual-QIF ledger rows (``origin='import'`` AND ``source_fetch_id`` NULL)
    on this connection's mapped accounts that fall inside the backfilled date
    range. SimpleFIN rows always carry a ``source_fetch_id``, so a NULL one is a
    hand-imported QIF line — overlapping one is a duplicate-risk *warning*, never
    a skip (content-hash still dedups identical rows; near-duplicates won't)."""
    rows = session.execute(
        select(Account.path, func.count(func.distinct(Transaction.id)))
        .select_from(Account)
        .join(Posting, Posting.account_id == Account.id)
        .join(Transaction, Transaction.id == Posting.transaction_id)
        .where(
            Account.connection_id == connection_id,
            Transaction.origin == "import",
            Transaction.source_fetch_id.is_(None),
            Transaction.date >= start,
            Transaction.date <= end,
        )
        .group_by(Account.path)
    ).all()
    return {str(path): count for path, count in rows if count}


def backfill_connection(
    session: Session,
    *,
    connection_id: uuid.UUID,
    actor_token_id: uuid.UUID | None,
    windows: int = 4,
    target_date: date | None = None,
    accounts: list[str] | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Page historical windows BACKWARD from the first sync's start (issue #126).

    Mirrors ``sync_connection``'s archive-then-import shape but walks toward the
    past: each window is a ≤90-day range ending one day into already-covered
    territory (1-day overlap; idempotent import eats it), archived verbatim as a
    RawFetch, then imported with ``window_start=None``.

    ``window_start=None`` is MANDATORY: it makes ``import_fetch`` skip
    vanished-pending reconciliation. A backfill payload is an old, pending-free
    window; reconciling live pendings against it would wrongly void every one.

    Backfill NEVER touches ``last_fetch_at`` (that drives the incremental sync
    window) or ``connection.status``, and emits no sync events. It shares the
    daily fetch budget with sync via ``_fetches_today`` and reserves
    ``BACKFILL_RESERVE_FETCHES`` for the scheduled sync + one manual sync.

    ``accounts`` focuses the run on specific external ids (issue #154) via the
    bridge's repeatable ``account`` param. Advisory — a bridge that ignores it
    returns everyone and per-account ``feed_start`` still guards the import.
    A filtered run's empty streak proves only the FILTERED accounts exhausted,
    so it stops (``empty_history``) without latching ``backfill_complete``.

    Returns a result dict; ``stopped_because`` is one of ``complete`` (already
    exhausted — no fetch), ``empty_history`` (empty-window streak), ``target_
    reached``, ``rate_budget`` (day's reserved budget ran out mid-run),
    ``windows_exhausted`` (did the requested count), or ``fetch_error`` (a
    SimpleFinError — partial result, cursor kept, status untouched).
    """
    connection = get_connection(session, connection_id)

    if connection.backfill_complete:
        return {
            "windows_fetched": 0,
            "coverage_start": None,
            "coverage_end": None,
            "imported": 0,
            "skipped": 0,
            "skipped_before_feed_start": 0,
            "skipped_masked_duplicate": 0,
            "skipped_redescribed": 0,
            "empty_windows": 0,
            "backfill_complete": True,
            "stopped_because": "complete",
            "qif_overlap": {},
        }

    if connection.access_url_enc is None:
        raise FeedError("connection has no access URL to backfill")

    cursor = connection.backfill_cursor
    if cursor is None:
        cursor = _first_fetch_window_start(session, connection.id)
        if cursor is None:
            raise FeedError("no raw fetches yet — run a sync first")

    # Budget: bounded by the request, the per-run cap, and the day's remaining
    # fetches minus the reserve. Backfill's archived RawFetches count toward
    # _fetches_today automatically, so the same guard covers sync and backfill.
    requested = min(windows, BACKFILL_MAX_WINDOWS_PER_RUN)
    budget = (
        simplefin.BRIDGE_MAX_FETCHES_PER_DAY
        - BACKFILL_RESERVE_FETCHES
        - _fetches_today(session, connection.id)
    )
    if budget <= 0:
        raise FeedError(
            f"Bridge rate limit reached ({simplefin.BRIDGE_MAX_FETCHES_PER_DAY} fetches/day); "
            "try later"
        )
    n = min(requested, budget)

    access_url = crypto.decrypt(connection.access_url_enc)
    floor = target_date or date.min

    windows_fetched = 0
    imported = 0
    skipped = 0
    skipped_before_feed_start = 0
    skipped_masked_duplicate = 0
    skipped_redescribed = 0
    empty_windows = 0
    empty_streak = 0
    coverage_start: date | None = None
    coverage_end: date | None = None
    # Default reason if the loop finishes without an early stop: budget-limited
    # runs report rate_budget, otherwise we simply did the requested count.
    stopped_because = "rate_budget" if n < requested else "windows_exhausted"

    for _ in range(n):
        end = cursor + timedelta(days=BACKFILL_WINDOW_OVERLAP_DAYS)
        start = max(end - timedelta(days=FIRST_SYNC_DAYS), floor)
        try:
            payload = simplefin.fetch_accounts(
                access_url,
                start=start,
                end=end,
                include_pending=False,
                accounts=accounts,
                client=client,
            )
        except simplefin.SimpleFinError:
            # Partial result: keep the cursor where it is, do NOT raise, touch
            # status, emit an event, or move last_fetch_at.
            stopped_because = "fetch_error"
            break

        raw = _archive_payload(session, connection.id, payload)
        stats = importer.import_fetch(session, raw, window_start=None)
        imported += stats["imported"]
        skipped += stats["skipped"]
        skipped_before_feed_start += stats["skipped_before_feed_start"]
        skipped_masked_duplicate += stats["skipped_masked_duplicate"]
        skipped_redescribed += stats["skipped_redescribed"]

        windows_fetched += 1
        if coverage_end is None:
            coverage_end = end
        coverage_start = start
        cursor = start
        connection.backfill_cursor = start
        session.flush()  # persist progress per window; the API layer commits

        payload_txns = sum(
            len(sf_account.get("transactions", []) or [])
            for sf_account in payload.get("accounts", [])
        )
        if payload_txns == 0:
            empty_windows += 1
            empty_streak += 1
        else:
            empty_streak = 0

        if empty_streak >= BACKFILL_EMPTY_STREAK_STOP:
            # A filtered run's emptiness proves only the filtered accounts'
            # history exhausted — latching would wrongly mark the WHOLE
            # connection complete (#154). Unfiltered runs latch as before.
            if not accounts:
                connection.backfill_complete = True
                session.flush()
            stopped_because = "empty_history"
            break
        if target_date is not None and start <= target_date:
            stopped_because = "target_reached"
            break

    qif_overlap: dict[str, int] = {}
    if coverage_start is not None and coverage_end is not None:
        qif_overlap = _qif_overlap_counts(session, connection.id, coverage_start, coverage_end)

    result: dict[str, Any] = {
        "windows_fetched": windows_fetched,
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
        "imported": imported,
        "skipped": skipped,
        "skipped_before_feed_start": skipped_before_feed_start,
        "skipped_masked_duplicate": skipped_masked_duplicate,
        "skipped_redescribed": skipped_redescribed,
        "empty_windows": empty_windows,
        "backfill_complete": connection.backfill_complete,
        "stopped_because": stopped_because,
        "qif_overlap": qif_overlap,
    }
    audit.record(
        session,
        actor_token_id=actor_token_id,
        action="connection.backfill",
        entity_type="connection",
        entity_id=connection.id,
        after={
            **result,
            "coverage_start": coverage_start.isoformat() if coverage_start else None,
            "coverage_end": coverage_end.isoformat() if coverage_end else None,
        },
    )
    return result


def sync_all_active() -> None:
    """Scheduled daily sync for every active connection (worker/serve job)."""
    from purser.db import get_sessionmaker

    with get_sessionmaker()() as session:
        ids = (
            session.execute(select(Connection.id).where(Connection.status != "disabled"))
            .scalars()
            .all()
        )
        for connection_id in ids:
            try:
                sync_connection(session, connection_id=connection_id, actor_token_id=None)
                session.commit()
            except FeedError:
                session.commit()  # keep status="error" + sync.failed event
                log.warning("scheduled sync failed", connection_id=str(connection_id))
            except Exception:
                session.rollback()
                log.exception("scheduled sync crashed", connection_id=str(connection_id))

        # Post-sync categorization (DESIGN §11): deterministic memory first,
        # configured model for misses — posted rows included (system actor).
        # Isolated: a categorization failure never taints the sync itself.
        try:
            from purser import categorize

            stats = categorize.run(session, token=None)
            session.commit()
            if any(stats.values()):
                log.info("post-sync categorization", **stats)
        except Exception:
            session.rollback()
            log.exception("post-sync categorization failed")

        # Backend review (#175, DESIGN §11.2): a no-op unless a provider is
        # configured AND ai_review is on — configuring a provider grants
        # nothing by itself. Isolated the same way: a failing provider must
        # never break a sync, it just leaves rows unreviewed.
        try:
            from purser import review

            review_stats = review.ai_review_pass(session)
            session.commit()
            if any(review_stats.values()):
                log.info("post-sync review", **review_stats)
        except Exception:
            session.rollback()
            log.exception("post-sync review failed")

    # Backdated feed rows dirty net_worth_daily; consume the watermark inline
    # (this is the scheduled job — there is no request to background off of).
    from purser import networth

    networth.consume_watermark()


def discover_accounts(session: Session, connection_id: uuid.UUID) -> list[dict[str, Any]]:
    """SimpleFIN accounts from the latest archived payload, with their current
    Purser mapping and — for mapped cash/card accounts — the live reported-vs-
    ledger drift (the wizard and Config → SimpleFIN consume this).

    The drift here is ungated and recomputed on every read: the UI shows the
    current raw comparison immediately, while the event stream stays behind
    the emission gate (deliberate asymmetry, DESIGN §7.3). It is measured on
    the basis each account's sync history selects (decision 64), or on the
    account's pin (decision 83), reported as ``drift_basis`` with
    ``drift_basis_pinned`` and the history's own pick in
    ``drift_basis_learned``."""
    connection = get_connection(session, connection_id)
    latest = session.execute(
        select(RawFetch)
        .where(RawFetch.connection_id == connection.id)
        .order_by(RawFetch.fetched_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest is None:
        raise FeedError("no fetches yet; sync the connection first")

    blocks = drift.recent_blocks(session, connection.id)  # basis history, read once
    discovered = []
    for sf_account in crypto.decrypt_json(latest.payload_enc).get("accounts", []):
        mapped = session.execute(
            select(Account).where(
                Account.connection_id == connection.id,
                Account.external_id == sf_account["id"],
            )
        ).scalar_one_or_none()
        comp = (
            drift.live_compare(session, account=mapped, sf_account=sf_account, blocks=blocks)
            if mapped is not None
            else None
        )
        discovered.append(
            {
                "external_id": sf_account["id"],
                "name": sf_account.get("name"),
                "org": (sf_account.get("org") or {}).get("name"),
                "currency": sf_account.get("currency", "USD"),
                "balance": sf_account.get("balance"),
                "balance_date": drift.parse_balance_date(sf_account.get("balance-date")),
                "mapped_account_id": mapped.id if mapped else None,
                "mapped_account_path": mapped.path if mapped else None,
                "ledger_balance": drift.canonical(comp.pending) if comp else None,
                "drift": drift.canonical(comp.delta) if comp and comp.delta != 0 else None,
                "drift_basis": comp.basis if comp else None,
                "drift_basis_pinned": comp.pinned if comp else False,
                "drift_basis_learned": comp.learned_basis if comp else None,
            }
        )
    return discovered


def map_account(
    session: Session,
    *,
    connection_id: uuid.UUID,
    external_id: str,
    account_id: uuid.UUID | None = None,
    create: dict[str, str] | None = None,
    actor_token_id: uuid.UUID | None,
) -> Account:
    """Map a feed account onto a Purser account (existing or new), then re-import
    the archived fetch(es) for it — the archive makes this free."""
    connection = get_connection(session, connection_id)
    if (account_id is None) == (create is None):
        raise FeedError("provide exactly one of account_id or create")

    if account_id is not None:
        account = ledger_service.get_account(session, account_id)
    else:
        assert create is not None
        account = ledger_service.create_account(
            session,
            name=create["name"],
            type_=create.get("type", "checking"),
            path=create.get("path"),
            currency=create.get("currency", "USD"),
            actor_token_id=actor_token_id,
        )
    if account.is_virtual:
        raise FeedError("cannot map a feed onto a category")
    if account.closed_at is not None:
        raise FeedError(f"{account.path} is closed; reopen it first before mapping a feed onto it")
    # Account before watermark (decision 88): the connection's accounts and
    # this one are locked in id order before the re-import below marks it.
    from purser.holdings import projection
    from purser.holdings import service as holdings_service

    projection.lock_accounts(
        session,
        [
            *session.execute(
                select(Account.id).where(Account.connection_id == connection.id)
            ).scalars(),
            account.id,
        ],
    )
    # Remapped onto another feed account, an open manual period stays open:
    # the account is still mapped, so the new mapping's first fetch after the
    # switch ends it with its own set, and the hand-tracked set holds until
    # then (#422). Ending it today with nothing reported blanked the account.
    # A holder that loses its mapping below has no fetch coming: it ends.

    # Remapping: if another account already holds this (connection, external_id)
    # pair, clear it first — two accounts matching the same feed account breaks
    # the importer's scalar_one_or_none() lookups on every subsequent sync.
    holders = (
        session.execute(
            select(Account).where(
                Account.connection_id == connection.id,
                Account.external_id == external_id,
                Account.id != account.id,
            )
        )
        .scalars()
        .all()
    )
    for holder in holders:
        holdings_service.end_period_unmapped(session, holder, actor_token_id=actor_token_id)
        holder.external_id = None
        holder.connection_id = None
        audit.record(
            session,
            actor_token_id=actor_token_id,
            action="account.unmap",
            entity_type="account",
            entity_id=holder.id,
            before={"external_id": external_id, "connection_id": str(connection.id)},
            after={
                "external_id": None,
                "connection_id": None,
                "moved_to_account_id": str(account.id),
            },
        )
    session.flush()  # release the old mapping before claiming it below

    account.external_id = external_id
    account.connection_id = connection.id
    session.flush()
    audit.record(
        session,
        actor_token_id=actor_token_id,
        action="account.map",
        entity_type="account",
        entity_id=account.id,
        after={"external_id": external_id, "connection_id": str(connection.id)},
    )

    latest = session.execute(
        select(RawFetch)
        .where(RawFetch.connection_id == connection.id)
        .order_by(RawFetch.fetched_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest is not None:
        importer.import_fetch(session, latest)
    # Mapping a card/loan brings its history in (and lets the auto-linker
    # merge the payments to it): suggest the payment schedule, never activate
    # it (#234).
    from purser import schedules as schedules_service

    schedules_service.detect_for_new_liability(session, account, actor_token_id=actor_token_id)
    return account


def connection_health(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(select(Connection).order_by(Connection.created_at)).scalars()
    out = []
    for c in rows:
        latest = session.execute(
            select(RawFetch)
            .where(RawFetch.connection_id == c.id)
            .order_by(RawFetch.fetched_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        out.append(
            {
                "id": c.id,
                "provider": c.provider,
                "status": c.status,
                "created_at": c.created_at,
                "last_fetch_at": c.last_fetch_at,
                "last_errors": (
                    crypto.decrypt_json(latest.payload_enc).get("errors", []) if latest else []
                ),
                "fetches_today": _fetches_today(session, c.id),
                "backfill_cursor": c.backfill_cursor,
                "backfill_complete": c.backfill_complete,
            }
        )
    return out
