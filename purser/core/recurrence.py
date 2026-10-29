"""Recurrence detection v2 (issue #234, DESIGN §6, decision 57) — pure.

Given the occurrences of one (account, counterparty) group — dated amounts
seen through that account's leg — decide whether they form a schedule, and
describe it: an RRULE anchored on the last occurrence, an amount model, the
evidence, and a deterministic confidence.

Three things differ from the v1 detector this replaces:

- **Amount is evidence, not a gate.** Card payments vary threefold month to
  month and are still a monthly schedule; variability lowers the confidence
  and picks the ``estimated`` amount model instead of vetoing the pattern.
- **Cadence tolerates a miss.** One skipped month (a 63-day gap in a monthly
  series) is a miss, not a disproof; a run is regular when at least 80% of
  its gaps sit in the bucket and misses are rare.
- **Semi-monthly exists.** Two paydays a month (the 6th and the 20th) are
  ``FREQ=MONTHLY;BYMONTHDAY=6,20``, distinguished from every-two-weeks by the
  days of month clustering while the weekday drifts.

Same-day occurrences are summed first (split legs), and a pattern that
stopped is not proposed: the last occurrence must be recent relative to the
cadence.
"""

from dataclasses import dataclass
from datetime import date as date_type
from decimal import Decimal
from statistics import median
from uuid import UUID

MIN_OCCURRENCES = 3
MIN_OCCURRENCES_YEARLY = 2
IN_BUCKET_RATIO_MIN = 0.8
MISSES_PER_GAPS = 12  # at most one miss per twelve gaps (plus one)
LAPSED_CADENCES = 1.5  # last occurrence older than this many periods = lapsed
FIXED_CV_MAX = Decimal("0.05")  # coefficient of variation under which the amount is "fixed"
CONFIDENCE_MIN = Decimal("0.4")

_WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


@dataclass(frozen=True)
class Occurrence:
    date: date_type
    amount: Decimal
    transaction_id: UUID | None = None


@dataclass(frozen=True)
class Bucket:
    """A cadence: the gap range that belongs to it and its nominal period."""

    name: str
    low: int
    high: int
    period_days: int
    min_occurrences: int = MIN_OCCURRENCES

    def contains(self, gap: int) -> bool:
        return self.low <= gap <= self.high


# Ordered finest to coarsest. Semi-monthly shares biweekly's gap range and is
# separated by the day-of-month test below, so it is not a bucket of its own.
BUCKETS: tuple[Bucket, ...] = (
    Bucket("weekly", 6, 8, 7),
    Bucket("biweekly", 12, 17, 14),
    Bucket("monthly", 26, 36, 30),
    Bucket("bimonthly", 55, 66, 61),
    Bucket("quarterly", 85, 98, 91),
    Bucket("semi-annual", 170, 195, 182),
    Bucket("yearly", 350, 381, 365, MIN_OCCURRENCES_YEARLY),
)


@dataclass(frozen=True)
class GapStats:
    median_days: int
    in_bucket: int  # gaps inside the bucket (a miss counts as inside)
    misses: int  # gaps that were a whole multiple of the period: skipped occurrences
    out_of_bucket: int

    @property
    def total(self) -> int:
        return self.in_bucket + self.out_of_bucket

    @property
    def in_bucket_ratio(self) -> float:
        return self.in_bucket / self.total if self.total else 0.0


@dataclass(frozen=True)
class Detected:
    cadence: str
    rrule: str
    period_days: int
    dtstart: date_type  # the last occurrence: anchor-sensitive rules count from it
    amount_type: str  # fixed | estimated
    typical_amount: Decimal  # median
    amount_low: Decimal
    amount_high: Decimal
    amount_cv: Decimal
    occurrences: int
    gaps: GapStats
    confidence: Decimal


def collapse_same_day(occurrences: list[Occurrence]) -> list[Occurrence]:
    """Split legs (two rows, one day, one payee) are one occurrence: sum them.
    The first row's id is kept as the evidence pointer."""
    by_day: dict[date_type, Occurrence] = {}
    for o in sorted(occurrences, key=lambda o: o.date):
        seen = by_day.get(o.date)
        by_day[o.date] = (
            o
            if seen is None
            else Occurrence(
                date=o.date, amount=seen.amount + o.amount, transaction_id=seen.transaction_id
            )
        )
    return [by_day[d] for d in sorted(by_day)]


def _gap_stats(gaps: list[int], bucket: Bucket) -> GapStats:
    in_bucket = misses = out = 0
    for gap in gaps:
        if bucket.contains(gap):
            in_bucket += 1
            continue
        # A gap that is k whole periods (within the bucket's tolerance scaled
        # by k) is k-1 skipped occurrences, not an irregular series.
        matched = False
        for k in (2, 3):
            if bucket.low * k <= gap <= bucket.high * k:
                in_bucket += 1
                misses += k - 1
                matched = True
                break
        if not matched:
            out += 1
    return GapStats(
        median_days=int(median(gaps)) if gaps else 0,
        in_bucket=in_bucket,
        misses=misses,
        out_of_bucket=out,
    )


def _pick_bucket(gaps: list[int]) -> tuple[Bucket, GapStats] | None:
    best: tuple[Bucket, GapStats] | None = None
    for bucket in BUCKETS:
        stats = _gap_stats(gaps, bucket)
        if stats.in_bucket_ratio < IN_BUCKET_RATIO_MIN:
            continue
        if stats.misses > 1 + len(gaps) // MISSES_PER_GAPS:
            continue
        # Prefer the bucket whose nominal period is nearest the median gap;
        # the ratio gate above already rejected the others.
        if best is None or abs(bucket.period_days - stats.median_days) < abs(
            best[0].period_days - best[1].median_days
        ):
            best = (bucket, stats)
    return best


def _two_day_clusters(days: list[int]) -> tuple[int, int] | None:
    """Two paydays a month: the days of month split into two tight groups.
    Returns the (median) day of each, or None."""
    if len(days) < 4:
        return None
    ordered = sorted(days)
    # Split at the largest circular gap between consecutive days.
    best_split, best_gap = 0, -1
    for i in range(1, len(ordered)):
        gap = ordered[i] - ordered[i - 1]
        if gap > best_gap:
            best_split, best_gap = i, gap
    first, second = ordered[:best_split], ordered[best_split:]
    if not first or not second:
        return None
    if max(first) - min(first) > 3 or max(second) - min(second) > 3:
        return None
    d1, d2 = int(median(first)), int(median(second))
    if d2 - d1 < 10:  # the two groups must be roughly half a month apart
        return None
    return min(d1, 28), min(d2, 28)


def _rrule_for(bucket: Bucket, ordered: list[Occurrence]) -> tuple[str, str]:
    """(cadence label, rrule). Anchor-sensitive rules rely on dtstart = last."""
    last = ordered[-1].date
    days = [o.date.day for o in ordered]
    if bucket.name == "weekly":
        return "weekly", f"FREQ=WEEKLY;BYDAY={_WEEKDAYS[last.weekday()]}"
    if bucket.name == "biweekly":
        weekdays = {o.date.weekday() for o in ordered}
        clusters = _two_day_clusters(days)
        if len(weekdays) > 1 and clusters is not None:
            return "semi-monthly", f"FREQ=MONTHLY;BYMONTHDAY={clusters[0]},{clusters[1]}"
        return "every 2 weeks", f"FREQ=WEEKLY;INTERVAL=2;BYDAY={_WEEKDAYS[last.weekday()]}"
    day = min(int(median(days)), 28)
    if bucket.name == "monthly":
        return "monthly", f"FREQ=MONTHLY;BYMONTHDAY={day}"
    if bucket.name == "bimonthly":
        return "every 2 months", f"FREQ=MONTHLY;INTERVAL=2;BYMONTHDAY={day}"
    if bucket.name == "quarterly":
        return "every 3 months", f"FREQ=MONTHLY;INTERVAL=3;BYMONTHDAY={day}"
    if bucket.name == "semi-annual":
        return "every 6 months", f"FREQ=MONTHLY;INTERVAL=6;BYMONTHDAY={day}"
    return "yearly", f"FREQ=YEARLY;BYMONTH={last.month};BYMONTHDAY={min(last.day, 28)}"


def _population_stdev(amounts: list[Decimal]) -> Decimal:
    """Money stays Decimal end to end (guardrail 1): no float detour through
    statistics.pstdev."""
    n = Decimal(len(amounts))
    mean = sum(amounts, Decimal(0)) / n
    variance = sum(((a - mean) ** 2 for a in amounts), Decimal(0)) / n
    return variance.sqrt()


def _confidence(occurrences: int, gaps: GapStats, cv: Decimal) -> Decimal:
    """Deterministic and explainable: how much evidence, how regular, how
    steady. Weights sum to one; every term is in [0, 1]."""
    evidence = Decimal(min(occurrences, 8)) / Decimal(8)
    regularity = Decimal(str(round(gaps.in_bucket_ratio, 4)))
    steadiness = Decimal(1) - min(cv, Decimal(1))
    score = Decimal("0.4") * evidence + Decimal("0.35") * regularity + Decimal("0.25") * steadiness
    return score.quantize(Decimal("0.001"))


def detect(occurrences: list[Occurrence], *, today: date_type) -> Detected | None:
    """Same account + same counterparty + regular cadence -> a schedule draft's
    worth of facts, or None. Detection only describes; nothing here activates."""
    ordered = collapse_same_day(occurrences)
    if len(ordered) < MIN_OCCURRENCES_YEARLY:
        return None
    gaps = [(ordered[i + 1].date - ordered[i].date).days for i in range(len(ordered) - 1)]
    picked = _pick_bucket(gaps)
    if picked is None:
        return None
    bucket, stats = picked
    if len(ordered) < bucket.min_occurrences:
        return None
    if (today - ordered[-1].date).days > bucket.period_days * LAPSED_CADENCES:
        return None  # the pattern stopped; proposing it would be a stale bill

    amounts = [o.amount for o in ordered]
    typical = Decimal(median(amounts))
    if typical == 0:
        return None
    cv = (_population_stdev(amounts) / abs(typical)).quantize(Decimal("0.0001"))
    cadence, rrule = _rrule_for(bucket, ordered)
    confidence = _confidence(len(ordered), stats, cv)
    if confidence < CONFIDENCE_MIN:
        return None
    return Detected(
        cadence=cadence,
        rrule=rrule,
        period_days=bucket.period_days,
        dtstart=ordered[-1].date,
        amount_type="fixed" if cv <= FIXED_CV_MAX else "estimated",
        typical_amount=typical.quantize(Decimal("0.0001")),
        amount_low=min(amounts),
        amount_high=max(amounts),
        amount_cv=cv,
        occurrences=len(ordered),
        gaps=stats,
        confidence=confidence,
    )
