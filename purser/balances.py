"""Stored per-account running balances + the shared balance dirty-watermark
(DESIGN §3.5, decisions 37/38, issues #146/#145).

``account_running_balances`` holds one row per (real account, chain-head
transaction with status pending|posted having a posting on the account):
``delta`` is that account's posting sum in the transaction, ``balance`` the
running sum in the total order ``(date, created_at, transaction_id)``. It is
derived data — rebuildable at any time, never authoritative over postings.

``balance_watermark`` is a singleton row with two independent dirty cursors,
both LEAST-updated by :func:`mark_dirty` at every balance-affecting write:

- ``running_dirty_from`` — consumed SYNCHRONOUSLY: the ``before_commit``
  hook (registered on the sessionmaker in :mod:`purser.db`) recomputes
  stored balances for ``date >= cursor`` inside the committing transaction,
  so no reader can ever observe a stale register balance.
- ``networth_dirty_from`` — consumed ASYNCHRONOUSLY by
  ``purser.networth.consume_watermark`` (nightly job + post-import kicks),
  which re-materializes ``net_worth_daily`` from the cursor forward.

The watermark row's UPDATE lock doubles as the recompute mutex: concurrent
writers serialize on :func:`mark_dirty` and their recomputes run in commit
order. The networth consumer deliberately claims-and-clears in a SHORT
transaction and never holds the lock across its minutes-long materialize
loop.
"""

import uuid
from collections.abc import Sequence
from datetime import date as date_type
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from purser.logging import get_logger
from purser.models import AccountRunningBalance

log = get_logger(__name__)

# Decision 37's total order, written ONCE. The running sum accumulates in the
# ascending form; every read that has to pick "the row in effect at date D",
# and the register itself, use the exact reverse. A second hand-written copy
# that dropped a tiebreaker would silently return a different row whenever an
# account has more than one transaction on the same day — the id is
# load-bearing because created_at is transaction_timestamp, identical across
# one import batch.
REGISTER_ORDER_ASC_SQL = "date, created_at, transaction_id"
REGISTER_ORDER_DESC_SQL = "date DESC, created_at DESC, transaction_id DESC"


def transaction_order_desc(transaction_model: Any) -> tuple[Any, Any, Any]:
    """The same order, newest first, for ORM reads of ``transactions`` (the
    register), whose id column is ``id`` rather than ``transaction_id``.

    Kept next to the SQL fragments above so the register's page order and the
    stored running balances can never be sorted by two different keys."""
    return (
        transaction_model.date.desc(),
        transaction_model.created_at.desc(),
        transaction_model.id.desc(),
    )


_MARK_BOTH = text(
    "UPDATE balance_watermark SET"
    " running_dirty_from = LEAST(COALESCE(running_dirty_from, :d), :d),"
    " networth_dirty_from = LEAST(COALESCE(networth_dirty_from, :d), :d),"
    " updated_at = now()"
)

_MARK_NETWORTH = text(
    "UPDATE balance_watermark SET"
    " networth_dirty_from = LEAST(COALESCE(networth_dirty_from, :d), :d),"
    " updated_at = now()"
)

# Canonical recompute — the ONE implementation shared by the runtime consumer,
# the 0017 migration backfill, and the nightly rebuild, so they can never
# drift. CAST(:d AS DATE) IS NULL means full rebuild. The window's ORDER BY
# names the deltas columns unqualified: `anchors` carries only account_id and
# balance, so they cannot be ambiguous (Postgres would refuse if they were).
_RECOMPUTE_DELETE = text(
    "DELETE FROM account_running_balances"
    " WHERE CAST(:d AS DATE) IS NULL OR date >= CAST(:d AS DATE)"
)

_RECOMPUTE_INSERT = text(
    f"""
WITH heads AS (
    SELECT t.id, t.date, t.created_at
    FROM transactions t
    WHERE t.status IN ('pending', 'posted')
      AND NOT EXISTS (SELECT 1 FROM transactions r WHERE r.reverses_id = t.id)
      AND (CAST(:d AS DATE) IS NULL OR t.date >= CAST(:d AS DATE))
), deltas AS (
    SELECT p.account_id, h.id AS transaction_id, h.date, h.created_at,
           MIN(p.currency) AS currency, SUM(p.amount) AS delta
    FROM postings p
    JOIN heads h ON h.id = p.transaction_id
    JOIN accounts a ON a.id = p.account_id AND NOT a.is_virtual
    GROUP BY p.account_id, h.id, h.date, h.created_at
), anchors AS (
    SELECT DISTINCT ON (account_id) account_id, balance
    FROM account_running_balances
    WHERE CAST(:d AS DATE) IS NOT NULL AND date < CAST(:d AS DATE)
    ORDER BY account_id, {REGISTER_ORDER_DESC_SQL}
)
INSERT INTO account_running_balances
    (account_id, transaction_id, date, created_at, currency, delta, balance)
SELECT d.account_id, d.transaction_id, d.date, d.created_at, d.currency, d.delta,
       COALESCE(a.balance, 0) + SUM(d.delta) OVER (
           PARTITION BY d.account_id
           ORDER BY {REGISTER_ORDER_ASC_SQL}
           ROWS UNBOUNDED PRECEDING
       )
FROM deltas d
LEFT JOIN anchors a USING (account_id)
"""
)

_CLAIM_NETWORTH = text(
    """
UPDATE balance_watermark b
SET networth_dirty_from = NULL, updated_at = now()
FROM (SELECT id, networth_dirty_from AS prior FROM balance_watermark FOR UPDATE) o
WHERE b.id = o.id AND o.prior IS NOT NULL
RETURNING o.prior
"""
)


def mark_dirty(session: Session, from_date: date_type, *, networth_only: bool = False) -> None:
    """Record that balances at ``from_date`` (and everything after) changed.

    Called transactionally by every balance-affecting writer, colocated with
    its ``audit.record``. The UPDATE's row lock serializes concurrent
    writers. Non-networth marks also flag the session so the
    ``before_commit`` hook consumes the running cursor at commit.
    """
    if networth_only:
        session.execute(_MARK_NETWORTH, {"d": from_date})
        return
    session.execute(_MARK_BOTH, {"d": from_date})
    session.info["balances_dirty"] = True


def recompute_running(session: Session) -> None:
    """Consume ``running_dirty_from``: delete + re-derive stored balances for
    ``date >= cursor``, anchored per account on the last surviving row. Runs
    inside the caller's transaction; no-op when the cursor is clean."""
    dirty_from = session.execute(
        text("SELECT running_dirty_from FROM balance_watermark FOR UPDATE")
    ).scalar_one_or_none()
    if dirty_from is None:
        return
    _recompute(session, dirty_from)
    session.execute(
        text("UPDATE balance_watermark SET running_dirty_from = NULL, updated_at = now()")
    )


def rebuild_running(session: Session) -> None:
    """Full rebuild of ``account_running_balances`` (migration backfill and
    the nightly parity heal). Takes the watermark lock like any consumer."""
    session.execute(text("SELECT id FROM balance_watermark FOR UPDATE"))
    _recompute(session, None)
    session.execute(
        text("UPDATE balance_watermark SET running_dirty_from = NULL, updated_at = now()")
    )


def _recompute(session: Session, from_date: date_type | None) -> None:
    session.execute(_RECOMPUTE_DELETE, {"d": from_date})
    session.execute(_RECOMPUTE_INSERT, {"d": from_date})


def balance_asof(session: Session, account_id: uuid.UUID, as_of: date_type) -> Decimal:
    """Point-in-time balance from the stored table: the running balance of
    the last row dated <= as_of, else 0 (decision 37 register semantics —
    chain heads only, pending included)."""
    row = session.execute(
        text(
            "SELECT balance FROM account_running_balances"
            " WHERE account_id = CAST(:a AS UUID) AND date <= :d"
            f" ORDER BY {REGISTER_ORDER_DESC_SQL} LIMIT 1"
        ),
        {"a": str(account_id), "d": as_of},
    ).scalar_one_or_none()
    return Decimal(row) if row is not None else Decimal("0")


def balances_for_transactions(
    session: Session, account_id: uuid.UUID, transaction_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, Decimal]:
    """Stored running balances for specific transactions from one account's
    perspective — the register's Balance column, as a map.

    THE one implementation, shared by ``GET /transactions?account=`` and MCP
    ``search_transactions`` (same reason ``_RECOMPUTE_*`` is shared): both
    surfaces must show byte-identical numbers, and a row is present here only
    if it is a chain head with a posting on the account, which is decision
    37's definition and not something to restate per caller.

    Rows with no entry (void, expected, or no posting on this account) are
    simply absent — the caller renders them blank rather than 0.
    """
    if not transaction_ids:
        return {}
    rows = session.execute(
        select(AccountRunningBalance.transaction_id, AccountRunningBalance.balance).where(
            AccountRunningBalance.account_id == account_id,
            AccountRunningBalance.transaction_id.in_(list(transaction_ids)),
        )
    )
    return {transaction_id: Decimal(balance) for transaction_id, balance in rows}


# One indexed seek per requested date, against the existing
# (account_id, date, created_at, transaction_id) index — no table scan, no
# materialization, no second watermark. `unnest` rather than
# generate_series so the caller owns the density (core.sampling).
_SERIES = text(
    f"""
SELECT d::date AS as_of, COALESCE(b.balance, 0) AS balance
FROM unnest(CAST(:dates AS date[])) AS d
LEFT JOIN LATERAL (
    SELECT balance FROM account_running_balances
    WHERE account_id = CAST(:a AS UUID) AND date <= d
    ORDER BY {REGISTER_ORDER_DESC_SQL} LIMIT 1
) b ON true
ORDER BY d
"""
)


# Extremes rank END-OF-DAY balances (decision 63). DISTINCT ON keeps each
# date's last row in the total order — exactly the value balance_series
# reports for that date — so the order rows happen to land in within one day
# (gross pay before its deductions, a duplicate charge before its reversal)
# can never surface as a low or a high. Ties go to the earliest date.
def _extreme(direction: str) -> Any:
    return text(
        f"""
WITH end_of_day AS (
    SELECT DISTINCT ON (date) date, balance
    FROM account_running_balances
    WHERE account_id = CAST(:a AS UUID) AND date >= :s AND date <= :e
    ORDER BY {REGISTER_ORDER_DESC_SQL}
)
SELECT date, balance FROM end_of_day
ORDER BY balance {direction}, date
LIMIT 1
"""
    )


_EXTREME_ASC = _extreme("ASC")
_EXTREME_DESC = _extreme("DESC")


# Density ladder for balance series, finer than net worth's (which starts at
# weekly): a point here is one indexed seek, where a net-worth point is a full
# per-account scan. 31 points makes a 30-day window a TRUE daily series, which
# is what "balance over the last 30 days" means; 90d lands on every 3rd day,
# 1y on every 14th, 5y quarterly.
BALANCE_INTERVALS_DAYS = (1, 2, 3, 7, 14, 30, 91, 365)
BALANCE_MAX_POINTS = 31


def balance_series(
    session: Session, account_id: uuid.UUID, *, dates: Sequence[date_type]
) -> dict[date_type, Decimal]:
    """End-of-day balance on each requested date, as {date: balance}.

    Sparse store, dense answer: the table holds one row per transaction, so a
    date with no activity resolves to the last row at or before it — the same
    rule as :func:`balance_asof`, batched. 0 before the account's first
    activity, so a window that opens before the account existed reads flat-at-
    zero rather than absent.
    """
    if not dates:
        return {}
    rows = session.execute(_SERIES, {"a": str(account_id), "dates": list(dates)})
    return {as_of: Decimal(balance) for as_of, balance in rows}


def change_points(
    session: Session,
    account_id: uuid.UUID,
    *,
    start: date_type,
    end: date_type,
    limit: int = 5000,
) -> tuple[list[Any], bool]:
    """Every stored row in ``[start, end]`` oldest-first, as
    ``(rows, truncated)``. Rows carry ``date, balance, delta,
    transaction_id``.

    This is the FULL resolution of a balance history: per-transaction is the
    grain the store keeps and the register shows, and each point carries a
    drill target. Returns one extra row past ``limit`` internally to set
    ``truncated`` honestly rather than silently cutting the tail.
    """
    rows = list(
        session.execute(
            text(
                "SELECT date, balance, delta, transaction_id"
                " FROM account_running_balances"
                " WHERE account_id = CAST(:a AS UUID) AND date >= :s AND date <= :e"
                f" ORDER BY {REGISTER_ORDER_ASC_SQL}"
                " LIMIT :lim"
            ),
            {"a": str(account_id), "s": start, "e": end, "lim": limit + 1},
        )
    )
    return rows[:limit], len(rows) > limit


def window_extremes(
    session: Session, account_id: uuid.UUID, *, start: date_type, end: date_type
) -> tuple[Any | None, Any | None]:
    """The lowest and highest END-OF-DAY balance in the window, as
    ``(low, high)`` rows carrying ``date, balance`` (decision 63); ``(None,
    None)`` when the window holds no activity.

    A day's end-of-day balance is its last stored row in the total order —
    the value :func:`balance_series` reports for that date. Ranking those
    rather than every per-transaction row means intra-day ordering never
    creates an extreme: a gross pay leg posted before its same-day deductions
    is not a high, and a duplicate charge posted before its same-day reversal
    is not a low.

    Computed over EVERY day with activity in the window, never over a sampled
    subset — that is what makes downsampling safe to publish: the extremes
    survive it, so a caller can report "low 1,842.10 on Jul 19" truthfully
    from a 31-point series that never contains the 19th.
    """
    params = {"a": str(account_id), "s": start, "e": end}
    low = session.execute(_EXTREME_ASC, params).first()
    high = session.execute(_EXTREME_DESC, params).first()
    return low, high


def largest_moves(
    session: Session,
    account_id: uuid.UUID,
    *,
    start: date_type,
    end: date_type,
    limit: int = 2,
) -> list[Any]:
    """The biggest balance movements in the window by |delta|, as rows
    carrying ``date, delta, transaction_id`` — the attribution tier that
    turns "down 612.40" into "down 612.40, the mortgage landed on the 14th".

    Payee names are deliberately NOT joined here: they are encrypted at rest
    and only the ORM's type decorator decrypts them (decision 36), so a
    caller that wants names resolves the transaction_ids through the ORM.
    """
    return list(
        session.execute(
            text(
                "SELECT date, delta, transaction_id FROM account_running_balances"
                " WHERE account_id = CAST(:a AS UUID) AND date >= :s AND date <= :e"
                " AND delta <> 0"
                " ORDER BY abs(delta) DESC, date DESC, transaction_id DESC"
                " LIMIT :lim"
            ),
            {"a": str(account_id), "s": start, "e": end, "lim": limit},
        )
    )


def claim_networth_watermark(session: Session) -> date_type | None:
    """Atomically read-and-clear ``networth_dirty_from`` (None = clean).
    Deliberately a short operation: the caller commits immediately and
    materializes WITHOUT holding the watermark lock; on failure it re-marks."""
    return session.execute(_CLAIM_NETWORTH).scalar_one_or_none()


def on_before_commit(session: Session) -> None:
    """Sessionmaker-level consumer (decision 38): no-op unless a writer
    marked this session dirty; then flush + recompute inside the committing
    transaction. Kept tiny — the work lives in :func:`recompute_running`."""
    if not session.info.pop("balances_dirty", False):
        return
    session.flush()
    recompute_running(session)


def heal_and_probe(session: Session) -> None:
    """Nightly belt-and-suspenders (decision 38). A leftover running cursor
    means a writer marked without its commit hook firing — consume it and say
    so. Then the parity probe: every real account's newest stored balance
    must equal the live postings sum; a mismatch means an unmarked writer
    slipped through, so rebuild and re-mark networth from scratch."""
    from purser import ledger
    from purser.models import Account, Transaction

    leftover = session.execute(
        text("SELECT running_dirty_from FROM balance_watermark FOR UPDATE")
    ).scalar_one_or_none()
    if leftover is not None:
        log.warning("balance watermark: healing leftover running cursor", dirty_from=str(leftover))
        _recompute(session, leftover)
        session.execute(
            text("UPDATE balance_watermark SET running_dirty_from = NULL, updated_at = now()")
        )

    from sqlalchemy import func, select

    account_ids = list(
        session.execute(select(Account.id).where(Account.is_virtual.is_(False))).scalars()
    )
    live = ledger.account_balances(session, account_ids)
    stored: dict[uuid.UUID, Decimal] = {}
    for account_id, balance in session.execute(
        text(
            "SELECT DISTINCT ON (account_id) account_id, balance"
            " FROM account_running_balances"
            f" ORDER BY account_id, {REGISTER_ORDER_DESC_SQL}"
        )
    ):
        stored[account_id] = Decimal(balance)
    mismatched = [
        str(a) for a in account_ids if live.get(a, Decimal("0")) != stored.get(a, Decimal("0"))
    ]
    if mismatched:
        log.error("balance parity probe failed; rebuilding", accounts=mismatched)
        rebuild_running(session)
        earliest = session.execute(select(func.min(Transaction.date))).scalar_one_or_none()
        if earliest is not None:
            mark_dirty(session, earliest, networth_only=True)
