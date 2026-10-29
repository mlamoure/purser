"""Read-side aggregates: account balances, category rollups, spending
summaries and spending baselines."""

import calendar
import uuid
from datetime import date as date_type
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from purser.ledger.errors import LedgerError, check_path
from purser.models import Payee, Posting, Transaction


def account_balances(
    session: Session, account_ids: list[uuid.UUID] | None = None
) -> dict[uuid.UUID, Decimal]:
    """SUM(postings) per account over pending+posted transactions."""
    stmt = (
        select(Posting.account_id, func.coalesce(func.sum(Posting.amount), 0))
        .join(Transaction, Transaction.id == Posting.transaction_id)
        .where(Transaction.status.in_(["pending", "posted"]))
        .group_by(Posting.account_id)
    )
    if account_ids is not None:
        stmt = stmt.where(Posting.account_id.in_(account_ids))
    return {row[0]: Decimal(row[1]) for row in session.execute(stmt)}


def category_rollup(
    session: Session,
    *,
    subtree: str,
    start: date_type | None = None,
    end: date_type | None = None,
) -> dict[str, Any]:
    """Spending rollup for a category subtree — ONE indexed ltree query."""
    conditions = ["a.path <@ CAST(:subtree AS ltree)", "t.status IN ('pending', 'posted')"]
    params: dict[str, Any] = {"subtree": check_path(subtree)}
    if start is not None:
        conditions.append("t.date >= :start")
        params["start"] = start
    if end is not None:
        conditions.append("t.date <= :end")
        params["end"] = end
    row = session.execute(
        text(
            "SELECT COALESCE(SUM(p.amount), 0) AS total, COUNT(DISTINCT t.id) AS tx_count "
            "FROM postings p "
            "JOIN accounts a ON a.id = p.account_id "
            "JOIN transactions t ON t.id = p.transaction_id "
            f"WHERE {' AND '.join(conditions)}"
        ),
        params,
    ).one()
    return {"path": subtree, "total": Decimal(row.total), "transaction_count": row.tx_count}


def _payee_labels(session: Session, payee_ids: list[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    """Decrypt payee names for a set of payee_id skeleton UUIDs → display labels
    (#137). NULL ids are the caller's '(none)' bucket and are skipped here."""
    ids = {pid for pid in payee_ids if pid is not None}
    if not ids:
        return {}
    return {
        row.id: row.name
        for row in session.execute(select(Payee.id, Payee.name).where(Payee.id.in_(ids)))
    }


def spending_summary(
    session: Session,
    *,
    group_by: str,
    start: date_type | None = None,
    end: date_type | None = None,
    category_subtree: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Server-side group-by so agents never sum rows in-context (DESIGN §10).

    Sums category-leg postings (expense categories positive by convention)
    grouped by category path, payee, or month. Returns the top `limit` groups
    plus `grand_total` (SUM over ALL matching postings, never truncated) and
    `truncated` — so callers never sum groups themselves.

    group_by=payee regroups on the plaintext payee_id skeleton and decrypts the
    labels app-side (#137): the payee name is encrypted, so it can't be a SQL
    grouping key. Payee-less rows collapse to a single '(none)' group (a small
    documented change — each distinct description was formerly its own group).
    """
    if group_by not in ("category", "payee", "month"):
        raise LedgerError("group_by must be category | payee | month")
    if category_subtree is not None:
        check_path(category_subtree)
    by_payee = group_by == "payee"
    if by_payee:
        key_expr = "t.payee_id"
        # NULL payee_id is one '(none)' group — COUNT(DISTINCT payee_id) would
        # drop it, so coalesce to a sentinel string for the group count.
        group_count_expr = "COUNT(DISTINCT COALESCE(t.payee_id::text, '(none)'))"
    else:
        key_expr = {"category": "a.path::text", "month": "to_char(t.date, 'YYYY-MM')"}[group_by]
        group_count_expr = f"COUNT(DISTINCT {key_expr})"
    conditions = [
        "a.is_virtual",
        "t.status IN ('pending', 'posted')",
    ]
    params: dict[str, Any] = {}
    if category_subtree is not None:
        conditions.append("a.path <@ CAST(:subtree AS ltree)")
        params["subtree"] = category_subtree
    else:
        conditions.append("a.type = 'category_expense'")
    if start is not None:
        conditions.append("t.date >= :start")
        params["start"] = start
    if end is not None:
        conditions.append("t.date <= :end")
        params["end"] = end
    from_where = (
        "FROM postings p "
        "JOIN accounts a ON a.id = p.account_id "
        "JOIN transactions t ON t.id = p.transaction_id "
        f"WHERE {' AND '.join(conditions)}"
    )
    # Grand total over ALL matching postings — same conditions, no limit — so
    # a truncated group list never yields a wrong "how much did I spend" sum.
    grand = session.execute(
        text(
            "SELECT COALESCE(SUM(p.amount), 0) AS grand_total, "
            f"{group_count_expr} AS group_count {from_where}"
        ),
        params,
    ).one()
    order = "1" if group_by == "month" else "2 DESC"
    rows = session.execute(
        text(
            f"SELECT {key_expr} AS key, SUM(p.amount) AS total, COUNT(DISTINCT t.id) AS tx_count "
            f"{from_where} GROUP BY 1 ORDER BY {order} LIMIT :limit"
        ),
        {**params, "limit": limit},
    ).all()
    if by_payee:
        labels = _payee_labels(session, [r.key for r in rows])
        groups = [
            {
                "key": labels.get(r.key, "(none)"),
                "total": Decimal(r.total),
                "transaction_count": r.tx_count,
            }
            for r in rows
        ]
    else:
        groups = [
            {"key": r.key, "total": Decimal(r.total), "transaction_count": r.tx_count} for r in rows
        ]
    return {
        "groups": groups,
        "grand_total": Decimal(grand.grand_total),
        "truncated": grand.group_count > len(groups),
    }


SPENDING_BASELINES = ("avg_3mo", "avg_12mo", "same_month_last_year")
_BASELINE_ROW_CAP = 8  # bar rows; totals stay exact (guardrail 6)


def _month_bounds(period: str) -> tuple[date_type, date_type]:
    year, month = int(period[:4]), int(period[5:7])
    start = date_type(year, month, 1)
    last_day = calendar.monthrange(year, month)[1]
    return start, date_type(year, month, last_day)


def _shift_month(anchor: date_type, months_back: int) -> date_type:
    total = anchor.year * 12 + (anchor.month - 1) - months_back
    return date_type(total // 12, total % 12 + 1, 1)


def spending_baseline(
    session: Session,
    *,
    period: str,
    baseline: str,
    category_subtree: str | None = None,
    group_by: str = "category",
    limit: int = _BASELINE_ROW_CAP,
    today: date_type | None = None,
) -> dict[str, Any]:
    """Spending for one month against the user's own baseline (issue #76).

    baseline = avg_3mo | avg_12mo | same_month_last_year. When `period` is
    the current month, the baseline is pace-adjusted: baseline months only
    count postings on days <= today's day-of-month (compare like days
    elapsed), so mid-month comparisons aren't misleading.

    group_by=category rolls categories up (top-level by default; one level
    below `category_subtree` when given) and attaches top_payee_mover per
    row. group_by=payee is the drill — same signature, payee rows.
    """
    if baseline not in SPENDING_BASELINES:
        raise LedgerError(f"baseline must be one of {' | '.join(SPENDING_BASELINES)}")
    if group_by not in ("category", "payee"):
        raise LedgerError("group_by must be category | payee in baseline mode")
    try:
        period_start, period_end = _month_bounds(period)
    except (ValueError, IndexError) as exc:
        raise LedgerError(f"period must look like YYYY-MM, got {period!r}") from exc
    if category_subtree is not None:
        check_path(category_subtree)  # before the nlevel() cast below

    today = today or date_type.today()
    like_days = today.day if (period_start <= today <= period_end) else None

    if baseline == "same_month_last_year":
        months = [date_type(period_start.year - 1, period_start.month, 1)]
    else:
        span = 3 if baseline == "avg_3mo" else 12
        months = [_shift_month(period_start, back) for back in range(1, span + 1)]
    k = len(months)
    baseline_start = min(months)
    baseline_end = _month_bounds(f"{max(months).year:04d}-{max(months).month:02d}")[1]

    if group_by == "payee":
        # Regroup on the payee_id skeleton; labels decrypted app-side (#137).
        key_expr = "t.payee_id"
    else:
        depth = 2
        if category_subtree is not None:
            depth_row = session.execute(
                text("SELECT nlevel(CAST(:subtree AS ltree)) AS n"), {"subtree": category_subtree}
            ).one()
            depth = int(depth_row.n) + 1
        key_expr = f"subltree(a.path, 0, LEAST(nlevel(a.path), {depth}))::text"

    conditions = ["a.is_virtual", "t.status IN ('pending', 'posted')"]
    params: dict[str, Any] = {}
    if category_subtree is not None:
        conditions.append("a.path <@ CAST(:subtree AS ltree)")
        params["subtree"] = category_subtree
    else:
        conditions.append("a.type = 'category_expense'")
    from_where = (
        "FROM postings p "
        "JOIN accounts a ON a.id = p.account_id "
        "JOIN transactions t ON t.id = p.transaction_id "
        f"WHERE {' AND '.join(conditions)}"
    )

    def window_conditions(*, is_baseline: bool) -> tuple[str, dict[str, Any]]:
        if is_baseline:
            # Baseline spans whole calendar months; for avg windows the months
            # are contiguous, so the [start, end] range covers exactly them.
            extra = "AND t.date >= :bstart AND t.date <= :bend"
            window_params: dict[str, Any] = {"bstart": baseline_start, "bend": baseline_end}
            if like_days is not None:
                extra += " AND EXTRACT(DAY FROM t.date) <= :like_days"
                window_params["like_days"] = like_days
            return extra, window_params
        return "AND t.date >= :pstart AND t.date <= :pend", {
            "pstart": period_start,
            "pend": period_end,
        }

    def grouped(*, is_baseline: bool) -> dict[str, tuple[Decimal, int]]:
        extra, window_params = window_conditions(is_baseline=is_baseline)
        rows = session.execute(
            text(
                f"SELECT {key_expr} AS key, SUM(p.amount) AS total, "
                f"COUNT(DISTINCT t.id) AS tx_count {from_where} {extra} GROUP BY 1"
            ),
            {**params, **window_params},
        ).all()
        return {r.key: (Decimal(r.total), r.tx_count) for r in rows}

    current = grouped(is_baseline=False)
    base = grouped(is_baseline=True)

    def cents(value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"))

    grand_total = cents(sum((v for v, _ in current.values()), Decimal(0)))
    baseline_total = cents(sum((v for v, _ in base.values()), Decimal(0)) / k)

    keys = set(current) | set(base)
    rows_out: list[dict[str, Any]] = []
    for key in keys:
        amount = cents(current.get(key, (Decimal(0), 0))[0])
        base_avg = cents(base.get(key, (Decimal(0), 0))[0] / k)
        rows_out.append(
            {
                "key": key,
                "amount": str(amount),
                "baseline": str(base_avg),
                "delta": str(cents(amount - base_avg)),
            }
        )
    rows_out.sort(key=lambda r: Decimal(r["amount"]), reverse=True)
    truncated = len(rows_out) > limit
    rows_out = rows_out[:limit]

    if group_by == "category" and rows_out:
        movers = _top_payee_movers(
            session,
            key_expr=key_expr,
            from_where=from_where,
            base_params=params,
            window_conditions=window_conditions,
            keys=[r["key"] for r in rows_out],
            k=k,
        )
        for row in rows_out:
            row["top_payee_mover"] = movers.get(row["key"])

    if group_by == "payee":
        # Keys are payee_id skeletons until here (ordering is numeric); decrypt
        # the surviving top-`limit` labels only (#137). NULL → '(none)'.
        labels = _payee_labels(session, [r["key"] for r in rows_out])
        for row in rows_out:
            row["key"] = labels.get(row["key"], "(none)")

    return {
        "group_by": group_by,
        "period": period,
        "baseline": baseline,
        "grand_total": str(grand_total),
        "baseline_total": str(baseline_total),
        "rows": rows_out,
        "truncated": truncated,
    }


def _top_payee_movers(
    session: Session,
    *,
    key_expr: str,
    from_where: str,
    base_params: dict[str, Any],
    window_conditions: Any,
    keys: list[str],
    k: int,
) -> dict[str, dict[str, Any] | None]:
    """Per returned category row, the payee that moved it most vs baseline —
    one grouped query per window (never per row). Regroups on the payee_id
    skeleton and decrypts the winning labels app-side (#137)."""
    payee_expr = "t.payee_id"

    def grouped(*, is_baseline: bool) -> dict[tuple[str, uuid.UUID | None], tuple[Decimal, int]]:
        extra, window_params = window_conditions(is_baseline=is_baseline)
        rows = session.execute(
            text(
                f"SELECT {key_expr} AS key, {payee_expr} AS payee, SUM(p.amount) AS total, "
                f"COUNT(DISTINCT t.id) AS tx_count {from_where} {extra} "
                f"AND {key_expr} = ANY(:keys) GROUP BY 1, 2"
            ),
            {**base_params, **window_params, "keys": keys},
        ).all()
        return {(r.key, r.payee): (Decimal(r.total), r.tx_count) for r in rows}

    current = grouped(is_baseline=False)
    base = grouped(is_baseline=True)
    out: dict[str, dict[str, Any] | None] = {}
    for key in keys:
        best: dict[str, Any] | None = None
        best_size = Decimal(0)
        payees = {p for (key_, p) in current if key_ == key} | {
            p for (key_, p) in base if key_ == key
        }
        for payee in payees:  # payee is a payee_id (UUID) or None
            cur_total, cur_count = current.get((key, payee), (Decimal(0), 0))
            base_total, base_count = base.get((key, payee), (Decimal(0), 0))
            delta = (cur_total - base_total / k).quantize(Decimal("0.01"))
            if abs(delta) > best_size:
                best_size = abs(delta)
                best = {
                    "payee": payee,  # decrypted to a label below
                    "delta": str(delta),
                    "count": cur_count,
                    "baseline_count": round(base_count / k),
                }
        out[key] = best
    labels = _payee_labels(session, [b["payee"] for b in out.values() if b is not None])
    for best in out.values():
        if best is not None:
            best["payee"] = labels.get(best["payee"], "(none)")
    return out
