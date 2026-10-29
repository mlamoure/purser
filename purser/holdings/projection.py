"""The manual snapshot projection (DESIGN §8, decision 88).

A manual account's ``holdings_snapshots`` rows (``source='manual'``) are a
pure function of its positions' events, the stored closes, the feed-implied
prices, the account's last feed snapshot and today. ``project_account``
recomputes them and rewrites only the rows that differ, so running it twice
changes nothing the second time, and the net-worth watermark is lowered only
to the first date that actually changed.

Bounds:

- ``lower``: the first effective event, or the day after the account's last
  FEED snapshot when that is later. Feed history is never rewritten and no
  date carries rows of both sources.
- ``upper``: today (UTC), or the close date of a closed account (readers cut
  off there anyway).

Back to the feed: the switch touches no row, and the hand-tracked set stays
in effect until the first feed fetch taken after the switch ends the manual
period (``end_manual_period``, from the importer): from that fetch's date the
manual rows go, the fetch's set imports, and a MANUAL zero row ends each
hand-tracked position it does not report. A date so holds the feed's rows
plus, on the period-end date only, manual tombstones for the symbols the feed
does not report: at most one row per (account, date, symbol). The tombstones
are manual, so they never move the feed floor.

Locking: every caller locks the account rows first (``lock_accounts``, FOR NO
KEY UPDATE, in id order) and only then reaches ``balances.mark_dirty``. A
sync holds the watermark while it inserts postings, whose foreign-key check
takes KEY SHARE on the account row; NO KEY UPDATE does not conflict with
that, so the two can never wait on each other in a cycle (the reconciliation
lock, §3.7, made the same choice for the same reason).
"""

import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext

from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.orm import Session, load_only

from purser import audit, balances, locks
from purser.core import holdings as core
from purser.ledger import LedgerError, NotFoundError
from purser.logging import get_logger
from purser.models import Account, HoldingsSnapshot, Position, PositionEvent, SecurityPrice

log = get_logger(__name__)


def utc_today() -> date:
    return datetime.now(UTC).date()


@dataclass(frozen=True)
class ProjectionResult:
    """What one projection changed: the first date whose rows differ (None
    when nothing did), and how many rows it inserted and deleted."""

    changed_from: date | None
    inserted: int
    deleted: int


def lock_accounts(session: Session, account_ids: Iterable[uuid.UUID]) -> list[Account]:
    """Lock these account rows, in id order (``purser.locks``). Callers lock
    everything they will project BEFORE the first ``mark_dirty``, which is
    what keeps the balance watermark out of every wait cycle."""
    return locks.lock_accounts(session, account_ids)


def lock_account(session: Session, account_id: uuid.UUID) -> Account:
    account = locks.lock_account(session, account_id)
    if account is None:
        raise NotFoundError(f"account {account_id} not found")
    return account


# pg_advisory_xact_lock(PRICE_LOCK, hashtext(symbol)): a per-symbol lock
# between a close and a write that starts holding the symbol (#415).
PRICE_LOCK = 1347773012


def lock_symbol(session: Session, symbol: str) -> None:
    """Take ``symbol`` exclusively for this transaction: ``record_price``
    holds it from before it reads which manual accounts hold the symbol to
    its commit, so no account can start holding it unseen."""
    session.execute(
        text("SELECT pg_advisory_xact_lock(:ns, hashtext(:symbol))"),
        {"ns": PRICE_LOCK, "symbol": symbol},
    )


def share_symbols(session: Session, symbols: Iterable[str]) -> None:
    """Take these symbols shared for this transaction, in order, before a
    write that makes a manual account hold them projects. A close being
    recorded makes it wait (its projection then reads that close); a close
    recorded meanwhile waits for it (and then sees the account). The caller
    holds the account, which holds none of these symbols yet, so it is never
    one a waiting close has locked: the two cannot deadlock."""
    for symbol in sorted(set(symbols)):
        session.execute(
            text("SELECT pg_advisory_xact_lock_shared(:ns, hashtext(:symbol))"),
            {"ns": PRICE_LOCK, "symbol": symbol},
        )


def event_fact(row: PositionEvent) -> core.Event:
    return core.Event(
        id=row.id,
        occurred_on=row.occurred_on,
        created_at=row.created_at,
        kind=row.kind,
        quantity=Decimal(row.quantity),
        price=Decimal(row.price) if row.price is not None else None,
        fees=Decimal(row.fees) if row.fees is not None else None,
        cost_basis=Decimal(row.cost_basis) if row.cost_basis is not None else None,
        reverses_id=row.reverses_id,
        replaces_id=row.replaces_id,
    )


def position_events(
    session: Session, position_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[PositionEvent]]:
    """Every event row of these positions, reversals included, with only the
    columns a timeline reads: the notes are ciphertext, decrypted on load,
    and no timeline shows them (they still load on access)."""
    out: dict[uuid.UUID, list[PositionEvent]] = defaultdict(list)
    if not position_ids:
        return out
    for row in session.execute(
        select(PositionEvent)
        .options(
            load_only(
                PositionEvent.id,
                PositionEvent.position_id,
                PositionEvent.occurred_on,
                PositionEvent.created_at,
                PositionEvent.kind,
                PositionEvent.quantity,
                PositionEvent.price,
                PositionEvent.fees,
                PositionEvent.cost_basis,
                PositionEvent.reverses_id,
            )
        )
        .where(PositionEvent.position_id.in_(position_ids))
    ).scalars():
        out[row.position_id].append(row)
    return out


def position_facts(session: Session, account_id: uuid.UUID) -> list[core.PositionFacts]:
    positions = list(
        session.execute(select(Position).where(Position.account_id == account_id)).scalars()
    )
    events = position_events(session, [p.id for p in positions])
    return [
        core.PositionFacts(
            id=p.id, symbol=p.symbol, events=[event_fact(e) for e in events.get(p.id, [])]
        )
        for p in positions
    ]


def feed_floor(session: Session, account_id: uuid.UUID) -> date | None:
    """The day after the account's last FEED snapshot: the first date a
    manual row may carry (None when the feed never snapshotted it)."""
    last = session.execute(
        select(func.max(HoldingsSnapshot.as_of)).where(
            HoldingsSnapshot.account_id == account_id, HoldingsSnapshot.source == "feed"
        )
    ).scalar_one_or_none()
    return last + timedelta(days=1) if last is not None else None


# One feed-implied close per (symbol, date): market value over quantity of a
# feed snapshot of that symbol that day, from the snapshot holding the most
# shares (ties by id, so the choice is deterministic).
_FEED_IMPLIED = text(
    """
SELECT DISTINCT ON (symbol, as_of) symbol, as_of, market_value, quantity
FROM holdings_snapshots
WHERE source = 'feed'
  AND symbol = ANY(CAST(:symbols AS text[]))
  AND as_of <= :upper
  AND quantity > 0
  AND market_value > 0
ORDER BY symbol, as_of, quantity DESC, id
"""
)


def price_points(
    session: Session, symbols: Iterable[str], *, upper: date
) -> dict[str, list[core.PricePoint]]:
    """Every close of these symbols dated on or before ``upper``: the stored
    ones (manual and provider) plus the feed-implied ones, computed here and
    never stored (decision 89)."""
    wanted = sorted(set(symbols))
    out: dict[str, list[core.PricePoint]] = defaultdict(list)
    if not wanted:
        return out
    for row in session.execute(
        select(SecurityPrice).where(
            SecurityPrice.symbol.in_(wanted), SecurityPrice.price_date <= upper
        )
    ).scalars():
        out[row.symbol].append(core.PricePoint(row.price_date, Decimal(row.close), row.source))
    for symbol, as_of, market, quantity in session.execute(
        _FEED_IMPLIED, {"symbols": wanted, "upper": upper}
    ):
        with localcontext() as ctx:
            ctx.prec = 60
            close = (Decimal(market) / Decimal(quantity)).quantize(core.PRICE_QUANT)
        if close > 0:
            out[symbol].append(core.PricePoint(as_of, close, core.SOURCE_FEED))
    return out


def price_books(
    session: Session, symbols: Iterable[str], *, upper: date
) -> dict[str, core.PriceBook]:
    return {
        symbol: core.PriceBook(points)
        for symbol, points in price_points(session, symbols, upper=upper).items()
    }


def upper_bound(account: Account, today: date) -> date:
    if account.closed_at is None:
        return today
    return min(today, account.closed_at.date())


@dataclass(frozen=True)
class ManualSpans:
    """Where an account's manual rows belong (#402): the gaps between its
    feed runs that are manual periods. A feed run is a maximal stretch of
    dates carrying feed rows with no manual-only date between them (a date
    with feed rows and a period's end tombstones is the feed's; a missed
    sync inside a run is too). The gap after the last run (or every date,
    with no feed rows) is the current period; an earlier gap is a manual
    period when it holds manual rows, so a manual -> feed -> manual account
    re-derives its first period, while a feed account switched to manual
    never gains rows before its feed history."""

    gaps: tuple[tuple[date | None, date | None], ...]  # inclusive; None = unbounded

    def holds(self, day: date) -> bool:
        return any((lo is None or lo <= day) and (hi is None or day <= hi) for lo, hi in self.gaps)

    def ranges(self, *, lower: date, upper: date) -> list[tuple[date, date]]:
        out = []
        for lo, hi in self.gaps:
            start = max(lower, lo) if lo is not None else lower
            end = min(upper, hi) if hi is not None else upper
            if start <= end:
                out.append((start, end))
        return out


def manual_spans(session: Session, account_id: uuid.UUID) -> ManualSpans:
    runs: list[tuple[date, date]] = []
    manual_only: list[date] = []
    start: date | None = None
    end: date | None = None
    for as_of, has_feed in session.execute(
        select(
            HoldingsSnapshot.as_of,
            func.bool_or(HoldingsSnapshot.source == "feed"),
        )
        .where(HoldingsSnapshot.account_id == account_id)
        .group_by(HoldingsSnapshot.as_of)
        .order_by(HoldingsSnapshot.as_of)
    ):
        if has_feed:
            start = start or as_of
            end = as_of
            continue
        manual_only.append(as_of)
        if start is not None and end is not None:
            runs.append((start, end))
            start = end = None
    if start is not None and end is not None:
        runs.append((start, end))
    gaps: list[tuple[date | None, date | None]] = []
    lo: date | None = None
    for run_start, run_end in runs:
        hi = run_start - timedelta(days=1)
        if any((lo is None or lo <= d) and d <= hi for d in manual_only):
            gaps.append((lo, hi))
        lo = run_end + timedelta(days=1)
    gaps.append((lo, None))  # the current period
    return ManualSpans(tuple(gaps))


def computed_rows(
    session: Session, account: Account, *, today: date
) -> tuple[list[core.Row], ManualSpans]:
    """The account's manual rows as the facts give them now (one projection
    per manual period, so each starts with a full set), and those periods."""
    spans = manual_spans(session, account.id)
    positions = position_facts(session, account.id)
    first = core.first_event_date(positions)
    if first is None:
        return [], spans
    upper = upper_bound(account, today)
    ranges = spans.ranges(lower=first, upper=upper)
    books = price_books(session, {p.symbol for p in positions}, upper=upper)
    rows: list[core.Row] = []
    try:
        for lo, hi in ranges:
            rows.extend(core.projection_rows(positions, books, lower=lo, upper=hi))
    except core.NegativeQuantity as exc:
        raise LedgerError(str(exc)) from exc
    except core.OutOfRange as exc:  # a market value, share count or basis
        raise LedgerError(str(exc)) from exc
    return rows, spans


def _values(row: core.Row) -> tuple[object, ...]:
    return (
        row.symbol,
        row.quantity,
        row.cost_basis,
        row.market_value,
        row.price,
        row.price_as_of,
    )


def project_account(
    session: Session,
    account: Account,
    *,
    from_date: date | None = None,
    today: date | None = None,
) -> ProjectionResult:
    """Bring ``account``'s manual snapshot rows dated on or after
    ``from_date`` in line with its events and prices, outside the feed's
    runs, in its manual periods (``manual_spans``: an earlier one too, #402).

    The caller holds the account's lock (``lock_accounts``). A feed-sourced
    account is left alone: its positions are read-only history until it is
    switched back. Rows are compared on (date, position) and only the ones
    that differ are deleted and inserted; ``networth_dirty_from`` is lowered
    to the first changed date, so ``consume_watermark`` heals
    ``net_worth_daily`` from there (decision 38)."""
    if account.holdings_source != "manual":
        return ProjectionResult(None, 0, 0)
    today = today or utc_today()
    rows, spans = computed_rows(session, account, today=today)

    write_from = from_date
    if write_from is not None:
        rows = [r for r in rows if r.as_of >= write_from]

    old_stmt = select(
        HoldingsSnapshot.id,
        HoldingsSnapshot.as_of,
        HoldingsSnapshot.position_id,
        HoldingsSnapshot.symbol,
        HoldingsSnapshot.quantity,
        HoldingsSnapshot.cost_basis,
        HoldingsSnapshot.market_value,
        HoldingsSnapshot.price,
        HoldingsSnapshot.price_as_of,
    ).where(HoldingsSnapshot.account_id == account.id, HoldingsSnapshot.source == "manual")
    if write_from is not None:
        old_stmt = old_stmt.where(HoldingsSnapshot.as_of >= write_from)
    old: dict[tuple[date, uuid.UUID], tuple[uuid.UUID, tuple[object, ...]]] = {}
    for row in session.execute(old_stmt):
        if not spans.holds(row.as_of):
            continue  # a feed run's date: a period's end tombstones stay
        assert row.position_id is not None  # holdings_manual_has_position
        old[(row.as_of, row.position_id)] = (
            row.id,
            (
                row.symbol,
                row.quantity,
                row.cost_basis,
                row.market_value,
                row.price,
                row.price_as_of,
            ),
        )
    new = {(r.as_of, r.position_id): r for r in rows}

    gone = {
        key: row_id
        for key, (row_id, values) in old.items()
        if key not in new or _values(new[key]) != values
    }
    fresh = [r for key, r in new.items() if key not in old or _values(r) != old[key][1]]
    stale_ids = list(gone.values())
    changed = [key[0] for key in gone] + [r.as_of for r in fresh]
    if stale_ids:
        session.execute(delete(HoldingsSnapshot).where(HoldingsSnapshot.id.in_(stale_ids)))
    if fresh:
        session.execute(
            insert(HoldingsSnapshot),
            [
                {
                    "account_id": account.id,
                    "as_of": r.as_of,
                    "symbol": r.symbol,
                    "quantity": r.quantity,
                    "cost_basis": r.cost_basis,
                    "market_value": r.market_value,
                    "source": "manual",
                    "position_id": r.position_id,
                    "price": r.price,
                    "price_as_of": r.price_as_of,
                }
                for r in sorted(fresh, key=lambda r: (r.as_of, r.position_id))
            ],
        )

    # Logged once per stretch (decision 89, #422): where a position's value
    # starts not to fit, not on each later set that carries it again (each
    # day's close writes a new one). The share count is the likely error.
    def unfit(values: tuple[object, ...]) -> bool:
        return values[4] is not None and values[3] is None  # a price, no value

    stored: dict[uuid.UUID, list[tuple[date, bool]]] = defaultdict(list)
    for (as_of, position_id), (_, values) in old.items():
        stored[position_id].append((as_of, unfit(values)))
    starts = []
    for r in fresh:
        if r.price is None or r.market_value is not None:
            continue
        before = [entry for entry in stored[r.position_id] if entry[0] <= r.as_of]
        if before and max(before)[1]:
            continue  # it already did not fit on the stored set before
        starts.append(r)
    if starts:
        log.warning(
            "holdings.value_out_of_range",
            account_id=str(account.id),
            symbols=sorted({r.symbol for r in starts}),
            first=min(r.as_of for r in starts).isoformat(),
        )
    changed_from = min(changed) if changed else None
    if changed_from is not None:
        balances.mark_dirty(session, changed_from, networth_only=True)
    return ProjectionResult(changed_from, len(fresh), len(stale_ids))


def manual_accounts_holding(session: Session, symbol: str) -> list[uuid.UUID]:
    """Manual accounts with any position in ``symbol``, open or closed: a
    backdated close moves the history of a position sold since, too."""
    return list(
        session.execute(
            select(Position.account_id)
            .join(Account, Account.id == Position.account_id)
            .where(Position.symbol == symbol, Account.holdings_source == "manual")
            .distinct()
        ).scalars()
    )


def manual_accounts_with_positions(session: Session) -> list[uuid.UUID]:
    return list(
        session.execute(
            select(Position.account_id)
            .join(Account, Account.id == Position.account_id)
            .where(Account.holdings_source == "manual")
            .distinct()
        ).scalars()
    )


def end_manual_period(
    session: Session,
    account: Account,
    on: date,
    *,
    reported: set[str],
    raw_fetch_id: uuid.UUID | None,
    actor_token_id: uuid.UUID | None = None,
    reason: str = "fetch",
) -> tuple[int, int]:
    """End ``account``'s manual period on ``on`` (decision 88), once: the
    first feed fetch taken after the switch back to the feed (the importer),
    or the day an account whose period is still open loses its feed mapping.
    The caller holds the account lock.

    - The manual rows dated ``on`` or later go: the feed takes over from
      that date.
    - Each position the set in effect before ``on`` holds by hand gets a
      MANUAL zero row (a tombstone, ``position_id`` set) dated ``on``, unless
      the feed reports its symbol (``reported``): the feed's own row stands
      for it. A cash-only feed so ends every position.
    - ``manual_period_open`` is cleared, and one ``account.manual_period_end``
      audit row records the rows deleted, the tombstones and the fetch.

    So ``on`` holds the feed's rows plus, for the symbols it does not report,
    these tombstones: at most one row per (account, date, symbol). Being
    manual rows, they never move the feed floor: switched back to manual,
    the projection re-derives from the old floor and replaces them. Returns
    (manual rows deleted, tombstones written)."""
    result = session.execute(
        delete(HoldingsSnapshot).where(
            HoldingsSnapshot.account_id == account.id,
            HoldingsSnapshot.source == "manual",
            HoldingsSnapshot.as_of >= on,
        )
    )
    deleted = int(getattr(result, "rowcount", 0) or 0)
    before = session.execute(
        select(func.max(HoldingsSnapshot.as_of)).where(
            HoldingsSnapshot.account_id == account.id, HoldingsSnapshot.as_of < on
        )
    ).scalar_one_or_none()
    ended: list[tuple[uuid.UUID, str]] = []
    if before is not None:
        ended = sorted(
            (position_id, symbol)
            for position_id, symbol in session.execute(
                select(HoldingsSnapshot.position_id, HoldingsSnapshot.symbol).where(
                    HoldingsSnapshot.account_id == account.id,
                    HoldingsSnapshot.as_of == before,
                    HoldingsSnapshot.source == "manual",
                    HoldingsSnapshot.quantity > 0,
                )
            )
            if position_id is not None and symbol not in reported
        )
    if ended:
        session.execute(
            insert(HoldingsSnapshot),
            [
                {
                    "account_id": account.id,
                    "as_of": on,
                    "symbol": symbol,
                    "quantity": Decimal(0).quantize(core.QUANTITY_QUANT),
                    "cost_basis": Decimal(0).quantize(core.MONEY_QUANT),
                    "market_value": Decimal(0).quantize(core.MONEY_QUANT),
                    "source": "manual",
                    "position_id": position_id,
                    "price": None,
                    "price_as_of": None,
                }
                for position_id, symbol in ended
            ],
        )
    account.manual_period_open = False
    session.flush()
    audit.record(
        session,
        actor_token_id=actor_token_id,
        action="account.manual_period_end",
        entity_type="account",
        entity_id=account.id,
        after={
            "on": on.isoformat(),
            "reason": reason,
            "manual_rows_deleted": deleted,
            "tombstones_written": len(ended),
            "raw_fetch_id": str(raw_fetch_id) if raw_fetch_id is not None else None,
        },
    )
    return deleted, len(ended)
