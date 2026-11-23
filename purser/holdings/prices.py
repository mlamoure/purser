"""Security prices (DESIGN §8, decision 89): manual closes, the daily
provider fetch, and staleness.

Resolution is newest date first; the source order (manual, then a provider,
then feed-implied) only breaks a tie between closes of the same date
(``core.PriceBook``). Feed-implied closes are computed on read by the
projection and never stored.

Nothing calls out to a provider from an ordinary request path: closes arrive
through the daily job (``fetch_daily``, 06:40 UTC, after the 06:30 sync) or
an admin's explicit fetch-now, which runs the same body. For each symbol a
manual position has held (closed positions and closed accounts included)
the fetch asks for the dates its positions need (each position's held span,
from a week before its first day held: ``core.needed_ranges``) less the
date ranges the provider has answered for good (``price_answered``,
``core.fetch_windows``), newest first. An answer is final for dates at least
three days old and up to the newest close it returned; an empty answer or a
not-found for such dates counts as answered too, so a range the provider has
nothing for (before a listing, a weekend after a sell-out, an unknown
ticker) is asked for once, while a gap between answered ranges still is.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from purser import appsettings, audit, balances, clock, events
from purser.core import holdings as core
from purser.holdings import overlay, projection
from purser.holdings.providers import TIINGO, PriceFetchError, PriceProvider, TiingoProvider
from purser.holdings.service import checked, symbol_of
from purser.ledger import LedgerError
from purser.logging import get_logger
from purser.models import (
    Account,
    ApiToken,
    Event,
    Position,
    SecurityPrice,
)

log = get_logger(__name__)

STALE_EVENT = "price.stale"
FETCH_FAILED_EVENT = "price.fetch_failed"
UNPRICED_EVENT = "holding.unpriced"  # a feed position valued at 0 with no stored close (#392)
# A switched-back account whose manual period no fetch has ended (#422).
PERIOD_OPEN_EVENT = "holding.period_open"


@dataclass(frozen=True)
class PriceRecorded:
    symbol: str
    price_date: date
    close: Decimal
    previous: Decimal | None  # the manual close it replaced, if any
    accounts: int  # manual accounts re-projected
    # Accounts holding the symbol in a feed row the feed values at 0, whose
    # net worth from price_date re-materializes (decision 92).
    feed_accounts: int = 0


def record_price(
    session: Session,
    *,
    symbol: str,
    price_date: date,
    close: Decimal,
    token: ApiToken | None,
    today: date | None = None,
) -> PriceRecorded:
    """Record (or replace) the manual close of ``symbol`` on ``price_date``,
    then re-project every manual account holding it from that date. An
    account holding the symbol in a feed row the feed values at 0, dated
    ``price_date`` or later, is valued from the close on read (decision 92):
    nothing of it is written, and net worth is marked dirty from
    ``price_date`` so its stored days heal through the watermark."""
    today = today or clock.ledger_today()
    normalized = symbol_of(symbol)
    close = checked(core.check_price, close, "close")
    if price_date > today:
        raise LedgerError(f"a close cannot be dated in the future ({price_date.isoformat()})")
    # The symbol first (#415): no write can start holding it on a manual
    # account until this commits, and one already doing so has committed,
    # so the account set read next is complete.
    projection.lock_symbol(session, normalized)
    # Lock every account the price moves BEFORE writing (projection.lock_accounts).
    accounts = projection.lock_accounts(
        session, projection.manual_accounts_holding(session, normalized)
    )
    previous = session.execute(
        select(SecurityPrice.close).where(
            SecurityPrice.symbol == normalized,
            SecurityPrice.price_date == price_date,
            SecurityPrice.source == core.SOURCE_MANUAL,
        )
    ).scalar_one_or_none()
    stmt = pg_insert(SecurityPrice).values(
        symbol=normalized, price_date=price_date, source=core.SOURCE_MANUAL, close=close
    )
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=["symbol", "price_date", "source"],
            set_={"close": stmt.excluded.close, "fetched_at": func.now()},
        )
    )
    feed_accounts = overlay.accounts_valued_from(session, normalized, price_date)
    audit.record(
        session,
        actor_token_id=token.id if token else None,
        action="price.record",
        entity_type="price",
        before=(
            {"symbol": normalized, "date": price_date.isoformat(), "close": str(previous)}
            if previous is not None
            else None
        ),
        after={
            "symbol": normalized,
            "date": price_date.isoformat(),
            "close": str(close),
            "feed_accounts_revalued": len(feed_accounts),
        },
    )
    # The closes every account reads, loaded once from the day before the
    # price (#404): each projection computes only its rows from price_date.
    books = projection.price_books(
        session,
        projection.symbols_of(session, [a.id for a in accounts]),
        upper=today,
        start=price_date,
    )
    changed = [price_date] if feed_accounts else []
    for account in accounts:
        result = projection.project_account(
            session, account, from_date=price_date, today=today, books=books, mark_dirty=False
        )
        if result.changed_from is not None:
            changed.append(result.changed_from)
    if changed:
        # Once, after every projection and the account locks
        # (projection.lock_accounts), like every holdings writer: the
        # watermark comes last and is held from here to the commit only.
        balances.mark_dirty(session, min(changed), networth_only=True)
    return PriceRecorded(
        symbol=normalized,
        price_date=price_date,
        close=close,
        previous=Decimal(previous) if previous is not None else None,
        accounts=len(accounts),
        feed_accounts=len(feed_accounts),
    )


def list_prices(
    session: Session,
    *,
    symbol: str,
    start: date | None = None,
    end: date | None = None,
    limit: int = 100,
) -> list[SecurityPrice]:
    stmt = select(SecurityPrice).where(SecurityPrice.symbol == symbol_of(symbol))
    if start is not None:
        stmt = stmt.where(SecurityPrice.price_date >= start)
    if end is not None:
        stmt = stmt.where(SecurityPrice.price_date <= end)
    return list(
        session.execute(
            stmt.order_by(SecurityPrice.price_date.desc(), SecurityPrice.source).limit(limit)
        ).scalars()
    )


def open_symbols(session: Session, *, today: date | None = None) -> dict[str, date]:
    """Symbols HELD today — more than zero shares in an open position on an
    open manual account — each with the earliest ``opened_on`` among those
    positions. A position sold down to zero but not closed yet holds nothing:
    its symbol is not fetched daily, never turns stale, and is not listed in
    the status."""
    today = today or clock.ledger_today()
    positions = list(
        session.execute(
            select(Position)
            .join(Account, Account.id == Position.account_id)
            .where(
                Position.closed_at.is_(None),
                Account.holdings_source == "manual",
                Account.closed_at.is_(None),
            )
        ).scalars()
    )
    events = projection.position_events(session, [p.id for p in positions])
    held: dict[str, date] = {}
    for position in positions:
        facts = [projection.event_fact(row) for row in events.get(position.id, [])]
        try:
            holds = core.Timeline(core.quantity_timeline(facts)).quantity_on(today) > 0
        except (core.NegativeQuantity, core.OutOfRange):
            holds = True  # never stored by the writes; keep fetching, never break the job
        if holds:
            opened = held.get(position.symbol)
            held[position.symbol] = (
                position.opened_on if opened is None else min(opened, position.opened_on)
            )
    return held


def watched_books(session: Session, *, today: date) -> dict[str, core.PriceBook | None]:
    """The symbols whose close can turn stale, each with the book its value
    reads: those held today by a manual position (``open_symbols``; every
    close, feed-implied ones included), and those the overlay values from a
    stored close in a feed account's newest set (decision 92: stored closes
    only, as the overlay reads them; a lapsed weekly price must not carry an
    old close silently). Both show in ``price.stale``, ``/prices/status``
    and ``whats_new`` the same way.

    Each book answers for ``today`` only (``book.on(today)``): the newest
    closes at or before it, never the symbol's whole history (#404)."""
    manual = set(open_symbols(session, today=today))
    overlaid = overlay.overlaid_symbols(session, today=today) - manual
    books: dict[str, core.PriceBook | None] = dict.fromkeys(manual | overlaid)
    books.update(projection.latest_books(session, manual, today=today))
    books.update(
        overlay.stored_books(session, overlaid, upper=today, start=today + timedelta(days=1))
    )
    return books


def fetch_needs(session: Session, *, today: date) -> dict[str, list[core.Range]]:
    """Per symbol, the dates it needs closes for (``core.needed_ranges``):
    every position on a manual account, closed positions and closed accounts
    included (their history is valued with the closes too), adds its held
    span read from its EFFECTIVE events (``core.held_span``): from the first
    day it holds shares to the last, which is today while it still holds
    them, the account's close date for a closed account (the projection's
    ``upper_bound``), else the day before the date that sold it down to
    zero. A reversed event adds nothing; two positions' spans stay apart, so
    the dates between them are not asked for."""
    rows = session.execute(
        select(Position, Account)
        .join(Account, Account.id == Position.account_id)
        .where(Account.holdings_source == "manual")
    ).all()
    events = projection.position_events(session, [position.id for position, _a in rows])
    spans: dict[str, list[core.Range]] = {}
    for position, account in rows:
        upper = projection.upper_bound(account, today)
        facts = [projection.event_fact(row) for row in events.get(position.id, [])]
        try:
            span = core.held_span(core.quantity_timeline(facts), upper)
        except (core.NegativeQuantity, core.OutOfRange):
            # Never stored by the writes; ask for the widest range rather
            # than break the job.
            span = (position.opened_on, upper) if position.opened_on <= upper else None
        if span is not None:
            spans.setdefault(position.symbol, []).append(span)
    return {symbol: core.needed_ranges(found) for symbol, found in spans.items()}


def _answered(session: Session, source: str, symbols: list[str]) -> dict[str, list[core.Range]]:
    """Per symbol, the date ranges ``source`` has answered for good
    (inclusive; Postgres keeps a daterange half-open, so the upper bound is
    the day after)."""
    rows = session.execute(
        text(
            "SELECT a.symbol, lower(r), upper(r) - 1"
            " FROM price_answered a, unnest(a.answered) AS r"
            " WHERE a.source = :source AND a.symbol = ANY(:symbols)"
        ),
        {"source": source, "symbols": symbols},
    ).all()
    out: dict[str, list[core.Range]] = {}
    for symbol, first, last in rows:
        out.setdefault(symbol, []).append((first, last))
    return out


def _record_answered(
    session: Session, symbol: str, source: str, start: date, through: date
) -> None:
    """Add [start, through] to the ranges ``source`` has answered for good
    for ``symbol`` (a multirange union: ranges that touch merge)."""
    session.execute(
        text(
            "INSERT INTO price_answered AS a (symbol, source, answered)"
            " VALUES (:symbol, :source, datemultirange(daterange(:start, :through, '[]')))"
            " ON CONFLICT (symbol, source) DO UPDATE"
            " SET answered = a.answered + excluded.answered, updated_at = now()"
        ),
        {"symbol": symbol, "source": source, "start": start, "through": through},
    )


def configured_provider(
    *, client: httpx.Client | None = None
) -> tuple[PriceProvider | None, str | None]:
    """The provider the config selects, or None with the reason."""
    name = appsettings.effective().price_provider
    if name == "none":
        return None, "no price provider is configured"
    if name == TIINGO:
        key = appsettings.price_api_key()
        if not key:
            return None, "the tiingo provider has no API key (Settings, Prices)"
        return TiingoProvider(key, client=client), None
    return None, f"unknown price provider {name!r}"


# Rows per INSERT: a backfill of years is a few statements, well inside
# Postgres' 65,535 bind parameters (four per row).
_INSERT_BATCH = 1000


def _insert_closes(
    session: Session, symbol: str, source: str, closes: list[tuple[date, Decimal]]
) -> list[date]:
    """Insert new closes, one multi-row INSERT per batch (an existing
    (symbol, date, source) is kept: a close does not change once published).
    Returns the dates actually inserted."""
    inserted: list[date] = []
    # One row per date, the first close given for it, as one INSERT per close
    # kept it (a close does not change once published).
    first: dict[date, Decimal] = {}
    for day, close in closes:
        first.setdefault(day, close)
    unique = sorted(first.items())
    for start in range(0, len(unique), _INSERT_BATCH):
        batch = unique[start : start + _INSERT_BATCH]
        inserted.extend(
            session.execute(
                pg_insert(SecurityPrice)
                .values(
                    [
                        {"symbol": symbol, "price_date": day, "source": source, "close": close}
                        for day, close in batch
                    ]
                )
                .on_conflict_do_nothing(index_elements=["symbol", "price_date", "source"])
                .returning(SecurityPrice.price_date)
            ).scalars()
        )
    return inserted


def _failure(exc: PriceFetchError) -> dict[str, Any]:
    """One symbol's entry in a run's ``failed`` list (and its audit row)."""
    return {"symbol": exc.symbol, "status": exc.status, "reason": exc.reason}


def _fetch_failed(
    session: Session, provider: str, exc: PriceFetchError, today: date, *, kind: str = "fetch"
) -> None:
    """One ``price.fetch_failed`` event per symbol, kind and day, however
    often the fetch runs: a refused request (``fetch``) and closes dropped as
    absurd (``dropped``) are each reported. Logged with the symbol, status
    and reason only — never the key (a reason is a fixed phrase or an
    exception's class name). Overlapping runs cannot both emit
    (``events.emit_once``, #405)."""
    log.warning(
        "price.fetch_failed",
        provider=provider,
        symbol=exc.symbol,
        status=exc.status,
        reason=exc.reason,
    )
    key = f"{exc.symbol}:{today.isoformat()}" + (":dropped" if kind == "dropped" else "")
    events.emit_once(
        session,
        type_=FETCH_FAILED_EVENT,
        dedup_key=key,
        entity_type="security",
        payload={
            "provider": provider,
            "symbol": exc.symbol,
            "status": exc.status,
            "reason": exc.reason,
        },
    )


def stale_symbols(
    books: dict[str, core.PriceBook | None], *, today: date, stale_days: int
) -> list[tuple[str, date | None]]:
    """The symbols of ``books`` (``watched_books``) whose close in effect
    today is older than ``stale_days``, or that have no close at all, with
    the date of that close."""
    out: list[tuple[str, date | None]] = []
    for symbol in sorted(books):
        book = books[symbol]
        point = book.on(today) if book is not None else None
        if point is None:
            out.append((symbol, None))
        elif core.stale(point.price_date, today, stale_days):
            out.append((symbol, point.price_date))
    return out


def emit_stale(session: Session, stale: list[tuple[str, date | None]], *, stale_days: int) -> int:
    """``price.stale`` once per symbol per stale close: the dedup key is the
    symbol and the date of the close in effect, so a symbol that turns stale
    again after a fresher close arrived is a new transition and alerts again,
    while a symbol that stays stale on the same close never repeats.
    Overlapping runs cannot both emit (``events.emit_once``, #405)."""
    emitted = 0
    for symbol, as_of in stale:
        key = f"{symbol}:{as_of.isoformat() if as_of is not None else 'none'}"
        if events.emit_once(
            session,
            type_=STALE_EVENT,
            dedup_key=key,
            entity_type="security",
            payload={
                "symbol": symbol,
                "price_as_of": as_of.isoformat() if as_of is not None else None,
                "stale_days": stale_days,
            },
        ):
            emitted += 1
    return emitted


def emit_period_open(session: Session, periods: list[Any], *, stale_days: int) -> int:
    """``holding.period_open`` once per account and switch (#422): its
    manual period has stayed open ``stale_days`` after the switch back to
    the feed. The dedup key is the account and the switch moment, so a
    later switch re-arms it."""
    emitted = 0
    for period in periods:
        if events.emit_once(
            session,
            type_=PERIOD_OPEN_EVENT,
            dedup_key=f"{period.account_id}:{period.switched_at.isoformat()}",
            entity_type="account",
            entity_id=period.account_id,
            payload={
                "account": period.account,
                "since": clock.ledger_date(period.switched_at).isoformat(),
                "stale_days": stale_days,
            },
        ):
            emitted += 1
    return emitted


def emit_unpriced(session: Session, unpriced: list[overlay.Unpriced]) -> int:
    """``holding.unpriced`` once per account and symbol when it turns
    unpriced (decision 92), however often the job runs: the dedup key is the
    account, the symbol and the newest stored close on or before the row
    (``none`` without one), so the line re-arms once the position was
    priced. Overlapping runs cannot both emit (``events.emit_once``)."""
    emitted = 0
    for u in unpriced:
        key = f"{u.account_id}:{u.symbol}:{u.last_close.isoformat() if u.last_close else 'none'}"
        if events.emit_once(
            session,
            type_=UNPRICED_EVENT,
            dedup_key=key,
            entity_type="account",
            entity_id=u.account_id,
            payload={
                "account": u.account,
                "symbol": u.symbol,
                "quantity": str(u.quantity),
                "as_of": u.as_of.isoformat(),
                # The symbol can never be a ticker: no recorded close values it.
                "unpriceable": u.unpriceable,
            },
        ):
            emitted += 1
    return emitted


@dataclass(frozen=True)
class _Answer:
    """What the provider answered for one window: its closes, or the failure
    (whatever the call raised, as a ``PriceFetchError``)."""

    start: date
    end: date
    closes: list[tuple[date, Decimal]] | None
    failure: PriceFetchError | None


def _ask(provider: PriceProvider, symbol: str, windows: list[core.Range]) -> list[_Answer]:
    """Ask ``provider`` for each of ``symbol``'s windows, newest first, with
    no database transaction open (#404). Whatever a call raises fails that
    window only (decision 89); only the exception's class is kept, since a
    message could carry request data."""
    answers: list[_Answer] = []
    for start, end in windows:
        try:
            answers.append(_Answer(start, end, provider.closes(symbol, start, end), None))
        except Exception as exc:  # isolation is per window (decision 89)
            failure = (
                exc
                if isinstance(exc, PriceFetchError)
                else PriceFetchError(symbol, None, type(exc).__name__)
            )
            answers.append(_Answer(start, end, None, failure))
    return answers


def _store_symbol(
    session: Session,
    provider: str,
    symbol: str,
    answers: list[_Answer],
    *,
    today: date,
    failed: list[dict[str, Any]],
) -> list[date]:
    """Store what the provider answered for ``symbol``'s windows, in the
    caller's transaction. Returns the dates of the closes inserted.

    Each window is stored and recorded on its own, in its own savepoint: one
    the provider refused, or whose store fails, is reported and asked again
    on the next run, and never costs the symbol its other windows. Every
    distinct failure is reported once in ``failed`` (and as a
    ``price.fetch_failed`` event once a day), and the closes dropped as absurd
    are reported too, summed over the windows, whatever else failed."""
    inserted_dates: list[date] = []
    dropped = 0
    reported: set[tuple[int | None, str]] = set()

    def report(failure: PriceFetchError, *, kind: str = "fetch") -> None:
        if (failure.status, failure.reason) in reported:
            return
        reported.add((failure.status, failure.reason))
        failed.append(_failure(failure))
        _fetch_failed(session, provider, failure, today, kind=kind)

    for answer in answers:
        start, end = answer.start, answer.end
        if answer.failure is not None:
            report(answer.failure)
            if answer.failure.status == 404 and core.settled(end, today):
                # The ticker is unknown there, for good for these dates.
                try:
                    with session.begin_nested():
                        _record_answered(session, symbol, provider, start, end)
                except SQLAlchemyError:
                    log.exception("price.store_failed", symbol=symbol)
                    report(PriceFetchError(symbol, None, "store failed"))
            continue
        closes = answer.closes or []
        try:
            with session.begin_nested():
                inserted = _insert_closes(session, symbol, provider, closes)
                # A "nothing" is final only for dates old enough to be
                # published; dates up to the newest close returned are final
                # too (the provider has published past them).
                newest = max((day for day, _close in closes), default=None)
                through = core.answered_through(start, end, newest, today)
                if through is not None:
                    _record_answered(session, symbol, provider, start, through)
        except SQLAlchemyError:
            log.exception("price.store_failed", symbol=symbol)
            report(PriceFetchError(symbol, None, "store failed"))
            continue
        inserted_dates.extend(inserted)
        dropped += getattr(answer.closes, "dropped", 0)
    if dropped:
        # The rest of each window is stored; the absurd rows are reported
        # once per symbol per day, beside any other failure of the symbol.
        report(
            PriceFetchError(symbol, 200, f"{dropped} close(s) out of range dropped"),
            kind="dropped",
        )
    return inserted_dates


def _commit_symbol(
    session: Session,
    provider: str,
    symbol: str,
    answers: list[_Answer],
    *,
    today: date,
    failed: list[dict[str, Any]],
    actor_token_id: uuid.UUID | None,
) -> list[date]:
    """One symbol's short write transaction (#404): its closes, the
    ``price.closes`` audit row recording them (committed together, #405), its
    failures, and — last, just before the commit, so the watermark is held
    for one statement and never across a provider call — net worth marked
    dirty from its earliest new close where a feed row valued from stored
    closes holds it (decision 92, ``overlay.first_revalued``)."""
    inserted = _store_symbol(session, provider, symbol, answers, today=today, failed=failed)
    if inserted:
        audit.record(
            session,
            actor_token_id=actor_token_id,
            action="price.closes",
            entity_type="price",
            after={
                "provider": provider,
                "symbols": {
                    symbol: {
                        "inserted": len(inserted),
                        "first": min(inserted).isoformat(),
                        "last": max(inserted).isoformat(),
                    }
                },
            },
        )
        dirty_from = overlay.first_revalued(session, {symbol: min(inserted)})
        if dirty_from is not None:
            balances.mark_dirty(session, dirty_from, networth_only=True)
    session.commit()
    return inserted


@dataclass
class FetchResult:
    provider: str
    skipped: str | None = None  # why nothing was fetched, when nothing was
    symbols: int = 0
    fetched: int = 0  # closes inserted
    failed: list[dict[str, Any]] = field(default_factory=list)
    accounts_projected: int = 0
    accounts_failed: int = 0
    stale: list[dict[str, str | None]] = field(default_factory=list)
    # Feed positions the feed values at 0 with no stored close (decision 92).
    unpriced: list[dict[str, str]] = field(default_factory=list)
    open_periods: list[str] = field(default_factory=list)


def _report_rolled_back(
    maker: Any,
    provider: str,
    symbol: str,
    answers: list[_Answer],
    *,
    today: date,
    failed: list[dict[str, Any]],
) -> None:
    """Report a symbol whose write transaction rolled back as a whole: its
    provider failures and "store failed", in a transaction of their own
    (the rolled-back one took its ``price.fetch_failed`` events with it).
    Never raises: the run goes on to the next symbol."""
    failures = [a.failure for a in answers if a.failure is not None]
    failures.append(PriceFetchError(symbol, None, "store failed"))
    seen: set[tuple[int | None, str]] = set()
    unique = []
    for failure in failures:
        if (failure.status, failure.reason) not in seen:
            seen.add((failure.status, failure.reason))
            unique.append(failure)
    failed.extend(_failure(f) for f in unique)
    try:
        with maker() as session:
            for failure in unique:
                _fetch_failed(session, provider, failure, today)
            session.commit()
    except Exception:
        log.exception("price.report_failed", symbol=symbol)


def run_fetch(
    *,
    today: date | None = None,
    client: httpx.Client | None = None,
    actor_token_id: uuid.UUID | None = None,
) -> FetchResult:
    """The daily price job's body, also run by an admin's fetch-now.

    1. Read what each symbol needs (``fetch_needs``, the answered ranges) and
       end that transaction. Then, one symbol at a time, ask the provider
       through one shared HTTP client with no transaction open, and store
       the answers in that symbol's own short transaction
       (``_commit_symbol``, #404): no connection sits idle in a transaction
       across the provider calls, and the watermark is never held across
       one. A failing ticker records ``price.fetch_failed`` and never blocks
       the others; a symbol whose store fails as a whole is rolled back and
       reported, and the run goes on.
    2. Re-project every manual account, each in its own transaction. Only
       rows that differ are rewritten, so this also picks up feed-implied
       prices and heals any account a missed trigger left behind.
    3. Emit ``price.stale`` for each symbol that has turned stale.
       ``holding.unpriced`` for a feed position nothing values, and
       ``holding.period_open`` for an account whose manual period no
       fetch has ended (#422).

    The run is audited as one ``price.fetch`` row (``connection.sync``'s
    shape): what was fetched, what failed, what turned stale. The closes
    themselves are audited in the transaction that stores them
    (``price.closes``, one row per symbol, #405): the summary comes after
    the per-account projections, so a run that dies before it still leaves
    every stored close audited.
    """
    from purser.db import get_sessionmaker

    today = today or clock.ledger_today()
    settings = appsettings.effective()
    result = FetchResult(provider=settings.price_provider)
    maker = get_sessionmaker()

    shared = client if client is not None else httpx.Client(timeout=TiingoProvider.TIMEOUT)
    try:
        with maker() as session:
            needs = fetch_needs(session, today=today)
            result.symbols = len(needs)
            provider, reason = configured_provider(client=shared)
            result.skipped = reason
            windows: dict[str, list[core.Range]] = {}
            if provider is not None:
                answered = _answered(session, provider.name, sorted(needs))
                windows = {
                    symbol: core.fetch_windows(needs[symbol], answered.get(symbol, []))
                    for symbol in sorted(needs)
                }
            session.rollback()  # read only: end the transaction before any provider call

        if provider is not None:
            for symbol, symbol_windows in windows.items():
                answers = _ask(provider, symbol, symbol_windows)
                if not answers:
                    continue
                failed_before = len(result.failed)
                try:
                    with maker() as session:
                        inserted = _commit_symbol(
                            session,
                            provider.name,
                            symbol,
                            answers,
                            today=today,
                            failed=result.failed,
                            actor_token_id=actor_token_id,
                        )
                except Exception:
                    # The whole symbol rolled back: nothing of it is stored,
                    # and it is asked again on the next run.
                    log.exception("price.store_failed", symbol=symbol)
                    del result.failed[failed_before:]
                    _report_rolled_back(
                        maker, provider.name, symbol, answers, today=today, failed=result.failed
                    )
                    continue
                result.fetched += len(inserted)
    finally:
        if client is None:
            shared.close()

    with maker() as session:
        account_ids = projection.manual_accounts_with_positions(session)
    for account_id in account_ids:
        try:
            with maker() as session:
                account = projection.lock_account(session, account_id)
                projection.project_account(session, account, today=today)
                session.commit()
            result.accounts_projected += 1
        except Exception:
            result.accounts_failed += 1
            log.exception("price.projection_failed", account_id=str(account_id))

    with maker() as session:
        stale = stale_symbols(
            watched_books(session, today=today),
            today=today,
            stale_days=settings.price_stale_days,
        )
        emit_stale(session, stale, stale_days=settings.price_stale_days)
        result.stale = [
            {"symbol": symbol, "price_as_of": as_of.isoformat() if as_of is not None else None}
            for symbol, as_of in stale
        ]
        unpriced = overlay.unpriced_holdings(session, today=today)
        emit_unpriced(session, unpriced)
        result.unpriced = [{"account": u.account, "symbol": u.symbol} for u in unpriced]
        from purser.holdings import service as holdings_service

        open_periods = holdings_service.stale_open_periods(
            session, stale_days=settings.price_stale_days, now=datetime.now(UTC)
        )
        emit_period_open(session, open_periods, stale_days=settings.price_stale_days)
        result.open_periods = [p.account for p in open_periods]
        audit.record(
            session,
            actor_token_id=actor_token_id,
            action="price.fetch",
            entity_type="price",
            after={
                "provider": result.provider,
                "skipped": result.skipped,
                "symbols": result.symbols,
                "fetched": result.fetched,
                "failed": result.failed,
                "accounts_projected": result.accounts_projected,
                "accounts_failed": result.accounts_failed,
                "stale": result.stale,
                "unpriced": result.unpriced,
                "open_periods": result.open_periods,
            },
        )
        session.commit()
    return result


def fetch_daily() -> None:
    """Scheduled job (06:40 UTC): ``run_fetch``, then the net-worth
    watermark the projections lowered (the 07:30 materialization would
    consume it too; consuming here keeps 06:40-07:30 reads off the live
    path)."""
    from purser import networth

    result = run_fetch()
    log.info(
        "price fetch",
        provider=result.provider,
        symbols=result.symbols,
        fetched=result.fetched,
        failed=len(result.failed),
        accounts_projected=result.accounts_projected,
        stale=len(result.stale),
    )
    networth.consume_watermark()


@dataclass(frozen=True)
class SymbolPrice:
    symbol: str
    close: Decimal | None
    price_as_of: date | None
    source: str | None
    stale: bool


def status(session: Session, *, today: date | None = None) -> dict[str, Any]:
    """What Settings, Prices shows: the provider, the newest provider fetch,
    each open symbol's close in effect today, and the last failure."""
    today = today or clock.ledger_today()
    settings = appsettings.effective()
    books = watched_books(session, today=today)
    symbols = sorted(books)
    rows: list[SymbolPrice] = []
    for symbol in symbols:
        book = books.get(symbol)
        point = book.on(today) if book is not None else None
        rows.append(
            SymbolPrice(
                symbol=symbol,
                close=point.close if point is not None else None,
                price_as_of=point.price_date if point is not None else None,
                source=point.source if point is not None else None,
                stale=point is None
                or core.stale(point.price_date, today, settings.price_stale_days),
            )
        )
    last_fetched: datetime | None = session.execute(
        select(func.max(SecurityPrice.fetched_at)).where(SecurityPrice.source != core.SOURCE_MANUAL)
    ).scalar_one_or_none()
    failure = session.execute(
        select(Event).where(Event.type == FETCH_FAILED_EVENT).order_by(Event.at.desc()).limit(1)
    ).scalar_one_or_none()
    return {
        "provider": settings.price_provider,
        "stale_days": settings.price_stale_days,
        "last_fetched_at": last_fetched,
        "symbols": rows,
        "last_failure": (
            {
                "at": failure.at,
                "symbol": failure.payload.get("symbol"),
                "status": failure.payload.get("status"),
                "reason": failure.payload.get("reason"),
            }
            if failure is not None
            else None
        ),
    }
