"""Lots, captures and awards API (issue #393, DESIGN section 8, decision
107).

Every route declares its scope (guardrail 4): reads are read scope, writes
(including a capture's dry run, which shares the write's validation) write
scope. A broker's lot and grant references are account identifiers: they are
encrypted at rest and omitted for non-admin API tokens (``external_ref``
null, ``external_ref_withheld`` true), as account external ids are; a caller
still NAMES a lot or award by its reference on input (matched by blind
index).
"""

import uuid
from datetime import date as date_type
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from purser import ledger, networth
from purser.admin import security
from purser.admin.auth import ReadToken, WriteToken
from purser.api.disclosure import NOTHING, Emits, disclose
from purser.db import get_session
from purser.holdings import awards as award_service
from purser.holdings import captures, lots
from purser.models import ApiToken, Award, AwardVest, HoldingsCapture, Lot, LotDisposition

from .positions import Acquisition, _handle

DbSession = Annotated[Session, Depends(get_session)]

router = APIRouter(tags=["lots"])


def _withhold(token: ApiToken) -> bool:
    return security.withholds_external_ids(token.kind, token.scopes)


def _handle_capture(exc: ledger.LedgerError) -> HTTPException:
    if isinstance(exc, captures.PreviewStale | award_service.AwardPreviewStale):
        return HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "preview_stale",
                "message": str(exc),
                "plan_hash": exc.preview.plan_hash,
            },
        )
    if isinstance(exc, captures.CaptureConflicts | award_service.AwardCaptureConflicts):
        return HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "capture_conflicts",
                "message": str(exc),
                "plan_hash": exc.preview.plan_hash,
            },
        )
    if isinstance(exc, award_service.VestConflict):
        return HTTPException(
            status.HTTP_409_CONFLICT, detail={"code": "vest_conflict", "message": str(exc)}
        )
    return _handle(exc)


# ---------------------------------------------------------------------------
# Lots
# ---------------------------------------------------------------------------


class LotOut(BaseModel):
    id: uuid.UUID
    position_id: uuid.UUID
    status: Literal["open", "closed", "voided", "reversed"]
    acquired_on: date_type
    effective_on: date_type  # counts from this day (an adopted lot: from its end)
    acquisition: str
    plan: str | None
    external_ref: str | None
    external_ref_withheld: bool
    ordinal: int
    quantity: Decimal  # when it began counting
    remaining: Decimal
    basis_per_share: Decimal | None  # null: unknown, never 0 by default
    cost_basis: Decimal | None  # for `quantity`
    remaining_basis: Decimal | None
    basis_source: str | None
    origin: Literal["event", "capture"]
    opened_by_event_id: uuid.UUID | None
    capture_id: uuid.UUID | None
    award_vest_id: uuid.UUID | None
    dispositions: int
    voided_at: datetime | None

    @classmethod
    def from_view(cls, v: lots.LotView, withhold: bool) -> "LotOut":
        lot = v.lot
        return cls(
            id=lot.id,
            position_id=lot.position_id,
            status=v.status,
            acquired_on=lot.acquired_on,
            effective_on=lot.effective_on,
            acquisition=lot.acquisition,
            plan=lot.plan,
            external_ref=None if withhold else lot.external_ref,
            external_ref_withheld=withhold and lot.external_ref_bidx is not None,
            ordinal=lot.ordinal,
            quantity=lot.quantity,
            remaining=v.remaining,
            basis_per_share=lot.basis_per_share,
            cost_basis=lot.cost_basis,
            remaining_basis=v.remaining_basis,
            basis_source=lot.basis_source,
            origin="capture" if lot.capture_id else "event",
            opened_by_event_id=lot.opened_by_event_id,
            capture_id=lot.capture_id,
            award_vest_id=v.award_vest_id,
            dispositions=v.dispositions,
            voided_at=lot.voided_at,
        )


class FreshnessOut(BaseModel):
    """When the broker's lots were last observed (capture freshness), as
    opposed to the price's date (quote freshness, on the position)."""

    capture_id: uuid.UUID
    as_of: date_type
    captured_at: str
    completeness: str


class LotSummaryOut(BaseModel):
    position_id: uuid.UUID
    symbol: str
    quantity: Decimal
    tracks_lots: bool
    open_lots: int
    lots_quantity: Decimal
    unallocated_quantity: Decimal
    basis_known: Decimal
    basis_unknown_quantity: Decimal
    cost_basis: Decimal | None
    capture: FreshnessOut | None

    @classmethod
    def from_summary(cls, s: lots.LotSummary) -> "LotSummaryOut":
        return cls(
            position_id=s.position.id,
            symbol=s.position.symbol,
            quantity=s.quantity,
            tracks_lots=s.tracks_lots,
            open_lots=s.open_lots,
            lots_quantity=s.lots_quantity,
            unallocated_quantity=s.unallocated,
            basis_known=s.basis_known,
            basis_unknown_quantity=s.basis_unknown_quantity,
            cost_basis=s.cost_basis,
            capture=FreshnessOut(**s.freshness.__dict__) if s.freshness else None,
        )


class LotPageOut(BaseModel):
    summary: LotSummaryOut
    lots: list[LotOut]
    total: int
    next_cursor: str | None


@router.get(
    "/positions/{position_id}/lots",
    dependencies=[
        disclose(
            Emits(
                description=("lots[].plan",),
                amount=("lots[].remaining_basis", "lots[].basis_per_share", "lots[].cost_basis"),
            )
        )
    ],
)
def list_lots(
    position_id: uuid.UUID,
    token: ReadToken,
    session: DbSession,
    lot_status: Annotated[
        Literal["open", "closed", "voided", "reversed", "all"], Query(alias="status")
    ] = "open",
    limit: Annotated[int, Query(ge=1, le=lots.MAX_PAGE)] = 50,
    cursor: str | None = None,
) -> LotPageOut:
    """A position's lots in FIFO order (acquisition date, then ordinal), a
    page at a time, with the position's lot summary: shares in lots,
    unallocated shares, known basis and shares of unknown basis, and when
    the broker's lots were last captured."""
    try:
        page = lots.list_lots(session, position_id, status=lot_status, limit=limit, cursor=cursor)
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    withhold = _withhold(token)
    return LotPageOut(
        summary=LotSummaryOut.from_summary(page.summary),
        lots=[LotOut.from_view(v, withhold) for v in page.lots],
        total=page.total,
        next_cursor=page.next_cursor,
    )


class DispositionOut(BaseModel):
    id: uuid.UUID
    event_id: uuid.UUID
    lot_id: uuid.UUID | None  # null: the unallocated shares
    disposed_on: date_type
    quantity: Decimal
    cost_basis: Decimal | None
    proceeds: Decimal | None
    realized_gain: Decimal | None
    term: Literal["short", "long"] | None
    method: str
    live: bool  # false once its event is reversed

    @classmethod
    def from_model(cls, d: LotDisposition, live: bool) -> "DispositionOut":
        return cls(
            id=d.id,
            event_id=d.event_id,
            lot_id=d.lot_id,
            disposed_on=d.disposed_on,
            quantity=d.quantity,
            cost_basis=d.cost_basis,
            proceeds=d.proceeds,
            realized_gain=d.realized_gain,
            term=d.term,
            method=d.method,
            live=live,
        )


class LotDetailOut(LotOut):
    disposition_rows: list[DispositionOut]


@router.get(
    "/lots/{lot_id}",
    dependencies=[
        disclose(
            Emits(
                description=("plan",),
                amount=(
                    "remaining_basis",
                    "basis_per_share",
                    "cost_basis",
                    "disposition_rows[].cost_basis",
                    "disposition_rows[].proceeds",
                    "disposition_rows[].realized_gain",
                ),
            )
        )
    ],
)
def get_lot(lot_id: uuid.UUID, token: ReadToken, session: DbSession) -> LotDetailOut:
    """One lot with every disposition that drew on it (reversed ones
    included, `live: false`)."""
    try:
        detail = lots.lot_detail(session, lot_id)
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    base = LotOut.from_view(detail.view, _withhold(token))
    return LotDetailOut(
        **base.model_dump(),
        disposition_rows=[DispositionOut.from_model(d, live) for d, live in detail.dispositions],
    )


class LotVoid(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class LotBasisUpdate(BaseModel):
    basis_per_share: Decimal | None = Field(default=None, ge=0, decimal_places=6, max_digits=20)
    cost_basis: Decimal | None = Field(default=None, ge=0, decimal_places=4, max_digits=20)
    note: str | None = Field(default=None, max_length=500)


def _lot_out(session: Session, lot: Lot, token: ApiToken) -> LotOut:
    detail = lots.lot_detail(session, lot.id)
    return LotOut.from_view(detail.view, _withhold(token))


@router.post("/lots/{lot_id}/void")
def void_lot(
    lot_id: uuid.UUID,
    token: WriteToken,
    session: DbSession,
    background_tasks: BackgroundTasks,
    body: LotVoid | None = None,
) -> LotOut:
    """Void a lot adopted from a capture: it stops counting and stays as
    read-only history. Refused (422 `lot_has_dispositions`) while a live
    sale or transfer out draws on it — reverse those first — and for a lot
    opened by an event (reverse the event instead)."""
    try:
        lot = lots.void_lot(session, lot_id=lot_id, note=body.note if body else None, token=token)
        out = _lot_out(session, lot, token)
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    background_tasks.add_task(networth.consume_watermark)
    return out


@router.patch("/lots/{lot_id}")
def correct_lot_basis(
    lot_id: uuid.UUID,
    body: LotBasisUpdate,
    token: WriteToken,
    session: DbSession,
    background_tasks: BackgroundTasks,
) -> LotOut:
    """Set or correct a lot's basis (per share, total, or both agreeing;
    neither sets it back to unknown). Refused (422 `basis_locked`) while a
    disposition drew on the lot: its basis came from it."""
    try:
        lot = lots.correct_basis(
            session,
            lot_id=lot_id,
            basis_per_share=body.basis_per_share,
            cost_basis=body.cost_basis,
            note=body.note,
            token=token,
        )
        out = _lot_out(session, lot, token)
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    session.commit()
    background_tasks.add_task(networth.consume_watermark)
    return out


# ---------------------------------------------------------------------------
# Lot captures
# ---------------------------------------------------------------------------


class CaptureLotIn(BaseModel):
    acquired_on: date_type
    quantity: Decimal = Field(
        ge=0,
        decimal_places=6,
        max_digits=20,
        description="Shares remaining in the lot on `as_of` (the broker's figure).",
    )
    external_ref: str | None = Field(
        default=None,
        max_length=200,
        description="The broker's lot reference, when it shows one: the lot's identity.",
    )
    plan: str | None = Field(default=None, max_length=200, description="Plan or source label.")
    ordinal: int | None = Field(
        default=None,
        ge=1,
        description="Without a reference, tells lots of one acquisition date and plan apart "
        "(required when there are several; keep it stable between captures).",
    )
    basis_per_share: Decimal | None = Field(default=None, ge=0, decimal_places=6, max_digits=20)
    cost_basis: Decimal | None = Field(
        default=None,
        ge=0,
        decimal_places=4,
        max_digits=20,
        description="Total basis of the REMAINING shares, as the broker shows it.",
    )
    acquisition: Acquisition = "unknown"


class CapturePositionIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=15)
    name: str | None = Field(default=None, max_length=200)
    lots: list[CaptureLotIn] = Field(max_length=captures.MAX_ROWS)


class LotCaptureBody(BaseModel):
    as_of: date_type = Field(description="The day the broker's figures are as of.")
    captured_at: datetime = Field(description="When the figures were read (an ISO instant).")
    completeness: Literal["complete", "partial"] = Field(
        description="`complete`: each listed symbol's lot list is the broker's whole list "
        "(missing lots become conflicts, the share count is recounted up to the total). "
        "`partial`: only the rows given; nothing is inferred from absence, and the share "
        "count never changes."
    )
    positions: list[CapturePositionIn] = Field(min_length=1, max_length=captures.MAX_SYMBOLS)
    source_note: str | None = Field(default=None, max_length=500)
    basis_policy: Literal["keep", "replace"] = "keep"
    dry_run: bool = Field(default=True, description="Preview only (the default).")
    expect_hash: str | None = Field(
        default=None, description="Commit: the `plan_hash` of the preview being committed."
    )
    skip_conflicts: bool = False

    def capture(self, account_id: uuid.UUID) -> captures.LotCaptureIn:
        return captures.LotCaptureIn(
            account=str(account_id),
            as_of=self.as_of,
            captured_at=self.captured_at,
            completeness=self.completeness,
            source_note=self.source_note,
            basis_policy=self.basis_policy,
            positions=tuple(
                captures.SymbolIn(
                    symbol=p.symbol,
                    name=p.name,
                    lots=tuple(
                        captures.LotIn(
                            acquired_on=r.acquired_on,
                            quantity=r.quantity,
                            external_ref=r.external_ref,
                            plan=r.plan,
                            ordinal=r.ordinal,
                            basis_per_share=r.basis_per_share,
                            cost_basis=r.cost_basis,
                            acquisition=r.acquisition,
                        )
                        for r in p.lots
                    ),
                )
                for p in self.positions
            ),
        )


class PlanItemOut(BaseModel):
    kind: str  # a change kind, or a conflict code
    row: int | None = None
    lot_id: uuid.UUID | None = None
    quantity: Decimal | None = None
    message: str | None = None


class SymbolPlanOut(BaseModel):
    symbol: str
    position_id: uuid.UUID | None
    stale: bool
    latest_capture_as_of: date_type | None
    changes: list[PlanItemOut]
    conflicts: list[PlanItemOut]
    unchanged: int
    skipped_zero: int
    not_in_capture: int
    target_quantity: Decimal | None


class CaptureOut(BaseModel):
    """A capture's plan (dry run) or what its commit applied."""

    dry_run: bool
    applied: bool
    retry: bool  # the exact request was applied before; nothing written now
    repeat_of: uuid.UUID | None  # same holdings observed again: freshness only
    plan_hash: str
    capture_id: uuid.UUID | None
    conflicts: int
    symbols: list[SymbolPlanOut]
    skipped_symbols: list[str] = []
    created_lots: list[uuid.UUID] = []


def _symbol_out(s: captures.SymbolPreview) -> SymbolPlanOut:
    return SymbolPlanOut(
        symbol=s.symbol,
        position_id=s.position_id,
        stale=s.stale,
        latest_capture_as_of=s.latest_capture_as_of,
        changes=[
            PlanItemOut(kind=c.kind, row=c.row, lot_id=c.lot_id, quantity=c.quantity)
            for c in s.plan.changes
        ],
        conflicts=[
            PlanItemOut(kind=c.code, row=c.row, lot_id=c.lot_id, message=c.message)
            for c in s.plan.conflicts
        ],
        unchanged=s.plan.unchanged,
        skipped_zero=s.plan.skipped_zero,
        not_in_capture=s.plan.not_in_capture,
        target_quantity=s.plan.target_quantity,
    )


@router.post(
    "/accounts/{account_id}/lot-captures",
    responses={409: {"description": "`preview_stale` or `capture_conflicts` (detail.code)."}},
)
def capture_lots(
    account_id: uuid.UUID,
    body: LotCaptureBody,
    token: WriteToken,
    session: DbSession,
    background_tasks: BackgroundTasks,
) -> CaptureOut:
    """Bulk, idempotent lot input: a broker's lot list as of a day. A dry run
    (the default) returns the plan and its `plan_hash`; commit with
    `dry_run: false` and `expect_hash`. A commit whose state changed since
    the preview is 409 `preview_stale`; one with conflicts is 409
    `capture_conflicts` unless `skip_conflicts` (then a symbol with any
    conflict is left out whole). The exact request again is a no-op
    (`retry`); the same holdings observed later record freshness only
    (`repeat_of`). A lower remaining quantity, an increase, a lot missing
    from a complete capture, a differing basis, an ambiguous identity or an
    older capture is a conflict, never a sale, a deletion or an edit."""
    capture = body.capture(account_id)
    try:
        if body.dry_run:
            preview = captures.preview(session, capture)
            session.rollback()
            return CaptureOut(
                dry_run=True,
                applied=False,
                retry=preview.already_applied is not None,
                repeat_of=preview.repeat_of,
                plan_hash=preview.plan_hash,
                capture_id=preview.already_applied.id if preview.already_applied else None,
                conflicts=preview.conflicts,
                symbols=[_symbol_out(s) for s in preview.symbols],
            )
        if not body.expect_hash:
            raise ledger.LedgerError("a commit needs expect_hash: the plan_hash of its preview")
        applied = captures.commit(
            session,
            capture,
            expect_hash=body.expect_hash,
            skip_conflicts=body.skip_conflicts,
            token=token,
        )
    except ledger.LedgerError as exc:
        raise _handle_capture(exc) from exc
    session.commit()
    background_tasks.add_task(networth.consume_watermark)
    return CaptureOut(
        dry_run=False,
        applied=not applied.retry,
        retry=applied.retry,
        repeat_of=applied.capture.repeat_of,
        plan_hash=applied.preview.plan_hash,
        capture_id=applied.capture.id,
        conflicts=applied.preview.conflicts,
        symbols=[_symbol_out(s) for s in applied.preview.symbols],
        skipped_symbols=list(applied.skipped_symbols),
        created_lots=list(applied.created_lots),
    )


class CaptureRecordOut(BaseModel):
    id: uuid.UUID
    kind: str
    as_of: date_type
    captured_at: datetime
    completeness: str
    symbols: list[str]
    repeat_of: uuid.UUID | None
    origin: str
    source_note: str | None
    result: dict[str, Any]
    created_at: datetime


@router.get("/accounts/{account_id}/captures", dependencies=[disclose(NOTHING)])
def list_captures(
    account_id: uuid.UUID,
    token: ReadToken,
    session: DbSession,
    kind: Literal["lots", "awards"] | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> list[CaptureRecordOut]:
    """The account's applied captures, newest first: provenance and capture
    freshness."""
    stmt = select(HoldingsCapture).where(HoldingsCapture.account_id == account_id)
    if kind:
        stmt = stmt.where(HoldingsCapture.kind == kind)
    rows = session.execute(stmt.order_by(HoldingsCapture.created_at.desc()).limit(limit)).scalars()
    return [
        CaptureRecordOut(
            id=r.id,
            kind=r.kind,
            as_of=r.as_of,
            captured_at=r.captured_at,
            completeness=r.completeness,
            symbols=r.symbols,
            repeat_of=r.repeat_of,
            origin=r.origin,
            source_note=r.source_note,
            result=r.result,
            created_at=r.created_at,
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Awards
# ---------------------------------------------------------------------------


class VestOut(BaseModel):
    id: uuid.UUID
    vest_date: date_type
    ordinal: int
    quantity: Decimal  # scheduled units
    status: Literal["scheduled", "vested", "forfeited"]
    lot_id: uuid.UUID | None
    vest_event_id: uuid.UUID | None
    vested_gross: Decimal | None
    vested_withheld: Decimal | None
    vested_net: Decimal | None
    vested_fmv: Decimal | None

    @classmethod
    def from_model(cls, v: AwardVest) -> "VestOut":
        return cls(
            id=v.id,
            vest_date=v.vest_date,
            ordinal=v.ordinal,
            quantity=v.quantity,
            status=v.status,
            lot_id=v.lot_id,
            vest_event_id=v.vest_event_id,
            vested_gross=v.vested_gross,
            vested_withheld=v.vested_withheld,
            vested_net=v.vested_net,
            vested_fmv=v.vested_fmv,
        )


class AwardOut(BaseModel):
    id: uuid.UUID
    account_id: uuid.UUID
    symbol: str
    grant_ref: str | None
    grant_ref_withheld: bool
    granted_on: date_type
    plan: str | None
    granted_quantity: Decimal
    cancelled_quantity: Decimal
    # The broker's reported vested baseline plus the vests Purser recorded
    # after its date (vests on or before it are counted in the baseline).
    vested_quantity: Decimal
    # Units not yet vested: granted - cancelled - vested when that can be
    # known, else the scheduled units (unvested_known false). Informational,
    # never net worth.
    unvested_quantity: Decimal
    unvested_known: bool
    scheduled_quantity: Decimal  # units on dated vests still to come
    unscheduled_quantity: Decimal | None  # unvested units with no date yet; null: unknown
    forfeited_quantity: Decimal
    reported_vested_quantity: Decimal | None
    reported_as_of: date_type | None
    next_vest_date: date_type | None
    next_vest_quantity: Decimal | None
    due_unrecorded: int  # scheduled vests on or before today not yet recorded
    vests: list[VestOut] | None = None

    @classmethod
    def from_view(
        cls, v: award_service.AwardView, withhold: bool, with_vests: bool = False
    ) -> "AwardOut":
        a = v.award
        return cls(
            id=a.id,
            account_id=a.account_id,
            symbol=a.symbol,
            grant_ref=None if withhold else a.grant_ref,
            grant_ref_withheld=withhold,
            granted_on=a.granted_on,
            plan=a.plan,
            granted_quantity=a.granted_quantity,
            cancelled_quantity=a.cancelled_quantity,
            vested_quantity=v.vested,
            unvested_quantity=v.unvested,
            unvested_known=v.unvested_known,
            scheduled_quantity=v.scheduled,
            unscheduled_quantity=v.unscheduled,
            forfeited_quantity=v.forfeited,
            reported_vested_quantity=a.reported_vested_quantity,
            reported_as_of=a.reported_as_of,
            next_vest_date=v.next_vest.vest_date if v.next_vest else None,
            next_vest_quantity=v.next_vest.quantity if v.next_vest else None,
            due_unrecorded=v.due_unrecorded,
            vests=[VestOut.from_model(x) for x in v.vests] if with_vests else None,
        )


class UnvestedOut(BaseModel):
    """Unvested units valued at the latest close, for information only:
    never counted in net worth, balances or holdings."""

    symbol: str
    units: Decimal
    value: Decimal | None
    price: Decimal | None
    price_as_of: date_type | None
    next_vest_date: date_type | None
    next_vest_units: Decimal | None
    awards: int
    unscheduled: Decimal  # of the units, those with no vest date yet
    complete: bool  # false: an award's remaining units are unknown (units = its scheduled ones)


class AwardsOut(BaseModel):
    awards: list[AwardOut]
    unvested: list[UnvestedOut]
    capture: FreshnessOut | None


def _unvested_out(u: award_service.Unvested) -> UnvestedOut:
    return UnvestedOut(
        symbol=u.symbol,
        units=u.units,
        value=u.value,
        price=u.price.close if u.price else None,
        price_as_of=u.price.price_date if u.price else None,
        next_vest_date=u.next_vest_date,
        next_vest_units=u.next_vest_units,
        awards=u.awards,
        unscheduled=u.unscheduled,
        complete=u.complete,
    )


def _awards_freshness(session: Session, account_id: uuid.UUID) -> FreshnessOut | None:
    row = session.execute(
        select(HoldingsCapture)
        .where(HoldingsCapture.account_id == account_id, HoldingsCapture.kind == "awards")
        .order_by(HoldingsCapture.as_of.desc(), HoldingsCapture.captured_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    return FreshnessOut(
        capture_id=row.id,
        as_of=row.as_of,
        captured_at=row.captured_at.isoformat(),
        completeness=row.completeness,
    )


@router.get(
    "/accounts/{account_id}/awards",
    dependencies=[
        disclose(
            # A vest's net shares times its fair market value is the basis of
            # the lot it opened, a single amount (#667).
            Emits(description=("awards[].plan",), amount=("awards[].vests[].vested_fmv",))
        )
    ],
)
def list_awards(
    account_id: uuid.UUID,
    token: ReadToken,
    session: DbSession,
    symbol: str | None = None,
    include_vests: bool = False,
) -> AwardsOut:
    """The account's equity awards with their unvested units and next vest,
    and the unvested total per symbol (informational: never net worth)."""
    try:
        account = ledger.get_account(session, account_id)
        views = award_service.award_views(session, account, symbol=symbol)
        unvested = award_service.unvested(session, account)
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    withhold = _withhold(token)
    return AwardsOut(
        awards=[AwardOut.from_view(v, withhold, include_vests) for v in views],
        unvested=[_unvested_out(u) for u in unvested],
        capture=_awards_freshness(session, account_id),
    )


@router.get(
    "/awards/{award_id}",
    dependencies=[disclose(Emits(description=("plan",), amount=("vests[].vested_fmv",)))],
)
def get_award(award_id: uuid.UUID, token: ReadToken, session: DbSession) -> AwardOut:
    """One award with its whole vest schedule."""
    try:
        award = award_service.get_award(session, award_id)
        account = ledger.get_account(session, award.account_id)
        view = next(
            v for v in award_service.award_views(session, account) if v.award.id == award_id
        )
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    return AwardOut.from_view(view, _withhold(token), with_vests=True)


class AwardUpdate(BaseModel):
    granted_quantity: Decimal | None = Field(default=None, gt=0, decimal_places=6, max_digits=20)
    cancelled_quantity: Decimal | None = Field(default=None, ge=0, decimal_places=6, max_digits=20)
    note: str | None = Field(default=None, max_length=500)


def _award_out(session: Session, award: Award, token: ApiToken) -> AwardOut:
    account = ledger.get_account(session, award.account_id)
    view = next(v for v in award_service.award_views(session, account) if v.award.id == award.id)
    return AwardOut.from_view(view, _withhold(token), with_vests=True)


@router.patch("/awards/{award_id}")
def update_award(
    award_id: uuid.UUID, body: AwardUpdate, token: WriteToken, session: DbSession
) -> AwardOut:
    """Correct an award's granted or cancelled units (audited)."""
    try:
        award = award_service.update_award(
            session,
            award_id=award_id,
            cancelled_quantity=body.cancelled_quantity,
            granted_quantity=body.granted_quantity,
            note=body.note,
            token=token,
        )
        out = _award_out(session, award, token)
    except ledger.LedgerError as exc:
        raise _handle_capture(exc) from exc
    session.commit()
    return out


class VestUpdate(BaseModel):
    status: Literal["scheduled", "forfeited"]
    note: str | None = Field(default=None, max_length=500)


@router.patch("/award-vests/{vest_id}")
def update_award_vest(
    vest_id: uuid.UUID, body: VestUpdate, token: WriteToken, session: DbSession
) -> VestOut:
    """Forfeit a scheduled vest, reinstate a forfeited one, or set a vest
    that was LINKED to an existing lot back to scheduled. A vest recorded as
    an event goes back by reversing that event."""
    try:
        vest = award_service.update_vest(
            session, vest_id=vest_id, status=body.status, note=body.note, token=token
        )
        out = VestOut.from_model(vest)
    except ledger.LedgerError as exc:
        raise _handle_capture(exc) from exc
    session.commit()
    return out


class VestRecord(BaseModel):
    vest_date: date_type
    ordinal: int | None = Field(default=None, ge=1)
    gross_quantity: Decimal = Field(gt=0, decimal_places=6, max_digits=20)
    withheld_quantity: Decimal = Field(
        ge=0,
        decimal_places=6,
        max_digits=20,
        description="Required explicit units withheld for tax (zero only when known); "
        "all of them (net 0) records the vest with no "
        "shares and no lot.",
    )
    net_quantity: Decimal | None = Field(
        default=None, ge=0, decimal_places=6, max_digits=20, description="Checked if given."
    )
    fmv: Decimal | None = Field(
        default=None, gt=0, decimal_places=6, max_digits=20, description="Per share at vest."
    )
    lot: str | None = Field(
        default=None,
        max_length=200,
        description="Link this existing lot (id or broker reference) instead of adding shares.",
    )
    lot_ref: str | None = Field(default=None, max_length=200)
    plan: str | None = Field(default=None, max_length=200)
    force_new_lot: bool = False
    note: str | None = Field(default=None, max_length=500)
    source_note: str | None = Field(default=None, max_length=500)
    dry_run: bool = False


class VestResultOut(BaseModel):
    award_id: uuid.UUID
    vest: VestOut | None
    lot_id: uuid.UUID | None
    event_id: uuid.UUID | None
    position_id: uuid.UUID | None
    linked: bool
    already_recorded: bool
    dry_run: bool
    gross_quantity: Decimal
    withheld_quantity: Decimal
    net_quantity: Decimal


@router.post(
    "/awards/{award_id}/vests",
    responses={
        409: {
            "description": "`vest_conflict`: recorded differently, forfeited, or a "
            "possible duplicate lot."
        }
    },
)
def record_vest(
    award_id: uuid.UUID,
    body: VestRecord,
    token: WriteToken,
    session: DbSession,
    background_tasks: BackgroundTasks,
) -> VestResultOut:
    """Record a vest: a `vest` event adds the net shares and opens their lot,
    or `lot` links a lot already held (no shares added). The same vest again
    with the same figures is a no-op (`already_recorded`)."""
    try:
        out = award_service.record_vest(
            session,
            award=str(award_id),
            vest_date=body.vest_date,
            ordinal=body.ordinal,
            gross_quantity=body.gross_quantity,
            withheld_quantity=body.withheld_quantity,
            net_quantity=body.net_quantity,
            fmv=body.fmv,
            lot=body.lot,
            lot_ref=body.lot_ref,
            plan=body.plan,
            force_new_lot=body.force_new_lot,
            note=body.note,
            source_note=body.source_note,
            token=token,
            dry_run=body.dry_run,
        )
        result = VestResultOut(
            award_id=out.award.id,
            vest=VestOut.from_model(out.vest) if out.vest is not None else None,
            lot_id=out.lot_id,
            event_id=out.event_id,
            position_id=out.position_id,
            linked=out.linked,
            already_recorded=out.already_recorded,
            dry_run=out.dry_run,
            gross_quantity=out.gross,
            withheld_quantity=out.withheld,
            net_quantity=out.net,
        )
    except ledger.LedgerError as exc:
        raise _handle_capture(exc) from exc
    if body.dry_run:
        session.rollback()
        return result
    session.commit()
    background_tasks.add_task(networth.consume_watermark)
    return result


class CaptureVestIn(BaseModel):
    vest_date: date_type
    quantity: Decimal = Field(gt=0, decimal_places=6, max_digits=20)
    ordinal: int | None = Field(default=None, ge=1)


class CaptureAwardIn(BaseModel):
    grant_ref: str = Field(min_length=1, max_length=200)
    symbol: str = Field(min_length=1, max_length=15)
    granted_on: date_type
    granted_quantity: Decimal = Field(gt=0, decimal_places=6, max_digits=20)
    cancelled_quantity: Decimal | None = Field(default=None, ge=0, decimal_places=6, max_digits=20)
    vested_quantity: Decimal | None = Field(
        default=None, ge=0, decimal_places=6, max_digits=20, description="As the broker reports."
    )
    plan: str | None = Field(default=None, max_length=200)
    schedule: list[CaptureVestIn] = Field(
        default_factory=list, max_length=200, description="Vests not yet delivered."
    )


class AwardCaptureBody(BaseModel):
    as_of: date_type
    captured_at: datetime
    completeness: Literal["complete", "partial"]
    awards: list[CaptureAwardIn] = Field(min_length=1, max_length=award_service.MAX_AWARDS)
    source_note: str | None = Field(default=None, max_length=500)
    dry_run: bool = True
    expect_hash: str | None = None
    skip_conflicts: bool = False

    def capture(self, account_id: uuid.UUID) -> award_service.AwardCaptureIn:
        return award_service.AwardCaptureIn(
            account=str(account_id),
            as_of=self.as_of,
            captured_at=self.captured_at,
            completeness=self.completeness,
            source_note=self.source_note,
            awards=tuple(
                award_service.AwardIn(
                    grant_ref=a.grant_ref,
                    symbol=a.symbol,
                    granted_on=a.granted_on,
                    granted_quantity=a.granted_quantity,
                    cancelled_quantity=a.cancelled_quantity,
                    vested_quantity=a.vested_quantity,
                    plan=a.plan,
                    schedule=tuple(
                        award_service.VestIn(v.vest_date, v.quantity, v.ordinal) for v in a.schedule
                    ),
                )
                for a in self.awards
            ),
        )


class AwardPlanItemOut(BaseModel):
    kind: str
    award: int | None = None
    award_id: uuid.UUID | None = None
    vest_id: uuid.UUID | None = None
    vest_date: date_type | None = None
    quantity: Decimal | None = None
    message: str | None = None


class AwardCaptureOut(BaseModel):
    dry_run: bool
    applied: bool
    retry: bool
    repeat_of: uuid.UUID | None
    plan_hash: str
    capture_id: uuid.UUID | None
    stale: bool
    changes: list[AwardPlanItemOut]
    conflicts: list[AwardPlanItemOut]
    unchanged: int
    due_unrecorded: int
    not_in_capture: int


def _award_plan_out(
    preview: award_service.AwardPreview,
    *,
    dry_run: bool,
    applied: bool,
    retry: bool,
    capture_id: uuid.UUID | None,
    repeat_of: uuid.UUID | None,
) -> AwardCaptureOut:
    plan = preview.plan
    return AwardCaptureOut(
        dry_run=dry_run,
        applied=applied,
        retry=retry,
        repeat_of=repeat_of,
        plan_hash=preview.plan_hash,
        capture_id=capture_id,
        stale=preview.stale,
        changes=[
            AwardPlanItemOut(
                kind=c.kind,
                award=c.award,
                award_id=c.award_id,
                vest_id=c.vest_id,
                vest_date=c.vest_date,
                quantity=c.quantity,
            )
            for c in plan.changes
        ],
        conflicts=[
            AwardPlanItemOut(
                kind=c.code,
                award=c.award,
                award_id=c.award_id,
                vest_id=c.vest_id,
                message=c.message,
            )
            for c in plan.conflicts
        ],
        unchanged=plan.unchanged,
        due_unrecorded=plan.due_unrecorded,
        not_in_capture=plan.not_in_capture,
    )


@router.post(
    "/accounts/{account_id}/award-captures",
    responses={409: {"description": "`preview_stale` or `capture_conflicts` (detail.code)."}},
)
def capture_awards(
    account_id: uuid.UUID, body: AwardCaptureBody, token: WriteToken, session: DbSession
) -> AwardCaptureOut:
    """Bulk, idempotent award input: grants and their vests still to come,
    keyed by grant reference and vest date. Same dry-run / `expect_hash` /
    retry / conflict contract as lot captures. A scheduled vest a complete
    capture leaves out is a conflict (forfeit it explicitly), never a
    forfeit; one on or before `as_of` is counted as due and unrecorded."""
    capture = body.capture(account_id)
    try:
        if body.dry_run:
            preview = award_service.preview_awards(session, capture)
            session.rollback()
            return _award_plan_out(
                preview,
                dry_run=True,
                applied=False,
                retry=preview.already_applied is not None,
                capture_id=preview.already_applied.id if preview.already_applied else None,
                repeat_of=preview.repeat_of,
            )
        if not body.expect_hash:
            raise ledger.LedgerError("a commit needs expect_hash: the plan_hash of its preview")
        applied = award_service.commit_awards(
            session,
            capture,
            expect_hash=body.expect_hash,
            skip_conflicts=body.skip_conflicts,
            token=token,
        )
    except ledger.LedgerError as exc:
        raise _handle_capture(exc) from exc
    session.commit()
    return _award_plan_out(
        applied.preview,
        dry_run=False,
        applied=not applied.retry,
        retry=applied.retry,
        capture_id=applied.capture.id,
        repeat_of=applied.capture.repeat_of,
    )


# ---------------------------------------------------------------------------
# Realized gains
# ---------------------------------------------------------------------------


class TermOut(BaseModel):
    disposals: int
    quantity: Decimal
    proceeds: Decimal  # of dispositions with both basis and proceeds
    cost_basis: Decimal
    realized_gain: Decimal
    unknown_basis_quantity: Decimal  # counted, never valued as 0
    unknown_proceeds_quantity: Decimal


class RealizedGainsOut(BaseModel):
    account_id: uuid.UUID
    year: int
    symbol: str | None
    short: TermOut
    long: TermOut
    unallocated_quantity: Decimal  # disposed of from outside every lot: no basis, no term
    note: str = "Informational: Purser computes no tax."


@router.get("/accounts/{account_id}/realized-gains", dependencies=[disclose(NOTHING)])
def realized_gains(
    account_id: uuid.UUID,
    token: ReadToken,
    session: DbSession,
    year: Annotated[int, Query(ge=1900, le=9999)],
    symbol: str | None = None,
) -> RealizedGainsOut:
    """Realized gains of the account's lot dispositions in a calendar year,
    by term (long only after the one-year anniversary)."""
    try:
        r = lots.realized_gains(session, account_ref=str(account_id), year=year, symbol=symbol)
    except ledger.LedgerError as exc:
        raise _handle(exc) from exc
    return RealizedGainsOut(
        account_id=account_id,
        year=year,
        symbol=r.symbol,
        short=TermOut(**r.by_term["short"].__dict__),
        long=TermOut(**r.by_term["long"].__dict__),
        unallocated_quantity=r.unallocated_quantity,
    )
