"""Net worth, holdings, and valuations API."""

import uuid
from datetime import UTC, datetime, timedelta
from datetime import date as date_type
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from purser import networth as service
from purser.admin.auth import ReadToken, WriteToken
from purser.db import get_session
from purser.holdings.overlay import Holding
from purser.ledger import LedgerError, NotFoundError
from purser.models import Valuation

DbSession = Annotated[Session, Depends(get_session)]

router = APIRouter(prefix="/networth", tags=["networth"])
holdings_router = APIRouter(prefix="/holdings", tags=["holdings"])
valuations_router = APIRouter(prefix="/accounts/{account_id}/valuations", tags=["valuations"])


class BreakdownItem(BaseModel):
    account_id: uuid.UUID
    path: str
    type: str
    value: Decimal
    method: str


class NetWorthOut(BaseModel):
    as_of: date_type
    assets: Decimal
    liabilities: Decimal
    net_worth: Decimal
    breakdown: list[BreakdownItem]


class SeriesPoint(BaseModel):
    as_of: date_type
    assets: Decimal
    liabilities: Decimal
    net_worth: Decimal


class HoldingOut(BaseModel):
    account_id: uuid.UUID
    as_of: date_type
    symbol: str
    quantity: Decimal | None
    cost_basis: Decimal | None
    market_value: Decimal | None  # null = no price (a manual position adds nothing then)
    # feed = a SimpleFIN snapshot; manual = projected from a position tracked
    # by hand (decision 88), which also carries the close it used and its date.
    source: str = "feed"
    position_id: uuid.UUID | None = None
    price: Decimal | None = None
    price_as_of: date_type | None = None
    # A feed row the feed values at 0 (a plan-only fund), valued from a stored
    # close (decision 92): that close's source, `manual` or the provider.
    price_source: str | None = None
    # A feed row with no value whose symbol is no ticker (the feed sent a
    # description): no recorded close can value it (decision 92).
    unpriceable: bool = False
    # A close, but a value too large to store: check the share count.
    value_out_of_range: bool = False

    @classmethod
    def from_model(cls, h: Holding) -> "HoldingOut":
        return cls(
            account_id=h.account_id,
            as_of=h.as_of,
            symbol=h.symbol,
            quantity=h.quantity,
            cost_basis=h.cost_basis,
            market_value=h.market_value,
            source=h.source,
            position_id=h.position_id,
            price=h.price,
            price_as_of=h.price_as_of,
            price_source=h.price_source,
            unpriceable=h.unpriceable,
            value_out_of_range=(
                h.market_value is None and h.price is not None and bool(h.quantity)
            ),
        )


class ValuationCreate(BaseModel):
    as_of: date_type
    value: Decimal = Field(
        decimal_places=4,
        max_digits=20,
        description="The account's value on as_of, in ledger sign unless `convention` says "
        "otherwise: a property is positive, a loan is NEGATIVE while money is owed. A "
        "positive value on a loan is rejected (422) rather than recorded as an asset.",
    )
    source: str = "manual"
    convention: Literal["ledger", "statement"] = Field(
        default="ledger",
        description="How `value`'s sign reads. `ledger` (default): as Purser stores it, a "
        "loan negative when owed. `statement`: as the statement prints it, a loan POSITIVE "
        "when owed (a credit balance is negative); Purser negates it into ledger sign, the "
        "same conversion reconciliation applies. A property reads the same either way.",
    )


class ValuationOut(BaseModel):
    id: uuid.UUID
    account_id: uuid.UUID
    as_of: date_type
    value: Decimal
    source: str


def _handle(exc: LedgerError) -> HTTPException:
    code = status.HTTP_404_NOT_FOUND if isinstance(exc, NotFoundError) else 422
    return HTTPException(code, str(exc))


@router.get("")
def net_worth(
    token: ReadToken,
    session: DbSession,
    as_of: Annotated[date_type | None, Query()] = None,
) -> NetWorthOut:
    """Point-in-time net worth with a per-account breakdown — consistent for
    any historical date (postings <= T, latest snapshot/valuation <= T)."""
    result = service.net_worth_at(session, as_of or datetime.now(UTC).date())
    return NetWorthOut(
        as_of=result["as_of"],
        assets=result["assets"],
        liabilities=result["liabilities"],
        net_worth=result["net_worth"],
        breakdown=[BreakdownItem(**b) for b in result["breakdown"]],
    )


@router.get("/series")
def net_worth_series(
    token: ReadToken,
    session: DbSession,
    start: Annotated[date_type, Query()],
    end: Annotated[date_type, Query()],
) -> list[SeriesPoint]:
    try:
        points = service.series(session, start=start, end=end)
    except LedgerError as exc:
        raise _handle(exc) from exc
    return [SeriesPoint(**p) for p in points]


@router.post("/materialize")
def materialize(
    token: WriteToken,
    session: DbSession,
    start: Annotated[date_type, Query()],
    end: Annotated[date_type, Query()],
) -> dict[str, int]:
    """Backfill/refresh the net_worth_daily materialization for a range."""
    try:
        points = service.series(session, start=start, end=end)
    except LedgerError as exc:
        raise _handle(exc) from exc
    day = start
    count = 0
    while day <= end:
        service.materialize_day(session, day)
        day = day + timedelta(days=1)
        count += 1
    session.commit()
    return {"days_materialized": count, "points": len(points)}


@holdings_router.get("")
def list_holdings(
    token: ReadToken,
    session: DbSession,
    account: Annotated[str | None, Query(description="account id or path")] = None,
    as_of: Annotated[date_type | None, Query()] = None,
) -> list[HoldingOut]:
    """Latest snapshot set per brokerage account (cost basis + market value),
    whichever source the account's holdings come from: feed snapshots, or the
    projection of positions tracked by hand (`source` says which)."""
    from sqlalchemy import select

    from purser import ledger as ledger_service
    from purser.models import Account

    if account is not None:
        try:
            accounts = [ledger_service.resolve_account(session, account)]
        except LedgerError as exc:
            raise _handle(exc) from exc
    else:
        accounts = list(
            session.execute(select(Account).where(Account.type == "brokerage")).scalars()
        )
    out: list[HoldingOut] = []
    for acct in accounts:
        out.extend(
            HoldingOut.from_model(h) for h in service.latest_holdings(session, acct.id, as_of)
        )
    return out


@valuations_router.post("", status_code=status.HTTP_201_CREATED)
def create_valuation(
    account_id: uuid.UUID,
    body: ValuationCreate,
    token: WriteToken,
    session: DbSession,
    background_tasks: BackgroundTasks,
) -> ValuationOut:
    """Record a loan or property account's value on a date: the balance it
    reports (its latest valuation replaces the postings sum). Any other
    account type is refused (422): its balance comes from its transactions,
    plus holdings for a brokerage account. The answer's `value` is in ledger
    sign whichever `convention` the request used."""
    try:
        valuation = service.record_valuation(
            session,
            account_ref=str(account_id),
            as_of=body.as_of,
            value=body.value,
            source=body.source,
            convention=body.convention,
            actor_token_id=token.id,
        )
    except LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    background_tasks.add_task(service.consume_watermark)
    return ValuationOut(
        id=valuation.id,
        account_id=valuation.account_id,
        as_of=valuation.as_of,
        value=valuation.value,
        source=valuation.source,
    )


@valuations_router.get("")
def list_valuations(
    account_id: uuid.UUID, token: ReadToken, session: DbSession
) -> list[ValuationOut]:
    rows: list[Valuation] = service.list_valuations(session, account_id)
    return [
        ValuationOut(
            id=v.id, account_id=v.account_id, as_of=v.as_of, value=v.value, source=v.source
        )
        for v in rows
    ]
