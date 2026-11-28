"""Positions and their events (DESIGN §8, decision 88).

Every write here: locks the account row (``projection.lock_account``),
validates against the WHOLE timeline after the change (a backdated sell can
push a later point below zero), writes its audit row, emits
``position.updated``, and re-projects the account's snapshot rows in the same
transaction. Corrections are reversals, never edits (``position_events`` is
append-only by trigger). Writes are write scope at every surface; a
manually tracked position has no pending state to propose into.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from purser import audit, balances, clock, events
from purser import ledger as ledger_service
from purser.core import holdings as core
from purser.core import lots as core_lots
from purser.holdings import cash as trade_cash
from purser.holdings import lotbook, projection
from purser.ledger import LedgerError, NotFoundError
from purser.models import (
    Account,
    ApiToken,
    AwardVest,
    Lot,
    LotDisposition,
    Position,
    PositionEvent,
    Transaction,
)

SOURCES = ("feed", "manual")


class PositionConflict(LedgerError):
    """The account already has an open position in that symbol (REST 409)."""


class LotRuleError(LedgerError):
    """A write that would break a lot invariant (``core.lots.LotError``):
    REST 422, its ``code`` naming the rule (decision 107)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AllocationRequest:
    """How a sell or transfer_out draws on a position's lots (decision 107):
    a selection ``method`` (fifo, lifo, hifo), or ``lots``, a specific
    identification of (lot id, shares) pairs where a None id names the
    unallocated remainder. A method never reaches the unallocated shares."""

    method: str | None = None
    lots: tuple[tuple[uuid.UUID | None, Decimal], ...] = ()


def resolve_allocation(
    session: Session,
    position_id: uuid.UUID,
    *,
    method: str | None,
    items: list[tuple[str | None, bool, Decimal]] | None,
) -> AllocationRequest | None:
    """An allocation from what a caller names: a ``method``, or ``items`` of
    (lot id or broker reference, unallocated?, quantity). A reference is
    compared by blind index, so an agent can name a lot by the reference on
    the broker's page without Purser ever showing it."""
    if not method and not items:
        return None
    if not items:
        return AllocationRequest(method=method)
    from purser.holdings import lots as lot_service

    position = get_position(session, position_id)
    pairs: list[tuple[uuid.UUID | None, Decimal]] = []
    for ref, unallocated, quantity in items:
        if unallocated:
            if ref:
                raise LedgerError("an allocation names a lot or 'unallocated', not both")
            pairs.append((None, quantity))
            continue
        if not ref:
            raise LedgerError("each allocation names a lot (lot_id or external_ref)")
        pairs.append((lot_service.resolve_lot(session, position, ref), quantity))
    return AllocationRequest(method=method, lots=tuple(pairs))


@dataclass(frozen=True)
class LotSpec:
    """The lot a buy or a vest opens. Its basis defaults to the event's own
    (a priced buy's cost, a vest's fair market value); otherwise it is what
    is given here, or unknown."""

    acquisition: str | None = None
    external_ref: str | None = None
    plan: str | None = None
    acquired_on: date | None = None
    basis_per_share: Decimal | None = None
    cost_basis: Decimal | None = None


def checked[**P, R](fn: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    """Run a pure validator from ``purser.core.holdings``; its ValueError is
    the caller's 422."""
    try:
        return fn(*args, **kwargs)
    except ValueError as exc:
        raise LedgerError(str(exc)) from exc


def symbol_of(raw: str) -> str:
    return checked(core.normalize_symbol, raw)


def name_of(raw: str | None) -> str | None:
    """A position name as stored: stripped once, at entry; empty is none."""
    if raw is None:
        return None
    return raw.strip() or None


def origin_of(token: ApiToken | None) -> str:
    """Who recorded an event (decision 24's convention): a browser session is
    a person (``manual``); any other token is an agent."""
    if token is None or token.kind == "session":
        return "manual"
    return "agent"


def require_manual(account: Account) -> None:
    """Positions are written only on an open brokerage account whose
    holdings are tracked by hand."""
    if account.is_virtual:
        raise LedgerError(f"{account.path} is a category; positions belong to brokerage accounts")
    if account.type != "brokerage":
        raise LedgerError(
            f"{account.path} is a {account.type} account; positions belong to brokerage accounts"
        )
    if account.holdings_source != "manual":
        raise LedgerError(
            f"{account.path} takes its holdings from the feed. An admin switches it to "
            "holdings_source 'manual' (PATCH /accounts/{id}) before positions are entered in "
            "Purser; positions entered earlier stay read-only until then."
        )
    if account.closed_at is not None:
        raise LedgerError(f"account {account.path} is closed (read-only)")


def open_position(session: Session, account_id: uuid.UUID, symbol: str) -> Position | None:
    return session.execute(
        select(Position).where(
            Position.account_id == account_id,
            Position.symbol == symbol,
            Position.closed_at.is_(None),
        )
    ).scalar_one_or_none()


def get_position(session: Session, position_id: uuid.UUID) -> Position:
    position = session.get(Position, position_id)
    if position is None:
        raise NotFoundError(f"position {position_id} not found")
    return position


def _closed(position: Position) -> LedgerError:
    return LedgerError(
        f"the {position.symbol} position {position.id} is closed (read-only); reopen it "
        f"(reopen_position, or PATCH /positions/{position.id} with closed=false) to correct "
        "its events"
    )


def lock_position(
    session: Session, position_id: uuid.UUID, *, closed_ok: bool = False
) -> tuple[Account, Position]:
    """Lock a position's account, then read the position afresh, then check
    it is writable. A write that waited on the lock acts on the position as
    the writer before it left it: a close committed meanwhile refuses it, and
    an ``opened_on`` another write moved earlier is not overwritten.
    ``closed_ok`` is the reopen's: a closed position is what it acts on."""
    position = get_position(session, position_id)
    account = projection.lock_account(session, position.account_id)
    session.refresh(position)
    require_manual(account)
    if position.closed_at is not None and not closed_ok:
        raise _closed(position)
    return account, position


def _event_payload(account: Account, position: Position, **extra: Any) -> dict[str, Any]:
    return {
        "account": account.path,
        "position_id": str(position.id),
        "symbol": position.symbol,
        **extra,
    }


def _check_no_overlap(
    session: Session, account: Account, position: Position, *, today: date
) -> None:
    """A symbol's positions on one account never overlap in time: a date
    holding two positions in it would count the shares twice. Run after
    EVERY event on ``position`` (recorded or reversed, flushed), it looks
    both ways: refused (PositionConflict, 409) when the days ``position``
    now occupies (``core.occupied_span``: the days it holds shares, from its
    effective events, plus its sell-out day) meet the days any other
    position in the symbol occupies. A reversed event and its reversal are
    not history (the projection's own definition), a position that never
    holds shares occupies nothing, and the sell-out day counts on both sides:
    a re-buy on the day the other position sold out is refused, since that
    day's set would carry both."""
    others = [
        p
        for p in session.execute(
            select(Position).where(
                Position.account_id == account.id,
                Position.symbol == position.symbol,
                Position.id != position.id,
            )
        ).scalars()
    ]
    if not others:
        return
    upper = projection.upper_bound(account, today)
    rows = projection.position_events(session, [position.id, *(p.id for p in others)])

    def occupied(of: Position) -> tuple[date, date] | None:
        facts = [projection.event_fact(r) for r in rows.get(of.id, [])]
        try:
            return core.occupied_span(core.quantity_timeline(facts), upper)
        except (core.NegativeQuantity, core.OutOfRange):
            return (of.opened_on, upper)  # never stored; the widest range

    mine = occupied(position)
    if mine is None:
        return
    for other in others:
        theirs = occupied(other)
        if theirs is not None and mine[0] <= theirs[1] and theirs[0] <= mine[1]:
            state = "a closed position" if other.closed_at is not None else "another position"
            raise PositionConflict(
                f"{account.path} held {position.symbol} from {theirs[0].isoformat()} to "
                f"{theirs[1].isoformat()} in {state}; a {position.symbol} position here may "
                "not hold shares on any of those dates (positions never overlap)"
            )


def _new_position(
    session: Session,
    account: Account,
    *,
    symbol: str,
    name: str | None,
    opened_on: date,
    token: ApiToken | None,
) -> Position:
    if open_position(session, account.id, symbol) is not None:
        raise PositionConflict(
            f"{account.path} already has an open {symbol} position; record an event on it"
        )
    # The account starts holding the symbol: wait out a close being
    # recorded before this write projects (#415).
    projection.share_symbols(session, [symbol])
    position = Position(
        account_id=account.id, symbol=symbol, name=name_of(name), opened_on=opened_on
    )
    session.add(position)
    session.flush()
    audit.record(
        session,
        actor_token_id=token.id if token else None,
        action="position.create",
        entity_type="position",
        entity_id=position.id,
        after={
            "account_id": str(account.id),
            "symbol": symbol,
            "name": position.name,
            "opened_on": opened_on.isoformat(),
        },
    )
    events.emit(
        session,
        type_="position.updated",
        entity_type="position",
        entity_id=position.id,
        payload=_event_payload(account, position, change="opened"),
    )
    return position


def create_position(
    session: Session,
    *,
    account_ref: str,
    symbol: str,
    name: str | None = None,
    opened_on: date | None = None,
    token: ApiToken | None,
    today: date | None = None,
) -> Position:
    """Open a position on a manually tracked brokerage account. Refused with
    PositionConflict while an open position in that symbol exists."""
    today = today or clock.ledger_today()
    resolved = ledger_service.resolve_account(session, account_ref)
    account = projection.lock_account(session, resolved.id)
    require_manual(account)
    opened = opened_on or today
    if opened > today:
        raise LedgerError("opened_on cannot be in the future")
    return _new_position(
        session, account, symbol=symbol_of(symbol), name=name, opened_on=opened, token=token
    )


def _validated(
    kind: str,
    quantity: Decimal,
    price: Decimal | None,
    fees: Decimal | None,
    cost_basis: Decimal | None,
) -> tuple[Decimal, Decimal | None, Decimal | None, Decimal | None]:
    if kind not in core.KINDS:
        raise LedgerError(f"kind must be one of: {', '.join(core.KINDS)}")
    if kind == core.SPLIT:
        # The split path (record_split) writes its own row: a ratio, and
        # nothing a trade carries.
        if quantity != 0 or any(v is not None for v in (price, fees, cost_basis)):
            raise LedgerError(
                "a split takes a ratio (split_numerator:split_denominator) and an optional "
                "cash_in_lieu; no quantity, price, fees or cost_basis"
            )
        return Decimal(0), None, None, None
    quantity = checked(core.check_quantity, quantity)
    if kind == core.TRANSFER_OUT and (price is not None or fees is not None):
        raise LedgerError(
            "a transfer_out moves shares with no price and no cash; record a sell when they "
            "were sold"
        )
    if kind == core.VEST and fees is not None:
        raise LedgerError("a vest takes no fees; its price is the fair market value per share")
    if kind == core.SET_QUANTITY:
        if price is not None or fees is not None:
            raise LedgerError("set_quantity takes no price or fees; record a buy or sell to trade")
        if cost_basis is not None:
            cost_basis = checked(core.check_money, cost_basis, "cost_basis")
            if quantity == 0 and cost_basis != 0:
                raise LedgerError("a position that holds nothing has no cost basis")
        return quantity, None, None, cost_basis
    if quantity == 0:
        raise LedgerError(f"a {kind} needs a quantity above zero")
    if cost_basis is not None:
        raise LedgerError(
            f"cost_basis applies to set_quantity; a {kind} moves the basis through its price"
        )
    if price is not None:
        price = checked(core.check_price, price)
    if fees is not None:
        fees = checked(core.check_money, fees, "fees")
    return quantity, price, fees, None


def _facts_and_book(
    session: Session, position: Position, adding: core.Event | None = None
) -> tuple[list[core.Event], core_lots.Book]:
    rows = projection.position_events(session, [position.id]).get(position.id, [])
    facts = [projection.event_fact(r) for r in rows]
    if adding is not None:
        facts.append(adding)
    loaded = lotbook.load(session, [position.id]).get(position.id)
    return facts, lotbook.book(loaded, facts)


def _check_timeline(
    session: Session,
    position: Position,
    adding: core.Event | None = None,
    *,
    lots_adding: tuple[core_lots.LotFact, ...] = (),
    allocations_adding: tuple[core_lots.Allocation, ...] = (),
) -> core.Timeline:
    """The position's timeline, refused (422) when it goes below zero on any
    date or out of range. ``adding`` checks an event before its row exists:
    a trade's cash is planned, probed and posted only after its shares
    passed (decision 103), so an oversell is the same error whatever the cash
    would have run into, and a dry run fails exactly like the call.

    A position with lots is checked against them too (``core.lots.check``,
    decision 107): every write path — an event, a reversal, a recount, a
    capture — goes through here, so none can leave a lot below zero, lots
    holding more than the position, or a disposal unallocated.
    ``lots_adding`` / ``allocations_adding`` are the lot an ``adding`` event
    opens and the dispositions it makes."""
    facts, book = _facts_and_book(session, position, adding)
    if lots_adding:
        book = core_lots.with_lots(book, lots_adding)
    if allocations_adding and adding is not None:
        book = core_lots.with_disposals(book, adding.id, adding.occurred_on, allocations_adding)
    try:
        if book.lots or book.disposals:
            return core_lots.check(facts, book)
        return core.Timeline(core.quantity_timeline(facts))
    except core_lots.LotError as exc:
        raise LotRuleError(exc.code, f"{position.symbol}: {exc}") from exc
    except core.NegativeQuantity as exc:
        raise LedgerError(
            f"{position.symbol}: this would leave {exc.quantity} shares on "
            f"{exc.on.isoformat()}; a position never holds fewer than zero shares"
        ) from exc
    except core.OutOfRange as exc:
        raise LedgerError(f"{position.symbol}: {exc}") from exc


@dataclass(frozen=True)
class _Written:
    """What ``_record`` wrote: the event, and the row ``force_cash`` posted
    its cash over (the double-count probe's hit), if any."""

    event: PositionEvent
    cash_probe_overridden: Transaction | None
    lot: Lot | None = None
    dispositions: tuple[LotDisposition, ...] = ()


def _record(
    session: Session,
    account: Account,
    position: Position,
    *,
    kind: str,
    occurred_on: date,
    quantity: Decimal,
    price: Decimal | None = None,
    fees: Decimal | None = None,
    cost_basis: Decimal | None = None,
    note: str | None = None,
    source_note: str | None = None,
    replaces: uuid.UUID | None = None,
    token: ApiToken | None,
    today: date,
    proceeds_to: str | None = None,
    funded_from: str | None = None,
    settle_on: date | None = None,
    transaction_id: uuid.UUID | None = None,
    force_cash: bool = False,
    allocation: AllocationRequest | None = None,
    lot: LotSpec | None = None,
    unallocated: bool = False,
    gross_quantity: Decimal | None = None,
    withheld_quantity: Decimal | None = None,
    defer_projection: bool = False,
    split: tuple[int, int] | None = None,
    in_lieu_of: uuid.UUID | None = None,
) -> _Written:
    """Record one event; the caller holds the account lock. A buy or sell
    that moves cash posts (or links) its cash first (``holdings.cash``):
    events are append-only, so the event row is written knowing its cash.
    ``force_cash`` posts it over a row of the same amount the double-count
    probe found (audited with that row's id).

    Lots (decision 107): a sell or transfer_out on a day the position's
    lots can be drawn on needs an ``allocation``; a vest always opens a lot,
    and a buy opens one when ``lot`` is given or the position tracks lots
    (``unallocated`` keeps a buy's shares outside every lot)."""
    if position.closed_at is not None:
        raise _closed(position)
    if replaces is not None:
        _check_replaceable(session, position, replaces, occurred_on)
    if occurred_on > today:
        # The projection runs up to today: a future event would only take
        # effect if some later projection happened to cover its date (C5).
        raise LedgerError(
            f"occurred_on {occurred_on.isoformat()} is in the future; record an event "
            "once it has happened"
        )
    if (kind == core.SPLIT) != (split is not None):
        raise LedgerError(
            "record a split with its ratio (record_split, or kind 'split' with "
            "split_numerator and split_denominator)"
        )
    quantity, price, fees, cost_basis = _validated(kind, quantity, price, fees, cost_basis)
    gross_quantity, withheld_quantity = _vest_counts(
        kind, quantity, gross_quantity, withheld_quantity
    )
    trial = core.Event(
        id=uuid.uuid4(),
        occurred_on=occurred_on,
        created_at=datetime.max.replace(tzinfo=UTC),
        kind=kind,
        quantity=quantity,
        price=price,
        fees=fees,
        cost_basis=cost_basis,
        split=split,
    )
    _facts, book = _facts_and_book(session, position, trial)
    allocations = _allocations(
        book,
        kind=kind,
        occurred_on=occurred_on,
        quantity=quantity,
        price=price,
        fees=fees,
        proceeds_to=proceeds_to,
        allocation=allocation,
        symbol=position.symbol,
    )
    opening = _opening_lot(
        session,
        position,
        book,
        kind=kind,
        occurred_on=occurred_on,
        quantity=quantity,
        price=price,
        fees=fees,
        lot=lot,
        unallocated=unallocated,
        event_id=trial.id,
    )
    # The shares first: the event as it will be ordered, after every event
    # recorded so far (its created_at is the insert's clock_timestamp()),
    # with the lot it opens and the dispositions it makes.
    _check_timeline(
        session,
        position,
        adding=trial,
        lots_adding=(opening.fact,) if opening is not None else (),
        allocations_adding=tuple(allocations),
    )
    plan = trade_cash.plan(
        kind,
        occurred_on=occurred_on,
        quantity=quantity,
        price=price,
        fees=fees,
        proceeds_to=proceeds_to,
        funded_from=funded_from,
        settle_on=settle_on,
        transaction_id=transaction_id,
        force_cash=force_cash,
    )
    settled = trade_cash.settle(
        session,
        account,
        position,
        plan,
        kind=kind,
        occurred_on=occurred_on,
        quantity=quantity,
        price=price,
        transaction_id=transaction_id,
        force_cash=force_cash,
        token=token,
    )
    cash_tx = settled.transaction
    event = PositionEvent(
        position_id=position.id,
        occurred_on=occurred_on,
        kind=kind,
        quantity=quantity,
        price=price,
        fees=fees,
        cost_basis=cost_basis,
        origin=origin_of(token),
        note=note or None,
        source_note=source_note or None,
        replaces_id=replaces,
        actor_token_id=token.id if token else None,
        proceeds_to=plan.mode if kind == core.SELL else None,
        funded_from=plan.mode if kind == core.BUY else None,
        # A linked row settled on the bank's date (holdings.cash.link).
        settle_on=plan.settle_on or (cash_tx.date if cash_tx is not None else None),
        transaction_id=cash_tx.id if cash_tx is not None else None,
        gross_quantity=gross_quantity,
        withheld_quantity=withheld_quantity,
        split_numerator=split[0] if split is not None else None,
        split_denominator=split[1] if split is not None else None,
        in_lieu_of=in_lieu_of,
    )
    session.add(event)
    opened_before = position.opened_on
    if occurred_on < position.opened_on:
        position.opened_on = occurred_on  # the earliest event date
    session.flush()
    lot_row = _write_opening_lot(session, position, event, opening, token)
    disposal_rows = _write_dispositions(session, position, event, allocations, allocation)
    timeline = _check_timeline(session, position)
    _check_no_overlap(session, account, position, today=today)
    held = timeline.quantity_on(today)
    audit.record(
        session,
        actor_token_id=token.id if token else None,
        action="position.record",
        entity_type="position",
        entity_id=position.id,
        after={
            "event_id": str(event.id),
            "kind": kind,
            "occurred_on": occurred_on.isoformat(),
            "quantity": str(quantity),
            "price": str(price) if price is not None else None,
            "fees": str(fees) if fees is not None else None,
            "cost_basis": str(cost_basis) if cost_basis is not None else None,
            "origin": event.origin,
            "note": event.note,
            "source_note": event.source_note,
            **({"replaces_id": str(replaces)} if replaces is not None else {}),
            **({"split": f"{split[0]}:{split[1]}"} if split is not None else {}),
            **({"in_lieu_of": str(in_lieu_of)} if in_lieu_of is not None else {}),
            **(
                {
                    "gross_quantity": str(gross_quantity),
                    "withheld_quantity": str(withheld_quantity),
                }
                if kind == core.VEST
                else {}
            ),
            **({"lot_id": str(lot_row.id)} if lot_row is not None else {}),
            **(
                {
                    "lot_method": _method_of(allocation),
                    "dispositions": [
                        {
                            "lot_id": str(d.lot_id) if d.lot_id else core_lots.UNALLOCATED,
                            "quantity": str(d.quantity),
                            "cost_basis": str(d.cost_basis) if d.cost_basis is not None else None,
                            "proceeds": str(d.proceeds) if d.proceeds is not None else None,
                            "term": d.term,
                        }
                        for d in disposal_rows
                    ],
                }
                if disposal_rows
                else {}
            ),
            "quantity_held": str(held),
            **(
                {"opened_on": {"from": opened_before.isoformat(), "to": occurred_on.isoformat()}}
                if position.opened_on != opened_before
                else {}
            ),
            **(
                {
                    "proceeds_to": event.proceeds_to,
                    "funded_from": event.funded_from,
                    "settle_on": event.settle_on.isoformat() if event.settle_on else None,
                    "cash_transaction_id": str(cash_tx.id) if cash_tx is not None else None,
                    "cash_linked": cash_tx is not None and transaction_id is not None,
                }
                if kind in (core.BUY, core.SELL)
                else {}
            ),
            **(
                {
                    # The explicit override of the double-count probe, and
                    # the row of the same amount it posted over (if any).
                    "force_cash": True,
                    "cash_probe_overridden": (
                        str(settled.overridden.id) if settled.overridden is not None else None
                    ),
                }
                if force_cash
                else {}
            ),
        },
    )
    events.emit(
        session,
        type_="position.updated",
        entity_type="position",
        entity_id=position.id,
        payload=_event_payload(
            account,
            position,
            change="recorded",
            kind=kind,
            occurred_on=occurred_on.isoformat(),
            quantity_held=str(held),
            **({"cash_transaction_id": str(cash_tx.id)} if cash_tx is not None else {}),
        ),
    )
    # A bulk capture owns the same account lock and projects once after all
    # symbols and lots are applied, in this transaction. Individual writes
    # must still leave the projection current before returning.
    if not defer_projection:
        projection.project_account(session, account, from_date=occurred_on, today=today)
    return _Written(event, settled.overridden, lot_row, tuple(disposal_rows))


def _vest_counts(
    kind: str, quantity: Decimal, gross: Decimal | None, withheld: Decimal | None
) -> tuple[Decimal | None, Decimal | None]:
    """A vest's gross and withheld units: ``quantity`` is the net delivered,
    and gross = net + withheld (either may be given; withheld defaults to 0)."""
    if kind != core.VEST:
        if gross is not None or withheld is not None:
            raise LedgerError("gross_quantity and withheld_quantity apply to a vest")
        return None, None
    if withheld is not None:
        withheld = checked(core.check_quantity, withheld, "withheld_quantity")
    if gross is not None:
        gross = checked(core.check_quantity, gross, "gross_quantity")
    if gross is None:
        gross = quantity + (withheld or Decimal(0))
    if withheld is None:
        withheld = gross - quantity
    if withheld < 0 or gross != quantity + withheld:
        raise LedgerError(
            f"a vest's gross units ({gross}) are its net shares ({quantity}) plus those "
            f"withheld ({withheld})"
        )
    checked(core.check_quantity, gross, "gross_quantity")
    return gross, withheld


def _method_of(allocation: AllocationRequest | None) -> str:
    if allocation is None or allocation.lots:
        return core_lots.SPECIFIC
    return allocation.method or core_lots.SPECIFIC


def _proceeds(
    kind: str,
    quantity: Decimal,
    price: Decimal | None,
    fees: Decimal | None,
) -> Decimal | None:
    """A sale's net proceeds, in cents as its cash is (decision 103); None
    for a transfer out or a sale with no price."""
    if kind != core.SELL or price is None:
        return None
    return trade_cash.cash_amount(kind, quantity, price, fees)


def _allocations(
    book: core_lots.Book,
    *,
    kind: str,
    occurred_on: date,
    quantity: Decimal,
    price: Decimal | None,
    fees: Decimal | None,
    proceeds_to: str | None,
    allocation: AllocationRequest | None,
    symbol: str,
) -> list[core_lots.Allocation]:
    if kind not in core.REMOVES:
        if allocation is not None:
            raise LedgerError("lot allocations apply to a sell or transfer_out")
        return []
    start = book.first_disposable()
    if allocation is None:
        if start is not None and occurred_on >= start:
            open_lots = len(core_lots.candidates(book, occurred_on))
            raise LotRuleError(
                "allocation_required",
                f"{symbol} tracks lots ({open_lots} can be drawn on "
                f"{occurred_on.isoformat()}), so a {kind} names them: pass lot_method "
                "(fifo, lifo, hifo) or lots [{lot_id, quantity}] — 'unallocated' for "
                "shares outside every lot",
            )
        return []
    if not book.lots:
        raise LedgerError(
            f"{symbol} tracks no lots; record the {kind} without an allocation, or add its "
            "lots first"
        )
    try:
        if allocation.lots:
            if allocation.method not in (None, core_lots.SPECIFIC):
                raise core_lots.LotError(
                    "allocation", "give lot_method or lots, not both (lots is 'specific')"
                )
            pieces = core_lots.specific(book, occurred_on, quantity, list(allocation.lots))
        else:
            if allocation.method not in core_lots.SELECTION_METHODS:
                raise core_lots.LotError(
                    "method",
                    f"lot_method must be one of: {', '.join(core_lots.SELECTION_METHODS)} "
                    "(or name the lots)",
                )
            pieces = list(core_lots.select(allocation.method, book, occurred_on, quantity))
    except core_lots.LotError as exc:
        raise LotRuleError(exc.code, f"{symbol}: {exc}") from exc
    _ = proceeds_to  # the proceeds are the sale's own whatever its cash does
    return core_lots.allocate(book, occurred_on, pieces, _proceeds(kind, quantity, price, fees))


@dataclass(frozen=True)
class _Opening:
    fact: core_lots.LotFact
    acquisition: str
    basis_source: str | None
    external_ref: str | None
    plan: str | None


def _opening_lot(
    session: Session,
    position: Position,
    book: core_lots.Book,
    *,
    kind: str,
    occurred_on: date,
    quantity: Decimal,
    price: Decimal | None,
    fees: Decimal | None,
    lot: LotSpec | None,
    unallocated: bool,
    event_id: uuid.UUID,
) -> _Opening | None:
    if kind not in core.ADDS:
        if lot is not None or unallocated:
            raise LedgerError("a lot is opened by a buy or a vest")
        return None
    if kind == core.VEST and unallocated:
        raise LedgerError("a vest always opens the lot that holds its net shares")
    if unallocated:
        if lot is not None:
            raise LedgerError("give lot or unallocated, not both")
        return None
    if lot is None and not (kind == core.VEST or book.lots):
        return None  # a simple position stays simple
    spec = lot or LotSpec()
    acquired = spec.acquired_on or occurred_on
    if acquired > occurred_on:
        raise LedgerError("a lot's acquired_on cannot be after the event that opens it")
    acquisition = spec.acquisition or ("rsu_vest" if kind == core.VEST else "buy")
    if acquisition not in core_lots.ACQUISITIONS:
        raise LedgerError(f"acquisition must be one of: {', '.join(core_lots.ACQUISITIONS)}")
    try:
        if spec.basis_per_share is not None or spec.cost_basis is not None:
            basis = core_lots.normalize_basis(quantity, spec.basis_per_share, spec.cost_basis)
        elif price is not None:
            with_fees = core.money(quantity * price + (fees or Decimal(0)))
            basis = core_lots.LotBasisInput(
                (with_fees / quantity).quantize(core.PRICE_QUANT), with_fees, "event"
            )
        else:
            basis = core_lots.LotBasisInput(None, None, None)
    except (core_lots.LotError, ValueError) as exc:
        raise LedgerError(str(exc)) from exc
    plan_bidx = checked(lotbook.plan_index, spec.plan) if spec.plan else None
    ref_bidx = checked(lotbook.ref_index, spec.external_ref) if spec.external_ref else None
    ordinal = _next_ordinal(session, position, acquired, plan_bidx)
    if ref_bidx is not None and _ref_taken(session, position, ref_bidx):
        raise PositionConflict(f"{position.symbol}: a live lot already has that external_ref")
    return _Opening(
        fact=core_lots.LotFact(
            id=uuid.uuid4(),
            acquired_on=acquired,
            effective_on=occurred_on,
            quantity=quantity,
            cost_basis=basis.cost_basis,
            basis_per_share=basis.basis_per_share,
            ordinal=ordinal,
            adopted=False,
            opened_by=event_id,
        ),
        acquisition=acquisition,
        basis_source=basis.source,
        external_ref=lotbook.normalize_ref(spec.external_ref) if spec.external_ref else None,
        plan=spec.plan.strip() if spec.plan else None,
    )


def _next_ordinal(
    session: Session, position: Position, acquired: date, plan_bidx: str | None
) -> int:
    used = session.execute(
        select(func.max(Lot.ordinal)).where(
            Lot.position_id == position.id,
            Lot.acquired_on == acquired,
            Lot.voided_at.is_(None),
            (Lot.plan_bidx == plan_bidx) if plan_bidx is not None else Lot.plan_bidx.is_(None),
        )
    ).scalar_one()
    return (used or 0) + 1


def _ref_taken(session: Session, position: Position, ref_bidx: str) -> bool:
    return (
        session.execute(
            select(Lot.id).where(
                Lot.position_id == position.id,
                Lot.external_ref_bidx == ref_bidx,
                Lot.voided_at.is_(None),
            )
        ).first()
        is not None
    )


def _write_opening_lot(
    session: Session,
    position: Position,
    event: PositionEvent,
    opening: _Opening | None,
    token: ApiToken | None,
) -> Lot | None:
    if opening is None:
        return None
    fact = opening.fact
    row = Lot(
        position_id=position.id,
        acquired_on=fact.acquired_on,
        effective_on=fact.effective_on,
        quantity=fact.quantity,
        basis_per_share=fact.basis_per_share,
        cost_basis=fact.cost_basis,
        basis_source=opening.basis_source,
        acquisition=opening.acquisition,
        plan=opening.plan,
        plan_bidx=lotbook.plan_index(opening.plan) if opening.plan else None,
        external_ref=opening.external_ref,
        external_ref_bidx=(
            lotbook.ref_index(opening.external_ref) if opening.external_ref else None
        ),
        ordinal=fact.ordinal,
        opened_by_event_id=event.id,
        origin=_lot_origin(token),
        actor_token_id=token.id if token else None,
    )
    session.add(row)
    session.flush()
    return row


def _lot_origin(token: ApiToken | None) -> str:
    return "manual" if origin_of(token) == "manual" else "agent"


def _write_dispositions(
    session: Session,
    position: Position,
    event: PositionEvent,
    allocations: list[core_lots.Allocation],
    allocation: AllocationRequest | None,
) -> list[LotDisposition]:
    rows = [
        LotDisposition(
            event_id=event.id,
            position_id=position.id,
            lot_id=a.lot.id if a.lot is not None else None,
            disposed_on=event.occurred_on,
            quantity=a.quantity,
            cost_basis=a.cost_basis,
            proceeds=a.proceeds,
            realized_gain=a.realized_gain,
            term=a.term,
            method=_method_of(allocation),
        )
        for a in allocations
    ]
    session.add_all(rows)
    if rows:
        session.flush()
    return rows


def _check_replaceable(
    session: Session, position: Position, replaces: uuid.UUID, occurred_on: date
) -> None:
    """A correction may replace one reversed event of the same position and
    date, once (#401, decision 93)."""
    target = session.get(PositionEvent, replaces)
    if target is None or target.position_id != position.id:
        raise LedgerError(f"replaces: no event {replaces} on this {position.symbol} position")
    if target.reverses_id is not None:
        raise LedgerError("replaces names a reversal; name the event it reversed")
    if target.occurred_on != occurred_on:
        raise LedgerError(
            f"replaces: event {replaces} is dated {target.occurred_on.isoformat()}; a "
            "replacement takes its place in that day, so it carries the same date (record "
            "an event on another day without replaces)"
        )
    reversed_ = session.execute(
        select(PositionEvent.id).where(PositionEvent.reverses_id == target.id)
    ).first()
    if reversed_ is None:
        raise LedgerError(f"replaces: event {replaces} is not reversed; reverse it first")
    taken = session.execute(
        select(PositionEvent.id).where(PositionEvent.replaces_id == target.id)
    ).first()
    if taken is not None:
        raise LedgerError(f"replaces: event {replaces} is already replaced by {taken[0]}")


def record_event(
    session: Session,
    *,
    position_id: uuid.UUID,
    kind: str,
    occurred_on: date,
    quantity: Decimal,
    price: Decimal | None = None,
    fees: Decimal | None = None,
    cost_basis: Decimal | None = None,
    note: str | None = None,
    source_note: str | None = None,
    replaces: uuid.UUID | None = None,
    token: ApiToken | None,
    today: date | None = None,
    proceeds_to: str | None = None,
    funded_from: str | None = None,
    settle_on: date | None = None,
    transaction_id: uuid.UUID | None = None,
    force_cash: bool = False,
    allocation: AllocationRequest | None = None,
    lot: LotSpec | None = None,
    unallocated: bool = False,
    gross_quantity: Decimal | None = None,
    withheld_quantity: Decimal | None = None,
) -> PositionEvent:
    """Record a ``set_quantity``, ``buy``, ``sell``, ``vest`` or
    ``transfer_out`` on a position (lots: ``_record``). A sale's
    net proceeds move to the account's cash unless ``proceeds_to='none'``; a
    buy's cost leaves it only with ``funded_from='cash'`` (decision 103).
    ``force_cash`` posts that cash over the double-count probe's refusal.
    ``replaces`` names a reversed event of this position and date that this
    one corrects: it takes that event's place in the day's order instead of
    going after the day's other events (#401)."""
    return _record_event(
        session,
        position_id=position_id,
        kind=kind,
        occurred_on=occurred_on,
        quantity=quantity,
        price=price,
        fees=fees,
        cost_basis=cost_basis,
        note=note,
        source_note=source_note,
        replaces=replaces,
        token=token,
        today=today,
        proceeds_to=proceeds_to,
        funded_from=funded_from,
        settle_on=settle_on,
        transaction_id=transaction_id,
        force_cash=force_cash,
        allocation=allocation,
        lot=lot,
        unallocated=unallocated,
        gross_quantity=gross_quantity,
        withheld_quantity=withheld_quantity,
    ).event


def _record_event(
    session: Session,
    *,
    position_id: uuid.UUID,
    kind: str,
    occurred_on: date,
    quantity: Decimal,
    price: Decimal | None = None,
    fees: Decimal | None = None,
    cost_basis: Decimal | None = None,
    note: str | None = None,
    source_note: str | None = None,
    replaces: uuid.UUID | None = None,
    token: ApiToken | None,
    today: date | None = None,
    proceeds_to: str | None = None,
    funded_from: str | None = None,
    settle_on: date | None = None,
    transaction_id: uuid.UUID | None = None,
    force_cash: bool = False,
    allocation: AllocationRequest | None = None,
    lot: LotSpec | None = None,
    unallocated: bool = False,
    gross_quantity: Decimal | None = None,
    withheld_quantity: Decimal | None = None,
) -> _Written:
    account, position = lock_position(session, position_id)
    return _record(
        session,
        account,
        position,
        kind=kind,
        occurred_on=occurred_on,
        quantity=quantity,
        price=price,
        fees=fees,
        cost_basis=cost_basis,
        note=note,
        source_note=source_note,
        replaces=replaces,
        token=token,
        today=today or clock.ledger_today(),
        proceeds_to=proceeds_to,
        funded_from=funded_from,
        settle_on=settle_on,
        transaction_id=transaction_id,
        force_cash=force_cash,
        allocation=allocation,
        lot=lot,
        unallocated=unallocated,
        gross_quantity=gross_quantity,
        withheld_quantity=withheld_quantity,
    )


def quantity_on(session: Session, position: Position, day: date) -> Decimal:
    """What ``position`` holds on ``day``, from its effective events."""
    rows = projection.position_events(session, [position.id]).get(position.id, [])
    return core.Timeline(
        core.quantity_timeline([projection.event_fact(r) for r in rows])
    ).quantity_on(day)


@dataclass(frozen=True)
class DispositionView:
    """One disposition as plain values: a dry run's rows are rolled back
    before its answer is read."""

    lot_id: uuid.UUID | None
    quantity: Decimal
    cost_basis: Decimal | None
    proceeds: Decimal | None
    realized_gain: Decimal | None
    term: str | None

    @classmethod
    def of(cls, row: LotDisposition) -> "DispositionView":
        return cls(
            row.lot_id, row.quantity, row.cost_basis, row.proceeds, row.realized_gain, row.term
        )


@dataclass(frozen=True)
class Sale:
    """What a sale did, or on a dry run would do (decision 103)."""

    account: Account
    position: Position
    quantity_before: Decimal  # held today before the sale
    quantity_after: Decimal  # held today after it
    cash_amount: Decimal | None  # what lands on the account; None without cash
    settle_on: date | None
    cash_transaction_id: uuid.UUID | None  # the cash row; None on a dry run
    linked: bool  # the cash is a row the account already showed
    event_id: uuid.UUID | None  # the sell event; None on a dry run
    already_recorded: trade_cash.CashAlreadyRecorded | None  # a dry run's probe hit
    dry_run: bool
    # The row force_cash posts (or, on a dry run, would post) the cash over:
    # the double-count probe's hit, reported rather than hidden.
    cash_probe_overridden: Transaction | None = None
    # The lots it drew on (decision 107), on a dry run too.
    dispositions: tuple[DispositionView, ...] = ()


def record_sale(
    session: Session,
    *,
    account_ref: str,
    symbol: str,
    quantity: Decimal,
    price: Decimal,
    trade_date: date,
    settle_on: date | None = None,
    fees: Decimal | None = None,
    proceeds_to: str = trade_cash.CASH,
    transaction_id: uuid.UUID | None = None,
    note: str | None = None,
    token: ApiToken | None,
    dry_run: bool = False,
    force_cash: bool = False,
    today: date | None = None,
    allocation: AllocationRequest | None = None,
) -> Sale:
    """A ``sell`` on the account's open position in ``symbol``, made through
    ``record_event`` — the REST path's validation, double-count probe
    (``CashAlreadyRecorded``), ``transaction_id`` link and audit row.

    A dry run makes the same call inside a savepoint and rolls it back, so it
    answers with exactly what the write would do and leaves nothing behind.
    When the probe finds the cash, a dry run reports that row
    (``already_recorded``) with the sale's shares-only effect instead of
    refusing; the real call is refused."""
    today = today or clock.ledger_today()
    account = ledger_service.resolve_account(session, account_ref)
    require_manual(account)
    account = projection.lock_account(session, account.id)
    normalized = symbol_of(symbol)
    position = open_position(session, account.id, normalized)
    if position is None:
        raise LedgerError(
            f"{account.path} has no open {normalized} position; record_position first"
        )
    before = quantity_on(session, position, today)

    def sell(mode: str, settle: date | None, link: uuid.UUID | None, forced: bool) -> _Written:
        return _record_event(
            session,
            position_id=position.id,
            kind=core.SELL,
            occurred_on=trade_date,
            quantity=quantity,
            price=price,
            fees=fees,
            note=note,
            token=token,
            today=today,
            proceeds_to=mode,
            settle_on=settle,
            transaction_id=link,
            force_cash=forced,
            allocation=allocation,
        )

    def outcome(written: _Written, found: trade_cash.CashAlreadyRecorded | None) -> Sale:
        event = written.event
        amount: Decimal | None = None
        settle = event.settle_on
        if event.transaction_id is not None:
            cash_tx = ledger_service.get_transaction(session, event.transaction_id)
            real = next(p for p in cash_tx.postings if p.account_id == account.id)
            amount = Decimal(real.amount)
        elif found is not None:
            # The shares-only rerun moved no cash; report the sale's own plan.
            planned = trade_cash.plan(
                core.SELL,
                occurred_on=trade_date,
                quantity=quantity,
                price=price,
                fees=fees,
                proceeds_to=proceeds_to,
                funded_from=None,
                settle_on=settle_on,
                transaction_id=None,
            )
            amount, settle = planned.amount, planned.settle_on
        return Sale(
            account=account,
            position=position,
            quantity_before=before,
            quantity_after=quantity_on(session, position, today),
            cash_amount=amount,
            settle_on=settle,
            cash_transaction_id=None if dry_run else event.transaction_id,
            linked=transaction_id is not None and event.transaction_id is not None,
            event_id=None if dry_run else event.id,
            already_recorded=found,
            dry_run=dry_run,
            cash_probe_overridden=written.cash_probe_overridden,
            dispositions=tuple(DispositionView.of(d) for d in written.dispositions),
        )

    if not dry_run:
        return outcome(sell(proceeds_to, settle_on, transaction_id, force_cash), None)
    # Nothing the dry run writes survives, so it marks no balances: no row
    # lock on the global watermark, no recompute at this session's commit.
    with balances.suspend_marks(session):
        savepoint = session.begin_nested()
        try:
            try:
                return outcome(sell(proceeds_to, settle_on, transaction_id, force_cash), None)
            except trade_cash.CashAlreadyRecorded as found:
                savepoint.rollback()
                savepoint = session.begin_nested()
                return outcome(sell(trade_cash.NONE, None, None, False), found)
        finally:
            # Whatever the sale wrote (event, cash row or link, audit, events,
            # snapshot rows) goes; values were read above.
            savepoint.rollback()


@dataclass(frozen=True)
class SplitRecorded:
    """A split (#624): the split event, and its cash-in-lieu sale when the
    broker paid cash for the fractional share."""

    split: PositionEvent
    in_lieu: PositionEvent | None
    before: Decimal  # shares held the day before the effective date
    after: Decimal  # the shares those became
    fraction: Decimal | None  # the fractional share paid in cash
    cash_in_lieu: Decimal | None


CENT = Decimal("0.01")


def parse_ratio(raw: str) -> tuple[int, int]:
    """A split ratio as people write it, ``2:1`` (forward) or ``1:10``
    (reverse): new shares for old."""
    parts = raw.strip().split(":")
    if len(parts) != 2 or not all(p.strip().isdigit() for p in parts):
        raise LedgerError(
            f"a split ratio is new:old shares in whole numbers, e.g. 2:1 or 1:10 (got {raw!r})"
        )
    return int(parts[0]), int(parts[1])


def check_ratio(numerator: int, denominator: int) -> tuple[int, int]:
    for term in (numerator, denominator):
        if isinstance(term, bool) or not 1 <= term <= core.MAX_SPLIT_TERM:
            raise LedgerError(
                f"a split ratio's terms are whole numbers from 1 to {core.MAX_SPLIT_TERM:,}"
            )
    if numerator == denominator:
        raise LedgerError("a 1:1 split changes nothing")
    return numerator, denominator


def record_split(
    session: Session,
    *,
    position_id: uuid.UUID,
    occurred_on: date,
    numerator: int,
    denominator: int,
    cash_in_lieu: Decimal | None = None,
    note: str | None = None,
    source_note: str | None = None,
    replaces: uuid.UUID | None = None,
    settle_on: date | None = None,
    transaction_id: uuid.UUID | None = None,
    force_cash: bool = False,
    allocation: AllocationRequest | None = None,
    token: ApiToken | None,
    today: date | None = None,
) -> SplitRecorded:
    """Record a stock split effective ``occurred_on`` (#624, decision 127):
    every share the position held the day before becomes
    ``numerator / denominator`` shares at the start of that day (rounded to
    six places), each lot keeping its basis, acquisition date and term.

    ``cash_in_lieu``: the broker paid cash for the fractional share the
    split left. The fraction (the shares after the split less their whole
    number) leaves in a ``sell`` of that many shares whose net proceeds are
    exactly this amount (in cents), recorded with the split and reversed with
    it: cash, the double-count check, a link to the broker's row
    (``transaction_id``), ``settle_on``, ``force_cash`` and, on a position
    that tracks lots, the lots it draws on (``allocation``) work as for any
    sale (decision 103). Refused on a position that holds nothing the day
    before, a second split on one day, and a cash in lieu with no fraction
    left."""
    today = today or clock.ledger_today()
    numerator, denominator = check_ratio(numerator, denominator)
    account, position = lock_position(session, position_id)
    if cash_in_lieu is None and (
        settle_on is not None or transaction_id is not None or allocation is not None or force_cash
    ):
        raise LedgerError(
            "settle_on, transaction_id, force_cash and lot allocations apply to a split's "
            "cash_in_lieu"
        )
    if cash_in_lieu is not None:
        cash_in_lieu = checked(core.check_money, cash_in_lieu, "cash_in_lieu")
        if cash_in_lieu <= 0 or cash_in_lieu != cash_in_lieu.quantize(CENT):
            raise LedgerError("cash_in_lieu is the cash paid for the fraction, in cents, above 0")
    rows = projection.position_events(session, [position.id]).get(position.id, [])
    facts = [projection.event_fact(r) for r in rows]
    if any(
        e.kind == core.SPLIT and e.occurred_on == occurred_on and e.id != replaces
        for e in core.effective_events(facts)
    ):
        raise LedgerError(
            f"{position.symbol} already has a split effective {occurred_on.isoformat()}; "
            "reverse it to record another"
        )
    try:
        before = core.Timeline(core.quantity_timeline(facts)).quantity_on(
            occurred_on - timedelta(days=1)
        )
    except (core.NegativeQuantity, core.OutOfRange) as exc:  # never stored
        raise LedgerError(f"{position.symbol}: {exc}") from exc
    if before <= 0:
        raise LedgerError(
            f"{position.symbol} holds no shares on {(occurred_on - timedelta(days=1)).isoformat()}"
            ", the day before the split takes effect; a split changes the shares held "
            "before its effective date"
        )
    after = core.split_quantity(before, numerator, denominator)
    fraction = after - after.to_integral_value(rounding=ROUND_FLOOR)
    if cash_in_lieu is not None and fraction == 0:
        raise LedgerError(
            f"the split leaves {after} shares, a whole number: there is no fractional share "
            "for cash in lieu"
        )
    written = _record(
        session,
        account,
        position,
        kind=core.SPLIT,
        occurred_on=occurred_on,
        quantity=Decimal(0),
        note=note,
        source_note=source_note,
        replaces=replaces,
        token=token,
        today=today,
        split=(numerator, denominator),
        defer_projection=True,
    )
    in_lieu: PositionEvent | None = None
    if cash_in_lieu is not None:
        # The fraction leaves as a sale whose proceeds are exactly the cash:
        # a price to six places whose product with the fraction rounds to it.
        price = (cash_in_lieu / fraction).quantize(core.PRICE_QUANT)
        if trade_cash.cash_amount(core.SELL, fraction, price, None) != cash_in_lieu:
            raise LedgerError(  # unreachable for a fraction below 1 and a sum in cents
                f"cash_in_lieu {cash_in_lieu} cannot be expressed for {fraction} shares"
            )
        in_lieu = _record(
            session,
            account,
            position,
            kind=core.SELL,
            occurred_on=occurred_on,
            quantity=fraction,
            price=price,
            note="cash in lieu of a fractional share",
            source_note=source_note,
            token=token,
            today=today,
            proceeds_to=trade_cash.CASH,
            settle_on=settle_on,
            transaction_id=transaction_id,
            force_cash=force_cash,
            allocation=allocation,
            in_lieu_of=written.event.id,
            defer_projection=True,
        ).event
    projection.project_account(session, account, from_date=occurred_on, today=today)
    return SplitRecorded(
        split=written.event,
        in_lieu=in_lieu,
        before=before,
        after=after,
        fraction=fraction if in_lieu is not None else None,
        cash_in_lieu=cash_in_lieu,
    )


@dataclass(frozen=True)
class Recorded:
    position: Position
    event: PositionEvent
    created: bool


def record_position(
    session: Session,
    *,
    account_ref: str,
    symbol: str,
    quantity: Decimal,
    as_of: date,
    cost_basis: Decimal | None = None,
    name: str | None = None,
    note: str | None = None,
    source_note: str | None = None,
    token: ApiToken | None,
    today: date | None = None,
    require_new: bool = False,
) -> Recorded:
    """Record "I hold ``quantity`` of ``symbol`` as of ``as_of``" in one call: opens
    the position when the account has none open in that symbol
    (``opened_on = as_of``), then records a ``set_quantity`` event.
    ``require_new`` refuses an existing open position (PositionConflict)
    instead of recording on it."""
    today = today or clock.ledger_today()
    resolved = ledger_service.resolve_account(session, account_ref)
    account = projection.lock_account(session, resolved.id)
    require_manual(account)
    normalized = symbol_of(symbol)
    position = open_position(session, account.id, normalized)
    if position is not None and require_new:
        raise PositionConflict(
            f"{account.path} already has an open {normalized} position; record an event on it"
        )
    created = position is None
    if position is None:
        if as_of > today:
            raise LedgerError(
                f"as_of {as_of.isoformat()} is in the future; record a position as of today "
                "or earlier"
            )
        position = _new_position(
            session, account, symbol=normalized, name=name, opened_on=as_of, token=token
        )
    elif name_of(name) is not None:
        _rename(session, account, position, name_of(name), token=token)
    event = _record(
        session,
        account,
        position,
        kind=core.SET_QUANTITY,
        occurred_on=as_of,
        quantity=quantity,
        cost_basis=cost_basis,
        note=note,
        source_note=source_note,
        token=token,
        today=today,
    ).event
    return Recorded(position=position, event=event, created=created)


def reverse_event(
    session: Session,
    *,
    event_id: uuid.UUID,
    note: str | None = None,
    token: ApiToken | None,
    today: date | None = None,
) -> PositionEvent:
    """Reverse an event: a mirror row pointing at it, after which neither
    counts. Refused when the rest of the history would then go below zero.
    A split's cash in lieu goes with it: its sale is reversed first, so the
    shares and the cash are back exactly as before the split (#624); that
    sale alone is never reversed (reverse the split)."""
    today = today or clock.ledger_today()
    original = session.get(PositionEvent, event_id)
    if original is None:
        raise NotFoundError(f"position event {event_id} not found")
    account, position = lock_position(session, original.position_id)
    if original.in_lieu_of is not None:
        raise LedgerError(
            f"position event {original.id} is the cash in lieu of split {original.in_lieu_of}; "
            "reverse the split, which reverses its cash in lieu with it"
        )
    if original.kind == core.SPLIT and original.reverses_id is None:
        in_lieu = _standing_in_lieu(session, original)
        if in_lieu is not None:
            _reverse(session, account, position, in_lieu, note=note, token=token, today=today)
    reversal = _reverse(session, account, position, original, note=note, token=token, today=today)
    projection.project_account(session, account, from_date=original.occurred_on, today=today)
    return reversal


def in_lieu_reversal(session: Session, reversal: PositionEvent) -> PositionEvent | None:
    """For the reversal of a split, the reversal of its cash-in-lieu sale
    written with it (None otherwise)."""
    if reversal.kind != core.SPLIT or reversal.reverses_id is None:
        return None
    sale = session.execute(
        select(PositionEvent.id).where(PositionEvent.in_lieu_of == reversal.reverses_id)
    ).scalar_one_or_none()
    if sale is None:
        return None
    return session.execute(
        select(PositionEvent).where(PositionEvent.reverses_id == sale)
    ).scalar_one_or_none()


def _standing_in_lieu(session: Session, split: PositionEvent) -> PositionEvent | None:
    """The split's cash-in-lieu sale, unless it is reversed."""
    sale = session.execute(
        select(PositionEvent).where(PositionEvent.in_lieu_of == split.id)
    ).scalar_one_or_none()
    if sale is None:
        return None
    reversed_ = session.execute(
        select(PositionEvent.id).where(PositionEvent.reverses_id == sale.id)
    ).first()
    return None if reversed_ is not None else sale


def _reverse(
    session: Session,
    account: Account,
    position: Position,
    original: PositionEvent,
    *,
    note: str | None,
    token: ApiToken | None,
    today: date,
) -> PositionEvent:
    """Write one reversal under the account lock the caller holds; the
    caller projects."""
    if original.reverses_id is not None:
        raise LedgerError("a reversal cannot itself be reversed; record the event again instead")
    already = session.execute(
        select(PositionEvent.id).where(PositionEvent.reverses_id == original.id)
    ).first()
    if already is not None:
        raise LedgerError(f"position event {original.id} is already reversed")
    # The cash goes with the shares (decision 103): undone before the reversal
    # row is written, which names the row that undid it.
    undone = trade_cash.undo(session, account, position, original, token=token)
    reversal = PositionEvent(
        position_id=position.id,
        occurred_on=original.occurred_on,
        kind=original.kind,
        quantity=original.quantity,
        price=original.price,
        fees=original.fees,
        cost_basis=original.cost_basis,
        reverses_id=original.id,
        origin=origin_of(token),
        note=note or None,
        actor_token_id=token.id if token else None,
        proceeds_to=original.proceeds_to,
        funded_from=original.funded_from,
        settle_on=original.settle_on,
        transaction_id=undone.id if undone is not None else None,
        gross_quantity=original.gross_quantity,
        withheld_quantity=original.withheld_quantity,
        split_numerator=original.split_numerator,
        split_denominator=original.split_denominator,
    )
    session.add(reversal)
    session.flush()
    # A lot it opened goes with it (a lot is live while its opening event
    # is), refused while a disposal still draws on that lot; its
    # dispositions stay and count for nothing (decision 107).
    timeline = _check_timeline(session, position)
    _check_no_overlap(session, account, position, today=today)
    unvested = _unvest(session, original, token=token)
    retired = _retire_opened_lots(session, original, token=token)
    held = timeline.quantity_on(today)
    audit.record(
        session,
        actor_token_id=token.id if token else None,
        action="position.reverse",
        entity_type="position",
        entity_id=position.id,
        before={"event_id": str(original.id), "kind": original.kind},
        after={
            "reversal_id": str(reversal.id),
            "reverses_id": str(original.id),
            "occurred_on": original.occurred_on.isoformat(),
            "note": reversal.note,
            "quantity_held": str(held),
            **(
                {
                    "cash_undone_by": str(undone.id),
                    "cash_transaction_id": str(original.transaction_id),
                }
                if undone is not None
                else {}
            ),
            **({"award_vest_id": str(unvested)} if unvested is not None else {}),
            **({"lots_retired": [str(i) for i in retired]} if retired else {}),
        },
    )
    events.emit(
        session,
        type_="position.updated",
        entity_type="position",
        entity_id=position.id,
        payload=_event_payload(
            account,
            position,
            change="reversed",
            kind=original.kind,
            occurred_on=original.occurred_on.isoformat(),
            quantity_held=str(held),
        ),
    )
    return reversal


def _retire_opened_lots(
    session: Session, original: PositionEvent, *, token: ApiToken | None
) -> list[uuid.UUID]:
    """A reversed buy or vest takes the lot it opened with it: the lot is
    voided (read-only history, ``void_note`` naming the reversal), so its
    reference and its (date, plan, ordinal) are free for the corrected
    event recorded next. The invariant check already refused the reversal
    while a disposal drew on the lot."""
    rows = list(
        session.execute(
            select(Lot).where(Lot.opened_by_event_id == original.id, Lot.voided_at.is_(None))
        ).scalars()
    )
    for lot in rows:
        lot.voided_at = func.now()
        lot.void_note = f"opening event {original.id} reversed"
        lot.voided_by_token_id = token.id if token else None
        audit.record(
            session,
            actor_token_id=token.id if token else None,
            action="lot.void",
            entity_type="lot",
            entity_id=lot.id,
            before={"status": "live", "quantity": str(lot.quantity)},
            after={"status": "reversed", "reverses_event_id": str(original.id)},
        )
    if rows:
        session.flush()
    return [lot.id for lot in rows]


def _unvest(
    session: Session, original: PositionEvent, *, token: ApiToken | None
) -> uuid.UUID | None:
    """A reversed vest hands its award vest back to ``scheduled`` (its lot
    goes with the event), so the units count as unvested again."""
    if original.kind != core.VEST:
        return None
    vest = session.execute(
        select(AwardVest).where(AwardVest.vest_event_id == original.id).with_for_update()
    ).scalar_one_or_none()
    if vest is None:
        return None
    before = {
        "status": vest.status,
        "lot_id": str(vest.lot_id) if vest.lot_id else None,
        "vest_event_id": str(original.id),
    }
    vest.status = "scheduled"
    vest.lot_id = None
    vest.vest_event_id = None
    vest.vested_gross = None
    vest.vested_withheld = None
    vest.vested_net = None
    vest.vested_fmv = None
    vest.updated_at = func.now()
    session.flush()
    audit.record(
        session,
        actor_token_id=token.id if token else None,
        action="award_vest.unvest",
        entity_type="award_vest",
        entity_id=vest.id,
        before=before,
        after={"status": "scheduled", "reason": "vest event reversed"},
    )
    return vest.id


def _rename(
    session: Session,
    account: Account,
    position: Position,
    name: str | None,
    *,
    token: ApiToken | None,
) -> None:
    """Rename to ``name`` (already ``name_of``-cleaned). The same name is
    no change: nothing is audited or emitted."""
    if name == position.name:
        return
    before = position.name
    position.name = name
    session.flush()
    audit.record(
        session,
        actor_token_id=token.id if token else None,
        action="position.update",
        entity_type="position",
        entity_id=position.id,
        before={"name": before},
        after={"name": position.name},
    )
    events.emit(
        session,
        type_="position.updated",
        entity_type="position",
        entity_id=position.id,
        payload=_event_payload(account, position, change="renamed", name=position.name),
    )


def _reopen(
    session: Session, position_id: uuid.UUID, *, token: ApiToken | None
) -> tuple[Account, Position]:
    """Reopen a closed position so its events can be corrected (decision 103:
    a closed position's last sale is otherwise beyond reach — reversing needs
    an open position). Locked like every position write. Refused (409) while
    another position in the symbol is open on the account (one open per
    symbol: the unique index), and while a LATER position in it exists: its
    history follows this one's, so a correction recorded here could overlap
    it, and a symbol's positions never overlap in time."""
    account, position = lock_position(session, position_id, closed_ok=True)
    if position.closed_at is None:
        raise LedgerError(f"the {position.symbol} position is open; only a closed one reopens")
    other = open_position(session, account.id, position.symbol)
    if other is not None:
        raise PositionConflict(
            f"{account.path} already has an open {position.symbol} position ({other.id}); "
            "close it before reopening this one"
        )
    later = session.execute(
        select(Position.opened_on)
        .where(
            Position.account_id == account.id,
            Position.symbol == position.symbol,
            Position.id != position.id,
            Position.opened_on > position.opened_on,
        )
        .order_by(Position.opened_on)
        .limit(1)
    ).scalar_one_or_none()
    if later is not None:
        raise PositionConflict(
            f"{account.path} held {position.symbol} again from {later.isoformat()} in a later "
            "position; reopening this earlier one could let the two overlap, so correct the "
            "later one or record the correction there"
        )
    closed_at = position.closed_at
    position.closed_at = None
    session.flush()
    audit.record(
        session,
        actor_token_id=token.id if token else None,
        action="position.reopen",
        entity_type="position",
        entity_id=position.id,
        before={"closed_at": closed_at.isoformat()},
        after={"closed_at": None},
    )
    events.emit(
        session,
        type_="position.updated",
        entity_type="position",
        entity_id=position.id,
        payload=_event_payload(account, position, change="reopened"),
    )
    return account, position


def update_position(
    session: Session,
    *,
    position_id: uuid.UUID,
    name: str | None = None,
    close: bool = False,
    reopen: bool = False,
    token: ApiToken | None,
    today: date | None = None,
) -> Position:
    """Rename a position, close it, or reopen a closed one. Closing needs the
    position to hold nothing today; a closed position is read-only history
    until it is reopened (to reverse an event and record it again)."""
    today = today or clock.ledger_today()
    if close and reopen:
        raise LedgerError("a position is closed or reopened, not both")
    if reopen:
        account, position = _reopen(session, position_id, token=token)
    else:
        account, position = lock_position(session, position_id)
    if name is not None:
        _rename(session, account, position, name_of(name), token=token)
    if close:
        held = _check_timeline(session, position).quantity_on(today)
        if held != 0:
            raise LedgerError(
                f"the {position.symbol} position still holds {held} shares; record a sell "
                "or set_quantity 0 before closing it"
            )
        position.closed_at = datetime.now(UTC)
        session.flush()
        audit.record(
            session,
            actor_token_id=token.id if token else None,
            action="position.close",
            entity_type="position",
            entity_id=position.id,
            after={"closed_at": position.closed_at.isoformat()},
        )
        events.emit(
            session,
            type_="position.updated",
            entity_type="position",
            entity_id=position.id,
            payload=_event_payload(account, position, change="closed", quantity_held="0"),
        )
    return position


def switch_source(
    session: Session,
    account: Account,
    source: str,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Switch where ``account``'s holdings come from (decision 88). The
    caller (``ledger.update_account``, admin scope) holds the account lock
    and writes the ``account.update`` audit row; what this returns joins its
    ``after``.

    - feed -> manual: no row is touched. The projection starts the day after
      the account's last feed snapshot, so feed history is never rewritten,
      and a manual period opens (``manual_period_open``).
    - manual -> feed: no row is touched either. The hand-tracked set stays in
      effect, with no dip, until the first feed fetch taken AFTER the switch
      ends the manual period (the importer, ``projection.end_manual_period``:
      the fetch's set replaces any hand-tracked rows from its date on, and a
      manual zero row ends each hand-tracked position it does not report, a
      cash-only feed included). A fetch taken before the switch is inside the
      manual period and adds nothing, even re-imported later. An account
      with no feed mapping has no feed to take over: refused (422).
      Positions and events stay, read-only until the account is switched
      back.

    Either way ``holdings_source_changed_at`` becomes the switch's moment
    (the database clock, like ``raw_fetches.fetched_at``): the importer's
    line between the two periods. The account row is locked first."""
    if source not in SOURCES:
        raise LedgerError(f"holdings_source must be one of: {', '.join(SOURCES)}")
    if account.type != "brokerage":
        raise LedgerError(
            f"{account.path} is a {account.type} account; holdings_source applies only to "
            "brokerage accounts"
        )
    if account.holdings_source == source:
        return {}
    if source == "feed" and account.connection_id is None:
        raise LedgerError(
            f"{account.path} has no feed mapping, so no feed can take its holdings over; "
            "map a SimpleFIN account onto it first"
        )
    today = today or clock.ledger_today()
    account = projection.lock_account(session, account.id)
    switched_at: datetime = session.execute(select(func.now())).scalar_one()
    account.holdings_source = source
    account.holdings_source_changed_at = switched_at
    if source == "manual":
        account.manual_period_open = True
    switched: dict[str, Any] = {"holdings_source_changed_at": switched_at.isoformat()}
    # The balance true-up (decision 119) belongs to feed holdings: off with
    # hand-tracked ones (CHECK accounts_balance_true_up_feed_brokerage), and
    # on again, the default for a feed-holdings account, back on the feed.
    account.balance_true_up = source == "feed"
    switched["balance_true_up"] = account.balance_true_up
    if source == "feed" and account.feed_balance_is_cash:
        # The cash opt-in (decision 103) belongs to manual holdings: a feed
        # account's balance is market value again, so it leaves the drift
        # check, and a basis pin with it.
        account.feed_balance_is_cash = False
        account.drift_basis_override = None
        switched["feed_balance_is_cash_cleared"] = True
    session.flush()
    if source == "manual":
        # Its positions' symbols are held again from now on (#415).
        projection.share_symbols(
            session,
            session.execute(
                select(Position.symbol).where(Position.account_id == account.id)
            ).scalars(),
        )
        result = projection.project_account(session, account, today=today)
        switched["manual_rows_written"] = result.inserted
    return switched


def end_period_unmapped(
    session: Session,
    account: Account,
    *,
    actor_token_id: uuid.UUID | None,
    today: date | None = None,
) -> bool:
    """A feed-sourced account whose manual period is still open loses its
    feed mapping (unmapped, closed): no fetch of its own will end the period,
    so it ends today (decision 88), with tombstones for every hand-tracked position
    and an ``account.manual_period_end`` audit row. The caller holds the
    account lock. Returns whether a period ended."""
    if account.holdings_source != "feed" or not account.manual_period_open:
        return False
    today = today or clock.ledger_today()
    deleted, ended = projection.end_manual_period(
        session,
        account,
        today,
        reported=set(),
        raw_fetch_id=None,
        actor_token_id=actor_token_id,
        reason="unmapped",
    )
    if deleted or ended:
        balances.mark_dirty(session, today, networth_only=True)
    return True


@dataclass(frozen=True)
class OpenPeriod:
    """A feed account still showing its hand-tracked set: switched back to
    the feed, but no fetch of it has arrived to end the manual period."""

    account_id: uuid.UUID
    account: str  # the path
    switched_at: datetime


def stale_open_periods(session: Session, *, stale_days: int, now: datetime) -> list[OpenPeriod]:
    """Accounts switched back to the feed more than ``stale_days`` ago whose
    manual period is still open (#422): no fetch of theirs has ended it
    (the connection fails, or the bank stopped listing the account), so the
    frozen hand-tracked set keeps counting and nothing re-prices it."""
    rows = session.execute(
        select(Account.id, Account.path, Account.holdings_source_changed_at)
        .where(
            Account.holdings_source == "feed",
            Account.manual_period_open.is_(True),
            Account.closed_at.is_(None),
            Account.holdings_source_changed_at < now - timedelta(days=stale_days),
        )
        .order_by(Account.path)
    ).all()
    return [OpenPeriod(r.id, str(r.path), r.holdings_source_changed_at) for r in rows]


@dataclass(frozen=True)
class PositionState:
    """A position as it stands on a day: computed from its events and the
    resolved price, the way the projection computes its rows."""

    position: Position
    quantity: Decimal
    cost_basis: Decimal | None
    price: core.PricePoint | None
    market_value: Decimal | None
    # A close, but a value that does not fit an amount (core.Valued): check
    # the share count; no close fixes it.
    value_out_of_range: bool
    stale_price: bool
    unrealized_gain: Decimal | None
    events: int
    # Lots (decision 107): None for a position that tracks none. Its
    # cost_basis above is then the lots' (unknown while any share has none).
    lots: core_lots.Basis | None = None
    open_lots: int = 0


def _state_of(
    position: Position,
    facts: list[core.Event],
    book: core.PriceBook | None,
    lot_book: core_lots.Book | None,
    *,
    stale_days: int,
    today: date,
) -> PositionState:
    try:
        state = core.Timeline(core.quantity_timeline(facts)).state_on(today)
    except core.OutOfRange as exc:  # never stored by the writes; a 422, not a 500
        raise LedgerError(f"{position.symbol}: {exc}") from exc
    quantity = state.quantity if state is not None else Decimal(0)
    basis = state.basis if state is not None else Decimal(0)
    lots_basis: core_lots.Basis | None = None
    open_lots = 0
    if lot_book is not None and lot_book.counted(today):
        # The lots' basis, never the running average (decision 107).
        lots_basis = core_lots.position_basis(lot_book, today, quantity)
        basis = lots_basis.basis
        open_lots = sum(1 for lot in lot_book.counted(today) if lot_book.remaining(lot) > 0)
    # A value that does not fit an amount at this close keeps the close
    # and has no value, as in the projection (core.valued). A close from
    # before a split is in pre-split shares (core.adjusted_close, #624).
    close = core.adjusted_close(
        book.on(today) if book is not None else None, core.split_points(facts), today
    )
    worth = core.valued(quantity, close)
    price, market = worth
    return PositionState(
        position=position,
        quantity=quantity,
        cost_basis=basis,
        price=price,
        market_value=market,
        value_out_of_range=worth.out_of_range,
        # Zero shares are valued on nothing: their close cannot be
        # stale (the price job no longer asks for it either).
        stale_price=quantity > 0
        and price is not None
        and core.stale(price.price_date, today, stale_days),
        unrealized_gain=core.unrealized_gain(market, basis) if quantity > 0 else None,
        events=len(core.effective_events(facts)),
        lots=lots_basis,
        open_lots=open_lots,
    )


def position_states(
    session: Session,
    account: Account,
    *,
    include_closed: bool = False,
    stale_days: int,
    today: date | None = None,
) -> list[PositionState]:
    """Every position of ``account`` as it stands ``today``. The closes are
    the ones in effect today only (``projection.latest_books``, #404)."""
    today = today or clock.ledger_today()
    stmt = select(Position).where(Position.account_id == account.id)
    if not include_closed:
        stmt = stmt.where(Position.closed_at.is_(None))
    positions = list(session.execute(stmt.order_by(Position.symbol, Position.opened_on)).scalars())
    rows = projection.position_events(session, [p.id for p in positions])
    books = projection.latest_books(session, {p.symbol for p in positions}, today=today)
    all_facts = {p.id: [projection.event_fact(r) for r in rows.get(p.id, [])] for p in positions}
    lot_books = lotbook.books(session, all_facts)
    return [
        _state_of(
            position,
            all_facts[position.id],
            books.get(position.symbol),
            lot_books.get(position.id),
            stale_days=stale_days,
            today=today,
        )
        for position in positions
    ]


def position_state(
    session: Session, position: Position, *, stale_days: int, today: date | None = None
) -> PositionState:
    """One position as it stands ``today``: what ``position_states`` reports
    for it, from its own events, lots and close alone (#404) — the REST
    answer to a write on one position and MCP ``record_position``."""
    today = today or clock.ledger_today()
    facts = [
        projection.event_fact(r)
        for r in projection.position_events(session, [position.id]).get(position.id, [])
    ]
    books = projection.latest_books(session, {position.symbol}, today=today)
    lot_books = lotbook.books(session, {position.id: facts})
    return _state_of(
        position,
        facts,
        books.get(position.symbol),
        lot_books.get(position.id),
        stale_days=stale_days,
        today=today,
    )


def list_events(session: Session, position_id: uuid.UUID) -> list[PositionEvent]:
    get_position(session, position_id)
    return list(
        session.execute(
            select(PositionEvent)
            .where(PositionEvent.position_id == position_id)
            .order_by(
                PositionEvent.occurred_on.desc(),
                PositionEvent.created_at.desc(),
                PositionEvent.id.desc(),
            )
        ).scalars()
    )
