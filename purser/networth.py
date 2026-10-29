"""Net worth engine (DESIGN §8).

Net worth at time T = sum of postings <= T (cash-flow accounts, incl.
brokerage cash) + latest holdings snapshot <= T (brokerage market value)
+ latest valuation <= T (property, loans). Categories never count.
Materialized nightly into net_worth_daily.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from datetime import date as date_type
from decimal import Decimal, InvalidOperation
from typing import Any, NamedTuple

from sqlalchemy import and_, case, func, literal_column, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, aliased

from purser import audit, balances, events, reconciliation
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

# Accounts valued by their latest valuation row rather than postings.
VALUATION_TYPES = {"property", "loan"}

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
    instead (decision 81). Every reader of valuations reads loan and property
    accounts only, so a valuation on any other type would be stored, audited
    and announced, and then never read."""
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
        f"and property accounts. Its balance comes from {source}."
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
    """Record ``account_ref``'s value on ``as_of``. Only a loan or property
    account takes one (decision 81). ``value`` is in ledger sign unless
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
            .order_by(Valuation.as_of.desc())
        ).scalars()
    )


def holdings_set_on(
    session: Session, account_id: uuid.UUID, as_of: date_type | None = None
) -> list[HoldingsSnapshot]:
    """The snapshot set in effect at ``as_of``: every row of the account's
    newest snapshot date at or before it, zero rows included."""
    stmt = select(func.max(HoldingsSnapshot.as_of)).where(HoldingsSnapshot.account_id == account_id)
    if as_of is not None:
        stmt = stmt.where(HoldingsSnapshot.as_of <= as_of)
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
    not holdings. A feed row is returned as the bank sent it, zero shares
    included, valued on read when the feed values it at 0 (decision 92)."""
    return overlay.as_held(
        session,
        [
            h
            for h in holdings_set_on(session, account_id, as_of)
            if not (h.source == "manual" and h.quantity == 0)
        ],
    )


def _postings_balance(session: Session, account_id: uuid.UUID, as_of: date_type) -> Decimal:
    total = session.execute(
        select(func.coalesce(func.sum(Posting.amount), 0))
        .join(Transaction, Transaction.id == Posting.transaction_id)
        .where(
            Posting.account_id == account_id,
            Transaction.status.in_(["pending", "posted"]),
            Transaction.date <= as_of,
        )
    ).scalar_one()
    return Decimal(total)


def _latest_valuation(session: Session, account_id: uuid.UUID, as_of: date_type) -> Decimal | None:
    row = session.execute(
        select(Valuation.value)
        .where(Valuation.account_id == account_id, Valuation.as_of <= as_of)
        .order_by(Valuation.as_of.desc(), Valuation.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return Decimal(row) if row is not None else None


def valuation_balances(
    session: Session,
    account_ids: list[uuid.UUID] | None = None,
    as_of: date_type | None = None,
) -> dict[uuid.UUID, tuple[Decimal, date_type]]:
    """Presentation overlay for account surfaces (DESIGN §8, decision 42):
    the latest valuation at or before ``as_of`` (default today, UTC) per
    OPEN loan/property account, as {account_id: (value, valuation_as_of)}.

    Postings stay the ledger truth — ``ledger.account_balances()`` and the
    running-balance store are untouched (parity probe and close_account
    depend on postings-sum semantics); surfaces overlay this map on top.
    Accounts at/past their close date are omitted so stale valuations never
    resurrect a closed account (same cutoff rule as ``net_worth_at``).
    """
    effective = as_of or datetime.now(UTC).date()
    stmt = (
        select(Valuation.account_id, Valuation.value, Valuation.as_of, Account.closed_at)
        .distinct(Valuation.account_id)
        .join(Account, Account.id == Valuation.account_id)
        .where(Account.type.in_(VALUATION_TYPES), Valuation.as_of <= effective)
        .order_by(Valuation.account_id, Valuation.as_of.desc(), Valuation.id.desc())
    )
    if account_ids is not None:
        stmt = stmt.where(Valuation.account_id.in_(account_ids))
    overlay: dict[uuid.UUID, tuple[Decimal, date_type]] = {}
    for account_id, value, valued_as_of, closed_at in session.execute(stmt):
        if closed_at is not None and effective >= closed_at.date():
            continue
        overlay[account_id] = (Decimal(value), valued_as_of)
    return overlay


def holdings_addends(
    session: Session,
    account_ids: list[uuid.UUID] | None = None,
    as_of: date_type | None = None,
) -> dict[uuid.UUID, tuple[Decimal, date_type]]:
    """Presentation overlay for account surfaces (DESIGN §8, decision 50):
    the market value of the latest holdings snapshot at or before ``as_of``
    (default today, UTC) per OPEN brokerage account, as
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
    effective = as_of or datetime.now(UTC).date()
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
    # A feed row with no stored value adds what a stored close makes it
    # (decision 92), folded into the one aggregate (overlay.FEED_VALUE_SQL,
    # evaluated only for those rows), so a day costs no extra round trip.
    h = aliased(HoldingsSnapshot, name="h")
    stmt = (
        select(
            h.account_id,
            func.coalesce(
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
            ),
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
        if closed_at is not None and effective >= closed_at.date():
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

    Open loan/property answer with their latest valuation (decision 42); open
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
    ORDER BY as_of DESC, id DESC LIMIT 1
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
    :func:`account_balance_at`, and the single source behind the REST series
    endpoint, the MCP tool and its ``purser://`` resource.

    Branches ONCE on account type rather than per date, because an account
    does not change how it is valued mid-window:

    - loan/property -> the valuations series (``valuation``); the running
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
    closed_on = account.closed_at.date() if account.closed_at is not None else None

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


def net_worth_at(session: Session, as_of: date_type) -> dict[str, Any]:
    accounts = session.execute(
        select(Account).where(Account.is_virtual.is_(False)).order_by(Account.path)
    ).scalars()
    # One set-based pass for every brokerage account, rather than a
    # latest_holdings() round-trip inside the loop.
    market_overlay = holdings_addends(session, as_of=as_of)
    breakdown: list[dict[str, Any]] = []
    assets = Decimal(0)
    liabilities = Decimal(0)
    for account in accounts:
        value = Decimal(0)
        method_parts: list[str] = []
        # A closed account carries nothing into net worth from its close date
        # onward: the closing adjustment zeroed its postings, and stale
        # valuations/holdings after that date must not resurrect it. History
        # BEFORE the close date is untouched (DESIGN §3.4).
        closed_cutoff = account.closed_at is not None and as_of >= account.closed_at.date()
        if account.type in VALUATION_TYPES and not closed_cutoff:
            valuation = _latest_valuation(session, account.id, as_of)
            if valuation is not None:
                value = valuation
                method_parts.append("valuation")
            else:
                value = _postings_balance(session, account.id, as_of)
                if value != 0:
                    method_parts.append("postings")
        else:
            value = _postings_balance(session, account.id, as_of)
            if value != 0:
                method_parts.append("postings")
            if account.type == "brokerage" and not closed_cutoff:
                market = market_overlay.get(account.id)
                if market is not None:
                    value += market[0]
                    method_parts.append("holdings")
        if value == 0 and not method_parts:
            continue
        breakdown.append(
            {
                "account_id": account.id,
                "path": account.path,
                "type": account.type,
                "value": value,
                "method": "+".join(method_parts) or "postings",
            }
        )
        if value >= 0:
            assets += value
        else:
            liabilities += -value
    return {
        "as_of": as_of,
        "assets": assets,
        "liabilities": liabilities,
        "net_worth": assets - liabilities,
        "breakdown": breakdown,
    }


def earliest_activity_date(session: Session) -> date_type | None:
    """First date the ledger knows anything about (postings or materialized
    net-worth rows) — the natural start for 'all'-period series."""
    first_tx = session.execute(select(func.min(Transaction.date))).scalar()
    first_row = session.execute(select(func.min(NetWorthDaily.as_of))).scalar()
    candidates = [d for d in (first_tx, first_row) if d is not None]
    return min(candidates) if candidates else None


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
    start_values = {
        b["account_id"]: Decimal(b["value"]) for b in net_worth_at(session, start)["breakdown"]
    }
    end_values = {
        b["account_id"]: Decimal(b["value"]) for b in net_worth_at(session, end)["breakdown"]
    }
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
            computed = net_worth_at(session, day)
            out.append(
                {
                    "as_of": day,
                    "assets": computed["assets"],
                    "liabilities": computed["liabilities"],
                    "net_worth": computed["net_worth"],
                }
            )
    return out


def materialize_day(session: Session, as_of: date_type) -> None:
    result = net_worth_at(session, as_of)
    stmt = pg_insert(NetWorthDaily).values(
        as_of=as_of,
        currency="USD",
        assets=result["assets"],
        liabilities=result["liabilities"],
        net_worth=result["net_worth"],
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
    out: list[dict[str, Any]] = []
    day = start
    while day <= end:
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
            computed = net_worth_at(session, day)
            out.append(
                {
                    "as_of": day,
                    "assets": computed["assets"],
                    "liabilities": computed["liabilities"],
                    "net_worth": computed["net_worth"],
                }
            )
        day += timedelta(days=1)
    return out


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
    stored days from it through today with per-day commits — the watermark
    lock is never held across the (possibly minutes-long) loop. On failure
    the unfinished range is re-marked; concurrent callers no-op on the
    claim. Returns the number of days materialized."""
    from datetime import UTC, datetime

    from purser.db import get_sessionmaker

    maker = get_sessionmaker()
    with maker() as session:
        claimed = balances.claim_networth_watermark(session)
        session.commit()
    if claimed is None:
        return 0
    today = datetime.now(UTC).date()
    day = claimed
    done = 0
    try:
        with maker() as session:
            while day <= today:
                materialize_day(session, day)
                session.commit()
                day += timedelta(days=1)
                done += 1
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
    from datetime import UTC, datetime

    from purser.db import get_sessionmaker

    consume_watermark()
    today = datetime.now(UTC).date()
    with get_sessionmaker()() as session:
        for offset in range(3):
            materialize_day(session, today - timedelta(days=offset))
        balances.heal_and_probe(session)
        session.commit()
