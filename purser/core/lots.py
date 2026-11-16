"""Pure lot accounting for hand-tracked positions (DESIGN section 8, issue
#393, decision 107). No I/O: part of the mypy --strict core. The service
(``purser.holdings.lots``) loads the facts and persists what these functions
decide.

A position's share count is still the history of its events
(``core.holdings.quantity_timeline``). Lots are a sub-ledger beside it:

- A lot is opened by one event (a buy with a lot, a vest) and counts from that
  event's day, or it is ADOPTED from a capture and counts from the end of the
  capture's as-of day (shares disposed of that day are already out of the
  broker's figure, so only a later day may draw on it).
- A lot's remaining quantity is its quantity less its live dispositions. A
  disposition is live while its event is effective; a lot is live while its
  opening event is (voided lots are never loaded).
- The coverage invariant (``check``): on every day, the position holds at
  least what its counted lots have left; the difference is the UNALLOCATED
  remainder, derived and never stored. Every lot stays at or above zero.
- Once a lot can be drawn on, every sell or transfer_out is fully allocated
  (to lots, and to the unallocated remainder only by name).
- A position that tracks lots on a day takes its basis from them: the sum of
  the counted lots' remaining basis, unknown (None) when any of them has no
  basis or shares sit outside every lot. The running average-cost basis is
  never shown as a lot's cost.

Arithmetic runs in the 60-digit context of ``core.holdings``.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext
from typing import Any
from uuid import UUID

from purser.core import holdings as core

FIFO = "fifo"
LIFO = "lifo"
HIFO = "hifo"
SPECIFIC = "specific"
METHODS = (FIFO, LIFO, HIFO, SPECIFIC)
SELECTION_METHODS = (FIFO, LIFO, HIFO)

ACQUISITIONS = (
    "buy",
    "rsu_vest",
    "plan_purchase",
    "espp_purchase",
    "transfer_in",
    "drip",
    "gift",
    "unknown",
)

UNALLOCATED = "unallocated"
_PRECISION = 60
CENT = Decimal("0.01")


class LotError(ValueError):
    """A lot history that breaks an invariant. ``code`` names the rule,
    ``on`` the first day it breaks (when it is a day's)."""

    def __init__(self, code: str, message: str, on: date | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.on = on


@dataclass(frozen=True)
class LotFact:
    """One lot as the invariants read it."""

    id: UUID
    acquired_on: date
    effective_on: date
    quantity: Decimal
    cost_basis: Decimal | None
    basis_per_share: Decimal | None
    ordinal: int
    adopted: bool  # from a capture: counts from the END of effective_on
    opened_by: UUID | None = None  # the opening event; None when adopted

    def counted_on(self, day: date) -> bool:
        """Whether the lot is part of the position at the end of ``day``."""
        return day >= self.effective_on

    def disposable_on(self, day: date) -> bool:
        """Whether an event dated ``day`` may draw on the lot."""
        return day > self.effective_on if self.adopted else day >= self.effective_on


@dataclass(frozen=True)
class Disposal:
    """Shares of one lot (``lot_id``) or of the unallocated remainder
    (``lot_id`` None) leaving with one event."""

    event_id: UUID
    lot_id: UUID | None
    on: date
    quantity: Decimal
    cost_basis: Decimal | None = None


@dataclass(frozen=True)
class Book:
    """A position's LIVE lots and dispositions (``live`` filters them). It
    implements ``core.holdings.LotBasis`` for the projection."""

    lots: tuple[LotFact, ...]
    disposals: tuple[Disposal, ...] = ()
    _by_lot: Mapping[UUID, tuple[Disposal, ...]] = field(
        default_factory=dict, compare=False, repr=False
    )

    def __post_init__(self) -> None:
        grouped: dict[UUID, list[Disposal]] = defaultdict(list)
        for d in self.disposals:
            if d.lot_id is not None:
                grouped[d.lot_id].append(d)
        object.__setattr__(self, "_by_lot", {k: tuple(v) for k, v in grouped.items()})

    @property
    def tracked(self) -> bool:
        return bool(self.lots)

    def lot(self, lot_id: UUID) -> LotFact | None:
        return next((lot for lot in self.lots if lot.id == lot_id), None)

    def of(self, lot_id: UUID) -> tuple[Disposal, ...]:
        return self._by_lot.get(lot_id, ())

    def remaining(self, lot: LotFact, day: date | None = None) -> Decimal:
        """What the lot has left at the end of ``day`` (after every live
        disposition when ``day`` is None)."""
        out = lot.quantity
        for d in self.of(lot.id):
            if day is None or d.on <= day:
                out -= d.quantity
        return out

    def remaining_basis(self, lot: LotFact, day: date | None = None) -> Decimal | None:
        """The lot's cost basis less its dispositions' basis: conserved by
        construction (the disposition that empties a lot takes the remainder).
        None when the lot's basis is unknown."""
        if lot.cost_basis is None:
            return None
        out = lot.cost_basis
        for d in self.of(lot.id):
            if day is None or d.on <= day:
                if d.cost_basis is None:
                    return None
                out -= d.cost_basis
        return out

    def first_disposable(self) -> date | None:
        """The first day an event may draw on a lot."""
        days = [
            lot.effective_on if not lot.adopted else _next_day(lot.effective_on)
            for lot in self.lots
        ]
        return min(days) if days else None

    def first_counted(self) -> date | None:
        days = [lot.effective_on for lot in self.lots]
        return min(days) if days else None

    def counted(self, day: date) -> list[LotFact]:
        return [lot for lot in self.lots if lot.counted_on(day)]

    def covered(self, day: date) -> Decimal:
        """Shares the counted lots hold at the end of ``day``."""
        return sum((self.remaining(lot, day) for lot in self.counted(day)), Decimal(0))

    # core.holdings.LotBasis
    def change_dates(self) -> Iterable[date]:
        return sorted({lot.effective_on for lot in self.lots} | {d.on for d in self.disposals})

    def basis_on(self, day: date, quantity: Decimal, average: Decimal | None) -> Decimal | None:
        counted = self.counted(day)
        if not counted:
            return average
        return position_basis(self, day, quantity).basis


@dataclass(frozen=True)
class Basis:
    """A lot-tracked position's basis on a day: ``known`` sums the counted
    lots' remaining basis where known; ``unknown_quantity`` is held in lots
    with no basis; ``unallocated`` outside every lot. ``basis`` is the total,
    or None unless every share has a known basis."""

    known: Decimal
    unknown_quantity: Decimal
    unallocated: Decimal
    lots_quantity: Decimal

    @property
    def basis(self) -> Decimal | None:
        if self.unknown_quantity != 0 or self.unallocated != 0:
            return None
        return self.known


def position_basis(book: Book, day: date, quantity: Decimal) -> Basis:
    known = Decimal(0)
    unknown = Decimal(0)
    in_lots = Decimal(0)
    for lot in book.counted(day):
        left = book.remaining(lot, day)
        if left == 0:
            continue
        in_lots += left
        basis = book.remaining_basis(lot, day)
        if basis is None:
            unknown += left
        else:
            known += basis
    return Basis(
        known=core.money(known),
        unknown_quantity=unknown,
        unallocated=max(quantity - in_lots, Decimal(0)),
        lots_quantity=in_lots,
    )


def _next_day(day: date) -> date:
    return date.fromordinal(day.toordinal() + 1)


def live(
    lots: Iterable[LotFact],
    disposals: Iterable[Disposal],
    effective_ids: Iterable[UUID],
) -> Book:
    """The lots whose opening event is effective (or that were adopted) and
    the dispositions whose event is effective."""
    ids = set(effective_ids)
    return Book(
        lots=tuple(
            sorted(
                (lot for lot in lots if lot.opened_by is None or lot.opened_by in ids),
                key=lambda lot: (lot.acquired_on, lot.ordinal, lot.effective_on, lot.id),
            )
        ),
        disposals=tuple(d for d in disposals if d.event_id in ids),
    )


# --------------------------------------------------------------------------
# The invariants
# --------------------------------------------------------------------------


def check(events: Sequence[core.Event], book: Book) -> core.Timeline:
    """The position's timeline, after checking its lots against it. Raises
    ``core.NegativeQuantity`` / ``core.OutOfRange`` like the timeline, and
    ``LotError`` for:

    - ``dead_lot``: a live disposition of a lot that is not live (its
      opening event reversed, or the lot voided);
    - ``before_lot``: a disposition dated before its lot can be drawn on;
    - ``overdrawn``: a lot left below zero;
    - ``unallocated_disposal``: a sell or transfer_out on a day lots can be
      drawn on whose dispositions do not add up to its quantity (or one with
      dispositions that do not);
    - ``coverage``: a day on which the counted lots hold more than the
      position.
    """
    effective = core.effective_events(events)
    timeline = core.Timeline(core.quantity_timeline(effective))
    lots = {lot.id: lot for lot in book.lots}
    per_event: dict[UUID, Decimal] = defaultdict(Decimal)
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        for d in book.disposals:
            per_event[d.event_id] += d.quantity
            if d.lot_id is None:
                continue
            lot = lots.get(d.lot_id)
            if lot is None:
                raise LotError(
                    "dead_lot",
                    f"a disposition on {d.on.isoformat()} draws on lot {d.lot_id}, which is "
                    "voided or whose opening event is reversed; reverse that disposal first",
                    d.on,
                )
            if not lot.disposable_on(d.on):
                raise LotError(
                    "before_lot",
                    f"lot {lot.id} counts from {lot.effective_on.isoformat()}"
                    f"{' (end of day)' if lot.adopted else ''}; a disposal on "
                    f"{d.on.isoformat()} cannot draw on it",
                    d.on,
                )
        for lot in book.lots:
            left = book.remaining(lot)
            if left < 0:
                first = _first_negative(book, lot)
                raise LotError(
                    "overdrawn",
                    f"lot {lot.id} would hold {left} shares; a lot never goes below zero",
                    first,
                )
        start = book.first_disposable()
        for event in effective:
            if event.kind not in core.REMOVES:
                if event.id in per_event:
                    raise LotError(
                        "unallocated_disposal",
                        f"only a sell or transfer_out disposes of lots ({event.kind} on "
                        f"{event.occurred_on.isoformat()})",
                        event.occurred_on,
                    )
                continue
            allocated = per_event.get(event.id, Decimal(0))
            needs = start is not None and event.occurred_on >= start
            if (needs or event.id in per_event) and allocated != event.quantity:
                raise LotError(
                    "unallocated_disposal",
                    f"the {event.kind} of {event.quantity} on {event.occurred_on.isoformat()} "
                    f"allocates {allocated} to lots; this position tracks lots, so every "
                    "disposal names its lots (or 'unallocated' for shares outside them)",
                    event.occurred_on,
                )
        days = (
            {p.on for p in timeline.points}
            | {lot.effective_on for lot in book.lots}
            | {d.on for d in book.disposals}
        )
        for day in sorted(days):
            covered = book.covered(day)
            held = timeline.quantity_on(day)
            if covered > held:
                raise LotError(
                    "coverage",
                    f"on {day.isoformat()} the position's lots would hold {covered} shares "
                    f"but the position holds {held}; lots never hold more than the position",
                    day,
                )
    return timeline


def _first_negative(book: Book, lot: LotFact) -> date | None:
    for day in sorted({d.on for d in book.of(lot.id)}):
        if book.remaining(lot, day) < 0:
            return day
    return None


# --------------------------------------------------------------------------
# Selection and allocation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Allocation:
    """Shares of one lot (or of the unallocated remainder: ``lot`` None)
    that one disposal takes, with the basis, proceeds and term they carry."""

    lot: LotFact | None
    quantity: Decimal
    cost_basis: Decimal | None
    proceeds: Decimal | None
    realized_gain: Decimal | None
    term: str | None


def available(book: Book, lot: LotFact) -> Decimal:
    """What a NEW disposal may take from ``lot``: its remaining quantity
    after every live disposition, whatever their dates (a backdated sale
    only draws on shares no later disposal consumed)."""
    return book.remaining(lot)


def candidates(book: Book, day: date) -> list[LotFact]:
    return [lot for lot in book.lots if lot.disposable_on(day) and available(book, lot) > 0]


def _ordered(method: str, lots: list[LotFact]) -> list[LotFact]:
    fifo = sorted(lots, key=lambda lot: (lot.acquired_on, lot.ordinal, lot.effective_on, lot.id))
    if method == FIFO:
        return fifo
    if method == LIFO:
        return list(reversed(fifo))
    if method == HIFO:
        unknown = [lot for lot in lots if lot.basis_per_share is None]
        if unknown:
            raise LotError(
                "hifo_unknown_basis",
                f"hifo ranks lots by basis per share, and {len(unknown)} lot(s) that could "
                "be drawn on have no basis; name the lots (specific) instead",
            )
        return sorted(
            fifo,
            key=lambda lot: -(lot.basis_per_share or Decimal(0)),
        )
    raise LotError("method", f"lot_method must be one of: {', '.join(SELECTION_METHODS)}")


def select(method: str, book: Book, day: date, quantity: Decimal) -> list[tuple[LotFact, Decimal]]:
    """The lots a disposal of ``quantity`` on ``day`` takes under a method:
    FIFO by (acquired_on, ordinal), LIFO the reverse, HIFO by basis per
    share, highest first (ties FIFO; refused when a candidate has no basis).
    A method never reaches into the unallocated remainder: when the lots
    cannot cover ``quantity``, it is refused (``insufficient_lots``)."""
    picked: list[tuple[LotFact, Decimal]] = []
    left = quantity
    for lot in _ordered(method, candidates(book, day)):
        if left <= 0:
            break
        take = min(available(book, lot), left)
        picked.append((lot, take))
        left -= take
    if left > 0:
        raise LotError(
            "insufficient_lots",
            f"the lots that can be drawn on {day.isoformat()} hold "
            f"{quantity - left} of the {quantity} shares; name the rest as "
            f"'{UNALLOCATED}' (shares outside every lot) or record the missing lots",
            day,
        )
    return picked


def specific(
    book: Book, day: date, quantity: Decimal, pairs: Sequence[tuple[UUID | None, Decimal]]
) -> list[tuple[LotFact | None, Decimal]]:
    """Validate a specific identification: every lot live, drawable on
    ``day``, named once, taking more than zero and at most what it has left;
    the quantities add up to ``quantity``. ``None`` names the unallocated
    remainder (the coverage check proves there is enough of it)."""
    seen: set[UUID | None] = set()
    out: list[tuple[LotFact | None, Decimal]] = []
    total = Decimal(0)
    for lot_id, q in pairs:
        if q <= 0:
            raise LotError("allocation", "each allocation takes more than zero shares")
        if lot_id in seen:
            raise LotError("allocation", f"lot {lot_id or UNALLOCATED} is named twice")
        seen.add(lot_id)
        total += q
        if lot_id is None:
            out.append((None, q))
            continue
        lot = book.lot(lot_id)
        if lot is None:
            raise LotError("allocation", f"lot {lot_id} is not a live lot of this position", day)
        if not lot.disposable_on(day):
            raise LotError(
                "before_lot",
                f"lot {lot_id} counts from {lot.effective_on.isoformat()}"
                f"{' (end of day)' if lot.adopted else ''}; a disposal on {day.isoformat()} "
                "cannot draw on it",
                day,
            )
        left = available(book, lot)
        if q > left:
            raise LotError(
                "overdrawn", f"lot {lot_id} has {left} shares left; the allocation takes {q}", day
            )
        out.append((lot, q))
    if total != quantity:
        raise LotError(
            "allocation",
            f"the allocations add up to {total}; the disposal is {quantity} shares",
        )
    return out


def anniversary(acquired: date) -> date:
    """One year after ``acquired`` (Feb 29 -> Feb 28)."""
    try:
        return acquired.replace(year=acquired.year + 1)
    except ValueError:
        return acquired.replace(year=acquired.year + 1, day=28)


def term(acquired: date, disposed: date) -> str:
    """``long`` only when disposed of AFTER the one-year anniversary: the
    anniversary itself is short-term. Informational; Purser computes no tax."""
    return "long" if disposed > anniversary(acquired) else "short"


def disposal_basis(book: Book, lot: LotFact, quantity: Decimal) -> Decimal | None:
    """The basis ``quantity`` shares of ``lot`` carry: pro rata to the lot's
    total, rounded to 4 places, never more than the lot has left; the
    disposal that empties the lot takes the whole remainder, so the lot's
    dispositions and its remaining basis always sum to its basis exactly."""
    left_basis = book.remaining_basis(lot)
    if left_basis is None or lot.cost_basis is None:
        return None
    if quantity == available(book, lot):
        return left_basis
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        share = core.money(lot.cost_basis * quantity / lot.quantity)
    return max(min(share, left_basis), Decimal(0))


def split_amount(total: Decimal | None, quantities: Sequence[Decimal]) -> list[Decimal | None]:
    """``total`` spread over ``quantities`` pro rata in units of 0.0001, the
    parts summing to ``total`` exactly and each of its sign (never a part
    of the other sign, however small the total or many the parts): every
    part takes its share rounded toward zero, and the units left over go one
    each to the largest fractional remainders (ties to the earlier part)."""
    if total is None:
        return [None] * len(quantities)
    if not quantities:
        return []
    whole = sum(quantities, Decimal(0))
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        units = int((abs(core.money(total)) / core.MONEY_QUANT).to_integral_value())
        sign = -1 if total < 0 else 1
        if whole <= 0:
            shares = [Decimal(units) / len(quantities)] * len(quantities)
        else:
            shares = [Decimal(units) * q / whole for q in quantities]
        floors = [int(x) for x in shares]  # toward zero: shares are >= 0
        left = units - sum(floors)
        order = sorted(range(len(shares)), key=lambda i: (-(shares[i] - floors[i]), i))
        for i in order[:left]:
            floors[i] += 1
        return [sign * Decimal(f) * core.MONEY_QUANT for f in floors]


def allocate(
    book: Book,
    day: date,
    pieces: Sequence[tuple[LotFact | None, Decimal]],
    proceeds: Decimal | None,
) -> list[Allocation]:
    """Basis, proceeds, gain and term for each piece of one disposal.
    ``proceeds`` is the disposal's net proceeds (None when it has no price:
    a transfer out, an unpriced sale)."""
    parts = split_amount(proceeds, [q for _lot, q in pieces])
    out: list[Allocation] = []
    for (lot, q), part in zip(pieces, parts, strict=True):
        if lot is None:
            out.append(Allocation(None, q, None, part, None, None))
            continue
        basis = disposal_basis(book, lot, q)
        gain = core.money(part - basis) if part is not None and basis is not None else None
        out.append(Allocation(lot, q, basis, part, gain, term(lot.acquired_on, day)))
    return out


def with_disposals(
    book: Book, event_id: UUID, day: date, allocations: Sequence[Allocation]
) -> Book:
    """``book`` with one more event's dispositions."""
    return Book(
        lots=book.lots,
        disposals=book.disposals
        + tuple(
            Disposal(
                event_id=event_id,
                lot_id=a.lot.id if a.lot is not None else None,
                on=day,
                quantity=a.quantity,
                cost_basis=a.cost_basis,
            )
            for a in allocations
        ),
    )


def with_lots(book: Book, lots: Iterable[LotFact]) -> Book:
    return Book(lots=book.lots + tuple(lots), disposals=book.disposals)


# --------------------------------------------------------------------------
# Basis as given
# --------------------------------------------------------------------------


def check_basis_per_share(value: Decimal) -> Decimal:
    """A per-share basis NUMERIC(20,6) holds exactly; zero is a basis (a
    gift can carry one), unknown is None."""
    if value == 0:
        return _checked(core.check_quantity, value, "basis_per_share")
    return _checked(core.check_price, value, "basis_per_share")


@dataclass(frozen=True)
class LotBasisInput:
    basis_per_share: Decimal | None
    cost_basis: Decimal | None
    source: str | None  # per_share | total | both | event; None when unknown


def basis_tolerance(quantity: Decimal) -> Decimal:
    """How far a per-share basis (6 places) times a quantity may sit from a
    total stated in cents and still be the same basis."""
    return quantity * Decimal("0.000001") + CENT


def normalize_basis(
    quantity: Decimal, basis_per_share: Decimal | None, cost_basis: Decimal | None
) -> LotBasisInput:
    """A lot's basis from what the broker shows: per share (6 places), a
    total (4 places), both (checked to agree), or neither (unknown — never
    0, never an average). A given total is authoritative and kept as given;
    a derived per-share figure or total must fit its column
    (NUMERIC(20,6) / NUMERIC(20,4)), or the basis is refused."""
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        if basis_per_share is None and cost_basis is None:
            return LotBasisInput(None, None, None)
        if quantity <= 0:
            # A row with no shares left carries no basis to keep (a fully
            # sold lot the broker still lists, often with a total of 0).
            return LotBasisInput(None, None, None)
        if basis_per_share is not None and basis_per_share < 0:
            raise LotError("basis", "basis_per_share cannot be negative")
        if cost_basis is not None and cost_basis < 0:
            raise LotError("basis", "cost_basis cannot be negative")
        if cost_basis is None:
            assert basis_per_share is not None
            per_share = check_basis_per_share(basis_per_share)
            return LotBasisInput(per_share, _fit_total(per_share * quantity), "per_share")
        total = _checked(core.check_money, cost_basis, "cost_basis")
        if basis_per_share is None:
            derived = (total / quantity).quantize(core.PRICE_QUANT)
            return LotBasisInput(_fit_per_share(derived), total, "total")
        per_share = check_basis_per_share(basis_per_share)
        if abs(per_share * quantity - total) > basis_tolerance(quantity):
            raise LotError(
                "basis",
                f"basis_per_share {per_share} x {quantity} shares is "
                f"{core.money(per_share * quantity)}, not the cost_basis {total}; give one "
                "of them, or both that agree",
            )
        return LotBasisInput(per_share, total, "both")


def _checked(fn: Any, value: Decimal, field: str) -> Decimal:
    try:
        out: Decimal = fn(value, field)
    except ValueError as exc:
        raise LotError("basis", str(exc)) from exc
    return out


def _fit_total(value: Decimal) -> Decimal:
    total = core.money(value)
    if abs(total) >= core.MAX_MONEY:
        raise LotError("basis", f"a total basis of {total} is out of range for an amount")
    return total


def _fit_per_share(value: Decimal) -> Decimal:
    if value >= core.MAX_PRICE:
        raise LotError("basis", f"a basis per share of {value} is out of range for a price")
    return value


def same_basis(lot: LotFact, quantity: Decimal, given: LotBasisInput) -> bool:
    """Whether a reported basis (for ``quantity`` remaining shares) is the
    lot's basis: compared per share, within the cents a total is stated in."""
    if lot.basis_per_share is None or given.basis_per_share is None:
        return lot.basis_per_share is None and given.basis_per_share is None
    diff = abs(lot.basis_per_share - given.basis_per_share) * max(quantity, Decimal(1))
    return diff <= basis_tolerance(quantity)


# --------------------------------------------------------------------------
# Captures: comparing a broker's lot list with the book
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CaptureRow:
    """One validated lot row of a capture. ``ref``/``plan`` are blind
    indexes: the planner compares identities, never plaintext."""

    index: int
    ref: str | None
    acquired_on: date
    plan: str | None
    ordinal: int | None
    quantity: Decimal  # remaining on the capture's as-of day
    basis: LotBasisInput
    acquisition: str


@dataclass(frozen=True)
class KnownLot:
    fact: LotFact
    ref: str | None
    plan: str | None
    disposed: bool  # has live dispositions (its basis is then fixed)


@dataclass(frozen=True)
class Change:
    kind: str  # new_lot | fill_basis | replace_basis | assign_ref | recount | open_position
    row: int | None = None
    lot_id: UUID | None = None
    quantity: Decimal | None = None
    ordinal: int | None = None
    detail: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Conflict:
    code: str
    message: str
    row: int | None = None
    lot_id: UUID | None = None


@dataclass(frozen=True)
class SymbolPlan:
    changes: tuple[Change, ...]
    conflicts: tuple[Conflict, ...]
    unchanged: int  # rows that matched a lot exactly
    skipped_zero: int  # zero rows Purser never saw
    not_in_capture: int  # partial: live lots the capture did not list
    target_quantity: Decimal | None  # complete: the broker's share total


def validate_rows(rows: Sequence[CaptureRow], as_of: date) -> list[CaptureRow]:
    """Refuse a payload whose rows cannot be told apart: a reference named
    twice, or reference-less rows of one (acquired_on, plan) without
    distinct ordinals. A row's ordinal defaults to 1 when it alone has its
    (acquired_on, plan). Also refused: a row acquired after the as-of day,
    or holding fewer than zero shares."""
    refs: set[str] = set()
    keyed: dict[tuple[date, str | None], list[CaptureRow]] = defaultdict(list)
    for row in rows:
        if row.quantity < 0:
            raise LotError("row", f"row {row.index}: quantity cannot be negative")
        if row.acquired_on > as_of:
            raise LotError("row", f"row {row.index}: acquired_on is after the capture's as_of")
        if row.acquisition not in ACQUISITIONS:
            raise LotError(
                "row", f"row {row.index}: acquisition must be one of: {', '.join(ACQUISITIONS)}"
            )
        if row.ref is not None:
            if row.ref in refs:
                raise LotError("ambiguous_identity", f"row {row.index}: its reference repeats")
            refs.add(row.ref)
        else:
            keyed[(row.acquired_on, row.plan)].append(row)
    out: dict[int, CaptureRow] = {r.index: r for r in rows}
    for (acquired, _plan), group in keyed.items():
        if len(group) == 1 and group[0].ordinal is None:
            r = group[0]
            out[r.index] = CaptureRow(
                r.index, r.ref, r.acquired_on, r.plan, 1, r.quantity, r.basis, r.acquisition
            )
            continue
        ordinals = [r.ordinal for r in group]
        if any(o is None for o in ordinals) or len(set(ordinals)) != len(ordinals):
            raise LotError(
                "ambiguous_identity",
                f"rows {', '.join(str(r.index) for r in group)} share an acquisition date "
                f"({acquired.isoformat()}) and plan with no reference; give each its broker "
                "reference (external_ref) or a distinct ordinal",
            )
    return [out[r.index] for r in rows]


def plan_capture(
    rows: Sequence[CaptureRow],
    known: Sequence[KnownLot],
    events: Sequence[core.Event],
    book: Book,
    *,
    as_of: date,
    complete: bool,
    stale: bool,
    replace_basis: bool,
) -> SymbolPlan:
    """Compare one symbol's captured lots with the position (pure). Nothing
    the broker leaves out is ever taken away, and nothing it reports that
    the book cannot explain is applied: those become conflicts.

    - A row matches a lot by reference, else by (acquired_on, plan, ordinal).
      A referenced row meeting exactly one reference-less lot of its date and
      plan assigns the reference; meeting several is ambiguous.
    - A matched row equal to the lot's remaining on ``as_of`` changes
      nothing; a lower figure is ``quantity_decrease`` (record the sale or
      transfer out first), a higher one ``quantity_increase``.
    - Basis: a known lot basis that differs is ``basis_differs`` unless
      ``replace_basis``; an unknown one is filled. Either is
      ``basis_locked`` while the lot has live dispositions.
    - An unmatched row with shares is a new lot adopted at the end of
      ``as_of``; a zero row Purser never saw is skipped.
    - ``complete``: a live lot with shares left on ``as_of`` and no row is
      ``missing_from_capture``, and the position is recounted UP to the rows'
      total when that is more (the new lots); a total below Purser's count is
      ``unexplained_decrease`` (record the disposal first). A partial capture
      never changes the count: a new lot must fit in the position's unallocated shares on every day
      from ``as_of`` (``insufficient_unallocated``).
    - ``stale`` (older than the newest capture applied): differences are
      conflicts, nothing is applied.
    """
    conflicts: list[Conflict] = []
    changes: list[Change] = []
    unchanged = 0
    skipped_zero = 0
    by_ref = {k.ref: k for k in known if k.ref is not None}
    by_key: dict[tuple[date, str | None, int], KnownLot] = {
        (k.fact.acquired_on, k.plan, k.fact.ordinal): k for k in known
    }
    matched: dict[UUID, int] = {}
    new_rows: list[CaptureRow] = []

    def claim(lot: KnownLot, row: CaptureRow) -> bool:
        prior = matched.get(lot.fact.id)
        if prior is not None:
            conflicts.append(
                Conflict(
                    "ambiguous_identity",
                    f"rows {prior} and {row.index} both match lot {lot.fact.id}",
                    row.index,
                    lot.fact.id,
                )
            )
            return False
        matched[lot.fact.id] = row.index
        return True

    for row in rows:
        lot: KnownLot | None = None
        assign = False
        if row.ref is not None:
            lot = by_ref.get(row.ref)
            if lot is None:
                bare = [
                    k
                    for k in known
                    if k.ref is None
                    and k.fact.acquired_on == row.acquired_on
                    and k.plan == row.plan
                    and (row.ordinal is None or k.fact.ordinal == row.ordinal)
                ]
                if len(bare) > 1:
                    conflicts.append(
                        Conflict(
                            "ambiguous_identity",
                            f"row {row.index}: its reference is new, and {len(bare)} lots "
                            "without a reference share its date and plan; give the row an "
                            "ordinal, or void the lot it duplicates",
                            row.index,
                        )
                    )
                    continue
                if bare:
                    lot, assign = bare[0], True
        else:
            assert row.ordinal is not None  # validate_rows
            lot = by_key.get((row.acquired_on, row.plan, row.ordinal))
        if lot is None:
            if row.quantity == 0:
                skipped_zero += 1
            else:
                new_rows.append(row)
            continue
        if not claim(lot, row):
            continue
        fact = lot.fact
        if fact.acquired_on != row.acquired_on or lot.plan != row.plan:
            conflicts.append(
                Conflict(
                    "identity_mismatch",
                    f"row {row.index}: its reference names lot {fact.id}, acquired "
                    f"{fact.acquired_on.isoformat()}, with another date or plan",
                    row.index,
                    fact.id,
                )
            )
            continue
        if not fact.counted_on(as_of):
            conflicts.append(
                Conflict(
                    "lot_after_capture",
                    f"row {row.index}: lot {fact.id} counts from "
                    f"{fact.effective_on.isoformat()}, after this capture's as_of",
                    row.index,
                    fact.id,
                )
            )
            continue
        left = book.remaining(fact, as_of)
        row_changes: list[Change] = []
        if row.quantity < left:
            conflicts.append(
                Conflict(
                    "quantity_decrease",
                    f"row {row.index}: the broker shows {row.quantity} shares left in lot "
                    f"{fact.id}, Purser {left}; record the sale or transfer out that drew "
                    "on it (naming this lot) and capture again, or void the lot if it was "
                    "recorded wrong",
                    row.index,
                    fact.id,
                )
            )
            continue
        if row.quantity > left:
            conflicts.append(
                Conflict(
                    "quantity_increase",
                    f"row {row.index}: the broker shows {row.quantity} shares left in lot "
                    f"{fact.id}, Purser {left}; shares do not come back to a lot. Reverse "
                    "the disposal that was wrong, or void the lot and capture it again",
                    row.index,
                    fact.id,
                )
            )
            continue
        if row.basis.basis_per_share is not None and not same_basis(fact, row.quantity, row.basis):
            if lot.disposed:
                conflicts.append(
                    Conflict(
                        "basis_locked",
                        f"row {row.index}: lot {fact.id} has dispositions, so its basis is "
                        "fixed; reverse them, correct the lot, and record them again",
                        row.index,
                        fact.id,
                    )
                )
                continue
            if fact.basis_per_share is None:
                row_changes.append(Change("fill_basis", row.index, fact.id))
            elif replace_basis:
                row_changes.append(Change("replace_basis", row.index, fact.id))
            else:
                conflicts.append(
                    Conflict(
                        "basis_differs",
                        f"row {row.index}: the broker's basis for lot {fact.id} differs "
                        "from Purser's; capture again with basis_policy 'replace' to take "
                        "the broker's",
                        row.index,
                        fact.id,
                    )
                )
                continue
        if assign:
            row_changes.append(Change("assign_ref", row.index, fact.id))
        if row_changes:
            changes.extend(row_changes)
        else:
            unchanged += 1

    not_in_capture = 0
    for k in known:
        if k.fact.id in matched or not k.fact.counted_on(as_of):
            continue
        if book.remaining(k.fact, as_of) <= 0:
            continue
        if complete:
            conflicts.append(
                Conflict(
                    "missing_from_capture",
                    f"lot {k.fact.id} holds {book.remaining(k.fact, as_of)} shares on "
                    f"{as_of.isoformat()} and the complete capture does not list it; record "
                    "what took it (a sale or transfer out naming it), or void it if it was "
                    "recorded wrong — a missing row is never taken as a sale",
                    None,
                    k.fact.id,
                )
            )
        else:
            not_in_capture += 1

    used = {(k.fact.acquired_on, k.plan, k.fact.ordinal) for k in known}
    new_facts: list[LotFact] = []
    for row in new_rows:
        # A reference-less row keeps its ordinal (its identity; it matched
        # nothing, so it is free). A referenced one takes the next free.
        ordinal = row.ordinal
        if ordinal is None or (row.acquired_on, row.plan, ordinal) in used:
            ordinal = 1
            while (row.acquired_on, row.plan, ordinal) in used:
                ordinal += 1
        used.add((row.acquired_on, row.plan, ordinal))
        changes.append(Change("new_lot", row.index, None, row.quantity, ordinal))
        new_facts.append(
            LotFact(
                id=_placeholder(row.index),
                acquired_on=row.acquired_on,
                effective_on=as_of,
                quantity=row.quantity,
                cost_basis=row.basis.cost_basis,
                basis_per_share=row.basis.basis_per_share,
                ordinal=ordinal,
                adopted=True,
            )
        )

    target = sum((r.quantity for r in rows), Decimal(0)) if complete else None
    if stale:
        stale_conflicts = [
            Conflict(
                "stale_capture",
                f"this capture (as_of {as_of.isoformat()}) is older than one already "
                "applied; it is compared, never applied"
                + (f": {c.message}" if c.code != "stale_capture" else ""),
                c.row,
                c.lot_id,
            )
            for c in conflicts
        ]
        stale_conflicts.extend(
            Conflict(
                "stale_capture",
                f"this capture (as_of {as_of.isoformat()}) is older than one already "
                f"applied; its change ({c.kind}) is not applied",
                c.row,
                c.lot_id,
            )
            for c in changes
        )
        return SymbolPlan((), tuple(stale_conflicts), unchanged, skipped_zero, 0, target)

    if not conflicts:
        trial_events = list(events)
        timeline = core.Timeline(core.quantity_timeline(trial_events))
        held = timeline.quantity_on(as_of)
        if complete and target is not None and target < held:
            # Fewer shares than Purser holds, with every lot accounted for:
            # shares left outside any lot. Never taken as a sale.
            conflicts.append(
                Conflict(
                    "unexplained_decrease",
                    f"the broker shows {target} shares on {as_of.isoformat()}, Purser "
                    f"{held}; record the sale or transfer out that took the difference, "
                    "then capture again",
                )
            )
            return SymbolPlan((), tuple(conflicts), unchanged, skipped_zero, not_in_capture, target)
        if complete and target is not None and target != held:
            changes.append(Change("recount", None, None, target))
            trial_events.append(
                core.Event(
                    id=_placeholder(-1),
                    occurred_on=as_of,
                    created_at=_LAST,
                    kind=core.SET_QUANTITY,
                    quantity=target,
                )
            )
        try:
            check(trial_events, with_lots(book, new_facts))
        except LotError as exc:
            code = "insufficient_unallocated" if not complete else exc.code
            conflicts.append(
                Conflict(
                    code,
                    (
                        "a partial capture never changes the position's share count, and "
                        "its new lots need that many unallocated shares: "
                        if not complete
                        else ""
                    )
                    + str(exc),
                )
            )
        except core.NegativeQuantity as exc:
            conflicts.append(Conflict("coverage", str(exc)))
    if conflicts:
        return SymbolPlan((), tuple(conflicts), unchanged, skipped_zero, not_in_capture, target)
    return SymbolPlan(tuple(changes), (), unchanged, skipped_zero, not_in_capture, target)


_LAST = datetime.max.replace(tzinfo=UTC)  # a planned event goes after every recorded one


def _placeholder(index: int) -> UUID:
    """A stable stand-in id for a lot or event a plan would create."""
    return UUID(int=(index + 2) % (1 << 128))
