"""Pure planning of equity-award captures (DESIGN section 8, issue #393,
decision 107). No I/O: part of the mypy --strict core.

An award is a grant (reference, symbol, grant date, granted and cancelled
units) with a dated vest schedule. Units are not owned until they vest, so
nothing here touches a position, a lot or net worth: a vest becomes shares
only through ``record_vest`` (an event plus its lot, or a link to a lot the
broker's list already holds).

A capture is the broker's view on an as-of day: each grant and its vests not
yet delivered. ``plan_awards`` compares it with what Purser holds:

- a new grant, new scheduled vests, a re-estimated scheduled quantity, more
  cancelled units, a newer reported vested count: changes, applied;
- a grant whose symbol, grant date or granted units differ, fewer cancelled
  units, a listed vest Purser already marked vested or forfeited, a schedule
  that exceeds the grant: conflicts, never applied;
- a scheduled vest after the as-of day that a COMPLETE capture does not list:
  ``scheduled_vest_missing`` (forfeit it explicitly if it is gone); one on or
  before the as-of day is counted as due and unrecorded (record the vest);
- a capture older than one applied: compared, never applied.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from uuid import UUID


class AwardError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class VestIn:
    vest_date: date
    quantity: Decimal
    ordinal: int


@dataclass(frozen=True)
class AwardIn:
    index: int
    grant_ref: str  # blind index
    symbol: str
    granted_on: date
    granted: Decimal
    cancelled: Decimal | None
    reported_vested: Decimal | None
    schedule: tuple[VestIn, ...]


@dataclass(frozen=True)
class KnownVest:
    id: UUID
    vest_date: date
    ordinal: int
    quantity: Decimal
    status: str  # scheduled | vested | forfeited
    vested_gross: Decimal | None = None


@dataclass(frozen=True)
class KnownAward:
    id: UUID
    grant_ref: str
    symbol: str
    granted_on: date
    granted: Decimal
    cancelled: Decimal
    reported_vested: Decimal | None
    vests: tuple[KnownVest, ...] = ()
    reported_as_of: date | None = None


@dataclass(frozen=True)
class Change:
    # new_award | new_vest | reschedule | cancel | reported_vested
    kind: str
    award: int | None = None  # the capture's award index
    award_id: UUID | None = None
    vest_id: UUID | None = None
    vest_date: date | None = None
    ordinal: int | None = None
    quantity: Decimal | None = None
    before: Decimal | None = None


@dataclass(frozen=True)
class Conflict:
    code: str
    message: str
    award: int | None = None
    award_id: UUID | None = None
    vest_id: UUID | None = None


@dataclass(frozen=True)
class AwardsPlan:
    changes: tuple[Change, ...]
    conflicts: tuple[Conflict, ...]
    unchanged: int
    due_unrecorded: int
    not_in_capture: int
    notes: tuple[str, ...] = field(default_factory=tuple)


def validate(awards: Sequence[AwardIn]) -> None:
    refs: set[str] = set()
    for a in awards:
        if a.grant_ref in refs:
            raise AwardError("ambiguous_identity", f"award {a.index}: its grant reference repeats")
        refs.add(a.grant_ref)
        if a.granted <= 0:
            raise AwardError("award", f"award {a.index}: granted units must be above zero")
        if a.cancelled is not None and not 0 <= a.cancelled <= a.granted:
            raise AwardError("award", f"award {a.index}: cancelled units are 0 to granted")
        if a.reported_vested is not None and a.reported_vested < 0:
            raise AwardError("award", f"award {a.index}: vested units cannot be negative")
        slots: set[tuple[date, int]] = set()
        for v in a.schedule:
            if v.quantity <= 0:
                raise AwardError("award", f"award {a.index}: a vest's units must be above zero")
            if v.ordinal < 1:
                raise AwardError("award", f"award {a.index}: a vest's ordinal starts at 1")
            if (v.vest_date, v.ordinal) in slots:
                raise AwardError(
                    "ambiguous_identity",
                    f"award {a.index}: two vests on {v.vest_date.isoformat()} need distinct "
                    "ordinals",
                )
            slots.add((v.vest_date, v.ordinal))
            if v.vest_date < a.granted_on:
                raise AwardError(
                    "award", f"award {a.index}: a vest cannot be dated before the grant"
                )


@dataclass(frozen=True)
class VestFact:
    """One vest as the entitlement reads it."""

    vest_date: date
    status: str  # scheduled | vested | forfeited
    quantity: Decimal  # scheduled units
    gross: Decimal | None = None  # vested: the units that vested


@dataclass(frozen=True)
class Entitlement:
    """What an award's units are, from every piece of evidence (decision 107).

    ``vested``: the broker's reported vested count as of its date (the
    baseline) — taken to include every vest Purser recorded on or before that
    date, so a recorded historical vest is never counted twice — plus every
    vest Purser recorded after it; with no baseline, the recorded vests.
    ``remaining`` = granted - cancelled - vested: the unvested units, or None
    when it cannot be known (no reported baseline and the recorded vests and
    schedule do not account for every unit). ``scheduled``: units on dated
    vests still to come; ``unscheduled`` = remaining - scheduled (units with
    no date yet), None when remaining is unknown. ``over``: the evidence
    adds up to more than was granted.

    ``cancelled`` is the units that will never vest: the larger of the
    broker's cancelled count and the units of the vests Purser marked
    forfeited. They describe the same thing — a forfeited vest is what a
    broker reports as cancelled — so the larger is taken and one forfeiture
    is never counted twice; a forfeiture the broker has not reported yet
    still reduces the entitlement. ``forfeited`` is that vest total,
    ``reported_cancelled`` the broker's count."""

    granted: Decimal
    cancelled: Decimal
    vested: Decimal
    scheduled: Decimal
    remaining: Decimal | None
    unscheduled: Decimal | None
    over: bool
    forfeited: Decimal = Decimal(0)
    reported_cancelled: Decimal = Decimal(0)

    @property
    def unvested(self) -> Decimal:
        """The units to report as unvested: remaining when known, else the
        scheduled units (the only ones evidenced)."""
        return self.remaining if self.remaining is not None else self.scheduled


def entitlement(
    *,
    granted: Decimal,
    cancelled: Decimal,
    reported_vested: Decimal | None,
    reported_as_of: date | None,
    vests: Sequence[VestFact],
) -> Entitlement:
    vested_rows = [v for v in vests if v.status == "vested"]
    scheduled = sum((v.quantity for v in vests if v.status == "scheduled"), Decimal(0))
    forfeited = sum((v.quantity for v in vests if v.status == "forfeited"), Decimal(0))
    reported_cancelled = cancelled
    cancelled = max(reported_cancelled, forfeited)

    def gross(v: VestFact) -> Decimal:
        return v.gross if v.gross is not None else v.quantity

    if reported_vested is not None:
        before = sum(
            (
                gross(v)
                for v in vested_rows
                if reported_as_of is None or v.vest_date <= reported_as_of
            ),
            Decimal(0),
        )
        after = sum(
            (
                gross(v)
                for v in vested_rows
                if reported_as_of is not None and v.vest_date > reported_as_of
            ),
            Decimal(0),
        )
        vested = max(reported_vested, before) + after
        remaining: Decimal | None = granted - cancelled - vested
    else:
        vested = sum((gross(v) for v in vested_rows), Decimal(0))
        accounted = vested + scheduled + cancelled
        remaining = scheduled if accounted == granted else None
    over = vested + scheduled + cancelled > granted
    if remaining is not None and remaining < 0:
        remaining = Decimal(0)
    unscheduled = max(remaining - scheduled, Decimal(0)) if remaining is not None else None
    return Entitlement(
        granted,
        cancelled,
        vested,
        scheduled,
        remaining,
        unscheduled,
        over,
        forfeited,
        reported_cancelled,
    )


def _within(a: AwardIn, known: KnownAward | None, cancelled: Decimal, as_of: date) -> bool:
    """Whether the award as the capture would leave it fits its grant: the
    reported baseline (kept from Purser when the capture omits it), the
    vests Purser recorded, the capture's schedule replacing the scheduled
    vests it lists, and the cancelled units."""
    listed = {(v.vest_date, v.ordinal) for v in a.schedule}
    vests: list[VestFact] = [VestFact(v.vest_date, "scheduled", v.quantity) for v in a.schedule]
    for v in known.vests if known else ():
        if v.status == "scheduled" and (v.vest_date, v.ordinal) in listed:
            continue
        vests.append(VestFact(v.vest_date, v.status, v.quantity, v.vested_gross))
    baseline: Decimal | None
    baseline_as_of: date | None
    if a.reported_vested is not None:
        baseline, baseline_as_of = a.reported_vested, as_of
    elif known is not None:
        baseline, baseline_as_of = known.reported_vested, known.reported_as_of
    else:
        baseline, baseline_as_of = None, None
    return not entitlement(
        granted=a.granted,
        cancelled=cancelled,
        reported_vested=baseline,
        reported_as_of=baseline_as_of,
        vests=vests,
    ).over


def plan_awards(
    awards: Sequence[AwardIn],
    known: Sequence[KnownAward],
    *,
    as_of: date,
    complete: bool,
    stale: bool,
) -> AwardsPlan:
    """Compare one account's captured awards with Purser's (pure)."""
    changes: list[Change] = []
    conflicts: list[Conflict] = []
    unchanged = 0
    due = 0
    not_in = 0
    by_ref = {k.grant_ref: k for k in known}
    seen: set[UUID] = set()
    for a in awards:
        k = by_ref.get(a.grant_ref)
        cancelled = a.cancelled if a.cancelled is not None else (k.cancelled if k else Decimal(0))
        if not _within(a, k, cancelled, as_of):
            conflicts.append(
                Conflict(
                    "schedule_exceeds_grant",
                    f"award {a.index}: its schedule, vested and cancelled units add up to more "
                    "than were granted",
                    a.index,
                    k.id if k else None,
                )
            )
            if k:
                seen.add(k.id)
            continue
        if k is None:
            changes.append(Change("new_award", a.index))
            for v in a.schedule:
                changes.append(
                    Change("new_vest", a.index, None, None, v.vest_date, v.ordinal, v.quantity)
                )
            continue
        seen.add(k.id)
        if k.symbol != a.symbol or k.granted_on != a.granted_on:
            conflicts.append(
                Conflict(
                    "identity_mismatch",
                    f"award {a.index}: its grant reference names award {k.id}, of another "
                    "symbol or grant date",
                    a.index,
                    k.id,
                )
            )
            continue
        if k.granted != a.granted:
            conflicts.append(
                Conflict(
                    "grant_changed",
                    f"award {a.index}: granted units differ from Purser's ({a.granted} vs "
                    f"{k.granted}); a grant's size does not change — check the grant, or "
                    "correct the award (PATCH /awards/{id}) if Purser's was wrong",
                    a.index,
                    k.id,
                )
            )
            continue
        award_changes: list[Change] = []
        if a.cancelled is not None and a.cancelled != k.cancelled:
            if a.cancelled < k.cancelled:
                conflicts.append(
                    Conflict(
                        "cancelled_decrease",
                        f"award {a.index}: the broker shows fewer cancelled units "
                        f"({a.cancelled}) than Purser ({k.cancelled}); cancelled units do not "
                        "come back",
                        a.index,
                        k.id,
                    )
                )
                continue
            award_changes.append(
                Change("cancel", a.index, k.id, quantity=a.cancelled, before=k.cancelled)
            )
        if a.reported_vested is not None and a.reported_vested != k.reported_vested:
            award_changes.append(
                Change(
                    "reported_vested",
                    a.index,
                    k.id,
                    quantity=a.reported_vested,
                    before=k.reported_vested,
                )
            )
        slots = {(v.vest_date, v.ordinal): v for v in k.vests}
        listed: set[UUID] = set()
        bad = False
        for v in a.schedule:
            have = slots.get((v.vest_date, v.ordinal))
            if have is None:
                award_changes.append(
                    Change("new_vest", a.index, k.id, None, v.vest_date, v.ordinal, v.quantity)
                )
                continue
            listed.add(have.id)
            if have.status != "scheduled":
                conflicts.append(
                    Conflict(
                        "vest_state",
                        f"award {a.index}: the broker lists the {v.vest_date.isoformat()} vest "
                        f"as still to come, Purser has it {have.status}; if that was wrong, "
                        "reverse the vest event or set the vest back to scheduled",
                        a.index,
                        k.id,
                        have.id,
                    )
                )
                bad = True
                continue
            if have.quantity != v.quantity:
                award_changes.append(
                    Change(
                        "reschedule",
                        a.index,
                        k.id,
                        have.id,
                        v.vest_date,
                        v.ordinal,
                        v.quantity,
                        have.quantity,
                    )
                )
        if bad:
            continue
        for kv in k.vests:
            if kv.status != "scheduled" or kv.id in listed:
                continue
            if kv.vest_date <= as_of:
                due += 1
            elif complete:
                conflicts.append(
                    Conflict(
                        "scheduled_vest_missing",
                        f"award {a.index}: the {kv.vest_date.isoformat()} vest is scheduled in "
                        "Purser and the complete capture does not list it; forfeit it "
                        "(PATCH /award-vests/{id}) if it is gone, or record it if it vested",
                        a.index,
                        k.id,
                        kv.id,
                    )
                )
                bad = True
            else:
                not_in += 1
        if bad:
            continue
        if award_changes:
            changes.extend(award_changes)
        else:
            unchanged += 1
    for k in known:
        if k.id in seen:
            continue
        upcoming = [v for v in k.vests if v.status == "scheduled" and v.vest_date > as_of]
        if not upcoming:
            continue
        if complete:
            conflicts.append(
                Conflict(
                    "award_missing",
                    f"award {k.id} has {len(upcoming)} scheduled vest(s) after "
                    f"{as_of.isoformat()} and the complete capture does not list it; forfeit "
                    "its vests if the grant is gone",
                    None,
                    k.id,
                )
            )
        else:
            not_in += 1
    if stale:
        prefix = f"this capture (as_of {as_of.isoformat()}) is older than one already applied; "
        out = [
            Conflict("stale_capture", prefix + c.message, c.award, c.award_id) for c in conflicts
        ]
        out.extend(
            Conflict("stale_capture", prefix + f"its {c.kind} is not applied", c.award, c.award_id)
            for c in changes
        )
        return AwardsPlan((), tuple(out), unchanged, due, not_in)
    # An award in conflict contributed no change; the caller applies the
    # others only when told to skip conflicts.
    return AwardsPlan(tuple(changes), tuple(conflicts), unchanged, due, not_in)
