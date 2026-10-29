"""Adaptive-density date sampling for token-budgeted series (DESIGN §10).

Every series tool answers under guardrail 6's budget, so none of them may
return a raw daily range — they pick sample dates FIRST at the coarsest
density that still fits, then value only those days. Full resolution lives
behind ``purser://`` resources, where the budget does not apply.

The rule lives here, once, because two callers now need it and a second
hand-rolled copy would drift: ``networth.series_sampled`` (whole portfolio,
weekly and coarser — each point costs a full per-account scan) and
``balances.balance_series`` (one account, daily and finer — each point costs
one indexed seek). Same rule, different ladders.
"""

from datetime import date, timedelta


def sample_dates(
    start: date, end: date, *, intervals: tuple[int, ...], max_points: int
) -> list[date]:
    """Sample dates across ``[start, end]`` at the smallest interval from
    ``intervals`` whose point count fits ``max_points``.

    ``end`` is the anchor, so the freshest point is always exact — a series
    whose last point drifted off today would misreport the current balance,
    which is the number a reader checks first. ``start`` is then forced in as
    the opening point, so the first and last points are always the window's
    real edges and a delta over the series equals the delta over the window.

    Falls back to the coarsest interval when even that overflows; the caller
    decides whether the resulting density is worth reporting.
    """
    if end < start:
        raise ValueError("end must be >= start")
    if max_points < 2:
        raise ValueError("max_points must be >= 2")
    span = (end - start).days
    interval = next(
        (i for i in intervals if span // i + 1 <= max_points),
        intervals[-1],
    )
    dates = sorted({max(start, end - timedelta(days=n * interval)) for n in range(max_points)})
    if dates and dates[0] != start:
        dates.insert(0, start)
    return dates
