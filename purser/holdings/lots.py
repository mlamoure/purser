"""Lots of a hand-tracked position: reads, the void and the basis correction
(DESIGN section 8, issue #393, decision 107).

The correction paths, all audited, all under the account lock:

- A lot opened by an event goes with that event: reverse it
  (``reverse_position_event``), refused while a disposal still draws on the
  lot — reverse those disposals first, then record them again.
- A lot adopted from a capture is voided (``void_lot``), refused while a
  live disposal draws on it, or while an award vest is linked to it. Record
  the right lot again with a new capture (a new captured_at: the exact same
  request is a retry and writes nothing).
- A basis that was wrong is corrected in place (``correct_basis``) only
  while the lot has no live dispositions (their basis was computed from it);
  otherwise reverse them first.
"""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from purser import audit, clock, events
from purser import ledger as ledger_service
from purser.core import holdings as core
from purser.core import lots as core_lots
from purser.holdings import lotbook, projection
from purser.holdings import service as holdings
from purser.ledger import LedgerError, NotFoundError
from purser.models import (
    Account,
    ApiToken,
    AwardVest,
    HoldingsCapture,
    Lot,
    LotDisposition,
    Position,
    PositionEvent,
)

STATUSES = ("open", "closed", "voided", "reversed", "all")
MAX_PAGE = 200


@dataclass(frozen=True)
class LotView:
    lot: Lot
    status: str  # open | closed | voided | reversed
    remaining: Decimal
    remaining_basis: Decimal | None
    dispositions: int  # live ones
    award_vest_id: uuid.UUID | None


@dataclass(frozen=True)
class Freshness:
    """When the broker's lots were last observed (capture freshness),
    distinct from the price's date (quote freshness)."""

    capture_id: uuid.UUID
    as_of: date
    captured_at: str
    completeness: str


@dataclass(frozen=True)
class LotSummary:
    position: Position
    quantity: Decimal
    tracks_lots: bool
    open_lots: int
    lots_quantity: Decimal
    unallocated: Decimal
    basis_known: Decimal
    basis_unknown_quantity: Decimal
    cost_basis: Decimal | None
    freshness: Freshness | None


@dataclass(frozen=True)
class LotPage:
    summary: LotSummary
    lots: list[LotView]
    total: int
    next_cursor: str | None


def _book(session: Session, position: Position) -> tuple[list[core.Event], core_lots.Book]:
    facts = [
        projection.event_fact(r)
        for r in projection.position_events(session, [position.id]).get(position.id, [])
    ]
    loaded = lotbook.load(session, [position.id]).get(position.id)
    return facts, lotbook.book(loaded, facts)


def freshness(session: Session, account_id: uuid.UUID, symbol: str) -> Freshness | None:
    row = session.execute(
        select(HoldingsCapture)
        .where(
            HoldingsCapture.account_id == account_id,
            HoldingsCapture.kind == "lots",
            HoldingsCapture.symbols.contains([symbol]),
        )
        .order_by(HoldingsCapture.as_of.desc(), HoldingsCapture.captured_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    return Freshness(row.id, row.as_of, row.captured_at.isoformat(), row.completeness)


def summary(session: Session, position: Position, *, today: date | None = None) -> LotSummary:
    today = today or clock.ledger_today()
    facts, book = _book(session, position)
    quantity = core.Timeline(core.quantity_timeline(facts)).quantity_on(today)
    basis = core_lots.position_basis(book, today, quantity)
    tracks = bool(book.counted(today))
    return LotSummary(
        position=position,
        quantity=quantity,
        tracks_lots=tracks,
        open_lots=sum(1 for lot in book.counted(today) if book.remaining(lot, today) > 0),
        lots_quantity=basis.lots_quantity,
        unallocated=basis.unallocated if tracks else quantity,
        basis_known=basis.known,
        basis_unknown_quantity=basis.unknown_quantity,
        cost_basis=basis.basis if tracks else None,
        freshness=freshness(session, position.account_id, position.symbol),
    )


def _views(session: Session, position: Position) -> list[LotView]:
    _facts, book = _book(session, position)
    live = {lot.id: lot for lot in book.lots}
    reversed_events = set(
        session.execute(
            select(PositionEvent.reverses_id).where(
                PositionEvent.position_id == position.id, PositionEvent.reverses_id.is_not(None)
            )
        ).scalars()
    )
    rows = list(
        session.execute(
            select(Lot)
            .where(Lot.position_id == position.id)
            .order_by(Lot.acquired_on, Lot.ordinal, Lot.created_at, Lot.id)
        ).scalars()
    )
    vests: dict[uuid.UUID | None, uuid.UUID] = {
        lot_id: vest_id
        for lot_id, vest_id in session.execute(
            select(AwardVest.lot_id, AwardVest.id).where(AwardVest.lot_id.in_([r.id for r in rows]))
        ).tuples()
    }
    out: list[LotView] = []
    for r in rows:
        fact = live.get(r.id)
        if r.opened_by_event_id is not None and r.opened_by_event_id in reversed_events:
            status, left, left_basis, n = "reversed", Decimal(0), None, 0
        elif r.voided_at is not None:
            status, left, left_basis, n = "voided", Decimal(0), None, 0
        elif fact is None:
            status, left, left_basis, n = "reversed", Decimal(0), None, 0
        else:
            left = book.remaining(fact)
            left_basis = book.remaining_basis(fact)
            n = len(book.of(fact.id))
            status = "open" if left > 0 else "closed"
        out.append(LotView(r, status, left, left_basis, n, vests.get(r.id)))
    return out


def list_lots(
    session: Session,
    position_id: uuid.UUID,
    *,
    status: str = "open",
    limit: int = 50,
    cursor: str | None = None,
    today: date | None = None,
) -> LotPage:
    if status not in STATUSES:
        raise LedgerError(f"status must be one of: {', '.join(STATUSES)}")
    if not 1 <= limit <= MAX_PAGE:
        raise LedgerError(f"limit must be between 1 and {MAX_PAGE}")
    try:
        offset = int(cursor) if cursor else 0
    except ValueError as exc:
        raise LedgerError("cursor is the next_cursor of a previous page") from exc
    if offset < 0:
        raise LedgerError("cursor is the next_cursor of a previous page")
    position = holdings.get_position(session, position_id)
    views = _views(session, position)
    if status != "all":
        views = [v for v in views if v.status == status]
    page = views[offset : offset + limit]
    nxt = offset + limit
    return LotPage(
        summary=summary(session, position, today=today),
        lots=page,
        total=len(views),
        next_cursor=str(nxt) if nxt < len(views) else None,
    )


def get_lot(session: Session, lot_id: uuid.UUID) -> Lot:
    lot = session.get(Lot, lot_id)
    if lot is None:
        raise NotFoundError(f"lot {lot_id} not found")
    return lot


@dataclass(frozen=True)
class LotDetail:
    view: LotView
    dispositions: list[tuple[LotDisposition, bool]]  # (row, live)


def lot_detail(session: Session, lot_id: uuid.UUID) -> LotDetail:
    lot = get_lot(session, lot_id)
    position = holdings.get_position(session, lot.position_id)
    view = next(v for v in _views(session, position) if v.lot.id == lot.id)
    _facts, book = _book(session, position)
    live = {(d.event_id, d.lot_id) for d in book.disposals}
    rows = list(
        session.execute(
            select(LotDisposition)
            .where(LotDisposition.lot_id == lot.id)
            .order_by(LotDisposition.disposed_on, LotDisposition.created_at)
        ).scalars()
    )
    return LotDetail(view, [(d, (d.event_id, d.lot_id) in live) for d in rows])


def resolve_lot(session: Session, position: Position, ref: str) -> uuid.UUID:
    """A lot of ``position`` named by its id or by its broker reference
    (compared by blind index, so the reference never needs to be shown)."""
    try:
        lot_id = uuid.UUID(ref)
    except ValueError:
        found = session.execute(
            select(Lot.id).where(
                Lot.position_id == position.id,
                Lot.external_ref_bidx == holdings.checked(lotbook.ref_index, ref),
                Lot.voided_at.is_(None),
            )
        ).scalar_one_or_none()
        if found is None:
            raise LedgerError(f"{position.symbol} has no live lot with that reference") from None
        return found
    lot = session.get(Lot, lot_id)
    if lot is None or lot.position_id != position.id:
        raise LedgerError(f"lot {lot_id} is not a lot of this {position.symbol} position")
    return lot_id


def _lock_lot(session: Session, lot_id: uuid.UUID) -> tuple[Account, Position, Lot]:
    lot = get_lot(session, lot_id)
    account, position = holdings.lock_position(session, lot.position_id)
    session.refresh(lot)
    return account, position, lot


def _live_disposal_events(session: Session, position: Position, lot: Lot) -> list[uuid.UUID]:
    _facts, book = _book(session, position)
    return sorted({d.event_id for d in book.of(lot.id)})


def void_lot(
    session: Session,
    *,
    lot_id: uuid.UUID,
    note: str | None,
    token: ApiToken | None,
    today: date | None = None,
) -> Lot:
    """Void a lot adopted from a capture (decision 107): it stops counting,
    is kept read-only for history, and its reference is free again."""
    today = today or clock.ledger_today()
    account, position, lot = _lock_lot(session, lot_id)
    if lot.voided_at is not None:
        raise LedgerError(f"lot {lot.id} is already voided")
    if lot.opened_by_event_id is not None:
        raise LedgerError(
            f"lot {lot.id} was opened by event {lot.opened_by_event_id}; reverse that event "
            "(reverse_position_event) to remove it"
        )
    disposing = _live_disposal_events(session, position, lot)
    if disposing:
        raise holdings.LotRuleError(
            "lot_has_dispositions",
            f"lot {lot.id} is drawn on by {len(disposing)} event(s) "
            f"({', '.join(str(e) for e in disposing)}); reverse them first, then void the lot "
            "and record them again against the right lots",
        )
    linked = session.execute(select(AwardVest.id).where(AwardVest.lot_id == lot.id)).first()
    if linked is not None:
        raise holdings.LotRuleError(
            "lot_linked_to_vest",
            f"award vest {linked[0]} is linked to lot {lot.id}; set that vest back to "
            "scheduled first (PATCH /award-vests/{id})",
        )
    lot.voided_at = func.now()
    lot.void_note = note or None
    lot.voided_by_token_id = token.id if token else None
    session.flush()
    holdings._check_timeline(session, position)
    audit.record(
        session,
        actor_token_id=token.id if token else None,
        action="lot.void",
        entity_type="lot",
        entity_id=lot.id,
        before={"status": "live", "quantity": str(lot.quantity)},
        after={"status": "voided", "note": lot.void_note, "position_id": str(position.id)},
    )
    events.emit(
        session,
        type_="position.updated",
        entity_type="position",
        entity_id=position.id,
        payload=holdings._event_payload(account, position, change="lot_voided", lot_id=str(lot.id)),
    )
    projection.project_account(session, account, from_date=lot.effective_on, today=today)
    return lot


def correct_basis(
    session: Session,
    *,
    lot_id: uuid.UUID,
    basis_per_share: Decimal | None,
    cost_basis: Decimal | None,
    note: str | None,
    token: ApiToken | None,
    today: date | None = None,
) -> Lot:
    """Set or correct a lot's basis while nothing has drawn on it. Both None
    sets it back to unknown."""
    today = today or clock.ledger_today()
    account, position, lot = _lock_lot(session, lot_id)
    if lot.voided_at is not None:
        raise LedgerError(f"lot {lot.id} is voided (read-only)")
    disposing = _live_disposal_events(session, position, lot)
    if disposing:
        raise holdings.LotRuleError(
            "basis_locked",
            f"lot {lot.id} is drawn on by {len(disposing)} event(s) whose basis came from it; "
            "reverse them, correct the basis, and record them again",
        )
    try:
        basis = core_lots.normalize_basis(Decimal(lot.quantity), basis_per_share, cost_basis)
    except (core_lots.LotError, ValueError) as exc:
        raise LedgerError(str(exc)) from exc
    before = {
        "basis_per_share": str(lot.basis_per_share) if lot.basis_per_share is not None else None,
        "cost_basis": str(lot.cost_basis) if lot.cost_basis is not None else None,
    }
    lot.basis_per_share = basis.basis_per_share
    lot.cost_basis = basis.cost_basis
    lot.basis_source = basis.source
    session.flush()
    audit.record(
        session,
        actor_token_id=token.id if token else None,
        action="lot.basis",
        entity_type="lot",
        entity_id=lot.id,
        before=before,
        after={
            "basis_per_share": str(basis.basis_per_share) if basis.basis_per_share else None,
            "cost_basis": str(basis.cost_basis) if basis.cost_basis is not None else None,
            "note": note or None,
            "change": "correct_basis",
        },
    )
    events.emit(
        session,
        type_="position.updated",
        entity_type="position",
        entity_id=position.id,
        payload=holdings._event_payload(account, position, change="lot_basis", lot_id=str(lot.id)),
    )
    projection.project_account(session, account, from_date=lot.effective_on, today=today)
    return lot


@dataclass(frozen=True)
class TermTotals:
    disposals: int = 0
    quantity: Decimal = Decimal(0)
    proceeds: Decimal = Decimal(0)
    cost_basis: Decimal = Decimal(0)
    realized_gain: Decimal = Decimal(0)
    unknown_basis_quantity: Decimal = Decimal(0)
    unknown_proceeds_quantity: Decimal = Decimal(0)


@dataclass(frozen=True)
class Realized:
    year: int
    account: Account
    symbol: str | None
    by_term: dict[str, TermTotals]
    unallocated_quantity: Decimal


def realized_gains(
    session: Session, *, account_ref: str, year: int, symbol: str | None = None
) -> Realized:
    """Realized gains of an account's live lot dispositions in a calendar
    year, by term. Gains sum only the dispositions with both a basis and
    proceeds; what lacks either is counted, never valued as 0."""
    account = ledger_service.resolve_account(session, account_ref)
    if not 1900 <= year <= 9999:
        raise LedgerError("year is a four-digit calendar year")
    stmt = select(Position).where(Position.account_id == account.id)
    if symbol:
        stmt = stmt.where(Position.symbol == holdings.symbol_of(symbol))
    positions = list(session.execute(stmt).scalars())
    totals: dict[str, TermTotals] = {"short": TermTotals(), "long": TermTotals()}
    unallocated = Decimal(0)
    for position in positions:
        _facts, book = _book(session, position)
        live = {(d.event_id, d.lot_id) for d in book.disposals}
        rows = session.execute(
            select(LotDisposition).where(
                LotDisposition.position_id == position.id,
                LotDisposition.disposed_on >= date(year, 1, 1),
                LotDisposition.disposed_on <= date(year, 12, 31),
            )
        ).scalars()
        for d in rows:
            if (d.event_id, d.lot_id) not in live:
                continue
            if d.lot_id is None or d.term is None:
                unallocated += Decimal(d.quantity)
                continue
            t = totals[d.term]
            known = d.cost_basis is not None and d.proceeds is not None
            totals[d.term] = TermTotals(
                disposals=t.disposals + 1,
                quantity=t.quantity + Decimal(d.quantity),
                proceeds=t.proceeds
                + (Decimal(d.proceeds) if known and d.proceeds is not None else 0),
                cost_basis=t.cost_basis
                + (Decimal(d.cost_basis) if known and d.cost_basis is not None else 0),
                realized_gain=t.realized_gain
                + (Decimal(d.realized_gain) if d.realized_gain is not None else 0),
                unknown_basis_quantity=t.unknown_basis_quantity
                + (Decimal(d.quantity) if d.cost_basis is None else 0),
                unknown_proceeds_quantity=t.unknown_proceeds_quantity
                + (Decimal(d.quantity) if d.proceeds is None else 0),
            )
    return Realized(
        year, account, holdings.symbol_of(symbol) if symbol else None, totals, unallocated
    )


def event_of(session: Session, event_id: uuid.UUID) -> PositionEvent | None:
    return session.get(PositionEvent, event_id)
