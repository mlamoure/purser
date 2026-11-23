"""Net worth engine (DESIGN §8).

Net worth at time T = sum of postings <= T (cash-flow accounts, incl.
brokerage cash) + latest holdings snapshot <= T (brokerage market value)
+ latest valuation <= T (loans). A hand-valued asset (property) is a ledger
whose value is its postings (decision 113). Categories never count.
Materialized nightly into net_worth_daily.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date as date_type
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, NamedTuple

from sqlalchemy import and_, case, func, literal_column, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, aliased

from purser import audit, balances, clock, events, reconciliation
from purser import ledger as ledger_service
from purser.core import ledger as core
from purser.core import sampling
from purser.holdings import overlay
from purser.holdings.overlay import Holding
from purser.ledger import LedgerError
from purser.models import (
    Account,
    HoldingsSnapshot,
    NetWorthDaily,
    Posting,
    Transaction,
    Valuation,
)

# Accounts valued by their latest valuation row rather than postings: loans,
# whose feed reports a balance and nothing else. A hand-valued asset
# (property) left this set in decision 113: its value is its postings, and a
# change in value is an entry against equity.revaluation (purser.assets).
VALUATION_TYPES = {"loan"}

# The newest valuation first: by date, then by when it was recorded, so of two
# rows on one date the later recorded wins (#565). The id only breaks a tie
# between rows recorded in the same instant. Every reader orders with this.
VALUATION_NEWEST_FIRST = (Valuation.as_of.desc(), Valuation.created_at.desc(), Valuation.id.desc())

# The longest window a daily net-worth series spans: ten years. A day not
# already stored is a full per-account valuation computed live, so the bound
# keeps one read from computing thousands of them. Shared by series() and by
# the named `all` window of the net_worth tool and its resource (#343).
SERIES_CAP_DAYS = 3660

# How the sign of a valuation's figure reads (decision 71). A valuation is
# STORED in ledger sign — a liability is negative while money is owed, the
# sign SimpleFIN reports (§7.3) and every reader of valuations assumes. A
# caller may instead give the figure as the statement prints it, which is
# converted exactly as reconciliation converts a statement balance (§3.7).
CONVENTION_LEDGER = "ledger"
CONVENTION_STATEMENT = "statement"
CONVENTIONS = (CONVENTION_LEDGER, CONVENTION_STATEMENT)

# What a valuation's NUMERIC(20,4) column holds exactly.
_MAX_ABS = Decimal("1e16")


def _storable_value(value: Decimal) -> Decimal:
    """``value`` when the valuations column holds it exactly, else LedgerError:
    Postgres would round extra decimal places away silently. REST's request
    model refuses these first; MCP ``record_valuation`` relies on this check
    (decision 73)."""
    try:
        if not value.is_finite() or abs(value) >= _MAX_ABS:
            raise LedgerError("value is out of range")
        if value != value.quantize(core.QUANT):
            raise LedgerError("value has more than 4 decimal places")
    except InvalidOperation as exc:
        raise LedgerError("value is out of range") from exc
    return value


def ledger_signed_value(account: Account, value: Decimal, convention: str) -> Decimal:
    """``value`` for ``account`` in ledger sign, or LedgerError when its sign
    cannot be trusted.

    In the ledger convention, a positive figure on a liability would make the
    debt an asset: in net worth, on every balance surface, and in the
    pay-in-full schedule basis, which counts only a negative balance as owed.
    Such a figure is far likelier an amount owed typed the way the statement
    prints it, so it is refused with both ways to say it, never guessed."""
    if convention == CONVENTION_STATEMENT:
        return reconciliation.to_ledger_sign(account.type, value)
    if convention != CONVENTION_LEDGER:
        raise LedgerError(f"convention must be one of: {', '.join(CONVENTIONS)}")
    if account.type in reconciliation.LIABILITY_TYPES and value > 0:
        raise LedgerError(
            f"{account.path} is a {account.type} account: a positive valuation would "
            "make it an asset. Valuations are in ledger sign, where an amount owed is "
            f"negative. Send {-value}, or send {value} with convention 'statement' to "
            "give the figure as the statement shows it."
        )
    return value


def _not_valued(account: Account) -> str:
    """Why ``account`` takes no valuation, and where its balance comes from
    instead (decision 81). Every reader of valuations reads loan accounts
    only, so a valuation on any other type would be stored, audited and
    announced, and then never read."""
    if account.type == "property":
        return (
            f"{account.path} is a hand-valued asset: its value is a ledger, changed by "
            "entries. Record a change (MCP record_value_change, or POST "
            "/accounts/{id}/value-changes) or its value on a date (MCP reconcile_account, "
            "or POST /accounts/{id}/reconcile)."
        )
    if account.type == "brokerage":
        source = (
            "its transactions plus the market value of its latest holdings snapshot, "
            "which the feed records"
        )
    else:
        source = (
            "its transactions. To bring it to a statement's balance, reconcile it "
            "(MCP reconcile_account, or POST /accounts/{id}/reconcile)"
        )
    return (
        f"{account.path} is a {account.type} account: valuations apply only to loan "
        f"accounts. Its balance comes from {source}."
    )


def record_valuation(
    session: Session,
    *,
    account_ref: str,
    as_of: date_type,
    value: Decimal,
    source: str = "manual",
    convention: str = CONVENTION_LEDGER,
    actor_token_id: uuid.UUID | None,
) -> Valuation:
    """Record ``account_ref``'s value on ``as_of``. Only a loan account takes
    one (decisions 81, 113). ``value`` is in ledger sign unless
    ``convention`` is ``statement`` (decision 71); the stored row, the audit
    entry and the event all carry the ledger-sign figure."""
    account = ledger_service.resolve_account(session, account_ref)
    if account.is_virtual:
        raise LedgerError("valuations attach to real accounts")
    if account.type not in VALUATION_TYPES:
        raise LedgerError(_not_valued(account))
    if source not in ("manual", "statement", "api"):
        raise LedgerError("source must be manual | statement | api")
    value = ledger_signed_value(account, _storable_value(value), convention)
    valuation = Valuation(account_id=account.id, as_of=as_of, value=value, source=source)
    session.add(valuation)
    session.flush()
    balances.mark_dirty(session, as_of, networth_only=True)
    audit.record(
        session,
        actor_token_id=actor_token_id,
        action="valuation.create",
        entity_type="valuation",
        entity_id=valuation.id,
        after={"account_id": str(account.id), "as_of": as_of.isoformat(), "value": str(value)},
    )
    events.emit(
        session,
        type_="valuation.recorded",
        entity_type="account",
        entity_id=account.id,
        payload={"as_of": as_of.isoformat(), "value": str(value), "source": source},
    )
    return valuation


def list_valuations(session: Session, account_id: uuid.UUID) -> list[Valuation]:
    return list(
        session.execute(
            select(Valuation)
            .where(Valuation.account_id == account_id)
            .order_by(*VALUATION_NEWEST_FIRST)
        ).scalars()
    )


def holdings_set_on(
    session: Session, account_id: uuid.UUID, as_of: date_type | None = None
) -> list[HoldingsSnapshot]:
    """The snapshot set in effect at ``as_of`` (default: the ledger day,
    ``clock.ledger_today``): every row of the account's newest snapshot date
    at or before it, zero rows included. Never a later set: a hand-tracked
    sale whose cash lands after today has its landing day's set written
    ahead (#498 review), and it must not show before that day."""
    if as_of is None:
        as_of = clock.ledger_today()
    stmt = select(func.max(HoldingsSnapshot.as_of)).where(
        HoldingsSnapshot.account_id == account_id, HoldingsSnapshot.as_of <= as_of
    )
    latest_date = session.execute(stmt).scalar_one_or_none()
    if latest_date is None:
        return []
    return list(
        session.execute(
            select(HoldingsSnapshot)
            .where(
                HoldingsSnapshot.account_id == account_id,
                HoldingsSnapshot.as_of == latest_date,
            )
            .order_by(HoldingsSnapshot.symbol)
        ).scalars()
    )


def latest_holdings(
    session: Session, account_id: uuid.UUID, as_of: date_type | None = None
) -> list[Holding]:
    """The most recent snapshot set (one as_of date) at or before as_of, as
    the readers see it: a feed row the feed values at 0 is valued from a
    stored close, or has no price (decision 92, ``holdings.overlay``).

    A manual set's zero rows are left out: the tombstones of a position sold
    out that day, or ended with a manual period because the feed does not
    report it (decision 88). They keep an older set from resurfacing but are
    not holdings. A zero-share manual row that carries a sale's cash still in
    transit is kept: it is worth that cash (decision 104). A feed row is
    returned as the bank sent it, zero shares included, valued on read when
    the feed values it at 0 (decision 92)."""
    return overlay.as_held(
        session,
        [
            h
            for h in holdings_set_on(session, account_id, as_of)
            if not (h.source == "manual" and h.quantity == 0 and not h.in_transit)
        ],
    )


def valuation_balances(
    session: Session,
    account_ids: list[uuid.UUID] | None = None,
    as_of: date_type | None = None,
) -> dict[uuid.UUID, tuple[Decimal, date_type]]:
    """Presentation overlay for account surfaces (DESIGN §8, decision 42):
    the latest valuation at or before ``as_of`` (default the ledger day) per
    OPEN loan account, as {account_id: (value, valuation_as_of)}.

    Postings stay the ledger truth — ``ledger.account_balances()`` and the
    running-balance store are untouched (parity probe and close_account
    depend on postings-sum semantics); surfaces overlay this map on top.
    Accounts at/past their close date are omitted so stale valuations never
    resurrect a closed account (same cutoff rule as ``net_worth_at``).
    """
    effective = as_of or clock.ledger_today()
    stmt = (
        select(Valuation.account_id, Valuation.value, Valuation.as_of, Account.closed_at)
        .distinct(Valuation.account_id)
        .join(Account, Account.id == Valuation.account_id)
        .where(Account.type.in_(VALUATION_TYPES), Valuation.as_of <= effective)
        .order_by(Valuation.account_id, *VALUATION_NEWEST_FIRST)
    )
    if account_ids is not None:
        stmt = stmt.where(Valuation.account_id.in_(account_ids))
    overlay: dict[uuid.UUID, tuple[Decimal, date_type]] = {}
    for account_id, value, valued_as_of, closed_at in session.execute(stmt):
        if closed_at is not None and effective >= clock.ledger_date(closed_at):
            continue
        overlay[account_id] = (Decimal(value), valued_as_of)
    return overlay


def _set_market_value(h: Any) -> Any:
    """The market value of one snapshot set, as an aggregate over its rows
    (``h`` must be the ``HoldingsSnapshot`` alias named ``h``, which
    ``overlay.FEED_VALUE_SQL`` refers to). A feed row with no stored value
    adds what a stored close makes it (decision 92), folded into the one
    aggregate (evaluated only for those rows), so a set costs no extra round
    trip. Shared by :func:`holdings_addends` and :func:`net_worth_on`, so
    the account surfaces and net worth value a set one way."""
    return func.coalesce(
        func.sum(
            case(
                (
                    and_(
                        h.source == "feed",
                        h.quantity > 0,
                        or_(h.market_value.is_(None), h.market_value == 0),
                    ),
                    literal_column(overlay.FEED_VALUE_SQL),
                ),
                else_=h.market_value,
            )
        ),
        0,
    )


def holdings_addends(
    session: Session,
    account_ids: list[uuid.UUID] | None = None,
    as_of: date_type | None = None,
) -> dict[uuid.UUID, tuple[Decimal, date_type]]:
    """Presentation overlay for account surfaces (DESIGN §8, decision 50):
    the market value of the latest holdings snapshot at or before ``as_of``
    (default the ledger day) per OPEN brokerage account, as
    {account_id: (market_value, snapshot_as_of)}.

    This is an ADDEND, not a balance: brokerage cash lives in postings, so
    every surface adds this on top of *its own* postings basis
    (``ledger.account_balances()`` for the listing surfaces,
    ``balances.balance_asof(as_of)`` for the point-in-time ones). Returning a
    total instead would force one basis on all of them.

    Sibling of :func:`valuation_balances`, and the same cutoff rule: accounts
    at/past their close date are omitted so a stale snapshot never resurrects
    a closed account. Zero-valued snapshots are omitted too — an account with
    nothing to add reports plain ``postings``.
    """
    effective = as_of or clock.ledger_today()
    latest = (
        select(
            HoldingsSnapshot.account_id.label("account_id"),
            func.max(HoldingsSnapshot.as_of).label("as_of"),
        )
        .where(HoldingsSnapshot.as_of <= effective)
        .group_by(HoldingsSnapshot.account_id)
    )
    if account_ids is not None:
        latest = latest.where(HoldingsSnapshot.account_id.in_(account_ids))
    latest_cte = latest.cte("latest_holdings")
    h = aliased(HoldingsSnapshot, name="h")
    stmt = (
        select(
            h.account_id,
            _set_market_value(h),
            latest_cte.c.as_of,
            Account.closed_at,
        )
        .select_from(h)
        .join(
            latest_cte,
            (latest_cte.c.account_id == h.account_id) & (latest_cte.c.as_of == h.as_of),
        )
        .join(Account, Account.id == h.account_id)
        .where(Account.type == "brokerage")
        .group_by(h.account_id, latest_cte.c.as_of, Account.closed_at)
    )
    addends: dict[uuid.UUID, tuple[Decimal, date_type]] = {}
    for account_id, market_value, snapshot_as_of, closed_at in session.execute(stmt):
        if closed_at is not None and effective >= clock.ledger_date(closed_at):
            continue
        market = Decimal(market_value)
        if market == 0:
            continue
        addends[account_id] = (market, snapshot_as_of)
    return addends


class ReportedBalance(NamedTuple):
    """What an account surface shows: ``balance`` with the ``method`` that
    produced it, the overlay's ``as_of`` (None when postings alone), and the
    ``postings`` basis riding alongside (decision 42's ``postings_balance``)."""

    balance: Decimal
    method: str
    as_of: date_type | None
    postings: Decimal


def _apply_overlays(
    account_id: uuid.UUID,
    posted: Decimal,
    valuations: dict[uuid.UUID, tuple[Decimal, date_type]],
    markets: dict[uuid.UUID, tuple[Decimal, date_type]],
) -> ReportedBalance:
    """The overlay rule, in one place: a valuation REPLACES the postings
    basis (decision 42), a holdings market value ADDS to it (decision 50),
    and everything else is the basis alone."""
    if account_id in valuations:
        value, valued_as_of = valuations[account_id]
        return ReportedBalance(value, "valuation", valued_as_of, posted)
    if account_id in markets:
        addend, snapshot_as_of = markets[account_id]
        return ReportedBalance(posted + addend, "postings+holdings", snapshot_as_of, posted)
    return ReportedBalance(posted, "postings", None, posted)


def reported_balances(
    session: Session,
    account_ids: list[uuid.UUID] | None = None,
    as_of: date_type | None = None,
) -> dict[uuid.UUID, ReportedBalance]:
    """Reported balances for many accounts off the LIVE postings basis
    (``ledger.account_balances`` — every pending/posted row regardless of
    date), with both overlays applied. This is what the account listing, the
    register headline, ``describe``/``list_accounts`` and the forecast's
    opening balance all show, so they agree by construction.

    Contrast :func:`account_balance_at`, whose basis is the STORED running
    balance at a date. The two bases differ deliberately (DESIGN §3.5): this
    one includes future-dated rows, that one stops at ``as_of``.
    """
    postings = ledger_service.account_balances(session, account_ids)
    valuations = valuation_balances(session, account_ids, as_of=as_of)
    markets = holdings_addends(session, account_ids, as_of=as_of)
    # Union, not just the postings keys: a balance-only mortgage or a
    # holdings-only brokerage has no postings row at all and must still
    # report its overlay.
    ids = account_ids if account_ids is not None else list({*postings, *valuations, *markets})
    return {
        account_id: _apply_overlays(
            account_id, postings.get(account_id, Decimal(0)), valuations, markets
        )
        for account_id in ids
    }


def account_balance_at(
    session: Session, account: Account, as_of: date_type
) -> tuple[Decimal, str, date_type | None]:
    """One real account's REPORTED balance at ``as_of``, as
    ``(balance, method, overlay_as_of)`` — the single implementation behind
    ``GET /accounts/{id}/balance``, MCP ``account_balance`` and the
    per-account balance series.

    Open loans answer with their latest valuation (decision 42); open
    brokerage adds its latest holdings market value to the postings basis
    (decision 50); everything else is the stored running balance alone
    (decision 37 register semantics — chain heads, pending included, never
    recomputed). ``overlay_as_of`` is the valuation/snapshot date, or None
    when the answer is postings alone.
    """
    reported = _apply_overlays(
        account.id,
        balances.balance_asof(session, account.id, as_of),
        valuation_balances(session, [account.id], as_of=as_of),
        holdings_addends(session, [account.id], as_of=as_of),
    )
    return reported.balance, reported.method, reported.as_of


# Same LATERAL last-row-<=-d shape as balances._SERIES, for the two overlay
# sources. One indexed seek per requested date instead of a query per date.
_VALUATION_SERIES = text(
    """
SELECT d::date AS as_of, v.value, v.as_of AS valued_as_of
FROM unnest(CAST(:dates AS date[])) AS d
LEFT JOIN LATERAL (
    SELECT value, as_of FROM valuations
    WHERE account_id = CAST(:a AS UUID) AND as_of <= d
    ORDER BY as_of DESC, created_at DESC, id DESC LIMIT 1  -- VALUATION_NEWEST_FIRST
) v ON true
ORDER BY d
"""
)

# GROUP BY as_of then LIMIT 1 = the total of the newest snapshot SET at or
# before d (a snapshot is many symbol rows sharing one as_of).
_HOLDINGS_SERIES = text(
    """
SELECT d::date AS as_of, h.market, h.snapshot_as_of
FROM unnest(CAST(:dates AS date[])) AS d
LEFT JOIN LATERAL (
    SELECT COALESCE(SUM(market_value), 0) AS market, as_of AS snapshot_as_of
    FROM holdings_snapshots
    WHERE account_id = CAST(:a AS UUID) AND as_of <= d
    GROUP BY as_of
    ORDER BY as_of DESC LIMIT 1
) h ON true
ORDER BY d
"""
)


def account_balance_series(
    session: Session, account: Account, *, dates: Sequence[date_type]
) -> tuple[dict[date_type, Decimal], str]:
    """One account's reported balance on each requested date, as
    ``({date: balance}, method)`` — the series counterpart of
    :func:`account_balance_at`, and the single source behind MCP
    ``balance_history``'s series and extremes and the admin chart
    (``GET /accounts/{id}/balance/history``). The per-change series
    (``/balance/series`` and its ``purser://`` resource) is the postings
    history instead (``balances.change_series``, decision 129).

    Branches ONCE on account type rather than per date, because an account
    does not change how it is valued mid-window:

    - loan -> the valuations series (``valuation``); the running
      balance store is not consulted at all
    - brokerage -> the running balance plus that date's holdings market value
      (``postings+holdings``)
    - everything else -> the running balance alone (``postings``)

    The closed-account cutoff applies per DATE, not per account: history
    before the close date keeps its overlay, and from the close date onward
    the account answers from postings (which closing zeroed), so a stale
    valuation or snapshot can never resurrect it mid-series. An overlay
    account with no valuation/snapshot yet falls back to postings for those
    dates, exactly as the point-in-time surfaces do.
    """
    if not dates:
        return {}, "postings"
    postings = balances.balance_series(session, account.id, dates=dates)
    closed_on = clock.ledger_date(account.closed_at) if account.closed_at is not None else None

    def open_on(day: date_type) -> bool:
        return closed_on is None or day < closed_on

    params = {"a": str(account.id), "dates": list(dates)}
    if account.type in VALUATION_TYPES:
        out: dict[date_type, Decimal] = {}
        for day, value, _valued_as_of in session.execute(_VALUATION_SERIES, params):
            out[day] = (
                Decimal(value)
                if value is not None and open_on(day)
                else postings.get(day, Decimal(0))
            )
        return out, "valuation"

    if account.type == "brokerage":
        rows = list(session.execute(_HOLDINGS_SERIES, params))
        # The feed rows a feed values at 0, in the sets these dates read, add
        # what a stored close makes them (decision 92).
        set_dates = sorted({snapshot_as_of for _d, _m, snapshot_as_of in rows if snapshot_as_of})
        valued = (
            overlay.overlay_totals(
                session,
                session.execute(
                    select(
                        HoldingsSnapshot.account_id,
                        HoldingsSnapshot.as_of,
                        HoldingsSnapshot.symbol,
                        HoldingsSnapshot.quantity,
                    ).where(
                        HoldingsSnapshot.account_id == account.id,
                        HoldingsSnapshot.as_of.in_(set_dates),
                        overlay.zero_valued_clause(),
                    )
                ),
            )
            if set_dates
            else {}
        )
        series: dict[date_type, Decimal] = {}
        for day, market, snapshot_as_of in rows:
            base = postings.get(day, Decimal(0))
            if market is None or not open_on(day):
                series[day] = base
                continue
            series[day] = (
                base + Decimal(market) + valued.get((account.id, snapshot_as_of), Decimal(0))
            )
        return series, "postings+holdings"

    return postings, "postings"


# The days in [s, e] on which an overlay account's reported balance can move:
# a running-balance row (its postings basis), and a holdings snapshot set for
# brokerage or a valuation for a loan. A snapshot is valued at its own as_of
# (decision 92: a later close never re-values an older set), so between two of
# these days the reported balance is flat.
_OVERLAY_ACTIVITY = {
    "brokerage": text(
        """
SELECT date FROM account_running_balances
WHERE account_id = CAST(:a AS UUID) AND date >= :s AND date <= :e
UNION
SELECT as_of FROM holdings_snapshots
WHERE account_id = CAST(:a AS UUID) AND as_of >= :s AND as_of <= :e
ORDER BY 1
"""
    ),
    "loan": text(
        """
SELECT date FROM account_running_balances
WHERE account_id = CAST(:a AS UUID) AND date >= :s AND date <= :e
UNION
SELECT as_of FROM valuations
WHERE account_id = CAST(:a AS UUID) AND as_of >= :s AND as_of <= :e
ORDER BY 1
"""
    ),
}

Extreme = tuple[date_type, Decimal]


def account_window_extremes(
    session: Session, account: Account, *, start: date_type, end: date_type
) -> tuple[Extreme | None, Extreme | None]:
    """The lowest and highest END-OF-DAY reported balance over EVERY day
    with activity in ``[start, end]``, as ``((date, balance) | None,
    (date, balance) | None)`` — on the basis :func:`account_balance_series`
    reports (decision 129), earliest date on a tie, ``None`` when the window
    holds no activity.

    A postings account ranks its running-balance days
    (``balances.window_extremes``, decision 63). A brokerage or loan account's
    value also moves on days with no posting — a new holdings snapshot, a new
    valuation — and from its close date the account answers from postings, so
    those days are ranked too, each valued by :func:`account_balance_series`.
    Every such day, not a sample, so downsampling never hides an extreme."""
    if account.type not in _OVERLAY_ACTIVITY:
        low_row, high_row = balances.window_extremes(session, account.id, start=start, end=end)
        return (
            (low_row.date, Decimal(low_row.balance)) if low_row is not None else None,
            (high_row.date, Decimal(high_row.balance)) if high_row is not None else None,
        )
    days = set(
        session.execute(
            _OVERLAY_ACTIVITY[account.type], {"a": str(account.id), "s": start, "e": end}
        ).scalars()
    )
    if account.closed_at is not None:
        closed_on = clock.ledger_date(account.closed_at)
        if start <= closed_on <= end:
            days.add(closed_on)
    if not days:
        return None, None
    ordered = sorted(days)
    series, _method = account_balance_series(session, account, dates=ordered)
    low = min(ordered, key=lambda d: (series[d], d))
    high = min(ordered, key=lambda d: (-series[d], d))
    return (low, series[low]), (high, series[high])


def net_worth_at(session: Session, as_of: date_type) -> dict[str, Any]:
    """Net worth on one day, with its per-account breakdown — the single-date
    form of :func:`net_worth_on`."""
    return net_worth_on(session, [as_of])[as_of]


def _postings_by_day(
    session: Session, first: date_type, last: date_type
) -> dict[uuid.UUID, list[tuple[date_type, Decimal]]]:
    """Every real account's pending/posted posting sums by date through
    ``last``, oldest first, with everything dated before ``first`` folded
    into ``first`` — so a running sum read at any day >= ``first`` is the
    account's sum over all its rows dated on or before that day. Every row,
    not chain heads: net worth is a sum over all rows (decision 37's
    registered divergence from the running-balance store). One statement for
    every account and every day."""
    # Grouped by the output column, not a second copy of the expression: each
    # copy would carry its own bind parameter, and Postgres groups by text.
    bucket: Any = literal_column("bucket")
    rows = session.execute(
        select(
            Posting.account_id,
            func.greatest(Transaction.date, first).label("bucket"),
            func.sum(Posting.amount),
        )
        .join(Transaction, Transaction.id == Posting.transaction_id)
        .join(Account, Account.id == Posting.account_id)
        .where(
            Account.is_virtual.is_(False),
            Transaction.status.in_(["pending", "posted"]),
            Transaction.date <= last,
        )
        .group_by(Posting.account_id, bucket)
        .order_by(Posting.account_id, bucket)
    )
    out: dict[uuid.UUID, list[tuple[date_type, Decimal]]] = {}
    for account_id, day, total in rows:
        out.setdefault(account_id, []).append((day, Decimal(total)))
    return out


def _valuations_by_day(
    session: Session, last: date_type
) -> dict[uuid.UUID, list[tuple[date_type, Decimal]]]:
    """Every valuation of a valued account through ``last``, in
    :data:`VALUATION_NEWEST_FIRST` reversed, so the last one at or before a
    day is the one that day reads (of two on one date, the later recorded)."""
    rows = session.execute(
        select(Valuation.account_id, Valuation.as_of, Valuation.value)
        .join(Account, Account.id == Valuation.account_id)
        .where(Account.type.in_(VALUATION_TYPES), Valuation.as_of <= last)
        .order_by(Valuation.account_id, Valuation.as_of, Valuation.created_at, Valuation.id)
    )
    out: dict[uuid.UUID, list[tuple[date_type, Decimal]]] = {}
    for account_id, as_of, value in rows:
        out.setdefault(account_id, []).append((as_of, Decimal(value)))
    return out


def _holdings_by_day(
    session: Session, first: date_type, last: date_type
) -> dict[uuid.UUID, list[tuple[date_type, Decimal]]]:
    """Every brokerage snapshot set's market value through ``last``, oldest
    first, by :func:`_set_market_value` — from the set in effect on
    ``first`` on (older sets can never be read). One statement."""
    floors = (
        select(
            HoldingsSnapshot.account_id.label("account_id"),
            func.max(HoldingsSnapshot.as_of).label("floor"),
        )
        .where(HoldingsSnapshot.as_of <= first)
        .group_by(HoldingsSnapshot.account_id)
        .cte("holdings_floors")
    )
    h = aliased(HoldingsSnapshot, name="h")
    rows = session.execute(
        select(h.account_id, h.as_of, _set_market_value(h))
        .select_from(h)
        .join(Account, Account.id == h.account_id)
        .outerjoin(floors, floors.c.account_id == h.account_id)
        .where(
            Account.type == "brokerage",
            h.as_of <= last,
            or_(floors.c.floor.is_(None), h.as_of >= floors.c.floor),
        )
        .group_by(h.account_id, h.as_of)
        .order_by(h.account_id, h.as_of)
    )
    out: dict[uuid.UUID, list[tuple[date_type, Decimal]]] = {}
    for account_id, as_of, market in rows:
        out.setdefault(account_id, []).append((as_of, Decimal(market)))
    return out


class _Cursor:
    """The newest entry at or before a day, walking days in ascending order
    over one account's ``(date, value)`` list (oldest first)."""

    __slots__ = ("_entries", "_next", "current")

    def __init__(self, entries: list[tuple[date_type, Decimal]]) -> None:
        self._entries = entries
        self._next = 0
        self.current: Decimal | None = None

    def at(self, day: date_type) -> Decimal | None:
        while self._next < len(self._entries) and self._entries[self._next][0] <= day:
            self.current = self._entries[self._next][1]
            self._next += 1
        return self.current


class _RunningSum(_Cursor):
    """The sum of every entry at or before a day (0 before the first)."""

    __slots__ = ("total",)

    def __init__(self, entries: list[tuple[date_type, Decimal]]) -> None:
        super().__init__(entries)
        self.total = Decimal(0)

    def at(self, day: date_type) -> Decimal:
        while self._next < len(self._entries) and self._entries[self._next][0] <= day:
            self.total += self._entries[self._next][1]
            self._next += 1
        return self.total


def net_worth_on(session: Session, dates: Sequence[date_type]) -> dict[date_type, dict[str, Any]]:
    """Net worth on each of ``dates``, as ``{date: {as_of, assets,
    liabilities, net_worth, breakdown}}`` (DESIGN §8).

    Per real account and day: a loan reads its latest valuation at or before
    the day (its postings when it has none yet); every other account its
    postings sum over all rows dated on or before the day, plus, for a
    brokerage account, the market value of its latest snapshot set when that
    is not zero (the same rule as :func:`holdings_addends`). A closed account
    from its close date on contributes its postings alone, which closing
    zeroed; before the close date it is untouched (§3.4).

    Set-based (#610): three statements read every posting sum, valuation and
    snapshot set the dates need, for every account at once, and the days are
    then walked in order — never a query per account per day, so
    re-materializing years of history after an import costs a few
    statements, not one per account per day."""
    if not dates:
        return {}
    days = sorted(set(dates))
    first, last = days[0], days[-1]
    accounts = list(
        session.execute(
            select(Account).where(Account.is_virtual.is_(False)).order_by(Account.path)
        ).scalars()
    )
    postings = _postings_by_day(session, first, last)
    valuations = (
        _valuations_by_day(session, last)
        if any(a.type in VALUATION_TYPES for a in accounts)
        else {}
    )
    holdings = (
        _holdings_by_day(session, first, last)
        if any(a.type == "brokerage" for a in accounts)
        else {}
    )

    out: dict[date_type, dict[str, Any]] = {
        day: {
            "as_of": day,
            "assets": Decimal(0),
            "liabilities": Decimal(0),
            "net_worth": Decimal(0),
            "breakdown": [],
        }
        for day in days
    }
    for account in accounts:
        closed_on = clock.ledger_date(account.closed_at) if account.closed_at is not None else None
        basis = _RunningSum(postings.get(account.id, []))
        valued = _Cursor(valuations.get(account.id, []))
        market = _Cursor(holdings.get(account.id, []))
        for day in days:
            # A closed account carries nothing into net worth from its close
            # date onward: the closing adjustment zeroed its postings, and
            # stale valuations/holdings after that date must not resurrect it.
            # History BEFORE the close date is untouched (DESIGN §3.4).
            closed_cutoff = closed_on is not None and day >= closed_on
            method_parts: list[str] = []
            posted = basis.at(day)
            valuation = valued.at(day)
            set_value = market.at(day)
            if account.type in VALUATION_TYPES and not closed_cutoff and valuation is not None:
                value = valuation
                method_parts.append("valuation")
            else:
                value = posted
                if value != 0:
                    method_parts.append("postings")
                if (
                    account.type == "brokerage"
                    and not closed_cutoff
                    and set_value is not None
                    and set_value != 0
                ):
                    value += set_value
                    method_parts.append("holdings")
            if value == 0 and not method_parts:
                continue
            result = out[day]
            result["breakdown"].append(
                {
                    "account_id": account.id,
                    "path": account.path,
                    "type": account.type,
                    "value": value,
                    "method": "+".join(method_parts) or "postings",
                }
            )
            if value >= 0:
                result["assets"] += value
            else:
                result["liabilities"] += -value
    for result in out.values():
        result["net_worth"] = result["assets"] - result["liabilities"]
    return out


def earliest_activity_date(session: Session) -> date_type | None:
    """First date the ledger knows anything about (postings or materialized
    net-worth rows) — the natural start for 'all'-period series."""
    first_tx = session.execute(select(func.min(Transaction.date))).scalar()
    first_row = session.execute(select(func.min(NetWorthDaily.as_of))).scalar()
    candidates = [d for d in (first_tx, first_row) if d is not None]
    return min(candidates) if candidates else None


def account_first_activity(session: Session, account: Account) -> date_type | None:
    """The first date ONE account has anything to report: its first stored
    running-balance row, holdings snapshot or valuation, whichever is
    earliest (#594). A chart window clamps its start here so a 5-year view
    of a one-year-old account does not open on four flat years of zero."""
    return session.execute(
        text(
            "SELECT LEAST("
            " (SELECT min(date) FROM account_running_balances WHERE account_id = CAST(:a AS UUID)),"
            " (SELECT min(as_of) FROM holdings_snapshots WHERE account_id = CAST(:a AS UUID)),"
            " (SELECT min(as_of) FROM valuations WHERE account_id = CAST(:a AS UUID)))"
        ),
        {"a": str(account.id)},
    ).scalar()


# Density ladder for the admin chart (#594): a REST read is not token-budgeted,
# so it runs far denser than balance_history's 31 points — 1y is a true daily
# series at the default cap, 5y weekly, longer histories monthly.
CHART_INTERVALS_DAYS = (1, 2, 3, 7, 14, 30, 91)
CHART_MAX_POINTS = 400


@dataclass(frozen=True)
class BalanceHistory:
    """One account's reported end-of-day balance over sampled dates, with
    the window's change and extremes computed here in Decimal so a caller
    never does arithmetic on money (#594)."""

    start: date_type
    end: date_type
    method: str
    interval_days: int
    points: list[tuple[date_type, Decimal]]
    current: Decimal
    change: Decimal
    change_pct: Decimal | None
    low: tuple[date_type, Decimal] | None
    high: tuple[date_type, Decimal] | None
    first_activity: date_type | None


def account_balance_history(
    session: Session,
    account: Account,
    *,
    start: date_type,
    end: date_type,
    max_points: int = CHART_MAX_POINTS,
) -> BalanceHistory:
    """The chart series for one account (#594): ``account_balance_series``
    over dates sampled across ``[start, end]``, so every point is the same
    reported balance the account header, MCP ``balance_history`` and net
    worth use — holdings added for brokerage, valuations for loans.

    ``start`` clamps forward to the account's first activity (never past
    ``end``). Extremes rank EVERY day with activity on the same reported
    basis (:func:`account_window_extremes`, decisions 63 and 129), so
    sampling cannot hide a low — for an overlay account too, whose value also
    moves on a snapshot's or a valuation's date."""
    first = account_first_activity(session, account)
    if first is not None and start < first:
        start = min(first, end)
    dates = sampling.sample_dates(start, end, intervals=CHART_INTERVALS_DAYS, max_points=max_points)
    series, method = account_balance_series(session, account, dates=dates)
    points = [(d, series[d]) for d in dates]
    opening, current = points[0][1], points[-1][1]
    change = current - opening
    change_pct = (change / abs(opening) * 100).quantize(Decimal("0.01")) if opening != 0 else None
    low, high = account_window_extremes(session, account, start=start, end=end)
    return BalanceHistory(
        start=start,
        end=end,
        method=method,
        interval_days=(dates[-1] - dates[-2]).days if len(dates) > 1 else 0,
        points=points,
        current=current,
        change=change,
        change_pct=change_pct,
        low=low,
        high=high,
        first_activity=first,
    )


ATTRIBUTION_BUCKETS: dict[str, str] = {
    "checking": "cash",
    "savings": "cash",
    "cash": "cash",
    "credit_card": "credit",
    "brokerage": "market",
    "property": "property",
    "loan": "loan_principal",
}


def attribution(session: Session, *, start: date_type, end: date_type) -> list[dict[str, Any]]:
    """Net-worth delta decomposed by account class between two dates
    (issue #75). Buckets partition accounts and net worth is exactly the sum
    of per-account breakdown values, so bucket deltas sum to the period
    delta BY CONSTRUCTION. Tradeoff (documented): `market` moves include
    contributions as well as growth — separating them would break the
    partition property."""
    worth = net_worth_on(session, [start, end])
    start_values = {b["account_id"]: Decimal(b["value"]) for b in worth[start]["breakdown"]}
    end_values = {b["account_id"]: Decimal(b["value"]) for b in worth[end]["breakdown"]}
    involved = set(start_values) | set(end_values)
    if not involved:
        return []
    types = {
        a.id: a.type
        for a in session.execute(select(Account).where(Account.id.in_(involved))).scalars()
    }
    deltas: dict[str, Decimal] = {}
    for account_id in involved:
        bucket = ATTRIBUTION_BUCKETS.get(types.get(account_id, ""), "other")
        change = end_values.get(account_id, Decimal(0)) - start_values.get(account_id, Decimal(0))
        deltas[bucket] = deltas.get(bucket, Decimal(0)) + change
    out: list[dict[str, Any]] = [
        {"bucket": bucket, "delta": delta} for bucket, delta in deltas.items() if delta != 0
    ]
    out.sort(key=lambda entry: abs(entry["delta"]), reverse=True)
    return out


_SAMPLE_INTERVALS_DAYS = (7, 30, 91, 365)  # weekly, monthly, quarterly, yearly


def series_sampled(
    session: Session, *, start: date_type, end: date_type, max_points: int = 20
) -> list[dict[str, Any]]:
    """Adaptive-density series (issue #75): pick sample dates FIRST at the
    smallest interval that fits max_points, then value only those days
    (stored net_worth_daily rows where present, computed otherwise). Never
    walks the full daily range — that's what purser://net-worth/daily is
    for, where the token budget doesn't apply."""
    try:
        dates = sampling.sample_dates(
            start, end, intervals=_SAMPLE_INTERVALS_DAYS, max_points=max_points
        )
    except ValueError as exc:
        raise LedgerError(str(exc)) from exc

    dirty_from = _stale_from(session)
    stored = {
        row.as_of: row
        for row in session.execute(
            select(NetWorthDaily).where(NetWorthDaily.as_of.in_(dates))
        ).scalars()
        if dirty_from is None or row.as_of < dirty_from
    }
    return _stored_or_computed(session, dates, stored)


def _stored_or_computed(
    session: Session, dates: Sequence[date_type], stored: dict[date_type, NetWorthDaily]
) -> list[dict[str, Any]]:
    """Each of ``dates`` from its stored row when it has a usable one, the
    rest computed live in ONE :func:`net_worth_on` pass."""
    computed = net_worth_on(session, [day for day in dates if day not in stored])
    out: list[dict[str, Any]] = []
    for day in dates:
        if day in stored:
            row = stored[day]
            out.append(
                {
                    "as_of": day,
                    "assets": Decimal(row.assets),
                    "liabilities": Decimal(row.liabilities),
                    "net_worth": Decimal(row.net_worth),
                }
            )
        else:
            live = computed[day]
            out.append(
                {
                    "as_of": day,
                    "assets": live["assets"],
                    "liabilities": live["liabilities"],
                    "net_worth": live["net_worth"],
                }
            )
    return out


def materialize_day(session: Session, as_of: date_type) -> None:
    materialize_range(session, as_of, as_of)


# Days one consume_watermark step materializes and commits together: one
# net_worth_on pass and one upsert each, so a re-materialization of years of
# history is a few dozen statements and its progress is still committed as it
# goes (#610).
MATERIALIZE_CHUNK_DAYS = 92


def materialize_range(session: Session, start: date_type, end: date_type) -> int:
    """Store net worth for every day in ``[start, end]`` in ``net_worth_daily``
    (upsert), from one :func:`net_worth_on` pass and one statement. Returns
    the number of days written; the caller commits."""
    if end < start:
        return 0
    days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
    computed = net_worth_on(session, days)
    stmt = pg_insert(NetWorthDaily).values(
        [
            {
                "as_of": day,
                "currency": "USD",
                "assets": computed[day]["assets"],
                "liabilities": computed[day]["liabilities"],
                "net_worth": computed[day]["net_worth"],
            }
            for day in days
        ]
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["as_of", "currency"],
        set_={
            "assets": stmt.excluded.assets,
            "liabilities": stmt.excluded.liabilities,
            "net_worth": stmt.excluded.net_worth,
            "computed_at": func.now(),
        },
    )
    session.execute(stmt)
    return len(days)


def series(session: Session, *, start: date_type, end: date_type) -> list[dict[str, Any]]:
    """Materialized rows where they exist; computed on the fly for gaps."""
    if end < start:
        raise LedgerError("end must be >= start")
    if (end - start).days > SERIES_CAP_DAYS:
        raise LedgerError("series range is capped at 10 years")
    dirty_from = _stale_from(session)
    stored = {
        row.as_of: row
        for row in session.execute(
            select(NetWorthDaily).where(NetWorthDaily.as_of >= start, NetWorthDaily.as_of <= end)
        ).scalars()
        if dirty_from is None or row.as_of < dirty_from
    }
    days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
    return _stored_or_computed(session, days, stored)


def _stale_from(session: Session) -> date_type | None:
    """Stored net_worth_daily rows at/after this date are pending
    re-materialization (decision 38) and are served computed instead."""
    from sqlalchemy import text

    return session.execute(
        text("SELECT networth_dirty_from FROM balance_watermark")
    ).scalar_one_or_none()


def consume_watermark() -> int:
    """Async watermark consumer (decision 38, issue #145): claim-and-clear
    ``networth_dirty_from`` in a short transaction, then re-materialize
    stored days from it through today, committing every
    :data:`MATERIALIZE_CHUNK_DAYS` days (#610) — the watermark lock is never
    held across the loop. On failure the unfinished range is re-marked from
    the chunk that failed; concurrent callers no-op on the claim. Returns the
    number of days materialized."""

    from purser.db import get_sessionmaker

    maker = get_sessionmaker()
    with maker() as session:
        claimed = balances.claim_networth_watermark(session)
        session.commit()
    if claimed is None:
        return 0
    today = clock.ledger_today()
    day = claimed
    done = 0
    try:
        with maker() as session:
            while day <= today:
                chunk_end = min(today, day + timedelta(days=MATERIALIZE_CHUNK_DAYS - 1))
                written = materialize_range(session, day, chunk_end)
                session.commit()
                day = chunk_end + timedelta(days=1)
                done += written
    except Exception:
        with maker() as session:
            balances.mark_dirty(session, day, networth_only=True)
            session.commit()
        raise
    return done


def materialize_job() -> None:
    """Nightly job: consume the watermark, then the unconditional
    today-2 floor, then the running-balance heal + parity probe
    (belt-and-suspenders for a writer that missed its mark)."""

    from purser.db import get_sessionmaker

    consume_watermark()
    today = clock.ledger_today()
    with get_sessionmaker()() as session:
        materialize_range(session, today - timedelta(days=2), today)
        balances.heal_and_probe(session)
        session.commit()
