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
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from purser import audit, balances, events
from purser import ledger as ledger_service
from purser.core import holdings as core
from purser.holdings import projection
from purser.holdings.projection import utc_today
from purser.ledger import LedgerError, NotFoundError
from purser.models import Account, ApiToken, Position, PositionEvent

SOURCES = ("feed", "manual")


class PositionConflict(LedgerError):
    """The account already has an open position in that symbol (REST 409)."""


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
            "holdings_source 'manual' (PATCH /accounts/{id}) before positions are recorded "
            "by hand; positions recorded earlier stay read-only until then."
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


def lock_position(session: Session, position_id: uuid.UUID) -> tuple[Account, Position]:
    """Lock a position's account, then read the position afresh, then check
    it is writable. A write that waited on the lock acts on the position as
    the writer before it left it: a close committed meanwhile refuses it, and
    an ``opened_on`` another write moved earlier is not overwritten."""
    position = get_position(session, position_id)
    account = projection.lock_account(session, position.account_id)
    session.refresh(position)
    require_manual(account)
    if position.closed_at is not None:
        raise LedgerError(f"the {position.symbol} position is closed (read-only)")
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
    today = today or utc_today()
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
    quantity = checked(core.check_quantity, quantity)
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


def _check_timeline(session: Session, position: Position) -> core.Timeline:
    rows = projection.position_events(session, [position.id]).get(position.id, [])
    try:
        return core.Timeline(core.quantity_timeline([projection.event_fact(r) for r in rows]))
    except core.NegativeQuantity as exc:
        raise LedgerError(
            f"{position.symbol}: this would leave {exc.quantity} shares on "
            f"{exc.on.isoformat()}; a position never holds fewer than zero shares"
        ) from exc
    except core.OutOfRange as exc:
        raise LedgerError(f"{position.symbol}: {exc}") from exc


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
) -> PositionEvent:
    """Record one event; the caller holds the account lock."""
    if position.closed_at is not None:
        raise LedgerError(f"the {position.symbol} position is closed (read-only)")
    if replaces is not None:
        _check_replaceable(session, position, replaces, occurred_on)
    if occurred_on > today:
        # The projection runs up to today: a future event would only take
        # effect if some later projection happened to cover its date (C5).
        raise LedgerError(
            f"occurred_on {occurred_on.isoformat()} is in the future; record an event "
            "once it has happened"
        )
    quantity, price, fees, cost_basis = _validated(kind, quantity, price, fees, cost_basis)
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
    )
    session.add(event)
    opened_before = position.opened_on
    if occurred_on < position.opened_on:
        position.opened_on = occurred_on  # the earliest event date
    session.flush()
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
            "quantity_held": str(held),
            **(
                {"opened_on": {"from": opened_before.isoformat(), "to": occurred_on.isoformat()}}
                if position.opened_on != opened_before
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
        ),
    )
    projection.project_account(session, account, from_date=occurred_on, today=today)
    return event


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
) -> PositionEvent:
    """Record an event. ``replaces`` names a reversed event of this position
    and date that this one corrects: it takes that event's place in the
    day's order instead of going after the day's other events (#401)."""
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
        today=today or utc_today(),
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
    today = today or utc_today()
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
    )
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
    counts. Refused when the rest of the history would then go below zero."""
    today = today or utc_today()
    original = session.get(PositionEvent, event_id)
    if original is None:
        raise NotFoundError(f"position event {event_id} not found")
    account, position = lock_position(session, original.position_id)
    if original.reverses_id is not None:
        raise LedgerError("a reversal cannot itself be reversed; record the event again instead")
    already = session.execute(
        select(PositionEvent.id).where(PositionEvent.reverses_id == original.id)
    ).first()
    if already is not None:
        raise LedgerError(f"position event {original.id} is already reversed")
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
    )
    session.add(reversal)
    session.flush()
    timeline = _check_timeline(session, position)
    _check_no_overlap(session, account, position, today=today)
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
    projection.project_account(session, account, from_date=original.occurred_on, today=today)
    return reversal


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


def update_position(
    session: Session,
    *,
    position_id: uuid.UUID,
    name: str | None = None,
    close: bool = False,
    token: ApiToken | None,
    today: date | None = None,
) -> Position:
    """Rename a position, or close it. Closing needs the position to hold
    nothing today; a closed position is read-only history."""
    today = today or utc_today()
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
    today = today or utc_today()
    account = projection.lock_account(session, account.id)
    switched_at: datetime = session.execute(select(func.now())).scalar_one()
    account.holdings_source = source
    account.holdings_source_changed_at = switched_at
    if source == "manual":
        account.manual_period_open = True
    session.flush()
    switched: dict[str, Any] = {"holdings_source_changed_at": switched_at.isoformat()}
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
    today = today or utc_today()
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


def position_states(
    session: Session,
    account: Account,
    *,
    include_closed: bool = False,
    stale_days: int,
    today: date | None = None,
) -> list[PositionState]:
    today = today or utc_today()
    stmt = select(Position).where(Position.account_id == account.id)
    if not include_closed:
        stmt = stmt.where(Position.closed_at.is_(None))
    positions = list(session.execute(stmt.order_by(Position.symbol, Position.opened_on)).scalars())
    rows = projection.position_events(session, [p.id for p in positions])
    books = projection.price_books(session, {p.symbol for p in positions}, upper=today)
    out: list[PositionState] = []
    for position in positions:
        facts = [projection.event_fact(r) for r in rows.get(position.id, [])]
        book = books.get(position.symbol)
        try:
            state = core.Timeline(core.quantity_timeline(facts)).state_on(today)
        except core.OutOfRange as exc:  # never stored by the writes; a 422, not a 500
            raise LedgerError(f"{position.symbol}: {exc}") from exc
        quantity = state.quantity if state is not None else Decimal(0)
        basis = state.basis if state is not None else Decimal(0)
        # A value that does not fit an amount at this close keeps the close
        # and has no value, as in the projection (core.valued).
        worth = core.valued(quantity, book.on(today) if book is not None else None)
        price, market = worth
        out.append(
            PositionState(
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
            )
        )
    return out


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
