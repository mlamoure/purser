"""Manually tracked holdings API: positions, their events, and prices
(issue #390, DESIGN §8, decisions 88/89).

Every route declares its scope (guardrail 4). Writes are write scope: a
position has no pending state to propose into, and a recorded quantity moves
net worth the moment it lands. Each write is audited and re-projects the
account's snapshot rows in the same transaction; net worth heals through the
watermark, kicked after the response.
"""

import uuid
from datetime import date as date_type
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from purser import appsettings, clock, ledger, networth
from purser.admin.auth import AdminToken, ReadToken, WriteToken
from purser.db import get_session
from purser.holdings import cash as trade_cash
from purser.holdings import prices as price_service
from purser.holdings import service
from purser.models import Position, PositionEvent, SecurityPrice

DbSession = Annotated[Session, Depends(get_session)]

router = APIRouter(tags=["positions"])
prices_router = APIRouter(prefix="/prices", tags=["prices"])

EventKind = Literal["set_quantity", "buy", "sell", "vest", "transfer_out"]
LotMethod = Literal["fifo", "lifo", "hifo"]
Acquisition = Literal[
    "buy", "rsu_vest", "plan_purchase", "espp_purchase", "transfer_in", "drip", "gift", "unknown"
]


def _handle(exc: ledger.LedgerError) -> HTTPException:
    if isinstance(exc, ledger.NotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, service.PositionConflict):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, service.LotRuleError):
        return HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail={"code": exc.code, "message": str(exc)}
        )
    if isinstance(exc, trade_cash.CashAlreadyRecorded):
        return HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "cash_already_recorded",
                "message": str(exc),
                "transaction_id": str(exc.transaction_id),
                "date": exc.date.isoformat(),
                "amount": str(exc.amount),
            },
        )
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))


_CASH_CONFLICT = {
    "description": "Either an open position in that symbol exists (`detail` is a message), "
    "or the account already shows this trade's cash: `detail.code` is "
    "`cash_already_recorded` with the row's `transaction_id`, `date` and `amount`. Link it "
    "with `transaction_id` when it is this trade's cash, or pass `force_cash` when it is a "
    "different payment of the same amount."
}


class PositionOut(BaseModel):
    """A position as it stands today, computed from its events and the close
    in effect (newest date first; manual, then provider, then feed-implied
    only on a tie)."""

    id: uuid.UUID
    account_id: uuid.UUID
    symbol: str
    name: str | None
    opened_on: date_type
    closed_at: datetime | None
    quantity: Decimal
    cost_basis: Decimal | None  # total; null when unknown
    price: Decimal | None  # the close in effect today; null = no price yet
    price_as_of: date_type | None
    price_source: str | None  # manual | tiingo | feed (implied from a feed snapshot)
    market_value: Decimal | None  # null when there is no price: never a silent 0
    # A close, but the value does not fit an amount: check the share count
    # (recording a close cannot fix it). market_value is null then too.
    value_out_of_range: bool
    unrealized_gain: Decimal | None
    stale_price: bool  # the close is older than the configured staleness threshold
    events: int  # effective events (reversed ones and reversals excluded)
    # Lots (#393, decision 107). A position that tracks lots reports its
    # lots' basis as cost_basis (null while any share has none or sits
    # outside every lot), never an average.
    tracks_lots: bool = False
    open_lots: int = 0
    unallocated_quantity: Decimal | None = None  # shares outside every lot
    basis_unknown_quantity: Decimal | None = None  # shares in lots with no basis

    @classmethod
    def from_state(cls, state: service.PositionState) -> "PositionOut":
        p = state.position
        return cls(
            id=p.id,
            account_id=p.account_id,
            symbol=p.symbol,
            name=p.name,
            opened_on=p.opened_on,
            closed_at=p.closed_at,
            quantity=state.quantity,
            cost_basis=state.cost_basis,
            price=state.price.close if state.price is not None else None,
            price_as_of=state.price.price_date if state.price is not None else None,
            price_source=state.price.source if state.price is not None else None,
            market_value=state.market_value,
            value_out_of_range=state.value_out_of_range,
            unrealized_gain=state.unrealized_gain,
            stale_price=state.stale_price,
            events=state.events,
            tracks_lots=state.lots is not None,
            open_lots=state.open_lots,
            unallocated_quantity=state.lots.unallocated if state.lots is not None else None,
            basis_unknown_quantity=(
                state.lots.unknown_quantity if state.lots is not None else None
            ),
        )


class PositionCreate(BaseModel):
    symbol: str = Field(
        min_length=1,
        max_length=15,
        description="The ticker, e.g. `ACME`; stored upper case. Letters, digits, `.` and "
        "`-` (share classes such as `BRK.B`).",
    )
    name: str | None = Field(default=None, max_length=200)
    quantity: Decimal | None = Field(
        default=None,
        ge=0,
        decimal_places=6,
        max_digits=20,
        description="Shares held on `as_of`; records the opening `set_quantity` event. "
        "Omit to open the position empty and record its events separately.",
    )
    as_of: date_type | None = Field(
        default=None, description="The date `quantity` holds (today or earlier; default today)."
    )
    cost_basis: Decimal | None = Field(
        default=None,
        ge=0,
        decimal_places=4,
        max_digits=20,
        description="Total cost basis of `quantity`, when known (for unrealized gain).",
    )
    note: str | None = Field(default=None, max_length=500)
    source_note: str | None = Field(
        default=None,
        max_length=500,
        description="Where the figure came from, e.g. `broker holdings page, 2026-10-14`.",
    )


class PositionUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    closed: bool | None = Field(
        default=None,
        description="`true` closes the position (it must hold nothing today); a closed "
        "position is read-only history. `false` reopens a closed one so an event can be "
        "corrected (reverse it, record it again, close again); refused (409) while another "
        "position in the symbol is open on the account, or a later one exists.",
    )


class LotAllocationIn(BaseModel):
    """One lot a sell or transfer_out draws on: `lot_id`, or the broker's
    `external_ref` (matched by blind index), or `unallocated: true` for
    shares outside every lot."""

    lot_id: uuid.UUID | None = None
    external_ref: str | None = Field(default=None, max_length=200)
    unallocated: bool = False
    quantity: Decimal = Field(gt=0, decimal_places=6, max_digits=20)

    def item(self) -> tuple[str | None, bool, Decimal]:
        ref = str(self.lot_id) if self.lot_id is not None else self.external_ref
        return ref, self.unallocated, self.quantity


class LotOpenIn(BaseModel):
    """The lot a buy or vest opens. Basis defaults to the event's own (a
    priced buy's cost with fees; a vest's fair market value); otherwise the
    given per-share and/or total basis; otherwise unknown."""

    acquisition: Acquisition | None = None
    external_ref: str | None = Field(default=None, max_length=200)
    plan: str | None = Field(default=None, max_length=200)
    acquired_on: date_type | None = Field(
        default=None, description="Defaults to the event date; never after it."
    )
    basis_per_share: Decimal | None = Field(default=None, ge=0, decimal_places=6, max_digits=20)
    cost_basis: Decimal | None = Field(default=None, ge=0, decimal_places=4, max_digits=20)

    def spec(self) -> service.LotSpec:
        return service.LotSpec(
            acquisition=self.acquisition,
            external_ref=self.external_ref,
            plan=self.plan,
            acquired_on=self.acquired_on,
            basis_per_share=self.basis_per_share,
            cost_basis=self.cost_basis,
        )


class PositionEventCreate(BaseModel):
    kind: EventKind = Field(
        description="`set_quantity` (the absolute share count on that date), `buy` or "
        "`sell` (a change by `quantity`), `vest` (the NET shares an award vest delivered; "
        "`price` is the fair market value per share) or `transfer_out` (shares that left "
        "with no price and no cash)."
    )
    occurred_on: date_type = Field(description="Today or earlier; a future date is refused.")
    quantity: Decimal = Field(ge=0, decimal_places=6, max_digits=20)
    price: Decimal | None = Field(
        default=None,
        gt=0,
        decimal_places=6,
        max_digits=20,
        description="Per share, `buy`/`sell` only. A buy's price adds to the cost basis; "
        "without one the basis becomes unknown.",
    )
    fees: Decimal | None = Field(
        default=None, ge=0, decimal_places=4, max_digits=20, description="`buy`/`sell` only."
    )
    cost_basis: Decimal | None = Field(
        default=None,
        ge=0,
        decimal_places=4,
        max_digits=20,
        description="`set_quantity` only: the total basis after it. Omitted, a re-count of "
        "the count held just before keeps the basis, a changed count makes it unknown, and "
        "a count of 0 has a basis of 0.",
    )
    note: str | None = Field(default=None, max_length=500)
    source_note: str | None = Field(default=None, max_length=500)
    replaces: uuid.UUID | None = Field(
        default=None,
        description="A reversed event of this position, on the same date, that this one "
        "corrects. The correction takes that event's place in the day's order; without "
        "`replaces` a new event goes after every event already recorded that day.",
    )
    proceeds_to: Literal["cash", "none"] | None = Field(
        default=None,
        description="`sell` only. `cash` (the default): the net proceeds (quantity x price - "
        "fees, rounded half up to the cent) land on the account, balanced against the system "
        "account `equity.securities`, on the trade date (or `settle_on`). `none`: no cash "
        "moves, e.g. shares withheld for tax. Refused with 409 when the account already shows "
        "that cash.",
    )
    funded_from: Literal["cash", "none"] | None = Field(
        default=None,
        description="`buy` only. `cash`: the cost (quantity x price + fees, rounded half up "
        "to the cent) leaves the account's cash on the trade date (or `settle_on`). `none` "
        "(the default): the money came from outside the account, e.g. a payroll plan "
        "purchase.",
    )
    settle_on: date_type | None = Field(
        default=None,
        description="When the cash posts: by default the trade date, the day the shares "
        "leave the holdings (so net worth does not dip); a later date posts it at "
        "settlement instead. The double-count check looks from the trade to five days "
        "after `settle_on`, or after the next business day (T+1) when it is not given. A "
        "trade that links its cash (`transaction_id`) settles on that row's date, and its "
        "window comes from the trade alone (next business day + 5 days).",
    )
    transaction_id: uuid.UUID | None = Field(
        default=None,
        description="Link the trade's cash to this posted row on the account (one real leg, "
        "one category leg) instead of posting new cash: the row is re-booked to "
        "`equity.securities` through the reversal-pair correction. It must hold exactly the "
        "trade's cash (its net, to the cent, so the trade needs its `price`), dated from the "
        "trade to five days after the next business day (`settle_on` does not widen it), "
        "and no row of its correction chain may settle a bill; any other row is a 422.",
    )
    force_cash: bool | None = Field(
        default=None,
        description="Post the trade's cash even though the account already shows a row of "
        "exactly that amount around the settlement date (the 409 `cash_already_recorded` "
        "names it): for a true coincidence, a different payment that is not this trade's "
        "cash. The trade's audit row records the override and the row it posted over. Not "
        "with `transaction_id`, nor on a trade that moves no cash.",
    )
    gross_quantity: Decimal | None = Field(
        default=None,
        ge=0,
        decimal_places=6,
        max_digits=20,
        description="`vest` only: the gross units that vested (net `quantity` + withheld).",
    )
    withheld_quantity: Decimal | None = Field(
        default=None,
        ge=0,
        decimal_places=6,
        max_digits=20,
        description="`vest` only: units withheld for tax (default 0).",
    )
    lot_method: LotMethod | None = Field(
        default=None,
        description="`sell`/`transfer_out` on a position that tracks lots: draw on its lots "
        "first in, first out (`fifo`, by acquisition date), last in (`lifo`) or highest "
        "basis per share first (`hifo`; refused while a candidate lot has no basis). A method "
        "never takes unallocated shares. Give this or `lots`.",
    )
    lots: list[LotAllocationIn] | None = Field(
        default=None,
        max_length=500,
        description="`sell`/`transfer_out`: specific identification. The quantities add up "
        "to `quantity`; each lot gives at most what it has left after every disposal "
        "recorded so far.",
    )
    lot: LotOpenIn | None = Field(
        default=None,
        description="`buy`/`vest`: the lot it opens. A vest always opens one; a buy opens "
        "one when this is given or the position already tracks lots.",
    )
    unallocated: bool | None = Field(
        default=None,
        description="`buy` only: keep its shares outside every lot on a position that tracks lots.",
    )


class PositionEventOut(BaseModel):
    id: uuid.UUID
    position_id: uuid.UUID
    occurred_on: date_type
    kind: EventKind
    quantity: Decimal
    price: Decimal | None
    fees: Decimal | None
    cost_basis: Decimal | None
    reverses_id: uuid.UUID | None  # set on a reversal row
    replaces_id: uuid.UUID | None  # the reversed event this one corrects (#401)
    reversed_by: uuid.UUID | None  # the reversal that cancels this event, if any
    origin: Literal["manual", "agent", "import"]
    note: str | None
    source_note: str | None
    proceeds_to: Literal["cash", "none"] | None = None
    funded_from: Literal["cash", "none"] | None = None
    settle_on: date_type | None = None
    # The trade's cash row (posted by Purser or linked); on a reversal, the
    # row that undid it.
    transaction_id: uuid.UUID | None = None
    gross_quantity: Decimal | None = None  # vest only
    withheld_quantity: Decimal | None = None  # vest only
    created_at: datetime

    @classmethod
    def from_model(
        cls, e: PositionEvent, reversed_by: uuid.UUID | None = None
    ) -> "PositionEventOut":
        return cls(
            id=e.id,
            position_id=e.position_id,
            occurred_on=e.occurred_on,
            kind=e.kind,
            quantity=e.quantity,
            price=e.price,
            fees=e.fees,
            cost_basis=e.cost_basis,
            reverses_id=e.reverses_id,
            replaces_id=e.replaces_id,
            reversed_by=reversed_by,
            origin=e.origin,
            note=e.note,
            source_note=e.source_note,
            proceeds_to=e.proceeds_to,
            funded_from=e.funded_from,
            settle_on=e.settle_on,
            transaction_id=e.transaction_id,
            gross_quantity=e.gross_quantity,
            withheld_quantity=e.withheld_quantity,
            created_at=e.created_at,
        )


class PositionEventResult(BaseModel):
    """The event written and the position after it."""

    event: PositionEventOut
    position: PositionOut


class PositionReverseRequest(BaseModel):
    note: str | None = Field(default=None, max_length=500)


def _stale_days() -> int:
    return appsettings.effective().price_stale_days


def _state(session: Session, position: Position) -> service.PositionState:
    account = ledger.get_account(session, position.account_id)
    states = service.position_states(
        session, account, include_closed=True, stale_days=_stale_days()
    )
    return next(s for s in states if s.position.id == position.id)


@router.get("/accounts/{account_id}/positions")
def list_positions(
    account_id: uuid.UUID,
    token: ReadToken,
    session: DbSession,
    include_closed: Annotated[bool, Query()] = False,
) -> list[PositionOut]:
    """The account's positions, each valued as of today. A feed-sourced
    account lists the positions recorded while it was tracked by hand, if
    any; they are read-only until it is switched back to manual."""
    try:
        account = ledger.get_account(session, account_id)
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    if account.is_virtual:
        raise HTTPException(422, f"{account.path} is a category; positions belong to accounts")
    try:
        states = service.position_states(
            session, account, include_closed=include_closed, stale_days=_stale_days()
        )
    except ledger.LedgerError as exc:  # a value out of range (a stored close): 422
        raise _handle(exc) from exc
    return [PositionOut.from_state(s) for s in states]


@router.post("/accounts/{account_id}/positions", status_code=status.HTTP_201_CREATED)
def create_position(
    account_id: uuid.UUID,
    body: PositionCreate,
    token: WriteToken,
    session: DbSession,
    background_tasks: BackgroundTasks,
) -> PositionOut:
    """Open a position on a brokerage account whose holdings are tracked by
    hand (`holdings_source` `manual`; 422 otherwise). 409 while an open
    position in that symbol exists, and 409 when the new position would hold
    shares on a day a CLOSED position in the symbol held them or sold out
    (positions never overlap: start the new one after it). With `quantity`,
    it also records the opening `set_quantity` on `as_of`."""
    try:
        if body.quantity is not None:
            recorded = service.record_position(
                session,
                account_ref=str(account_id),
                symbol=body.symbol,
                quantity=body.quantity,
                as_of=body.as_of or clock.ledger_today(),
                cost_basis=body.cost_basis,
                name=body.name,
                note=body.note,
                source_note=body.source_note,
                token=token,
                require_new=True,
            )
            position = recorded.position
        else:
            if body.cost_basis is not None:
                raise ledger.LedgerError("cost_basis needs a quantity")
            position = service.create_position(
                session,
                account_ref=str(account_id),
                symbol=body.symbol,
                name=body.name,
                opened_on=body.as_of,
                token=token,
            )
        out = PositionOut.from_state(_state(session, position))
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    background_tasks.add_task(networth.consume_watermark)
    return out


_REOPEN_CONFLICT = {
    "description": "Reopen refused: another position in the symbol is open on the account, "
    "or a later position in it exists (a symbol's positions never overlap in time)."
}


@router.patch("/positions/{position_id}", responses={409: _REOPEN_CONFLICT})
def update_position(
    position_id: uuid.UUID, body: PositionUpdate, token: WriteToken, session: DbSession
) -> PositionOut:
    """Rename a position, close one that holds nothing today, or reopen a
    closed one (`closed: false`) to correct its events: reverse the wrong
    event, record the right one, close it again."""
    try:
        position = service.update_position(
            session,
            position_id=position_id,
            name=body.name,
            close=body.closed is True,
            reopen=body.closed is False,
            token=token,
        )
        out = PositionOut.from_state(_state(session, position))
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    return out


@router.get("/positions/{position_id}/events")
def list_position_events(
    position_id: uuid.UUID, token: ReadToken, session: DbSession
) -> list[PositionEventOut]:
    """Every event of the position, reversals included, newest first."""
    try:
        rows = service.list_events(session, position_id)
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    reversed_by = {r.reverses_id: r.id for r in rows if r.reverses_id is not None}
    return [PositionEventOut.from_model(r, reversed_by.get(r.id)) for r in rows]


@router.post(
    "/positions/{position_id}/events",
    status_code=status.HTTP_201_CREATED,
    responses={409: _CASH_CONFLICT},
)
def record_position_event(
    position_id: uuid.UUID,
    body: PositionEventCreate,
    token: WriteToken,
    session: DbSession,
    background_tasks: BackgroundTasks,
) -> PositionEventResult:
    """Record a `set_quantity`, `buy`, `sell`, `vest` or `transfer_out`.

    On a position that tracks lots, a `sell` or `transfer_out` names the
    lots it draws on (`lot_method` or `lots`; 422 `allocation_required`
    otherwise), a `buy` opens a lot unless `unallocated`, and every write is
    refused (422 with `detail.code`) that would leave a lot below zero or
    the lots holding more than the position on any day. The whole history is checked
    after it: an event that would leave the position below zero on ANY date
    (a backdated sell, say) is refused with 422 naming that date. Any event,
    a backdate or not, that would leave this position holding shares on a
    day a CLOSED position in the same symbol held them or sold out is
    refused with 409 (positions never overlap).

    A sale moves its net proceeds to the account's cash unless `proceeds_to`
    is `none`; a buy takes its cost from the cash only with `funded_from`
    `cash`. That cash is one posted system row against `equity.securities`
    (it joins the unreviewed set), or an existing row linked with
    `transaction_id`. When the account already shows a posted one-sided row
    of exactly that amount around the settlement date, the call is refused
    with 409 naming it, so the cash is never counted twice: link that row,
    or pass `force_cash` when it is a different payment."""
    try:
        allocation = service.resolve_allocation(
            session,
            position_id,
            method=body.lot_method,
            items=[a.item() for a in body.lots] if body.lots else None,
        )
        event = service.record_event(
            session,
            position_id=position_id,
            kind=body.kind,
            occurred_on=body.occurred_on,
            quantity=body.quantity,
            price=body.price,
            fees=body.fees,
            cost_basis=body.cost_basis,
            note=body.note,
            source_note=body.source_note,
            replaces=body.replaces,
            token=token,
            proceeds_to=body.proceeds_to,
            funded_from=body.funded_from,
            settle_on=body.settle_on,
            transaction_id=body.transaction_id,
            force_cash=bool(body.force_cash),
            allocation=allocation,
            lot=body.lot.spec() if body.lot is not None else None,
            unallocated=bool(body.unallocated),
            gross_quantity=body.gross_quantity,
            withheld_quantity=body.withheld_quantity,
        )
        position = service.get_position(session, position_id)
        out = PositionEventResult(
            event=PositionEventOut.from_model(event),
            position=PositionOut.from_state(_state(session, position)),
        )
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    background_tasks.add_task(networth.consume_watermark)
    return out


@router.post("/position-events/{event_id}/reverse")
def reverse_position_event(
    event_id: uuid.UUID,
    token: WriteToken,
    session: DbSession,
    background_tasks: BackgroundTasks,
    body: PositionReverseRequest | None = None,
) -> PositionEventResult:
    """Reverse an event (corrections are reversals, never edits). Refused when
    the remaining history would go below zero on some date. A trade's cash
    goes with it, through the reversal pair every posted correction uses:
    Purser's own cash row is corrected to zero, and a linked row gets its
    category back. The body (a `note`) is optional."""
    try:
        reversal = service.reverse_event(
            session, event_id=event_id, note=body.note if body else None, token=token
        )
        position = service.get_position(session, reversal.position_id)
        out = PositionEventResult(
            event=PositionEventOut.from_model(reversal),
            position=PositionOut.from_state(_state(session, position)),
        )
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    background_tasks.add_task(networth.consume_watermark)
    return out


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------


class PriceOut(BaseModel):
    symbol: str
    date: date_type
    close: Decimal
    source: str  # manual | tiingo
    fetched_at: datetime

    @classmethod
    def from_model(cls, p: SecurityPrice) -> "PriceOut":
        return cls(
            symbol=p.symbol,
            date=p.price_date,
            close=p.close,
            source=p.source,
            fetched_at=p.fetched_at,
        )


class PriceCreate(BaseModel):
    symbol: str = Field(min_length=1, max_length=15)
    date: date_type = Field(description="The close's date: today or earlier.")
    close: Decimal = Field(gt=0, decimal_places=6, max_digits=20)


class PriceRecordedOut(BaseModel):
    symbol: str
    date: date_type
    close: Decimal
    previous: Decimal | None  # the manual close of that date it replaced
    accounts_reprojected: int
    # Accounts holding the symbol in a feed row the feed values at 0, valued
    # from the close from that date on (decision 92).
    feed_accounts_revalued: int = 0


class SymbolPriceOut(BaseModel):
    symbol: str
    close: Decimal | None
    price_as_of: date_type | None
    source: str | None
    stale: bool  # older than stale_days, or no close at all


class FetchFailureOut(BaseModel):
    at: datetime
    symbol: str | None
    status: int | None
    # What went wrong, e.g. `not found`, `refused`, `1 close(s) out of range
    # dropped`, or the transport failure's class name; never request data.
    reason: str | None = None


class PriceStatusOut(BaseModel):
    provider: str
    stale_days: int
    last_fetched_at: datetime | None  # newest provider close stored
    symbols: list[SymbolPriceOut]  # every symbol an open manual position holds
    last_failure: FetchFailureOut | None


class FetchFailedOut(BaseModel):
    symbol: str
    status: int | None
    reason: str | None = None  # as in FetchFailureOut


class StaleOut(BaseModel):
    symbol: str
    price_as_of: str | None


class FetchOut(BaseModel):
    provider: str
    skipped: str | None  # why nothing was fetched, when nothing was
    symbols: int
    fetched: int  # closes stored
    failed: list[FetchFailedOut]
    accounts_projected: int
    accounts_failed: int
    stale: list[StaleOut]


@prices_router.get("")
def list_prices(
    token: ReadToken,
    session: DbSession,
    symbol: Annotated[str, Query(min_length=1, max_length=15)],
    start: Annotated[date_type | None, Query()] = None,
    end: Annotated[date_type | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> list[PriceOut]:
    """Stored closes of one symbol (manual and provider), newest first.
    Feed-implied prices are computed on read and never stored, so they are
    not listed here."""
    try:
        rows = price_service.list_prices(session, symbol=symbol, start=start, end=end, limit=limit)
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    return [PriceOut.from_model(p) for p in rows]


@prices_router.post("")
def record_price(
    body: PriceCreate,
    token: WriteToken,
    session: DbSession,
    background_tasks: BackgroundTasks,
) -> PriceRecordedOut:
    """Record a symbol's close on a date by hand (replaces the manual close of
    that date, if any). It wins over a provider's close of the SAME date; a
    newer close of any source still supersedes it."""
    try:
        recorded = price_service.record_price(
            session, symbol=body.symbol, price_date=body.date, close=body.close, token=token
        )
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    background_tasks.add_task(networth.consume_watermark)
    return PriceRecordedOut(
        symbol=recorded.symbol,
        date=recorded.price_date,
        close=recorded.close,
        previous=recorded.previous,
        accounts_reprojected=recorded.accounts,
        feed_accounts_revalued=recorded.feed_accounts,
    )


@prices_router.get("/status")
def price_status(token: ReadToken, session: DbSession) -> PriceStatusOut:
    """The configured provider, the newest provider fetch, each open
    position's close in effect today, and the last fetch failure."""
    report = price_service.status(session, today=clock.ledger_today())
    failure = report["last_failure"]
    return PriceStatusOut(
        provider=report["provider"],
        stale_days=report["stale_days"],
        last_fetched_at=report["last_fetched_at"],
        symbols=[
            SymbolPriceOut(
                symbol=s.symbol,
                close=s.close,
                price_as_of=s.price_as_of,
                source=s.source,
                stale=s.stale,
            )
            for s in report["symbols"]
        ],
        last_failure=FetchFailureOut(**failure) if failure is not None else None,
    )


@prices_router.post("/fetch")
def fetch_prices(token: AdminToken, background_tasks: BackgroundTasks) -> FetchOut:
    """Fetch now: the daily price job's body, on demand (admin). Fetches new
    closes from the configured provider, re-projects every manual account,
    and reports staleness."""
    result = price_service.run_fetch(actor_token_id=token.id)
    background_tasks.add_task(networth.consume_watermark)
    return FetchOut(
        provider=result.provider,
        skipped=result.skipped,
        symbols=result.symbols,
        fetched=result.fetched,
        failed=[FetchFailedOut(**f) for f in result.failed],
        accounts_projected=result.accounts_projected,
        accounts_failed=result.accounts_failed,
        stale=[StaleOut(**s) for s in result.stale],
    )
